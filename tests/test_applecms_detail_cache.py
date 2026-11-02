# -*- coding: utf-8 -*-
"""苹果 CMS detail 解析 TTL 缓存测试（播放优化 A2，全离线）。

口径：
- 同（源, vod）连取两发，detail 网络请求只发一次（第二发命中缓存）；
- TTL 分层：源 7 = 86400、源 9/13 = 43200、其余源 = 3600，
  地域 403 族播放主机（vip.ffzy-play* / svip.ryplay*）封顶 3600；
- 缓存条目在探活 403 / 404 / 解析失败后被淘汰并回源重解析一次，
  重解析仍失败按无缓存时的既有行为报错；
- 缓存关闭（APPLECMS_DETAIL_CACHE_ENABLED=0）时行为与今日一致：
  每发都现取 detail、不写缓存。
桩源 + 桩 http_get / http_probe，不联网；缓存走真实 CacheManager
（测试环境无 Redis 时是进程内内存回退），每条用例用独立 vod_id
隔离键，互不串扰。
"""
from types import SimpleNamespace

import pytest

from backend.database import CacheManager
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

BASE = "https://cms.example.com"
TOTAL = "UPSTREAM_PLAYBACK_ENABLED"


def _detail_payload(vod_id, address):
    return {"code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": vod_id, "vod_play_from": "直链线路",
        "vod_play_url": f"第01集${address}"}]}


def _source(source_id=7):
    return SimpleNamespace(
        id=source_id, source_type="applecms", base_url=BASE, enabled=True,
        user_agent=None, referer=None, extra_headers_json=None)


