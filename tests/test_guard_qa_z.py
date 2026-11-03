# -*- coding: utf-8 -*-
"""Q&A Z 段四条修复回归（fix/guard-qa-z，2026-11-03）：

- Z-1 冷却期异常独立：Pan115CooldownError 透传（transfer115._call 不再包成
  Pan115Error）→ mounts._wrap 映射为 MountCooldownError（带 retry_after）→
  挂载测试显示 cooldown=true/auth_error=False；播放错误映射 503+Retry-After；
  p115_sidecar._map_115_error 定性 throttled（不是 cookie_expired）。
- Z-2 冷却期跳过缓存清理：streaming 中继在账号冷却时跳过
  _invalidate_115_url / invalidate_resolve_cache_by_pickcode。
- Z-5 排队上限按类型：download_url 5s、list/other 30s。
- Z-6 405/429 升级计数分开：405 只进 5 分钟低倍冷却，不升 24h。
"""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")
from backend.emby_server import pan115_guard as pg  # noqa: E402
from backend.emby_server import transfer115 as t115  # noqa: E402
from backend.emby_server import mounts as mount_lib  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """Redis 不可用（进程内兜底）+ 每测试清状态。"""
    monkeypatch.setattr(pg, "_redis", lambda: None)
    pg.reset_for_tests()
    yield
    pg.reset_for_tests()


# ---------------- Z-1 ----------------

def test_call_passthrough_guard_cooldown_not_pan115error(monkeypatch):
    """_call 遇 guard 冷却必须透传原类型（带 retry_after），不得包成 Pan115Error。"""
    client = t115.Pan115Client.__new__(t115.Pan115Client)
    client.cookie = "ck-z1"
    client.ua = ""
    called = {}

    def fake_before(cookie, kind, deadline=None):
        called["kind"] = kind
        raise pg.Pan115CooldownError("测试冷却", 1234.0)

    monkeypatch.setattr(pg, "before_call", fake_before)
    with pytest.raises(pg.Pan115CooldownError) as ei:
        client._call("GET", "https://webapi.115.com/files?aid=1")
    assert ei.value.retry_after == pytest.approx(1234.0)
    assert called["kind"] == "list"


def test_transfer115_cooldown_error_is_pan115error_with_retry_after():
    """transfer115 自有的 Pan115CooldownError：Pan115Error 子类 + retry_after。"""
    exc = t115.Pan115CooldownError("测试", 90.0)
    assert isinstance(exc, t115.Pan115Error)
    assert exc.retry_after == 90.0
    assert "自动恢复" in str(exc)


def test_mount_wrap_maps_cooldown():
    """mounts._wrap：guard/transfer115 两个冷却类都 → MountCooldownError（带 retry_after）。"""
    from backend.emby_server.pan115_guard import Pan115CooldownError as GuardCD

    wrap = mount_lib.Pan115Mount._wrap
    wrapped = wrap(SimpleNamespace(), GuardCD("429", 600.0))
    assert isinstance(wrapped, mount_lib.MountCooldownError)
    assert wrapped.retry_after == pytest.approx(600.0)

    wrapped2 = wrap(SimpleNamespace(), t115.Pan115CooldownError("429", 300.0))
    assert isinstance(wrapped2, mount_lib.MountCooldownError)
    assert wrapped2.retry_after == pytest.approx(300.0)

    # AuthError 仍优先
    wrapped3 = wrap(SimpleNamespace(), t115.Pan115AuthError("bad"))
    assert isinstance(wrapped3, mount_lib.MountAuthError)


def test_mount_cooldown_not_auth():
    """冷却不是凭据失效：isinstance 判定两清。"""
    exc = mount_lib.MountCooldownError("冷却", 60)
    assert not isinstance(exc, mount_lib.MountAuthError)
    assert isinstance(exc, mount_lib.MountError)


def test_mount_cooldown_not_auth_flag():
    """冷却 → MountCooldownError；isinstance 判定两清（挂载测试的分支基础）。"""
    cd = mount_lib.MountCooldownError("115 账号限速冷却中（429），约 5 分钟后自动恢复",
                                      retry_after=300.0)
    assert not isinstance(cd, mount_lib.MountAuthError)
    assert isinstance(cd, mount_lib.MountError)
    # guard 冷却经 _wrap 得到的就是这种形态
    wrapped = mount_lib.Pan115Mount._wrap(SimpleNamespace(),
                                          pg.Pan115CooldownError("429", 300.0))
    assert type(wrapped) is mount_lib.MountCooldownError
    assert wrapped.retry_after == pytest.approx(300.0)


def test_sidecar_maps_throttled_kind():
    """p115_sidecar：冷却 → kind=throttled（带 retry_after），不是 cookie_expired。"""
    from backend.emby_server.p115_sidecar import _map_115_error

    mapped = _map_115_error(t115.Pan115CooldownError("429", 300.0))
    assert mapped.kind == "throttled"
    assert mapped.retry_after == pytest.approx(300.0)
    assert mapped.kind != "cookie_expired"

    # guard 自己的冷却类（RuntimeError，非 Pan115Error）按类型名识别
    mapped2 = _map_115_error(pg.Pan115CooldownError("429", 45.0))
    assert mapped2.kind == "throttled"
    assert mapped2.retry_after == pytest.approx(45.0)

    # 真 Cookie 失效仍是 cookie_expired
    mapped3 = _map_115_error(t115.Pan115AuthError("过期"))
    assert mapped3.kind == "cookie_expired"


