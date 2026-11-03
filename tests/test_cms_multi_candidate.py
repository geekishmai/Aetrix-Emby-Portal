# -*- coding: utf-8 -*-
"""CMS 同 vod 多候选解析回归（2026-11-03 生产实证）。

背景：剧集行 1155493 映射 292247（爱蛋 21，vod 209477）stream 连续
404。实查该 vod 的 detail 有 9 个全直链组，逐组探活只有第 7 组
（okm3u8）是 200，前 6 组全 404——旧实现 ``resolve_play`` 只取第一个
全直链组、``_play_from_cms_detail`` 只探那一条地址，探活一失败整条
线判死、从不试同 vod 的其它组。

钉死口径：
- 候选只许在同一条映射的同一个 vod 内（CMS detail 的平行播放组是
  同一内容的等价线路），不跨映射/条目/MediaSource；
- 逐候选探活，首个可播即下发；全灭抛首候选的错误（404 仍是可
  淘汰型，使缓存淘汰＋重取一轮的既有机制不变）；
- 403 软放行/缓存严判、单组行为、解析分支原因口径逐字不变。
"""
from types import SimpleNamespace

import pytest

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"

# 三个全直链组 × 两集（形态对齐生产 vod 209477 的多组结构）
MULTI_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 909, "vod_play_from": "组一$$$组二$$$组三",
        "vod_play_url":
            "第01集$https://cdn.example.com/g1e1.m3u8"
            "#第02集$https://cdn.example.com/g1e2.m3u8"
            "$$$第01集$https://cdn.example.com/g2e1.m3u8"
            "#第02集$https://cdn.example.com/g2e2.m3u8"
            "$$$第01集$https://cdn.example.com/g3e1.m3u8"
            "#第02集$https://cdn.example.com/g3e2.m3u8"}]}

# 无全直链组：甲组混合（第 1 集直链、第 2 集解析型），乙组纯解析型
MIXED_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 910, "vod_play_from": "甲组$$$乙组",
        "vod_play_url":
            "第01集$https://cdn.example.com/m1e1.m3u8"
            "#第02集$playtoken-ep2"
            "$$$第01集$playtoken-b1"}]}


def _source(**kw):
    row = SimpleNamespace(
        id=21, source_type="applecms", base_url=BASE, enabled=True,
        user_agent=None, referer=None, extra_headers_json=None)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


@pytest.fixture()
def cms_source(monkeypatch):
    row = _source()
    monkeypatch.setattr(us, "get_source", lambda db, sid: row)
    monkeypatch.setattr(up, "_capture_cms_quality_async",
                        lambda *a, **k: None)
    return row


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "0")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)


def _detail_get(payload):
    def http_get(url, headers, timeout):
        return 200, payload
    return http_get


def _probe_by_url(status_by_marker, captured):
    def probe(url, headers, timeout):
        captured.append(url)
        for marker, status in status_by_marker.items():
            if marker in url:
                return status, None
        return 200, None
    return probe


# ---------------- fetcher 候选层 ----------------

def test_candidates_order_direct_groups_then_mixed():
    vod = MULTI_DETAIL["list"][0]
    groups = acf.parse_play_groups(vod["vod_play_from"],
                                   vod["vod_play_url"])
    assert acf.episode_candidates(groups, 1) == [
        ("组一", "https://cdn.example.com/g1e1.m3u8"),
        ("组二", "https://cdn.example.com/g2e1.m3u8"),
        ("组三", "https://cdn.example.com/g3e1.m3u8")]
    # resolve_play 仍是取首候选，单候选口径不变
    assert acf.resolve_play(vod, 1) == (
        "ok", "组一", "https://cdn.example.com/g1e1.m3u8")


def test_candidates_mixed_group_direct_episode_counts():
    vod = MIXED_DETAIL["list"][0]
    # 无全直链组时，混合组里该集自身的直链地址算候选（不经解析分支）
    assert acf.resolve_play_candidates(vod, 1) == (
        "ok", [("甲组", "https://cdn.example.com/m1e1.m3u8")])
    assert acf.resolve_play(vod, 1) == (
        "ok", "甲组", "https://cdn.example.com/m1e1.m3u8")
    # 第 2 集在混合组里是解析型、无直链候选 → 解析原因口径不变
    assert acf.resolve_play_candidates(vod, 2) == (
        "unplayable", "parse_not_enabled")