def _env_on(monkeypatch):
    monkeypatch.setenv(TOTAL, "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)
    monkeypatch.delenv("APPLECMS_DETAIL_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("APPLECMS_DETAIL_CACHE_TTL_DEFAULT_SEC", raising=False)
    monkeypatch.delenv("APPLECMS_DETAIL_CACHE_TTL_BY_SOURCE_SEC",
                       raising=False)
    monkeypatch.delenv("APPLECMS_DETAIL_CACHE_TTL_BLOCKED_CAP_SEC",
                       raising=False)


def _counting_get(payloads):
    """payloads: 每次网络请求依次返回的 payload 列表（发完重复最后一个）。"""
    calls = []

    def http_get(url, headers, timeout):
        calls.append(url)
        idx = min(len(calls) - 1, len(payloads) - 1)
        return 200, payloads[idx]
    return http_get, calls


def _probe(status):
    def probe(url, headers, timeout):
        return status, None
    return probe


def _resolve(monkeypatch, source_id, vod_id, http_get, http_probe):
    monkeypatch.setattr(us, "get_source",
                        lambda db, sid: _source(source_id))
    return up.resolve_upstream_stream(
        None, source_id, str(vod_id),
        http_get=http_get, http_probe=http_probe,
        http_get_text=lambda url, headers, timeout: (404, None))


def test_second_resolve_hits_detail_cache(monkeypatch):
    """同（源, vod）连取两发：detail 网络请求计数 = 1。"""
    _env_on(monkeypatch)
    payload = _detail_payload(7001, "https://cdn.example.com/a.m3u8")
    http_get, calls = _counting_get([payload])
    url1 = _resolve(monkeypatch, 7, 7001, http_get, _probe(206))
    url2 = _resolve(monkeypatch, 7, 7001, http_get, _probe(206))
    assert url1 == url2 == "https://cdn.example.com/a.m3u8"
    assert len(calls) == 1
    CacheManager.delete(up.cms_detail_cache_key(7, "7001"))


def test_ttl_tiers_by_source(monkeypatch):
    """TTL 分层取值：7→86400、9→43200、13→43200、其余→3600。"""
    _env_on(monkeypatch)
    assert up.cms_detail_cache_ttl(7, "https://cdn.example.com/a.m3u8") == 86400
    assert up.cms_detail_cache_ttl(9, "https://cdn.example.com/a.m3u8") == 43200
    assert up.cms_detail_cache_ttl(13, "https://cdn.example.com/a.m3u8") == 43200
    assert up.cms_detail_cache_ttl(21, "https://cdn.example.com/a.m3u8") == 3600


def test_ttl_blocked_host_family_capped(monkeypatch):
    """地域 403 族主机封顶 3600：vip.ffzy-play* 与 svip.ryplay*。"""
    _env_on(monkeypatch)
    assert up.cms_detail_cache_ttl(
        7, "https://vip.ffzy-play5.com/2026/a.m3u8") == 3600
    assert up.cms_detail_cache_ttl(
        13, "https://svip.ryplay14.com/2026/a.m3u8") == 3600
    # 可达族不封顶：非凡 vod.ffzy-bofang、如意 svip.ryiplay 不受影响
    assert up.cms_detail_cache_ttl(
        7, "https://vod.ffzy-bofang.com/2026/a.m3u8") == 86400
    assert up.cms_detail_cache_ttl(
        13, "https://svip.ryiplay18.com/2026/a.m3u8") == 43200


def test_ttl_env_override(monkeypatch):
    """TTL 基值可 env 覆盖：默认层与分源层都认 env。"""
    _env_on(monkeypatch)
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_TTL_DEFAULT_SEC", "120")
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_TTL_BY_SOURCE_SEC", "7:600")
    assert up.cms_detail_cache_ttl(21, "https://cdn.example.com/a.m3u8") == 120
    assert up.cms_detail_cache_ttl(7, "https://cdn.example.com/a.m3u8") == 600


def test_cached_entry_evicted_on_probe_404_then_reparsed(monkeypatch):
    """命中缓存后探活 404：淘汰缓存 + 回源重解析一次，新地址成功下发。"""
    _env_on(monkeypatch)
    stale = _detail_payload(7002, "https://cdn.example.com/stale.m3u8")
    fresh = _detail_payload(7002, "https://cdn.example.com/fresh.m3u8")
    http_get, calls = _counting_get([stale, fresh])
    # 第一发：现取并写缓存（探活 206 成功）
    url = _resolve(monkeypatch, 7, 7002, http_get, _probe(206))
    assert url == "https://cdn.example.com/stale.m3u8"
    assert len(calls) == 1
    # 第二发：命中缓存的地址探活 404 → 淘汰并重解析 → 新地址
    def probe(url_, headers, timeout):
        return (404, None) if "stale" in url_ else (206, None)
    url2 = _resolve(monkeypatch, 7, 7002, http_get, probe)
    assert url2 == "https://cdn.example.com/fresh.m3u8"
    assert len(calls) == 2  # 只多发了一次 detail（重解析）
    CacheManager.delete(up.cms_detail_cache_key(7, "7002"))


def test_cached_entry_evicted_on_probe_403_then_soft_pass(monkeypatch):
    """命中缓存后探活 403：淘汰重解析；重解析的新鲜条目仍走软放行。"""
    _env_on(monkeypatch)
    stale = _detail_payload(7003, "https://cdn.example.com/stale3.m3u8")
    fresh = _detail_payload(7003, "https://cdn.example.com/fresh3.m3u8")
    http_get, calls = _counting_get([stale, fresh])
    url = _resolve(monkeypatch, 7, 7003, http_get, _probe(206))
    assert url == "https://cdn.example.com/stale3.m3u8"
    # 第二发探活恒 403：缓存条目先被淘汰重解析，重解析结果按软放行下发
    url2 = _resolve(monkeypatch, 7, 7003, http_get, _probe(403))
    assert url2 == "https://cdn.example.com/fresh3.m3u8"
    assert len(calls) == 2
    CacheManager.delete(up.cms_detail_cache_key(7, "7003"))


def test_fresh_probe_403_soft_pass_unchanged(monkeypatch):
    """未命中缓存时探活 403 仍软放行（今日行为逐字不变、不重解析）。"""
    _env_on(monkeypatch)
    payload = _detail_payload(7004, "https://cdn.example.com/a4.m3u8")
    http_get, calls = _counting_get([payload])
    url = _resolve(monkeypatch, 7, 7004, http_get, _probe(403))
    assert url == "https://cdn.example.com/a4.m3u8"
    assert len(calls) == 1
    CacheManager.delete(up.cms_detail_cache_key(7, "7004"))


def test_reparse_failure_matches_uncached_behavior(monkeypatch):
    """淘汰重解析后仍失败：按无缓存时行为报错（探活 404 → 404 码）。"""
    _env_on(monkeypatch)
    stale = _detail_payload(7005, "https://cdn.example.com/stale5.m3u8")
    fresh = _detail_payload(7005, "https://cdn.example.com/fresh5.m3u8")
    http_get, calls = _counting_get([stale, fresh])
    url = _resolve(monkeypatch, 7, 7005, http_get, _probe(206))
    assert url == "https://cdn.example.com/stale5.m3u8"
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        _resolve(monkeypatch, 7, 7005, http_get, _probe(404))
    assert (exc.value.status, exc.value.code) == (
        404, "upstream_item_not_found")
    assert len(calls) == 2  # 淘汰后只重解析一次，不无限重试
    CacheManager.delete(up.cms_detail_cache_key(7, "7005"))


def test_cache_disabled_matches_today(monkeypatch):
    """缓存关闭：每发都现取 detail（计数 = 发数），行为与今日一致。"""
    _env_on(monkeypatch)
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "0")
    payload = _detail_payload(7006, "https://cdn.example.com/a6.m3u8")
    http_get, calls = _counting_get([payload])
    _resolve(monkeypatch, 7, 7006, http_get, _probe(206))
    _resolve(monkeypatch, 7, 7006, http_get, _probe(206))
    assert len(calls) == 2
    assert up.read_cms_detail(7, "7006") is None
