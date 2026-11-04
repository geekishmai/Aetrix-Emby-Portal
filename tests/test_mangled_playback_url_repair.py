"""畸形播放地址修复口（2026-11-03 生产实证）。

背景：Yamby/2.1.0.11 把 PlaybackInfo 下发的绝对 DirectStreamUrl
当相对路径，直接拼到自己的 /emby 前缀后请求本站，形态如
``/embyhttps://cdn.135505.autos/emby/Videos/{guid}/upstream/stream
?MediaSourceId=...&uid=...&exp=...&sign=...``——必然落 FastAPI
默认 404（22 字节），2026-11-03 当日 60 次（其中 48 次是我们自家
加速域名的上游线路地址）。客户端拼接方式改不了，本口在服务端
把这类拼坏的地址还原：嵌回的绝对地址主机必须属于本站下发播放
地址用过的主机集合（请求基址 / EMBY_PUBLIC_URL / 加速域名名单 /
流节点注册表），命中才 302 还原（查询串原样带回）；名单外主机、
非 http(s) scheme 一律维持 404，不做开放重定向。

本文件只测路由层与还原判定，不碰真实 DB（配置读口用桩替换）。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.routing import Match

from backend.emby_server import api

GUID = "5d82511902746d01f65222e4a66faafc"
QUERY = (
    f"MediaSourceId={GUID}:upstream:2001530"
    "&uid=866&exp=1793700195&sign=1a29e2b0bfb281275f2e9d9e3a2f6811"
)
EMBEDDED = f"cdn.example.com/emby/Videos/{GUID}/upstream/stream"


def _req(query=QUERY, host="emby.example.com"):
    return SimpleNamespace(
        url=SimpleNamespace(query=query, hostname=host),
        headers={},
    )


def _hosts(monkeypatch, hosts):
    monkeypatch.setattr(
        api, "_issued_playback_hosts", lambda db, request: set(hosts))


# ----------------------------------------------------------------------
# 路由注册与形态匹配
# ----------------------------------------------------------------------

def test_repair_route_registered():
    paths = [getattr(r, "path", "") for r in api.emby_router.routes]
    assert "/emby{scheme}://{embedded:path}" in paths


def test_repair_route_matches_mangled_path():
    route = next(
        r for r in api.emby_router.routes
        if getattr(r, "path", "") == "/emby{scheme}://{embedded:path}")
    match, scope = route.matches({
        "type": "http", "method": "GET",
        "path": f"/embyhttps://{EMBEDDED}",
    })
    assert match == Match.FULL
    assert scope["path_params"]["scheme"] == "https"
    assert scope["path_params"]["embedded"] == EMBEDDED


def test_repair_route_does_not_match_normal_paths():
    route = next(
        r for r in api.emby_router.routes
        if getattr(r, "path", "") == "/emby{scheme}://{embedded:path}")
    for path in (f"/emby/Videos/{GUID}/upstream/stream",
                 "/emby/System/Info/Public",
                 "/emby/videos/x/stream.mkv"):
        match, _scope = route.matches(
            {"type": "http", "method": "GET", "path": path})
        assert match != Match.FULL, path


# ----------------------------------------------------------------------
# 还原判定（_mangled_redirect_url）
# ----------------------------------------------------------------------

def test_redirect_restores_accel_host_url_with_query(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com", "emby.example.com"})
    target = api._mangled_redirect_url(None, _req(), "https", EMBEDDED)
    assert target == f"https://{EMBEDDED}?{QUERY}"


def test_redirect_allows_request_host(monkeypatch):
    _hosts(monkeypatch, {"emby.example.com"})
    embedded = f"emby.example.com/emby/Videos/{GUID}/upstream/stream"
    target = api._mangled_redirect_url(None, _req(), "https", embedded)
    assert target == f"https://{embedded}?{QUERY}"


def test_redirect_without_query(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com"})
    target = api._mangled_redirect_url(
        None, _req(query=""), "http", EMBEDDED)
    assert target == f"http://{EMBEDDED}"


def test_foreign_host_stays_404(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com", "emby.example.com"})
    evil = "evil.example.com/phish"
    assert api._mangled_redirect_url(None, _req(), "https", evil) is None


def test_wrapped_third_party_proxy_host_stays_404(monkeypatch):
    # 客户端自带的第三方代理包装（如 sm.alib.cc.cd/gu/<谷歌地址>）
    # 不是本站下发过的主机，不许借本站域名做开放重定向。
    _hosts(monkeypatch, {"cdn.example.com", "emby.example.com"})
    wrapped = ("sm.alib.cc.cd/gu/https://drive.usercontent.google.com"
               "/download?id=abc")
    assert api._mangled_redirect_url(None, _req(), "https", wrapped) is None


def test_bad_scheme_rejected(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com"})
    assert api._mangled_redirect_url(None, _req(), "ftp", EMBEDDED) is None
    assert api._mangled_redirect_url(None, _req(), "", EMBEDDED) is None


# ----------------------------------------------------------------------
# 下发主机集合（_issued_playback_hosts）
# ----------------------------------------------------------------------

def test_issued_hosts_include_accel_list_and_nodes(monkeypatch):
    from backend.emby_server import stream_nodes
    from backend.integrations import store

    monkeypatch.setattr(
        store, "get_value",
        lambda db, key, default="": "emby.example.com,cdn.example.com")
    monkeypatch.setattr(
        stream_nodes, "_raw_nodes",
        lambda db: [{"url": "https://node1.example.com"}])
    monkeypatch.setattr(
        api, "_base_url", lambda request: "https://emby.example.com")
    hosts = api._issued_playback_hosts(object(), _req())
    assert hosts == {
        "emby.example.com", "cdn.example.com", "node1.example.com"}


def test_issued_hosts_fail_closed_on_config_errors(monkeypatch):
    from backend.integrations import store

    def _boom(db, key, default=""):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_value", _boom)
    monkeypatch.setattr(
        api, "_base_url", lambda request: "https://emby.example.com")
    hosts = api._issued_playback_hosts(object(), _req())
    # 配置读失败只剩请求侧主机，绝不放大到任意主机
    assert hosts == {"emby.example.com"}


# ----------------------------------------------------------------------
# handler 语义：命中 302、未命中 404
# ----------------------------------------------------------------------

def test_handler_redirects_when_host_allowed(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com"})
    resp = asyncio.run(api.mangled_playback_url_repair(
        "https", EMBEDDED, _req(), None))
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://{EMBEDDED}?{QUERY}"


def test_handler_404_when_host_not_allowed(monkeypatch):
    _hosts(monkeypatch, {"cdn.example.com"})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.mangled_playback_url_repair(
            "https", "evil.example.com/phish", _req(), None))
    assert excinfo.value.status_code == 404
