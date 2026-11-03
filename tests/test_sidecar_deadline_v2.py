# -*- coding: utf-8 -*-
"""首扫 partial 修法② v2（fix/sidecar-deadline-v2）：deadline 随 JSON-RPC
请求下发 + QA AD 段三处修复。

- AD-1：宿主截止已过 → 独立 ``deadline_exceeded`` kind / ``Pan115DeadlineExceeded``
  / ``MountTimeoutError``，**不是冷却**（不显示冷却文案、不触发 Z-2 缓存清理门）。
- AD-2：``_dispatch`` 改为**签名探测**决定是否传 deadline；删除 TypeError
  回落（否则 handler 内部含 "deadline" 字样的 TypeError 会重调 → 双打 115）。
- AD-3：``ProviderRuntimeError`` 的 retry_after 随错误信封跨进程序列化/还原。
"""
import sys
import time

import pytest

sys.path.insert(0, ".")
from backend.emby_server import mounts as _mounts_first  # noqa: F401,E402 — 先导避循环
from backend.emby_server import mount_sidecar  # noqa: E402
from backend.emby_server import pan115_guard as pg  # noqa: E402
from backend.emby_server import provider_runtime as pr  # noqa: E402
from backend.emby_server import transfer115 as t115  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    monkeypatch.setattr(pg, "_redis", lambda: None)
    pg.reset_for_tests()
    yield
    pg.reset_for_tests()


# ---------------- AD-3：retry_after 跨进程 ----------------

def test_error_envelope_carries_retry_after():
    exc = pr.ProviderRuntimeError("throttled", "冷却", retry_after=123.4)
    data = exc.as_error_dict()
    assert data["retry_after"] == pytest.approx(123.4)
    back = pr.error_from_dict(data)
    assert back.kind == "throttled"
    assert back.retry_after == pytest.approx(123.4)


def test_error_envelope_without_retry_after_stays_none():
    data = pr.ProviderRuntimeError("internal", "boom").as_error_dict()
    assert "retry_after" not in data
    assert pr.error_from_dict(data).retry_after is None


def test_error_envelope_retry_after_invalid_falls_back_none():
    back = pr.error_from_dict({"kind": "throttled", "message": "x", "retry_after": "abc"})
    assert back.retry_after is None
    back2 = pr.error_from_dict({"kind": "throttled", "message": "x", "retry_after": -5})
    assert back2.retry_after == 0.0


# ---------------- AD-1：过期 ≠ 冷却 ----------------

def test_dispatch_expired_raises_deadline_kind():
    class H:
        provider = "p115"
        extensions = ()
        def list_page(self, session, path, page_token, page_size, deadline=None):
            raise AssertionError("handler 不应被调用")

    server = pr.ProviderRuntimeServer(H(), socket_path="/tmp/x-dl1.sock")
    server._dispatch("session_upsert", {"session_id": "s", "data": {}})
    with pytest.raises(pr.ProviderRuntimeError) as ei:
        server._dispatch("list_page", {"session_id": "s", "path": "/x"},
                         deadline=time.time() - 1)
    assert ei.value.kind == "deadline_exceeded"
    assert ei.value.kind != "throttled"
    assert "超时" in str(ei.value)


def test_guard_deadline_expired_raises_deadline_not_cooldown():
    with pytest.raises(pg.Pan115DeadlineExceeded) as ei:
        pg.before_call("ck-dl", "list", deadline=time.time() - 5)
    assert not isinstance(ei.value, pg.Pan115CooldownError)
    assert "超时" in str(ei.value)


def test_guard_deadline_tightens_queue_cap(monkeypatch):
    class Clock:
        now = 1_000_000.0
        def time(self): return self.now
        def sleep(self, s): self.now += s
    clock = Clock()
    monkeypatch.setattr(pg.time, "time", clock.time)
    monkeypatch.setattr(pg.time, "sleep", clock.sleep)
    waits = []
    orig = pg._gcra_wait
    monkeypatch.setattr(pg, "_gcra_wait",
                        lambda key, rate, burst, now: (waits.append(orig(key, rate, burst, now)),
                                                       waits[-1])[1])
    deadline = time.time() + 2.0
    raised = None
    for _ in range(7):
        try:
            pg.before_call("ck-dl2", "list", deadline=deadline)
        except pg.Pan115DeadlineExceeded as exc:
            raised = exc
            break
    if raised:
        assert "超时" in str(raised)
    else:
        assert all(w <= 1.0 + 1e-6 for w in waits), waits


def test_transfer115_list_dir_deadline_stops_pages(monkeypatch):
    client = t115.Pan115Client.__new__(t115.Pan115Client)
    client.cookie = "ck-dl3"
    client.ua = ""
    called = {"n": 0}
    monkeypatch.setattr(t115, "_http_json",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or {"state": 1, "data": []})
    monkeypatch.setattr(pg, "before_call", lambda *a, **kw: None)
    with pytest.raises(pg.Pan115DeadlineExceeded):
        client.list_dir("123", deadline=time.time() - 1)
    assert called["n"] == 0


def test_transfer115_call_deadline_after_guard(monkeypatch):
    client = t115.Pan115Client.__new__(t115.Pan115Client)
    client.cookie = "ck-dl4"
    client.ua = ""
    http_called = []
    monkeypatch.setattr(pg, "before_call", lambda *a, **kw: time.sleep(0.05))
    monkeypatch.setattr(t115, "_http_json",
                        lambda *a, **kw: http_called.append(1) or {"state": 1})
    with pytest.raises(pg.Pan115DeadlineExceeded):
        client._call("GET", "https://webapi.115.com/files?aid=1",
                     deadline=time.time() + 0.01)
    assert http_called == []


