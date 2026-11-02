# -*- coding: utf-8 -*-
"""CMS 画质回填：跳转型播放地址 + media playlist 时长（回归）。

缺陷（2026-11-02 生产实查）：``_capture_cms_quality`` 只在播放地址
以 .m3u8 结尾时才抓主清单回填 cmsq 缓存。跳转型 share 地址（无
.m3u8 后缀）被直接跳过；其 detail 的 vod_remarks 又多是「已完结」
之类无画质标记，payload 恒空——生产 Redis ``rb:cmsq:*`` 全站仅
1 条，展示侧线路长期没有宽高/标签。

钉死口径：
- 非 .m3u8 地址也做一次有界尝试：与探活层同款手工逐跳跟随
  （每跳先 verify_outbound_url 复验、safe_urlopen 本身仍不跟随），
  仅当最终响应内容以 #EXTM3U 开头时才按主清单解析；HTML/403/
  跳私网写 no-master 标记（PA-080：门禁闭合、TTL 内不再重探），
  全程 fail-safe 不抛错。
- 抓到的是 media playlist（只有 EXTINF 分段、无主清单信号）时，
  分段时长求和存 duration_sec；展示侧 _synthetic_cms_base_source
  在本地行时长为空时用它填 RunTimeTicks。只回填时长，不探编码。
- .m3u8 直抓与 PA-073 新鲜度门禁行为不变（见
  test_applecms_playback 的 PA-073 组）。

测试分工：share 形态直测 ``_capture_cms_quality`` 本体（地址如何
经 resolve_play 直链组选路到达回填口是既有行为、本单不动）；
media playlist 时长另有一条 resolve 全链路用例。全部离线：
出站替身口径与 test_upstream_probe_redirect 同款（patch
upstream_http 模块属性，handler 按 URL 脚本化响应，非 2xx 以
HTTPError 形态抛出）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import email.message
import io
import time
import urllib.error
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import api as emby_api
from backend.emby_server import models as em
from backend.emby_server import quality_label as ql
from backend.emby_server import upstream_http
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"
TICKS = 10_000_000

SHARE_URL = "https://vip.example.com/share/abc123"
INDEX_URL = "https://cdn.example.com/index.m3u8"
MEDIA_URL = "https://bf.example.com/media.m3u8"

MEDIA_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 207, "vod_play_from": "直链线路",
        "vod_play_url": f"第01集${MEDIA_URL}"}]}

_MASTER_TEXT = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,'
    'CODECS="avc1.640028,mp4a.40.2"\n'
    "1080.m3u8\n"
)

# media playlist：只有分段、无主清单变体（暴风型源的真实形态）
_MEDIA_TEXT = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:21\n"
    "#EXTINF:10.5,\n"
    "seg1.ts\n"
    "#EXTINF:20.25,\n"
    "seg2.ts\n"
    "#EXT-X-ENDLIST\n"
)
_MEDIA_DURATION = 30.75


# ---------------------------------------------------------------- 夹具/替身


def _source(**kw):
    row = SimpleNamespace(
        id=7, source_type="applecms", base_url=BASE, enabled=True,
        user_agent=None, referer=None, extra_headers_json=None)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


@pytest.fixture()
def cms_source(monkeypatch):
    row = _source()
    monkeypatch.setattr(us, "get_source", lambda db, sid: row)
    return row


def _detail_get(payload):
    def http_get(url, headers, timeout):
        return 200, payload
    return http_get


def _probe_ok():
    def probe(url, headers, timeout):
        return 206, None
    return probe


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)


def _msg(headers):
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return msg


class _TextResp:
    """2xx 响应替身：带真实响应体（画质抓取要读体，与探活替身不同）。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]


