"""PA-076：源站 vod_pic 直通服务端拉图字段的存储型 SSRF 面。

修法在取图层一处（写入侧拦不住重定向后跳私网）：
- ``image_store._fetch_once``：发请求前 ``verify_outbound_url``，
  重定向手动逐跳复验（不跟随 httpx 默认重定向）；
- ``media_routes._proxy_remote_image``：代理段同款逐跳复验。

本文件全部离线：内网/公网地址一律用字面 IP，``verify_outbound_url``
对字面 IP 不走 DNS；传输层用假 httpx，不发真请求。
"""
import sys
from types import SimpleNamespace

import pytest

from backend.emby_server import image_store
from backend.emby_server.upstream_http import OutboundBlockedError

PRIVATE_URLS = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/api/health",
    "http://10.0.0.5:6379/",
]
PUBLIC_IMG = "http://93.184.216.34/poster.jpg"
PUBLIC_IMG_2 = "http://93.184.216.34/other.jpg"


class _Resp:
    def __init__(self, status=200, content=b"IMG", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def fake_httpx(monkeypatch):
    """按 URL 查表的假 httpx：state.routes[url] = _Resp 或 _Resp 列表。"""
    state = SimpleNamespace(routes={}, calls=[])

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            state.calls.append(url)
            route = state.routes[url]
            if isinstance(route, list):
                return route.pop(0)
            return route

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_Client))
    return state


def _img_resp():
    return _Resp(status=200, content=b"IMG",
                 headers={"content-type": "image/jpeg"})


# ---------- image_store._fetch_once ----------

@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_fetch_once_blocks_private_without_request(fake_httpx, url):
    """内网/回环/私网 vod_pic：守卫在发请求前拦下，传输层零调用。"""
    fake_httpx.routes[url] = _img_resp()
    assert image_store._fetch_once(url, timeout=5) == (None, False)
    assert fake_httpx.calls == []


def test_download_private_does_not_retry(fake_httpx):
    """被拦截是确定性失败：_download 不重试、不发出站请求。"""
    url = PRIVATE_URLS[1]
    fake_httpx.routes[url] = _img_resp()
    assert image_store._download(url) is None
    assert fake_httpx.calls == []


def test_fetch_once_public_direct(fake_httpx):
    fake_httpx.routes[PUBLIC_IMG] = _img_resp()
    content, retryable = image_store._fetch_once(PUBLIC_IMG, timeout=5)
    assert content == b"IMG" and retryable is False
    assert fake_httpx.calls == [PUBLIC_IMG]


def test_fetch_once_redirect_to_private_blocked(fake_httpx):
    """公网图 302 到内网：只发了首跳，第二跳在发请求前被拦下。"""
    fake_httpx.routes[PUBLIC_IMG] = _Resp(
        status=302, headers={"location": PRIVATE_URLS[1]})
    fake_httpx.routes[PRIVATE_URLS[1]] = _img_resp()
    assert image_store._fetch_once(PUBLIC_IMG, timeout=5) == (None, False)
    assert fake_httpx.calls == [PUBLIC_IMG]


def test_fetch_once_redirect_to_public_followed(fake_httpx):
    """公网图 302 到另一公网地址：逐跳复验通过，跟到终点出图。"""
    fake_httpx.routes[PUBLIC_IMG] = _Resp(
        status=302, headers={"location": PUBLIC_IMG_2})
    fake_httpx.routes[PUBLIC_IMG_2] = _img_resp()
    content, retryable = image_store._fetch_once(PUBLIC_IMG, timeout=5)
    assert content == b"IMG" and retryable is False
    assert fake_httpx.calls == [PUBLIC_IMG, PUBLIC_IMG_2]


# ---------- media_routes._proxy_remote_image ----------

class _FakeProxyClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs.get("follow_redirects") is False, \
            "代理段不得用客户端默认跟随重定向"
        self.calls.append(url)
        route = self.routes[url]
        return route.pop(0) if isinstance(route, list) else route


