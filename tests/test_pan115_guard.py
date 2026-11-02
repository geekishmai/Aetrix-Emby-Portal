"""pan115_guard：按账号限速与风控冷却（进程内兜底路径，假时钟，零网络）。"""
from __future__ import annotations

import pytest

from backend import database
from backend.emby_server import pan115_guard as guard
from backend.emby_server import transfer115

COOKIE = "UID=1_A1_1; CID=x; SEID=y"


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(database, "get_redis", lambda: None)
    monkeypatch.setattr(guard.time, "time", c.time)
    monkeypatch.setattr(guard.time, "sleep", c.sleep)
    for name in ("PAN115_GUARD_ENABLED", "PAN115_API_RPS", "PAN115_LIST_RPS", "PAN115_LINK_RPS"):
        monkeypatch.delenv(name, raising=False)
    guard.reset_for_tests()
    yield c
    guard.reset_for_tests()


def test_list_burst_then_paced(clock):
    for _ in range(5):
        guard.before_call(COOKIE, "list")
    assert clock.slept == []
    guard.before_call(COOKIE, "list")
    assert clock.slept and clock.slept[0] == pytest.approx(0.5)


def test_link_bucket_is_stricter(clock):
    guard.before_call(COOKIE, "download_url")
    guard.before_call(COOKIE, "download_url")
    assert clock.slept == []
    guard.before_call(COOKIE, "download_url")
    assert sum(clock.slept) == pytest.approx(1.0)


def test_accounts_do_not_share_buckets(clock):
    for _ in range(2):
        guard.before_call(COOKIE, "download_url")
    guard.before_call("UID=2_A1_1; CID=z; SEID=w", "download_url")
    assert clock.slept == []


def test_wait_beyond_max_raises(clock, monkeypatch):
    # Z-5：MAX_WAIT_SEC 仍是全局硬上限（download_url 的独立 LINK 上限
    # 永不超过它）；download_url burst=2，第 3 次排队 0.5s > 0.2s → 拒绝。
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_SEC", "0.2")
    for _ in range(2):
        guard.before_call(COOKIE, "download_url")
    with pytest.raises(guard.Pan115CooldownError):
        guard.before_call(COOKIE, "download_url")


def test_wait_cap_link_independent(clock, monkeypatch):
    """Z-5：download_url 的排队上限独立（LINK 键），不受 MAX_WAIT_SEC 影响。"""
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_SEC", "0.2")
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_LINK_SEC", "30")
    for _ in range(2):
        guard.before_call(COOKIE, "download_url")  # 等得起（30s 上限）→ 不抛
    # LINK 也设小后同样会拒绝
    monkeypatch.setenv("PAN115_GUARD_MAX_WAIT_LINK_SEC", "0.2")
    with pytest.raises(guard.Pan115CooldownError):
        guard.before_call(COOKIE, "download_url")


def test_429_cooldown_escalates(clock):
    guard.after_error(COOKIE, 429)
    with pytest.raises(guard.Pan115CooldownError) as first:
        guard.before_call(COOKIE, "list")
    assert first.value.remaining == pytest.approx(1800)
    clock.now += 1801
    guard.before_call(COOKIE, "list")          # 冷却过了恢复
    guard.after_error(COOKIE, 429)
    state = guard.cooldown_state(guard.scope_of(COOKIE))
    assert state is not None and state[1] == pytest.approx(3600)


def test_fourth_strike_stops_a_day(clock):
    for _ in range(4):
        guard.after_error(COOKIE, 429)
    assert guard.cooldown_state(guard.scope_of(COOKIE))[1] == pytest.approx(86400)


def test_verify_errno_stops_a_day(clock):
    guard.after_body(COOKIE, {"state": False, "errno": 911, "error": "请验证账号"})
    reason, remaining = guard.cooldown_state(guard.scope_of(COOKIE))
    assert "911" in reason and remaining == pytest.approx(86400)


def test_rate_limit_message_triggers_cooldown(clock):
    guard.after_body(COOKIE, {"state": False, "error": "已达到当前访问上限"})
    assert guard.cooldown_state(guard.scope_of(COOKIE)) is not None


def test_ordinary_failure_does_not_cooldown(clock):
    guard.after_body(COOKIE, {"state": False, "errno": 20004, "error": "文件不存在"})
    guard.after_error(COOKIE, 500)
    assert guard.cooldown_state(guard.scope_of(COOKIE)) is None


def test_405_needs_three_in_window(clock):
    guard.after_error(COOKIE, 405)
    guard.after_error(COOKIE, 405)
    assert guard.cooldown_state(guard.scope_of(COOKIE)) is None
    guard.after_error(COOKIE, 405)
    assert guard.cooldown_state(guard.scope_of(COOKIE)) is not None


def test_disabled_is_noop(clock, monkeypatch):
    monkeypatch.setenv("PAN115_GUARD_ENABLED", "0")
    guard.after_error(COOKIE, 429)
    for _ in range(10):
        guard.before_call(COOKIE, "download_url")
    assert clock.slept == []


def test_client_stops_calling_115_during_cooldown(clock, monkeypatch):
    calls = []

    def fake_http_json(method, url, **kwargs):
        calls.append(url)
        err = transfer115.Pan115Error("115 返回 HTTP 429")
        err.status = 429
        raise err

    monkeypatch.setattr(transfer115, "_http_json", fake_http_json)
    client = transfer115.Pan115Client(COOKIE, ua="t-ua")
    url = f"{transfer115.PAN115_WEBAPI_BASE}/files"
    with pytest.raises(transfer115.Pan115Error):
        client._call("GET", url)
    # Z-1：第二次起 guard 直接拒——透传 guard.Pan115CooldownError
    #（它是 RuntimeError，不是 Pan115Error 子类；这不是回归，是修复）。
    with pytest.raises(guard.Pan115CooldownError, match="冷却"):
        client._call("GET", url)
    assert len(calls) == 1, "冷却期间不许再打 115"
