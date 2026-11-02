# -*- coding: utf-8 -*-
"""苹果 CMS 播放分支测试（全离线）。

- 源行用桩（SimpleNamespace）并 monkeypatch ``us.get_source``：
  源类型允许集目前只有 emby/jellyfin（模型 CHECK 约束同款），
  苹果 CMS 源行尚建不出来，分支代码与测试先用桩源就绪，
  等允许集放行即接通（见 upstream_playback 模块 docstring 缺口）。
- detail 抓取与探活一律用桩注入（http_get / http_probe），不联网；
  夹具地址均为 example.com 占位域名。
"""
import os
import time
from types import SimpleNamespace

import pytest

from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"

SERIES_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 202, "vod_play_from": "直链线路$$$解析线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"
                      "#第02集$https://cdn.example.com/ep2.mp4?token=x"
                      "$$$第01集$playtoken202"}]}

PARSE_ONLY_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 203, "vod_play_from": "解析线路",
        "vod_play_url": "第01集$playtoken203"}]}

NO_PLAY_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 204, "vod_play_from": "", "vod_play_url": ""}]}


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


def _detail_get(payload, captured=None):
    def http_get(url, headers, timeout):
        if captured is not None:
            captured.append(url)
        return 200, payload
    return http_get


def _probe_ok(captured=None):
    def probe(url, headers, timeout):
        if captured is not None:
            captured.append(url)
        return 206, None
    return probe


def _probe_status(status, retry_after=None):
    def probe(url, headers, timeout):
        return status, retry_after
    return probe


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    # 本文件的既有用例按无缓存口径断言网络错误映射等旧语义：
    # detail 解析缓存关掉（缓存行为由 test_applecms_detail_cache 专测）。
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "0")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)


def test_switch_defaults_off(monkeypatch):
    for key in ("APPLECMS_ENABLED", "APPLECMS_PARSE_LINES_ENABLED",
                "APPLECMS_TIER_GATE", "UPSTREAM_SYNC_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    assert up.applecms_enabled() is False
    assert up.applecms_parse_lines_enabled() is False
    assert up.applecms_tier_gate_enabled() is False
    assert up.applecms_sync_enabled() is False
    assert up.applecms_tier_configured() is False


def test_total_gate_off_blocks_applecms(monkeypatch, cms_source):
    monkeypatch.setenv(TOTAL, "0")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(None, 7, "202")
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_playback_disabled")


def test_applecms_gate_off_matches_closed_behavior(monkeypatch, cms_source):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "0")
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL, captured))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_playback_disabled")
    assert captured == []  # 关着就不发任何上游请求


def test_disabled_source_rejected_before_branch(monkeypatch):
    monkeypatch.setattr(us, "get_source",
                        lambda db, sid: _source(enabled=False))
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(None, 7, "202")
    assert (exc.value.status, exc.value.code) == (
        409, "upstream_source_disabled")


def test_direct_group_selected_and_episode_address(monkeypatch, cms_source):
    _env_on(monkeypatch)
    captured = []
    url = up.resolve_upstream_stream(
        None, 7, "202", episode_index=2,
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok(captured))
    assert url == "https://cdn.example.com/ep2.mp4?token=x"
    assert captured == [url]
    assert "playtoken" not in url  # 解析组没被选中


def test_episode_defaults_to_first(monkeypatch, cms_source):
    """播放口形态：episode_index 未传时默认 1（播第 1 集）。

    HTTP 层（api.py upstream_stream 路由）目前未传 episode 参数，
    接线时由路由把 ?episode= 透传给本函数；未接线前一律播第 1 集。
    """
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 7, "202",
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert url == "https://cdn.example.com/ep1.mp4"


