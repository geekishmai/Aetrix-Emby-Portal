# -*- coding: utf-8 -*-
"""起播优化件三（PR-B）回归：画质回填后台化＋CMS 播放探活 2.5 秒。

语义（2026-11-02 定案）：
- ``_play_from_cms_detail`` 两处画质回填调用点不再同步抓清单，
  改走 ``_capture_cms_quality_async`` 后台线程：302 先回，后台再补。
  回填漏了不致命——门禁未满时下次点播再补。
- CMS 播放 302 关键路径探活单跳超时收紧到 2.5 秒（新常量
  ``CMS_PLAY_PROBE_TIMEOUT_SEC``）；共享常量
  ``UPSTREAM_PROBE_TIMEOUT_SEC``（10 秒）仍供 Emby 认证 /
  Emby-Jellyfin 分支 / 媒体参数拉取使用，一字不动。
- 全离线：detail / 探活 / 清单抓取一律桩注入，不联网。
"""
import time
from types import SimpleNamespace

import pytest

from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

TOTAL = "UPSTREAM_PLAYBACK_ENABLED"
BASE = "https://cms.example.com"

SERIES_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 202, "vod_play_from": "直链线路$$$解析线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"
                      "#第02集$https://cdn.example.com/ep2.mp4"
                      "$$$第01集$playtoken202"}]}

M3U8_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 205, "vod_play_from": "直链线路",
        "vod_play_url": "第01集$https://cdn.example.com/master.m3u8"}]}

_MASTER_TEXT = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,'
    'CODECS="avc1.640028,mp4a.40.2"\n'
    "1080.m3u8\n"
)


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


def _probe_status(status):
    def probe(url, headers, timeout):
        return status, None
    return probe


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "0")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)


def _wait_quality(mapping_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cached = up.read_cms_quality(mapping_id)
        if cached and cached.get("width"):
            return cached
        time.sleep(0.05)
    return up.read_cms_quality(mapping_id)


def test_cms_play_probe_timeout_is_2_5(monkeypatch, cms_source):
    _env_on(monkeypatch)
    seen = {}

    def probe(url, headers, timeout):
        seen["timeout"] = timeout
        return 206, None

    url = up.resolve_upstream_stream(
        None, 7, "202",
        http_get=_detail_get(SERIES_DETAIL), http_probe=probe)
    assert url == "https://cdn.example.com/ep1.mp4"
    assert seen["timeout"] == 2.5
    # 共享常量不动：Emby 认证 / Emby-Jellyfin 分支 / 媒体参数拉取仍用 10 秒
    assert up.UPSTREAM_PROBE_TIMEOUT_SEC == 10.0


def test_quality_capture_does_not_block_302(monkeypatch, cms_source):
    _env_on(monkeypatch)

    def slow_text(url, headers, timeout):
        time.sleep(2.0)
        return 200, _MASTER_TEXT

    t0 = time.perf_counter()
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977901,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok(),
        http_get_text=slow_text)
    elapsed = time.perf_counter() - t0
    assert url == "https://cdn.example.com/master.m3u8"
    # 清单抓取要 2 秒，302 回来必须远小于它（后台线程里慢慢抓）
    assert elapsed < 1.0
    cached = _wait_quality(977901)
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["label"] == "1080P"


def test_capture_dispatched_on_both_branches(monkeypatch, cms_source):
    _env_on(monkeypatch)
    calls = []

    def recorder(source, mapping_id, detail, address, *, http_get_text=None):
        calls.append((mapping_id, address))

    monkeypatch.setattr(up, "_capture_cms_quality_async", recorder)
    url_ok = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977902,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok())
    url_403 = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977903,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_status(403))
    assert url_ok == "https://cdn.example.com/master.m3u8"
    assert url_403 == "https://cdn.example.com/master.m3u8"
    assert [c[0] for c in calls] == [977902, 977903]