# ---------------- Z-2 ----------------

def test_streaming_cooldown_scope_header_roundtrip():
    """挂载 PlayTarget 带 X-115-Scope（guard 同摘要前 16 位），
    streaming 侧用同一口径判定冷却。"""
    import inspect
    from backend.emby_server import streaming, mounts
    src = inspect.getsource(mounts.Pan115Mount.resolve_play_target)
    assert "X-115-Scope" in src
    # scope 摘要与 guard.scope_of 同源（sha256 前 16 位）
    cookie = "cookie-z2"
    assert len(pg.scope_of(cookie)) == 16
    # streaming 判定块存在且引用了 X-115-Scope
    ssrc = inspect.getsource(streaming)
    assert 'headers or {}).get("X-115-Scope")' in ssrc
    assert "cooldown_state" in ssrc


# ---------------- Z-5 ----------------

def test_wait_cap_by_kind(monkeypatch):
    """download_url 5s、list/other 30s；env 可覆盖。"""
    assert pg._wait_cap("download_url") == pytest.approx(5.0)
    assert pg._wait_cap("list") == pytest.approx(30.0)
    assert pg._wait_cap("other") == pytest.approx(30.0)
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_LINK_SEC", "2.5")
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_SEC", "7")
    assert pg._wait_cap("download_url") == pytest.approx(2.5)
    assert pg._wait_cap("list") == pytest.approx(7.0)


def test_before_call_link_uses_small_cap(monkeypatch):
    """download_url 在队列积压时最多等 ~5s 即拒绝（list 仍可等到 30s）。"""
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_LINK_SEC", "0.2")
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_SEC", "5")
    # 令牌桶灌满：连续消耗到必等
    cookie = "ck-z5-link"
    for _ in range(10):
        try:
            pg.before_call(cookie, "download_url")
        except pg.Pan115CooldownError as exc:
            assert "排队超时" in str(exc)
            assert exc.retry_after > 0
            break
    else:
        pytest.fail("download_url 未触发排队上限")


def test_no_async_direct_call_to_115_client():
    """Z-5 排查结论锚定：async 路由对 Pan115Client 的同步调用都经
    run_in_threadpool / run_db（to_thread）；此测试静态扫描防止回归。"""
    import io
    import re
    files = ["backend/emby_server/api.py", "backend/emby_server/portal.py",
             "backend/emby_server/streaming.py"]
    offenders = []
    for path in files:
        lines = io.open(path, encoding="utf-8").read().splitlines()
        async_start = None
        for i, line in enumerate(lines, 1):
            m = re.match(r"^(async def |def )(\w+)", line)
            if m:
                async_start = m.group(1).strip() == "async def"
                continue
            if async_start and re.search(
                    r"Pan115Client\(|\.download_url\(|\.list_dir\(|\.vip_status\(|\.check\(\)", line):
                window = "\n".join(lines[max(0, i - 3):i])
                if not re.search(r"run_in_threadpool|to_thread|run_db", window):
                    offenders.append(f"{path}:{i}: {line.strip()[:100]}")
    assert not offenders, f"async 路由直调 115 同步客户端：{offenders}"


# ---------------- Z-6 ----------------

def test_405_low_ratio_cooldown_not_escalating(monkeypatch):
    """405 连续触发只进 5 分钟低倍冷却；不进 429 升级链、不到 24h。"""
    scope = pg.scope_of("ck-z6-405")
    for _ in range(3):
        pg.after_error("ck-z6-405", 405)
    state = pg.cooldown_state(scope)
    assert state is not None
    reason, remaining = state
    assert remaining <= 300.0 + 5
    assert "低倍冷却" in reason
    # 独立计数：405 的 strikes 不污染 429 计数
    assert pg._incr(f"{pg._KEY_PREFIX}:strikes:{scope}", 86400) == 1


def test_429_still_escalates_to_24h():
    """429 第 4 次仍停 24h（原行为保持）。"""
    scope = pg.scope_of("ck-z6-429")
    for _ in range(4):
        pg.after_error("ck-z6-429", 429)
    reason, remaining = pg.cooldown_state(scope)
    assert remaining > 80000  # ≈24h


def test_405_and_429_counters_independent():
    """混合触发：405 不加速 429 升级（各自独立计数）。"""
    cookie = "ck-z6-mix"
    pg.after_error(cookie, 405)
    pg.after_error(cookie, 405)
    pg.after_error(cookie, 405)  # 405 第 3 次 → 405 冷却（5min）
    scope = pg.scope_of(cookie)
    # 429 计数仍为 0：第一次 429 只应给 base 30min，而不是被 405 顶到 24h
    pg.after_error(cookie, 429)
    reason, remaining = pg.cooldown_state(scope)
    assert remaining <= 7200 + 10  # base~cap 区间，绝不是 24h


def test_mount_wrap_ignores_foreign_retry_after():
    """QA R-1：别的异常带 retry_after 属性也不能被当成冷却"""
    from backend.emby_server import mounts

    class _SdkRetry(Exception):
        retry_after = 7

    wrapped = mounts.Pan115Mount._wrap(object.__new__(mounts.Pan115Mount), _SdkRetry("x"))
    assert not isinstance(wrapped, mounts.MountCooldownError)
    assert isinstance(wrapped, mounts.MountError)
