"""全测试进程内状态隔离。

enrich_worker 里有几个模块级 60s TTL 缓存（豆瓣/altmeta/夜间配置、单集等父级
等待计数）：生产里这是有意的进程内缓存，但 pytest 同一进程跑全量 tests/ 时，
前一个测试文件 mock 进去的配置值会泄漏到后一个文件，造成顺序相关的假失败
（完美审查 R17 C-P2-5 引入）。

每个测试前把这些缓存清零，保证用例之间相互独立。只影响测试，生产代码无改动。
"""
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
    """保存 SessionLocal 代理当前指向的 factory（不是代理本身）。"""
    from backend import database as _dbmod
    return _dbmod.SessionLocal._factory


def restore_session_factory(token) -> None:
    """恢复 :func:`snapshot_session_factory` 保存的指向。"""
    from backend import database as _dbmod
    _dbmod.SessionLocal._factory = token


# ==================== 全局 factory 守卫（T1 第二层） ====================
#
# 「换绑全局 factory 再恢复」在**多文件全量**下对不齐：victim 文件（模块级
# init_db() + 全局 SessionLocal）的表建在它 **collection 时刻**的全局 engine；
# 运行期任何 restorer 把 factory 恢复到"自己快照的值"，都可能指向更早的库
# → 之后所有 victim ``no such table``。手写快照永远追不齐建表进度。
#
# 解法：进程级守卫——任何测试改绑全局 factory 前，先确保"当前指向"是可用的
# 全局库（有核心表）；用例结束时若发现全局 factory 指向**缺核心表的库**，
# 把它指回**最后已知良好**的全局库（最后一个成功 init_db 的全局 engine）。
# 只兜底、不掩盖：restorer 自己的语义不变，守卫只在"恢复结果缺表"时纠偏。

_LAST_GOOD_GLOBAL_FACTORY = None
_GUARD_CORE_TABLES = ("web_users", "system_configs")
_GUARD_INSTALLED = False
_GUARD_LOCK = None


def _factory_engine(factory):
    """从 factory 里取 bind 的 engine（sessionmaker.kw['bind'] 或 .kw['bind']）。"""
    kw = getattr(factory, "kw", None) or {}
    return kw.get("bind")


def _engine_has_core_tables(engine) -> bool:
    if engine is None:
        return False
    try:
        from sqlalchemy import inspect
        names = set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001 — 探测失败按缺表处理
        return False
    return all(t in names for t in _GUARD_CORE_TABLES)


def install_session_factory_guard():
    """装全局 factory 守卫（幂等；conftest autouse fixture 调用）。

    hook 两处：configure_session_local 与直接对 ``_factory`` 赋值都拦不住——
    这里用**用例级对账**：每个用例 setup 时记当前 factory；teardown 时若
    当前 factory 缺核心表且存在"最后已知良好"factory，则纠偏回去。
    """
    global _GUARD_INSTALLED, _GUARD_LOCK
    import threading
    if _GUARD_INSTALLED:
        return
    _GUARD_LOCK = threading.Lock()

    from backend import database as _dbmod

    def _note_good(factory):
        global _LAST_GOOD_GLOBAL_FACTORY
        with _GUARD_LOCK:
            if factory is not None and factory is not _dbmod._real_session_local \
                    and _engine_has_core_tables(_factory_engine(factory)):
                _LAST_GOOD_GLOBAL_FACTORY = factory

    def _patched_init_db():
        _dbmod._orig_init_db()
        _note_good(_dbmod.SessionLocal._factory)

    if not getattr(_dbmod, "_orig_init_db", None):
        _dbmod._orig_init_db = _dbmod.init_db
        _dbmod.init_db = _patched_init_db
    _GUARD_INSTALLED = True


@pytest.fixture(autouse=True)
def _guard_global_session_factory():
    """每用例对账：teardown 时全局 factory 缺核心表 → 纠偏回最后已知良好值。"""
    install_session_factory_guard()
    from backend import database as _dbmod
    before = _dbmod.SessionLocal._factory
    yield
    after = _dbmod.SessionLocal._factory
    if after is before:
        return
    # factory 被本用例动过：若动完之后缺核心表，纠偏
    global _LAST_GOOD_GLOBAL_FACTORY
    with _GUARD_LOCK:
        good = _LAST_GOOD_GLOBAL_FACTORY
    if good is not None and after is not good \
            and not _engine_has_core_tables(_factory_engine(after)):
        _dbmod.SessionLocal._factory = good