def test_candidates_out_of_range_precedence_kept():
    # 有全直链组时先判越界：混合组里有第 2 集也不许绕过越界判定
    vod = {"vod_play_from": "直链$$$混合",
           "vod_play_url":
               "第01集$https://cdn.example.com/only1.m3u8"
               "$$$第01集$https://cdn.example.com/m1.m3u8"
               "#第02集$https://cdn.example.com/m2.m3u8"
               "#第03集$playtoken-x"}
    assert acf.resolve_play_candidates(vod, 2) == (
        "unplayable", "episode_out_of_range")


def test_candidates_parse_only_reasons_unchanged():
    vod = {"vod_play_from": "解析线路",
           "vod_play_url": "第01集$playtoken202"}
    assert acf.resolve_play_candidates(vod, 1) == (
        "unplayable", "parse_not_enabled")
    assert acf.resolve_play_candidates(
        vod, 1, parse_lines_enabled=True) == (
        "unplayable", "parse_rule_missing")
    assert acf.resolve_play_candidates({}, 1) == (
        "unplayable", "no_direct_group")


# ---------------- 播放层逐候选探活 ----------------

def test_first_group_dead_falls_to_next_group(monkeypatch, cms_source):
    _env_on(monkeypatch)
    captured = []
    url = up.resolve_upstream_stream(
        None, 21, "909", episode_index=1,
        http_get=_detail_get(MULTI_DETAIL),
        http_probe=_probe_by_url({"g1": 404, "g2": 200}, captured))
    assert url == "https://cdn.example.com/g2e1.m3u8"
    # 探活顺序＝候选顺序，第 3 组没被碰
    assert captured == ["https://cdn.example.com/g1e1.m3u8",
                        "https://cdn.example.com/g2e1.m3u8"]


def test_all_groups_dead_raises_first_candidate_error(
        monkeypatch, cms_source):
    _env_on(monkeypatch)
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 21, "909", episode_index=1,
            http_get=_detail_get(MULTI_DETAIL),
            http_probe=_probe_by_url({"g1": 404, "g2": 404, "g3": 404},
                                     captured))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")
    # 404 仍是可淘汰型：缓存淘汰＋重取一轮的既有机制靠它触发
    assert isinstance(exc.value, up._CmsEvictableFailure)
    assert len(captured) == 3


def test_probe_limit_caps_candidate_probes(monkeypatch, cms_source):
    _env_on(monkeypatch)
    groups = "$$$".join(f"组{i}" for i in range(1, 13))
    urls = "$$$".join(
        f"第01集$https://cdn.example.com/x{i}e1.m3u8" for i in range(1, 13))
    detail = {"code": 1, "list": [{
        "vod_id": 911, "vod_play_from": groups, "vod_play_url": urls}]}
    captured = []
    with pytest.raises(up.UpstreamPlaybackError):
        up.resolve_upstream_stream(
            None, 21, "911", episode_index=1,
            http_get=_detail_get(detail),
            http_probe=_probe_by_url({"x": 404}, captured))
    assert len(captured) == up.CMS_CANDIDATE_PROBE_LIMIT


def test_soft_pass_403_stays_on_first_candidate(monkeypatch, cms_source):
    _env_on(monkeypatch)
    captured = []
    url = up.resolve_upstream_stream(
        None, 21, "909", episode_index=1,
        http_get=_detail_get(MULTI_DETAIL),
        http_probe=_probe_by_url({"g1": 403}, captured))
    # 现取路径 403 软放行口径不变：首候选即下发，不逐个试
    assert url == "https://cdn.example.com/g1e1.m3u8"
    assert captured == ["https://cdn.example.com/g1e1.m3u8"]


def test_second_candidate_403_soft_passes(monkeypatch, cms_source):
    _env_on(monkeypatch)
    url = up.resolve_upstream_stream(
        None, 21, "909", episode_index=1,
        http_get=_detail_get(MULTI_DETAIL),
        http_probe=_probe_by_url({"g1": 404, "g2": 403}, []))
    assert url == "https://cdn.example.com/g2e1.m3u8"


def test_single_group_404_unchanged(monkeypatch, cms_source):
    _env_on(monkeypatch)
    detail = {"code": 1, "list": [{
        "vod_id": 912, "vod_play_from": "唯一组",
        "vod_play_url": "第01集$https://cdn.example.com/solo.m3u8"}]}
    captured = []
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            None, 21, "912", episode_index=1,
            http_get=_detail_get(detail),
            http_probe=_probe_by_url({"solo": 404}, captured))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")
    assert captured == ["https://cdn.example.com/solo.m3u8"]