def test_mount_wrap_deadline_is_timeout_not_cooldown():
    wrap = _mounts_first.Pan115Mount._wrap
    from types import SimpleNamespace
    w = wrap(SimpleNamespace(), pg.Pan115DeadlineExceeded("过期", 0.0))
    assert isinstance(w, _mounts_first.MountTimeoutError)
    assert not isinstance(w, _mounts_first.MountCooldownError)
    assert not isinstance(w, _mounts_first.MountAuthError)
    # 真冷却仍走冷却
    w2 = wrap(SimpleNamespace(), pg.Pan115CooldownError("429", 300.0))
    assert isinstance(w2, _mounts_first.MountCooldownError)


def test_sidecar_maps_deadline_kind():
    from backend.emby_server.p115_sidecar import _map_115_error
    mapped = _map_115_error(pg.Pan115DeadlineExceeded("过期", 0.0))
    assert mapped.kind == "deadline_exceeded"
    assert mapped.kind != "throttled"
    # guard 自己的类（RuntimeError，非 Pan115Error）按类型名识别
    mapped2 = _map_115_error(pg.Pan115DeadlineExceeded("过期", 0.0))
    assert mapped2.kind == "deadline_exceeded"
    # 冷却映射不变
    assert _map_115_error(pg.Pan115CooldownError("429", 10.0)).kind == "throttled"


def test_sidecar_wrap_deadline_is_timeout():
    exc = pr.ProviderRuntimeError("deadline_exceeded", "宿主超时")
    wrapped = mount_sidecar._wrap_sidecar_error(exc, "sidecar 列目录失败")
    assert isinstance(wrapped, _mounts_first.MountTimeoutError)
    assert not isinstance(wrapped, _mounts_first.MountCooldownError)


# ---------------- AD-2：签名探测，不做 TypeError 重调 ----------------

def test_dispatch_old_handler_called_once_without_deadline():
    calls = []

    class OldHandler:
        provider = "p115"
        extensions = ()
        def list_page(self, session, path, page_token, page_size):
            calls.append(1)
            return {"entries": []}

    server = pr.ProviderRuntimeServer(OldHandler(), socket_path="/tmp/x-dl5.sock")
    server._dispatch("session_upsert", {"session_id": "s", "data": {}})
    result = server._dispatch("list_page", {"session_id": "s", "path": "/x",
                                            "page_token": "0", "page_size": "1000"},
                              deadline=time.time() + 30)
    assert result == {"entries": []}
    assert calls == [1]


def test_dispatch_internal_typeerror_with_deadline_word_not_retried():
    """AD-2 回归：handler 内部 TypeError 消息含 "deadline" 时只调用一次。"""
    calls = []

    class BadHandler:
        provider = "p115"
        extensions = ()
        def list_page(self, session, path, page_token, page_size, deadline=None):
            calls.append(1)
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'float' (deadline)")

    server = pr.ProviderRuntimeServer(BadHandler(), socket_path="/tmp/x-dl6.sock")
    server._dispatch("session_upsert", {"session_id": "s", "data": {}})
    with pytest.raises(TypeError):
        server._dispatch("list_page", {"session_id": "s", "path": "/x"},
                         deadline=time.time() + 30)
    assert calls == [1], f"同一请求被调用 {len(calls)} 次（AD-2 双打回归）"


def test_dispatch_passes_deadline_when_signature_accepts():
    seen = []

    class H:
        provider = "p115"
        extensions = ()
        def list_page(self, session, path, page_token, page_size, deadline=None):
            seen.append(deadline)
            return {"entries": []}

    server = pr.ProviderRuntimeServer(H(), socket_path="/tmp/x-dl7.sock")
    server._dispatch("session_upsert", {"session_id": "s", "data": {}})
    dl = time.time() + 30
    server._dispatch("list_page", {"session_id": "s", "path": "/x"}, deadline=dl)
    assert seen == [dl]


# ---------------- 端到端：宿主请求带 deadline 字段 ----------------

def test_host_request_carries_deadline(monkeypatch):
    client = pr.ProviderRuntimeClient("p115", socket_path="/tmp/nonexist-dl.sock",
                                      timeout_sec=7.0)
    captured = {}

    class FakeSock:
        def settimeout(self, t): captured["timeout"] = t
        def connect(self, addr): pass
        def sendall(self, data):
            import json as _json
            captured["req"] = _json.loads(data.decode().strip())
        def shutdown(self, flag): pass
        def recv(self, n):
            import socket as _s
            raise _s.timeout()
        def close(self): pass

    import socket as _sockmod
    monkeypatch.setattr(_sockmod, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(pr.socket, "socket", lambda family, proto: FakeSock())
    before = time.time()
    with pytest.raises(pr.ProviderRuntimeError) as ei:
        client.call("list_page", {"session_id": "s", "path": "/"})
    req = captured["req"]
    assert req["method"] == "list_page"
    assert req["deadline"] - before == pytest.approx(7.0, abs=1.0)
    assert ei.value.kind == "timeout"


def test_guard_deadline_cap_exceeded_is_deadline_not_cooldown(monkeypatch):
    """排队上限被 deadline 收紧后超限：抛 Pan115DeadlineExceeded，不冒冷却语义"""
    monkeypatch.setattr(pg, "_gcra_wait", lambda key, rate, burst, now: 1.5)
    with pytest.raises(pg.Pan115DeadlineExceeded):
        pg.before_call("ck-dl-cap", "list", deadline=time.time() + 2.0)
    # 没有 deadline 时超过自身上限仍是本机限速排队超时
    monkeypatch.setattr(pg, "_gcra_wait", lambda key, rate, burst, now: 10_000.0)
    with pytest.raises(pg.Pan115CooldownError):
        pg.before_call("ck-dl-cap2", "list")