def test_episode_out_of_range(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202", episode_index=9,
            http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")


def test_parse_only_unplayable_reasons(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    assert exc.value.status == 502
    assert exc.value.code == "upstream_unavailable"
    assert "解析型线路未开启" in exc.value.message

    monkeypatch.setenv("APPLECMS_PARSE_LINES_ENABLED", "1")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    assert exc.value.status == 502
    assert "本站无解析规则" in exc.value.message


def test_no_direct_group_unplayable(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "204",
            http_get=_detail_get(NO_PLAY_DETAIL), http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        502, "upstream_unavailable")


def test_tier_gate_on_unconfigured_returns_403(monkeypatch, cms_source):
    """分级 403 复用结论：文件里唯一的 403 码是 upstream_forbidden，
    本批复用它（标准语义「上游拒绝」，此处 message 区分成本地
    分级未配置；若协调员要单独立码，换 _resolve_applecms_stream
    一处即可）。"""
    _env_on(monkeypatch)
    monkeypatch.setenv("APPLECMS_TIER_GATE", "1")
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL, captured),
            http_probe=_probe_ok())
    assert exc.value.status == 403
    assert exc.value.code == "upstream_forbidden"
    assert "分级映射未配置" in exc.value.message
    assert captured == []  # 门控在取数前就拦，不发上游请求


def test_tier_gate_off_skips_check(monkeypatch, cms_source):
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 7, "202",
        http_get=_detail_get(SERIES_DETAIL), http_probe=_probe_ok())
    assert url == "https://cdn.example.com/ep1.mp4"


def test_cms_error_mapping(monkeypatch, cms_source):
    _env_on(monkeypatch)
    cases = [
        (lambda: _detail_get({"code": 0, "list": []}),
         502, "upstream_unavailable"),
        (lambda url, headers, timeout: (429, {}), 429,
         "upstream_quota_exhausted"),
        (lambda url, headers, timeout: (401, {}), 409,
         "upstream_credential_invalid"),
        (lambda url, headers, timeout: (403, {}), 403,
         "upstream_forbidden"),
        (lambda url, headers, timeout: (None, None), 502,
         "upstream_unavailable"),
    ]
    for http_get, status, code in cases:
        with pytest.raises(up.UpstreamPlaybackError) as exc:
            up.resolve_upstream_stream(
                None, 7, "202", http_get=http_get, http_probe=_probe_ok())
        assert (exc.value.status, exc.value.code) == (status, code)
    # detail 空列表 = vod 消失 → 404
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "999",
            http_get=_detail_get({"code": 1, "list": []}),
            http_probe=_probe_ok())
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")


def test_probe_failure_classified(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(404))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(429, retry_after=60))
    assert exc.value.status == 429
    assert exc.value.retry_after == 60


def test_error_body_contains_no_playback_url(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "203",
            http_get=_detail_get(PARSE_ONLY_DETAIL), http_probe=_probe_ok())
    body = exc.value.to_body()
    assert "cdn.example.com" not in str(body)
    assert "playtoken" not in str(body)
    assert set(body) <= {"code", "message", "source_id", "retry_after"}


# ==================== 探活 403 软放行（CMS 库类型一期·播放单） ====================
# 口径（2026-11-02 定案，只改 CMS 分支）：多家 CMS 播放 CDN 对数据
# 中心 IP 一律 403——探活 403 只证明「服务器这个 IP 被拒」，不证明
# 链死；detail 刚刚现取成功（源活着、条目在、直链组在）才是可用性
# 信号。故 CMS 分支探活 403 不判死：记 soft-pass 结构化日志（只记
# source_id、不记地址），照常下发 302，播放器在用户家里自拉。
# 404/401/429/5xx 维持原判；Emby/Jellyfin 分支逐字不动（其 403 仍
# 是 upstream_forbidden，见 test_upstream_playback 的同名回归）。

def test_probe_403_soft_pass_returns_address(monkeypatch, cms_source, caplog):
    import logging

    _env_on(monkeypatch)
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.upstream_playback"):
        url = up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(403))
    assert url == "https://cdn.example.com/ep1.mp4"
    soft = [r for r in caplog.records if "cms_probe_soft_pass" in r.message]
    assert soft and "source_id=7" in soft[0].message
    # 日志纪律：只记 id，绝不记播放地址
    assert all("cdn.example.com" not in r.message for r in caplog.records)