def _proxy_module():
    from backend.emby_server import media_routes
    return media_routes


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_proxy_blocks_private_without_request(url):
    """代理段首跳就是内网：零出站请求，抛 OutboundBlockedError。"""
    media_routes = _proxy_module()
    client = _FakeProxyClient({url: _img_resp()})
    with pytest.raises(OutboundBlockedError):
        media_routes._proxy_remote_image(url, client=client)
    assert client.calls == []


def test_proxy_redirect_to_private_blocked():
    media_routes = _proxy_module()
    routes = {
        PUBLIC_IMG: _Resp(status=302,
                           headers={"location": PRIVATE_URLS[0]}),
        PRIVATE_URLS[0]: _img_resp(),
    }
    client = _FakeProxyClient(routes)
    with pytest.raises(OutboundBlockedError):
        media_routes._proxy_remote_image(PUBLIC_IMG, client=client)
    assert client.calls == [PUBLIC_IMG]


def test_proxy_redirect_to_public_followed():
    media_routes = _proxy_module()
    routes = {
        PUBLIC_IMG: _Resp(status=302,
                           headers={"location": PUBLIC_IMG_2}),
        PUBLIC_IMG_2: _img_resp(),
    }
    client = _FakeProxyClient(routes)
    resp = media_routes._proxy_remote_image(PUBLIC_IMG, client=client)
    assert resp.content == b"IMG"
    assert client.calls == [PUBLIC_IMG, PUBLIC_IMG_2]


def test_proxy_too_many_redirects_rejected():
    """重定向链超限：不无限跟随，抛错由路由层转 404。"""
    media_routes = _proxy_module()
    routes = {PUBLIC_IMG: [_Resp(status=302,
                                  headers={"location": PUBLIC_IMG_2})] * 10,
              PUBLIC_IMG_2: [_Resp(status=302,
                                    headers={"location": PUBLIC_IMG})] * 10}
    client = _FakeProxyClient(routes)
    with pytest.raises(RuntimeError):
        media_routes._proxy_remote_image(PUBLIC_IMG, client=client)
    assert len(client.calls) <= 6


# ---------- item_image：被拦截也排队修复（冒烟钉住的口径） ----------

def test_item_image_blocked_still_queues_repair(monkeypatch):
    """PA-076 回归（#1768 首轮冒烟实证）：代理段被拦截时，路由不得跳过
    _queue_image_repair——否则“远程图失效时也排队修复”冒烟项会红。

    直接调 item_image，db / 可见性 / 取图链全部用替身，只验路由分支。
    """
    from types import SimpleNamespace

    from fastapi import HTTPException

    from backend.emby_server import models as em

    media_routes = _proxy_module()
    item = SimpleNamespace(guid="item-1")

    class _Query:
        def __init__(self, model):
            self.model = model

        def filter(self, *a, **k):
            return self

        def first(self):
            # 条目本身命中，库查询落空（不走库封面分支）。
            return item if self.model is em.MediaItem else None

    class _FakeDB:
        def query(self, model):
            return _Query(model)

    queued = []
    monkeypatch.setattr(media_routes, "_item_visible", lambda db, user, it: True)
    monkeypatch.setattr(media_routes, "_first_image",
                        lambda it, kind, db: PRIVATE_URLS[1])
    monkeypatch.setattr(media_routes.image_store, "localize", lambda src: "")

    def _blocked(src, client=None):
        raise OutboundBlockedError("上游主机不是公网地址，已拦截: 127.0.0.1")

    monkeypatch.setattr(media_routes, "_proxy_remote_image", _blocked)
    monkeypatch.setattr(media_routes, "_queue_image_repair",
                        lambda db, it, src: queued.append(src))
    with pytest.raises(HTTPException) as excinfo:
        media_routes.item_image("item-1", "Primary", None, _FakeDB(),
                                SimpleNamespace())
    assert excinfo.value.status_code == 404
    assert queued == [PRIVATE_URLS[1]]