class _TextTransport:
    """脚本化出站替身：handler(url) -> (status, headers, body bytes)。

    verify 记录每个被咨询的 URL，blocked 片段命中的抛
    OutboundBlockedError（与真实 verify 的拒绝形态一致）；
    open 记录每个真正发出的请求 URL，非 2xx 抛 HTTPError
    （与无重定向处理器的真实 opener 行为一致）。
    """

    def __init__(self, handler, blocked=()):
        self.handler = handler
        self.blocked = tuple(blocked)
        self.requested = []
        self.verified = []

    def verify(self, url):
        self.verified.append(url)
        if any(fragment in url for fragment in self.blocked):
            raise upstream_http.OutboundBlockedError("替身拦截")
        return None

    def open(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        status, headers, body = self.handler(url)
        if 200 <= status < 300:
            return _TextResp(status, _msg(headers), body)
        raise urllib.error.HTTPError(
            url, status, "fake", _msg(headers), io.BytesIO(body))


def _install_transport(monkeypatch, handler, blocked=()):
    transport = _TextTransport(handler, blocked)
    monkeypatch.setattr(upstream_http, "safe_urlopen", transport.open)
    monkeypatch.setattr(
        upstream_http, "verify_outbound_url", transport.verify)
    return transport


def _capture(mid, address):
    """直调回填口：detail 无 vod_remarks（信号只能来自抓取）。"""
    up._capture_cms_quality(_source(), mid, {}, address)


def _wait_quality(mapping_id, key, timeout=5.0):
    """等后台画质回填落缓存（resolve 全链路的回填已后台化）。

    回填本体整包一次性 store：等到目标字段出现即可断言其余字段；
    超时返回最后一次读数，调用方断言照常报错（不掩盖真失败）。
    与 test_cms_play_quality_background 的 _wait_quality 同口径。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        cached = up.read_cms_quality(mapping_id)
        if cached and cached.get(key):
            return cached
        time.sleep(0.05)
    return up.read_cms_quality(mapping_id)


# ---------------------------------------------------------------- 跳转型地址


def test_share_redirect_to_master_backfills_quality(monkeypatch):
    """share 地址 302 落到主清单 → 缓存写入宽高/带宽/标签。"""

    def handler(url):
        if url == SHARE_URL:
            return 302, {"Location": INDEX_URL}, b""
        if url == INDEX_URL:
            return 200, {}, _MASTER_TEXT.encode()
        raise AssertionError(f"unexpected url {url}")

    transport = _install_transport(monkeypatch, handler)
    _capture(978001, SHARE_URL)
    # 有界：恰好两跳（share → index），不多抓
    assert transport.requested == [SHARE_URL, INDEX_URL]
    cached = up.read_cms_quality(978001)
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["bandwidth"] == 8_000_000
    assert cached["label"] == "1080P"
    # 主清单没有时长信号，不许编 duration
    assert "duration_sec" not in cached


def test_share_master_closes_gate(monkeypatch):
    """PA-080(d)：share→主清单线第二发不再发 HTTP，且首发值不退化。"""

    def handler(url):
        if url == SHARE_URL:
            return 302, {"Location": INDEX_URL}, b""
        if url == INDEX_URL:
            return 200, {}, _MASTER_TEXT.encode()
        raise AssertionError(f"unexpected url {url}")

    transport = _install_transport(monkeypatch, handler)
    _capture(978009, SHARE_URL)
    cached = up.read_cms_quality(978009)
    assert cached["width"] == 1920 and cached["label"] == "1080P"
    _capture(978009, SHARE_URL)
    assert transport.requested == [SHARE_URL, INDEX_URL]
    assert up.read_cms_quality(978009) == cached


def test_share_html_response_not_cached(monkeypatch):
    """share 地址返回 HTML 页 → 写 no-master 标记、不抛错。"""

    def handler(url):
        assert url == SHARE_URL
        return 200, {}, b"<html><body>player page</body></html>"

    transport = _install_transport(monkeypatch, handler)
    _capture(978002, SHARE_URL)
    # PA-080：落点非清单且 payload 为空，写 no-master 标记使门禁闭合
    cached = up.read_cms_quality(978002)
    assert cached and cached.get("no_master") is True
    assert not cached.get("width") and not cached.get("label")
    # 第二发门禁闭合：不再发任何 HTTP
    _capture(978002, SHARE_URL)
    assert transport.requested == [SHARE_URL]


def test_share_403_response_not_cached(monkeypatch):
    """share 抓取 403（区域拒绝型源）→ 写 no-master 标记、不抛错。"""

    def handler(url):
        assert url == SHARE_URL
        return 403, {}, b"region has been denied"

    transport = _install_transport(monkeypatch, handler)
    _capture(978003, SHARE_URL)
    # PA-080：失败且 payload 为空，写 no-master 标记使门禁闭合
    cached = up.read_cms_quality(978003)
    assert cached and cached.get("no_master") is True
    # 第二发门禁闭合：不再发任何 HTTP
    _capture(978003, SHARE_URL)
    assert transport.requested == [SHARE_URL]


def test_share_redirect_to_private_blocked(monkeypatch):
    """重定向跳私网 → 守卫拦下、第二跳绝不发出、不写缓存。"""

    def handler(url):
        assert url == SHARE_URL
        return 302, {"Location": "https://169.254.169.254/latest"}, b""

    transport = _install_transport(
        monkeypatch, handler, blocked=("169.254.169.254",))
    _capture(978004, SHARE_URL)
    # 私网地址只被咨询校验、绝不真正请求
    assert transport.requested == [SHARE_URL]
    assert any("169.254.169.254" in u for u in transport.verified)
    # PA-080：跟随失败且 payload 为空，写 no-master 标记；第二发闭合
    cached = up.read_cms_quality(978004)
    assert cached and cached.get("no_master") is True
    _capture(978004, SHARE_URL)
    assert transport.requested == [SHARE_URL]


def test_share_redirect_loop_gives_up(monkeypatch):
    """重定向自环 → 跳数耗尽即放弃（有界），不写缓存。"""

    def handler(url):
        assert url == SHARE_URL
        return 302, {"Location": SHARE_URL}, b""

    transport = _install_transport(monkeypatch, handler)
    _capture(978005, SHARE_URL)
    assert len(transport.requested) == up.UPSTREAM_PROBE_MAX_REDIRECTS + 1
    # PA-080：跳数耗尽且 payload 为空，写 no-master 标记；第二发闭合
    cached = up.read_cms_quality(978005)
    assert cached and cached.get("no_master") is True
    _capture(978005, SHARE_URL)
    assert len(transport.requested) == up.UPSTREAM_PROBE_MAX_REDIRECTS + 1


# ---------------------------------------------------------------- media playlist 时长


def test_media_playlist_duration_backfilled(monkeypatch, cms_source):
    """直链 .m3u8 是 media playlist → resolve 全链路只回填时长。"""
    _env_on(monkeypatch)
    calls = []

    def http_get_text(url, headers, timeout):
        calls.append(url)
        return 200, _MEDIA_TEXT

    url = up.resolve_upstream_stream(
        None, 7, "207", quality_mapping_id=978006,
        http_get=_detail_get(MEDIA_DETAIL), http_probe=_probe_ok(),
        http_get_text=http_get_text)
    assert url == MEDIA_URL
    # 回填已后台化：先等时长落袋再断言抓取次数与缓存内容，
    # 不许按旧同步契约立即读（与后台线程竞速必偶红）
    cached = _wait_quality(978006, "duration_sec")
    assert calls == [MEDIA_URL]
    assert cached["duration_sec"] == _MEDIA_DURATION
    # media playlist 没有分辨率/带宽信号，不许编
    assert not cached.get("width") and not cached.get("bandwidth")


def test_media_playlist_duration_via_share_redirect(monkeypatch):
    """share 跳转落到 media playlist → 同样回填 duration_sec。"""

    def handler(url):
        if url == SHARE_URL:
            return 302, {"Location": MEDIA_URL}, b""
        if url == MEDIA_URL:
            return 200, {}, _MEDIA_TEXT.encode()
        raise AssertionError(f"unexpected url {url}")

    _install_transport(monkeypatch, handler)
    _capture(978007, SHARE_URL)
    cached = up.read_cms_quality(978007)
    assert cached["duration_sec"] == _MEDIA_DURATION


def test_media_playlist_duration_closes_gate(monkeypatch):
    """PA-080(a)：media playlist 线第二发 _capture 不再发 HTTP。"""

    def handler(url):
        if url == SHARE_URL:
            return 302, {"Location": MEDIA_URL}, b""
        if url == MEDIA_URL:
            return 200, {}, _MEDIA_TEXT.encode()
        raise AssertionError(f"unexpected url {url}")

    transport = _install_transport(monkeypatch, handler)
    _capture(978008, SHARE_URL)
    assert up.read_cms_quality(978008)["duration_sec"] == _MEDIA_DURATION
    _capture(978008, SHARE_URL)
    assert transport.requested == [SHARE_URL, MEDIA_URL]


def test_media_playlist_duration_parser_units():
    assert ql.media_playlist_duration_sec(_MEDIA_TEXT) == _MEDIA_DURATION
    # 主清单不是计量对象（时长在变体里，本函数不追）
    assert ql.media_playlist_duration_sec(_MASTER_TEXT) is None
    # 无 EXTINF / 空文本 → 判不出，不猜
    assert ql.media_playlist_duration_sec("#EXTM3U\n#EXT-X-ENDLIST\n") is None
    assert ql.media_playlist_duration_sec("") is None
    assert ql.media_playlist_duration_sec(None) is None


# ---------------------------------------------------------------- 展示侧时长回填


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _cms_item_with_mapping(db, *, duration_ticks=0):
    lib = em.Library(guid="cmsq-lib", name="cmsq时长库",
                     collection_type="movies", is_enabled=True,
                     kind="applecms")
    db.add(lib)
    db.flush()
    src = em.UpstreamSource(name="cmsq时长源", source_type="applecms",
                            base_url="http://cmsq.example", enabled=True)
    db.add(src)
    db.flush()
    item = em.MediaItem(guid="cmsq-item", library_id=lib.id,
                        item_type="movie", name="cmsq时长片",
                        duration_ticks=duration_ticks,
                        upstream_source_id=src.id, probe_status="done")
    db.add(item)
    db.flush()
    mapping = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=src.id, upstream_item_id="9",
        confidence=0.9, evidence="title+year")
    db.add(mapping)
    db.commit()
    return item, mapping


def test_display_runtime_filled_from_cached_duration(mem_db):
    """本地行时长为空 + 缓存有 duration_sec → RunTimeTicks 被填。"""
    item, mapping = _cms_item_with_mapping(mem_db, duration_ticks=0)
    up.store_cms_quality(mapping.id, {"duration_sec": _MEDIA_DURATION})
    dto = emby_api._synthetic_cms_base_source(
        item, mapping, "http://base", "", None)
    assert dto["RunTimeTicks"] == int(_MEDIA_DURATION * TICKS)


def test_display_runtime_keeps_local_duration(mem_db):
    """本地行已有真实时长 → 缓存时长不覆盖。"""
    item, mapping = _cms_item_with_mapping(
        mem_db, duration_ticks=100 * TICKS)
    up.store_cms_quality(mapping.id, {"duration_sec": _MEDIA_DURATION})
    dto = emby_api._synthetic_cms_base_source(
        item, mapping, "http://base", "", None)
    assert dto["RunTimeTicks"] == 100 * TICKS


# ---------------------------------------------------------------- 跟随总预算（PA-080）


def test_follow_total_budget_stops_at_deadline(monkeypatch):
    """PA-080(c)：预算耗尽即停——总耗时约 0.3 秒而非 4×0.3 秒。"""
    calls = []

    def single_hop(url, headers, timeout):
        calls.append(timeout)
        time.sleep(0.3)
        return 302, None, "https://cdn.example.com/next"

    monkeypatch.setattr(up, "_get_text_single_hop", single_hop)
    # 跟随层逐跳的出站复验一并打桩：不桩会对 cdn.example.com 做
    # 真实 DNS 复验，无解析的环境里结果由 DNS 决定、不确定。
    monkeypatch.setattr(
        upstream_http, "verify_outbound_url", lambda url: None)
    start = time.monotonic()
    status, text = up._default_http_get_text_followed(
        "https://a.example.com/start", {}, 0.3)
    elapsed = time.monotonic() - start
    assert (status, text) == (None, None)
    assert len(calls) == 1  # 旧口径会连发 4 跳（1.2 秒）
    assert elapsed < 0.9


def test_follow_budget_allows_multi_hop_within_budget(monkeypatch):
    """预算内多跳仍走完：单跳超时单调递减、首跳 ≤ 预算。"""
    calls = []
    hops = [
        (302, None, "https://b.example.com/2"),
        (302, None, "https://b.example.com/3"),
        (200, "#EXTM3U\n", None),
    ]

    def single_hop(url, headers, timeout):
        calls.append(timeout)
        time.sleep(0.05)
        return hops[len(calls) - 1]

    monkeypatch.setattr(up, "_get_text_single_hop", single_hop)
    # 跟随层逐跳的出站复验必须打桩：不桩会对 b.example.com 做
    # 真实 DNS 复验，CI 解析失败即 OutboundBlockedError 必红。
    monkeypatch.setattr(
        upstream_http, "verify_outbound_url", lambda url: None)
    status, text = up._default_http_get_text_followed(
        "https://b.example.com/1", {}, 3.0)
    assert (status, text) == (200, "#EXTM3U\n")
    assert len(calls) == 3
    assert calls[0] > calls[1] > calls[2]  # 每跳取 min(预算, 剩余)
    assert calls[0] <= 3.0