def test_probe_403_soft_pass_episode_two(monkeypatch, cms_source):
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 7, "202", episode_index=2,
        http_get=_detail_get(SERIES_DETAIL),
        http_probe=_probe_status(403))
    assert url.startswith("https://cdn.example.com/ep2.mp4")


def test_probe_401_still_credential_invalid_on_cms(monkeypatch, cms_source):
    _env_on(monkeypatch)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 7, "202",
            http_get=_detail_get(SERIES_DETAIL),
            http_probe=_probe_status(401))
    assert (exc.value.status, exc.value.code) == (
        409, "upstream_credential_invalid")


# ==================== PA-073 画质回填新鲜度门禁 ====================
# 口径：_capture_cms_quality 开头先读画质缓存——已有 label 且宽、高
# 非空即直接返回、不再同步抓 m3u8 主清单；仅缺失/不完整时才抓取
# 回填。回归核心：同线连续两次点播，第二发对主清单的 GET 计数为 0
# （修复前每次点播都抓、计数为 2）。mapping_id 用大号唯一值隔离
# 进程内共享的 CacheManager 内存缓存。

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


def _counting_text_get(calls):
    def http_get_text(url, headers, timeout):
        calls.append(url)
        return 200, _MASTER_TEXT
    return http_get_text

def _wait_quality(mapping_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cached = up.read_cms_quality(mapping_id)
        if cached and cached.get("width"):
            return cached
        time.sleep(0.05)
    return up.read_cms_quality(mapping_id)


def test_quality_master_fetched_once_across_two_plays(
        monkeypatch, cms_source):
    _env_on(monkeypatch)
    calls = []
    # 回填已改后台：第一发回来时抓取可能还在跑，先等落袋再发第二发，
    # 否则第二发会在缓存补齐前重复抓取（异步语义，不是回退）。
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977001,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok(),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    assert _wait_quality(977001)["width"] == 1920
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977001,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok(),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    # 第一发抓主清单回填，第二发命中完整缓存、零抓取
    assert len(calls) == 1
    cached = up.read_cms_quality(977001)
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["label"] == "1080P"


def test_quality_master_fetched_once_soft_pass_branch(
        monkeypatch, cms_source):
    """软探活分支（探活 403）同样受门禁约束：两次点播只抓一发。"""
    _env_on(monkeypatch)
    calls = []
    # 回填已改后台：第一发回来时抓取可能还在跑，先等落袋再发第二发，
    # 否则第二发会在缓存补齐前重复抓取（异步语义，不是回退）。
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977002,
        http_get=_detail_get(M3U8_DETAIL),
        http_probe=_probe_status(403),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    assert _wait_quality(977002)["width"] == 1920
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977002,
        http_get=_detail_get(M3U8_DETAIL),
        http_probe=_probe_status(403),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    assert len(calls) == 1


def test_quality_incomplete_cache_still_fetches(monkeypatch, cms_source):
    """缓存不完整（只有 label、缺宽高）时仍抓主清单回填数值。"""
    _env_on(monkeypatch)
    up.store_cms_quality(977003, {"label": "1080P"})
    assert up.read_cms_quality(977003).get("width") in (None, 0)
    calls = []
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977003,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok(),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    cached = _wait_quality(977003)
    assert len(calls) == 1
    assert cached["width"] == 1920 and cached["height"] == 1080


def test_quality_no_cache_fetches_and_backfills(monkeypatch, cms_source):
    """无缓存时抓取行为与修复前一致：抓一发并回填完整画质。"""
    _env_on(monkeypatch)
    assert up.read_cms_quality(977004) is None
    calls = []
    url = up.resolve_upstream_stream(
        None, 7, "205", quality_mapping_id=977004,
        http_get=_detail_get(M3U8_DETAIL), http_probe=_probe_ok(),
        http_get_text=_counting_text_get(calls))
    assert url == "https://cdn.example.com/master.m3u8"
    cached = _wait_quality(977004)
    assert calls == ["https://cdn.example.com/master.m3u8"]
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["bandwidth"] == 8_000_000
    assert cached["label"] == "1080P"
