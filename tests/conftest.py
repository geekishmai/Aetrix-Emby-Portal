"""全测试进程内状态隔离。

enrich_worker 里有几个模块级 60s TTL 缓存（豆瓣/altmeta/夜间配置、单集等父级
等待计数）：生产里这是有意的进程内缓存，但 pytest 同一进程跑全量 tests/ 时，
前一个测试文件 mock 进去的配置值会泄漏到后一个文件，造成顺序相关的假失败
（完美审查 R17 C-P2-5 引入）。

每个测试前把这些缓存清零，保证用例之间相互独立。只影响测试，生产代码无改动。
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _reset_enrich_worker_caches():
    try:
        from backend.emby_server import enrich_worker as ew
    except Exception:
        yield
        return
    for _name in ("_douban_cfg_cache", "_altmeta_cfg_cache", "_night_cfg_cache",
                  "_enrich_tuning_cache"):
        _c = getattr(ew, _name, None)
        if isinstance(_c, dict):
            _c.clear()
            _c.update({"at": 0.0, "cfg": None})
    _w = getattr(ew, "_parent_wait_counts", None)
    if isinstance(_w, dict):
        _w.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_pan115_http_client():
    """115 进程级共享 httpx.Client 逐测隔离。

    生产里这是有意的 keep-alive 单例；pytest 同进程跑全量时，若不清空，
    前一个用例 mock 的 httpx.Client 或真实连接会泄漏到后一个用例
    （simple_upload 等用 monkeypatch httpx.Client 的测试会失效）。
    """
    try:
        from backend.emby_server import transfer115 as _t115
    except Exception:
        yield
        return
    def _drop_and_close():
        """取出当前共享 client 并关闭（不留连接池泄漏）。"""
        with _t115._http_client_lock:
            client = _t115._http_client
            _t115._http_client = None
            _t115._http_client_pid = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    _drop_and_close()          # 上一个用例残留的：先关掉，不留连接池泄漏
    yield
    _drop_and_close()


@pytest.fixture(autouse=True)
def _isolate_transfer_stage_dir(tmp_path, monkeypatch):
    """跨 provider 源字节暂存是 worker 本地盘状态：测试间必须隔离。

    暂存目录默认挂在 tempfile.gettempdir() 下，同 pytest 进程内跨文件
    共享会导致「前一文件暂存了同内容、后一文件断言源读取次数」类
    顺序假失败（2026-10-31 第三单引入时实证：multipart 文件内前一
    用例暂存了同 key、后一用例哈希遍零次碰源）。每测一个独立目录，
    且清空进程内 stage 注册表。
    """
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "transfer-stage"))
    try:
        from backend.emby_server import transfer_variant as tv
        with tv._STAGE_REGISTRY_LOCK:
            tv._STAGE_REGISTRY.clear()
    except Exception:
        pass
    yield


# ==================== 全局 SessionLocal factory 的安全换绑 ====================
#
# 背景（2026-11-05 T1 复盘）：``configure_session_local`` 对「传入代理自身」有
# 防递归保护——被忽略并回退到 ``_real_session_local``（bind 到 backend.database
# **首次 import 时的 engine**）。测试里若写 ``original = dbmod.SessionLocal``
# （保存的是代理对象）再 ``configure_session_local(original)`` "还原"，实际把
# 全局 factory 打回进程首库（通常 0 表）——其后所有依赖全局 SessionLocal 的
# 测试文件 ``no such table``（全量 95 个环境类 ERROR 的根因，单跑全绿）。
#
# 正确姿势：换绑前用 :func:`snapshot_session_factory` 保存**代理内部指向**，
# 用 :func:`restore_session_factory` 恢复。


def snapshot_session_factory():
    """保存 SessionLocal 代理当前指向的 factory（不是代理本身）。

    T1-v2（QA BO-3 P2）：victim 型文件把 ``_dbmod.engine`` /
    ``os.environ["DATABASE_URL"]`` / factory 三件一起改；只快照 factory 会让
    engine 与 env 残留指向他人 tmp 库。返回**完整快照 dict**，恢复时三件
    一起还原。兼容：也接受旧式 factory 对象（restore 时只还原 factory 位）。
    """
    from backend import database as _dbmod
    factory = _dbmod.SessionLocal._factory
    kw = getattr(factory, "kw", None) or {}
    return {
        "factory": factory,
        # factory 自己的 bind 原值（sessionmaker 形态才有）：restore 时重指，
        # 修"同对象 bind 漂移"且不误伤"快照后未漂移"的 factory。
        "factory_bind": kw.get("bind"),
        "engine": _dbmod.engine,
        "database_url": os.environ.get("DATABASE_URL"),
    }


def restore_session_factory(token) -> None:
    """恢复 :func:`snapshot_session_factory` 保存的指向。

    ``token`` 为 dict（新式全量快照）→ factory/engine/DATABASE_URL 三件一起
    还原；为 factory 对象（旧式调用）→ 只还原 factory 位。
    """
    from backend import database as _dbmod
    if isinstance(token, dict):
        factory = token["factory"]
        # BO 同对象漂移：sessionmaker 对象没换、kw['bind'] 被外部改写 →
        # 恢复时把 bind 重指回快照 engine（对象身份与指向都还原）。
        if factory is not None and token.get("factory_bind") is not None:
            try:
                factory.kw["bind"] = token["factory_bind"]
            except (AttributeError, KeyError, TypeError):
                pass  # 非 sessionmaker 形态的 factory：跳过 bind 对齐
        _dbmod.SessionLocal._factory = factory
        _dbmod.engine = token["engine"]
        if token["database_url"] is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = token["database_url"]
    else:
        _dbmod.SessionLocal._factory = token


# ==================== 用例级 factory 起点守卫（T1-v2，替代启发式守卫） ====================
#
# QA BO-1/BO-2 实证废弃 c078855c 的「最后已知良好」启发式：
# - BO-1：factory 对象没换、但它指向的库被 DROP 表 → ``before is after`` 为真
#   → 守卫跳过，对同对象缺表全盲；
# - BO-2：起点 A、错误结束于空库 C、最后 good=B → 纠偏到 B = **跨库误读**
#   （表名存在 ≠ 目标库正确）。
#
# v2 不变量（Codex 定）：每个用例使用**自己的确定起点**，结束恢复它；
# 绝不因某库「有两张核心表」就自动选成目标。缺表事件**显式报数**进
# ``_FACTORY_GAPS``（测试/报告可断言），不静默换库。


#: 用例起点快照（setup 时刻 factory/engine/env）
_CASE_ORIGIN: dict = {}
#: 缺表事件计数（显式暴露，不静默）：teardown 时起点 factory 的库缺核心表
_FACTORY_GAPS: list = []
#: 「核心表」判定集：这两张表在**任何** init_db 过的全局库里都存在；
#: victim 型文件的库由各自 init_db 建，必然包含。临时库（restorer 目标）
#: 通常 0 表 → 稳定区分「全局库」与「临时库」。
_GUARD_CORE_TABLES = ("web_users", "system_configs")


def factory_gap_count() -> int:
    """守卫观测到的「起点库缺核心表」事件数（测试/诊断用）。"""
    return len(_FACTORY_GAPS)


def pytest_sessionfinish(session, exitstatus):
    """会话末尾汇总 GAP 计数：unknown / 缺表不能只存在无人消费的内部列表。"""
    if _FACTORY_GAPS:
        states = {}
        for g in _FACTORY_GAPS:
            states[g.get("state")] = states.get(g.get("state"), 0) + 1
        session.config.pluginmanager.get_plugin("terminalreporter") and \
            getattr(session.config.pluginmanager.get_plugin("terminalreporter"),
                    "write_line", lambda *a: None)(
            f"[test-db-isolation] factory 起点诊断 GAP 共 {len(_FACTORY_GAPS)} 条 "
            f"（按 state 汇总：{states}；详情只含 nodeid/reason/state，不含 URL）")


def _case_origin_engine_has_core_tables(engine) -> bool:
    """兼容口（旧调用方）：engine 视角的核心表检查。

    注意：守卫对账（_record_gap_if_origin_missing）用的是 **factory.bind**，
    不再是 dbmod.engine——两者可能不一致（QA 主会话反例），分开诊断。
    """
    if engine is None:
        return False
    try:
        from sqlalchemy import inspect
        names = set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001 — 探测失败按缺表处理（保守报 GAP）
        return False
    return all(t in names for t in _GUARD_CORE_TABLES)


@pytest.fixture(autouse=True)
def _case_origin_factory_guard():
    """用例级起点守卫（v2）：

    setup：快照当前 factory/engine/env 为「本用例起点」；
    teardown：
    - 把全局三件**恢复回本用例起点**（不管用例有没有动过——恢复是幂等的，
      且消除「restorer 恢复到错误值」的残留）；
    - 若**起点本身**就缺核心表（BO-1 的同对象缺表形态），记入
      ``_FACTORY_GAPS`` 显式报数，不静默换库、不回退 any-good。
    """
    from backend import database as _dbmod
    origin = snapshot_session_factory()
    _CASE_ORIGIN.clear()
    _CASE_ORIGIN.update(origin)
    yield
    restore_session_factory(origin)
    _record_gap_if_origin_missing(origin)


def _factory_bind(factory):
    """取 factory 实际出 session 用的 engine（诊断按它，不按 dbmod.engine）。"""
    kw = getattr(factory, "kw", None) or {}
    return kw.get("bind")


def _bind_state(bind):
    """bind 的可诊断状态：ok / missing-tables / unknown。

    - ok：有核心表；
    - missing-tables：engine 可 inspect 但缺核心表；
    - unknown：无 bind / inspect 不可用（显式报 unknown，不猜别的有表库）。
    """
    if bind is None:
        return "unknown"
    try:
        from sqlalchemy import inspect
        names = set(inspect(bind).get_table_names())
    except Exception:  # noqa: BLE001 — 探测失败按 unknown（不猜）
        return "unknown"
    return "ok" if all(t in names for t in _GUARD_CORE_TABLES) else "missing-tables"


def _record_gap_if_origin_missing(origin) -> None:
    """BO-1 对账口：按**起点 factory 实际绑定**诊断缺表 → GAP 显式 +1。

    - 诊断对象 = ``factory.kw['bind']``（SessionLocal() 真正连的库），
      与 ``dbmod.engine`` 属性无关——两者可能不一致，分开报告；
    - 无可检查 bind → state=``unknown``（显式，不猜别的有表库）；
    - GAP 证据只存 nodeid / 固定 reason / state，**绝不存 engine URL**
      （SQLite file URL 可能含路径甚至凭据形态的查询串）。
    """
    factory = origin.get("factory") if isinstance(origin, dict) else origin
    bind = _factory_bind(factory)
    state = _bind_state(bind)
    if state != "ok":
        _FACTORY_GAPS.append({"state": state, "reason": "origin-bind-not-ok"})