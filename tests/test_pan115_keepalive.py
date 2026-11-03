# -*- coding: utf-8 -*-
"""115 进程级共享 httpx.Client（fix/pan115-keepalive）。

背景：transfer115 每个请求都 `with httpx.Client(...)` 新建客户端，2 次/秒的
列目录 = 每秒 2 个新 TCP+TLS 握手；run 10 的握手超时两簇都落在这个时段。

覆盖：
- 同进程连续两次调用只建一次 client（连接复用）；
- Cookie/UA 逐请求传，不进 client 级 headers（账号不串）；
- 超时口径与 deadline 参数不变；
- 共享 client 拒收 Set-Cookie（A 账号的会话 Cookie 不会进 B 账号请求）；
- 连接池上限 4 / keepalive 30s；长传输（OSS 600s/上传 300s）不占共享池；
- fork 后按 pid 重建（不复用父进程连接）；
- 连接级错误丢弃并重建；普通读超时不重建。
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, ".")
from backend.emby_server import transfer115  # noqa: E402


class _FakeResponse:
    status_code = 200
    headers: dict = {}
    content = b""

    def json(self):
        return {"state": True, "data": []}


class _FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.requests = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return _FakeResponse()

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        self.closed = True


class _FakeCtxClient(_FakeClient):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture(autouse=True)
def _clean_shared_client():
    yield
    with transfer115._http_client_lock:
        client = transfer115._http_client
        transfer115._http_client = None
        transfer115._http_client_pid = None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _install(monkeypatch, *clients):
    created = []

    def factory():
        client = clients[len(created)]
        created.append(client)
        return client

    monkeypatch.setattr(transfer115, "_new_http_client", factory)
    return created


def test_two_calls_share_one_client(monkeypatch):
    created = _install(monkeypatch, _FakeClient())
    transfer115._http_json("GET", "https://webapi.115.com/files")
    transfer115._http_json("GET", "https://webapi.115.com/files")
    assert len(created) == 1
    assert len(created[0].requests) == 2


def test_cookie_and_ua_are_per_request_not_client_level(monkeypatch):
    created = _install(monkeypatch, _FakeClient())
    transfer115._http_json("GET", "https://webapi.115.com/a", cookie="UID=1_A",
                           ua="UA-1")
    transfer115._http_json("GET", "https://webapi.115.com/b", cookie="UID=2_B",
                           ua="UA-2")
    headers = [req[2]["headers"] for req in created[0].requests]
    assert headers[0]["Cookie"] == "UID=1_A"
    assert headers[1]["Cookie"] == "UID=2_B"
    assert headers[0]["User-Agent"] == "UA-1"
    assert headers[1]["User-Agent"] == "UA-2"


def test_timeout_passthrough_unchanged(monkeypatch):
    created = _install(monkeypatch, _FakeClient())
    transfer115._http_json("GET", "https://webapi.115.com/a", timeout=3.5)
    transfer115._http_json("GET", "https://webapi.115.com/b")
    assert created[0].requests[0][2]["timeout"] == 3.5
    assert created[0].requests[1][2]["timeout"] == transfer115.PAN115_TIMEOUT
    assert created[0].requests[0][2]["follow_redirects"] is True


def test_http_full_uses_dedicated_client_not_shared(monkeypatch):
    """OSS 长传输（read 600s）独立 client，不占共享池的 4 条连接。"""
    shared = _FakeClient()
    _install(monkeypatch, shared)
    dedicated = _FakeCtxClient()
    init = {}

    def factory(*args, **kwargs):
        init.update(kwargs)
        return dedicated

    monkeypatch.setattr(httpx, "Client", factory)
    transfer115._http_full("GET", "https://oss.example/x")
    assert shared.requests == [], "长传输不应走共享 client"
    assert len(dedicated.requests) == 1
    assert init["follow_redirects"] is True
    assert init["timeout"].read == 600.0
    assert init["timeout"].write == 600.0
    assert dedicated.closed is True, "独立 client 用完即关"


def test_pool_limits_are_small():
    client = transfer115._new_http_client()
    try:
        pool = client._transport._pool
        assert pool._max_connections == 4
        assert pool._keepalive_expiry == 30.0
    finally:
        client.close()


def test_fork_rebuilds_client(monkeypatch):
    old = _FakeClient()
    new = _FakeClient()
    created = _install(monkeypatch, new)
    with transfer115._http_client_lock:
        transfer115._http_client = old
        transfer115._http_client_pid = os.getpid() + 1   # 模拟父进程 pid
    assert transfer115._get_http_client() is new
    assert created == [new]
    assert old.closed is False, "fork 后不 close 父进程的连接（fd 属于父进程）"
    assert transfer115._http_client_pid == os.getpid()


def test_connection_error_discards_and_rebuilds(monkeypatch):
    broken = _FakeClient(error=httpx.ConnectError("handshake boom"))
    healthy = _FakeClient()
    created = _install(monkeypatch, broken, healthy)
    with pytest.raises(httpx.ConnectError):
        transfer115._http_json("GET", "https://webapi.115.com/a")
    assert broken.closed is True
    transfer115._http_json("GET", "https://webapi.115.com/b")
    assert len(created) == 2
    assert len(healthy.requests) == 1


def test_read_timeout_does_not_discard(monkeypatch):
    """普通读超时不是连接坏了：不重建（避免慢接口导致连接池抖动）。"""
    slow = _FakeClient(error=httpx.ReadTimeout("slow"))
    created = _install(monkeypatch, slow)
    with pytest.raises(httpx.ReadTimeout):
        transfer115._http_json("GET", "https://webapi.115.com/a")
    assert slow.closed is False
    assert len(created) == 1


def test_set_cookie_from_a_never_sent_to_b(monkeypatch):
    """A 账号响应里的 Set-Cookie 不进共享 jar，也不会出现在 B 的请求头。"""
    client = transfer115._new_http_client()
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200, headers={"Set-Cookie": "leak=1; Path=/"}, json={"state": True})

    client._transport = httpx.MockTransport(handler)
    with transfer115._http_client_lock:
        transfer115._http_client = client
        transfer115._http_client_pid = os.getpid()
    transfer115._http_json("GET", "https://115.com/a", cookie="UID=A")
    transfer115._http_json("GET", "https://115.com/b", cookie="UID=B")
    assert seen == ["UID=A", "UID=B"]
    assert not list(client.cookies.jar), "共享 client 不得保存任何 Set-Cookie"


def test_http_bytes_uses_shared_client(monkeypatch):
    created = _install(monkeypatch, _FakeClient())
    transfer115._http_bytes("POST", "https://uplb.115.com/4.0/initupload.php",
                            data=b"x")
    assert len(created) == 1
    assert created[0].requests[0][2]["follow_redirects"] is True
