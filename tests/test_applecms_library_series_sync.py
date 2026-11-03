# -*- coding: utf-8 -*-
"""苹果 CMS 剧集库同步回归（新媒体库类型·集行同步单）。

口径（2026-11-02 定案）：
- tvshows 库放行：白名单口径与电影库一致（cms_type_map 非空 =
  只有标 in 的顶级分类收）；收进来的条目一律按剧处理——拉 detail
  现取集列表建「剧行＋合成单季行＋集行＋逐集映射」（行形见
  applecms_series），播放地址绝不落库、点播时现取。
- 集序号 = 直链组内 1 起序号（parse_play_groups +
  pick_direct_group 选组，与取流口 resolve_play 同一口径）；
  解析不了的集（无直链组）整剧跳过并计数留痕，不建死行；
  detail 已消失（源侧删条目）跳过计数；detail 请求失败只计
  detail_errors，不许把整源回滚。
- 集数增减：只增不删——新集靠增量轮（h=24 里重现的条目重拉
  detail）补行，源侧删集不删已有行，存量对账兜底（对账单口径）。
- 跨源去重与电影同键（标题+年份）：首见源建树，其余源只往
  已有集行上补映射（一行多映射），不建第二棵树。
- 身份不豁免：剧/集行 enrich_status 保持 pending 交 enrich 补
  TMDB 身份（集跟随剧绑定），本工人不做任何货架过滤。
- 库是配置层：一库一白名单的机制不写死库数量/库名——按地区
  细拆的多个剧集库（国产剧/欧美剧/日韩剧/动漫/综艺，站主
  2026-11-02 14:05 定版）各自配白名单即可承接，港台并入国产
  的分类名清单由建库时逐源核定，不进代码。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_playback as up


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电视剧"},
    {"type_id": 2, "type_pid": 1, "type_name": "国产剧"},
    {"type_id": 5, "type_pid": 0, "type_name": "综艺"},
]


def _entry(vod_id, name, *, year="2019", type_id=2,
           vod_time="2024-05-01 10:00:00"):
    return {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": type_id,
        "type_name": "国产剧" if type_id == 2 else "综艺",
        "vod_serial": "", "vod_time": vod_time,
    }


def _detail(vod_id, name, episodes, *, year="2019"):
    """episodes: [(集标题, 地址), ...] 组成单个直链组。"""
    import zlib
    play_url = "#".join(f"{t}${a}" for t, a in episodes)
    return {"vod_id": vod_id, "vod_name": name, "vod_year": year,
            "vod_play_from": "直链线路", "vod_play_url": play_url,
            "vod_douban_id": str(
                100000 + zlib.crc32(f"{name}|{year}".encode()) % 900000)}


def _eps(prefix, count):
    return [(f"第{i}集", f"https://cdn.example.com/{prefix}/e{i}.m3u8")
            for i in range(1, count + 1)]


S7_LIST = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 5,
    "class": CLASS_TREE,
    "list": [
        _entry(202, "庆余年"),
        _entry(203, "解析剧"),
        _entry(204, "消失剧"),
        _entry(205, "快乐大本营", type_id=5),   # 综艺：白名单外
        _entry(206, "报错剧"),
    ],
}

S7_DETAILS = {
    "202": _detail(202, "庆余年", _eps("qyn", 3)),
    # 只有解析型组：无直链组 → 整剧跳过留痕
    "203": {"vod_id": 203, "vod_name": "解析剧",
            "vod_play_from": "解析线路",
            "vod_play_url": "第1集$https://parse.example.com/play?url=x"},
    # detail list 为空 → 源侧已消失
    "204": None,
    # detail 请求 500 → detail_errors
    "206": "HTTP500",
}

S8_LIST = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 1,
    "class": CLASS_TREE,
    "list": [_entry("777", "庆余年")],   # 与源 7 同剧（标题+年份去重）
}

S8_DETAILS = {
    "777": _detail(777, "庆余年", [
        (f"第{i}集", f"https://s8cdn.example.com/qyn/e{i}.m3u8")
        for i in range(1, 4)]),
}


class FakeHttpGet:
    """按 host + ac/ids 路由的 provide 桩；detail 载荷可替换。"""

    def __init__(self):
        self.urls = []
        self.lists = {"s7.example": S7_LIST, "s8.example": S8_LIST}
        self.details = {"s7.example": dict(S7_DETAILS),
                        "s8.example": dict(S8_DETAILS)}

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "list":
            return 200, json.loads(json.dumps(
                self.lists[host], ensure_ascii=False))
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            payload = self.details[host].get(vod_id)
            if payload == "HTTP500":
                return 500, {"code": 0, "msg": "boom"}
            if payload is None:
                return 200, {"code": 1, "page": 1, "pagecount": 0,
                             "list": []}
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db, *, source_ids="7", type_map=None):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-tv", name="国产剧", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids=source_ids,
        cms_type_map=json.dumps(
            {"电视剧": "in"} if type_map is None else type_map,
            ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def _rows(db, lib, item_type):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id,
                    em.MediaItem.item_type == item_type)
            .all())


def _maps_for(db, item_id):
    return (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id == item_id)
            .all())


def _probe_ok(url, headers, timeout):
    return 206, None


# ---------------------------------------------------------------------------
# 桩链一段：同步建剧+集
# ---------------------------------------------------------------------------

def test_series_sync_builds_tree_with_skip_traces(db):
    lib = _seed(db)
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    assert stats["blocked"] is None
    assert stats["errors"] == []          # detail 失败不许回滚整源
    assert stats["added"] == 1            # 只建成庆余年一棵树
    assert stats["episodes_added"] == 3
    assert stats["skipped_category"] == 1     # 综艺（白名单外）
    assert stats["skipped_no_direct_group"] == 1  # 解析剧
    assert stats["skipped_detail_missing"] == 1   # 消失剧
    assert stats["detail_errors"] == 1        # 报错剧
    assert stats["seen_by_source"] == {
        7: ["202", "203", "204", "205", "206"]}

    series = _rows(db, lib, "series")
    assert [r.name for r in series] == ["庆余年"]
    assert series[0].enrich_status == "pending"  # 身份交 enrich，不豁免
    assert series[0].tmdb_id is None
    assert series[0].probe_status == "done"
    seasons = _rows(db, lib, "season")
    assert len(seasons) == 1 and seasons[0].season_number == 1
    episodes = sorted(_rows(db, lib, "episode"),
                      key=lambda r: r.episode_number)
    assert [e.name for e in episodes] == ["第1集", "第2集", "第3集"]
    assert all(e.series_id == series[0].id
               and e.parent_id == seasons[0].id for e in episodes)
    # 播放地址绝不落库
    for row in series + seasons + episodes:
        blob = " ".join(str(v) for v in vars(row).values()
                        if isinstance(v, str))
        assert "cdn.example.com" not in blob
    # 逐集映射：episode_index 1..3、upstream_item_id = vod_id
    maps = [m for e in episodes for m in _maps_for(db, e.id)]
    assert sorted((m.upstream_item_id, m.episode_index)
                  for m in maps) == [("202", 1), ("202", 2), ("202", 3)]
    assert stats["mapped"] == 3
    # 进度写回含集行计数
    db.refresh(lib)
    saved = json.loads(lib.scan_stats)
    assert saved["cms_sync"]["episodes_added"] == 3


# ---------------------------------------------------------------------------
# 桩链二段：集行走既有 mapped 取流口 resolve 出对应集地址
# ---------------------------------------------------------------------------

def test_episode_row_resolves_nth_episode_address(db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    lib = _seed(db)
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    episodes = {e.episode_number: e for e in _rows(db, lib, "episode")}
    url2 = up.resolve_mapped_upstream_stream(
        db, episodes[2].id, http_get=http, http_probe=_probe_ok)
    assert url2 == "https://cdn.example.com/qyn/e2.m3u8"
    url1 = up.resolve_mapped_upstream_stream(
        db, episodes[1].id, http_get=http, http_probe=_probe_ok)
    assert url1 == "https://cdn.example.com/qyn/e1.m3u8"


# ---------------------------------------------------------------------------
# 桩链三段：增量补集（h=24 重现 → detail 多集 → 只补新行）
# ---------------------------------------------------------------------------

def test_incremental_round_adds_new_episodes(db):
    lib = _seed(db)
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    assert len(_rows(db, lib, "episode")) == 3
    # 源侧更新：同一条目涨到 5 集，增量轮（默认 h=24）重拉 detail
    http.details["s7.example"]["202"] = _detail(202, "庆余年",
                                                _eps("qyn", 5))
    http.urls.clear()
    stats2 = als.sync_library(db, lib, http_get=http,
                              sleep_fn=lambda s: None)
    list_urls = [u for u in http.urls if "ac=list" in u]
    assert list_urls and all("h=24" in u for u in list_urls)
    assert stats2["added"] == 0
    assert stats2["episodes_added"] == 2
    assert stats2["episodes_existing"] == 3
    episodes = sorted(_rows(db, lib, "episode"),
                      key=lambda r: r.episode_number)
    assert [e.episode_number for e in episodes] == [1, 2, 3, 4, 5]
    assert len(_rows(db, lib, "series")) == 1


def test_source_side_episode_removal_keeps_rows(db):
    """只增不删：源侧删集不删已有行（存量由对账口径兜底）。"""
    lib = _seed(db)
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    http.details["s7.example"]["202"] = _detail(202, "庆余年",
                                                _eps("qyn", 2))
    stats2 = als.sync_library(db, lib, http_get=http,
                              sleep_fn=lambda s: None)
    assert stats2["episodes_added"] == 0
    assert len(_rows(db, lib, "episode")) == 3


# ---------------------------------------------------------------------------
# 跨源去重：一行多映射，不建第二棵树
# ---------------------------------------------------------------------------

def test_cross_source_dedupe_attaches_episode_mappings(db):
    lib = _seed(db, source_ids="7,8")
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http,
                             sleep_fn=lambda s: None)
    assert stats["errors"] == []
    assert stats["added"] == 1
    assert stats["dedupe_hits"] >= 1
    assert len(_rows(db, lib, "series")) == 1
    episodes = _rows(db, lib, "episode")
    assert len(episodes) == 3
    pairs = {(m.source_id, m.upstream_item_id, m.episode_index)
             for e in episodes for m in _maps_for(db, e.id)}
    assert pairs == ({(7, "202", i) for i in (1, 2, 3)}
                     | {(8, "777", i) for i in (1, 2, 3)})


# ---------------------------------------------------------------------------
# PA-070：已补身份的剧行在新源同剧到来时仍须去重
# ---------------------------------------------------------------------------

def test_enriched_series_row_still_dedupes_new_source(db):
    """已补 tmdb 身份的剧行＋新源同标题+年份条目 → 只 1 棵树、新源只补集映射。

    触发条件：同轮内 run_cache 命中会掩盖本 bug，故第二轮只喂新源
    （老源条目不在本轮出现时，_find_series_row 的候选比对才真正跑到）。
    """
    lib = _seed(db, source_ids="7")
    http = FakeHttpGet()
    als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    assert len(_rows(db, lib, "series")) == 1
    # 模拟 enrich 补身份：行上已有 tmdb_id
    series = _rows(db, lib, "series")[0]
    series.tmdb_id = "12345"
    db.commit()
    # 新源 8 单独一轮
    lib.upstream_source_ids = "8"
    db.commit()
    stats2 = als.sync_library(db, lib, http_get=http,
                              sleep_fn=lambda s: None)
    assert stats2["added"] == 0
    assert len(_rows(db, lib, "series")) == 1
    assert len(_rows(db, lib, "episode")) == 3
    pairs = {(m.source_id, m.upstream_item_id, m.episode_index)
             for e in _rows(db, lib, "episode") for m in _maps_for(db, e.id)}
    assert (8, "777", 1) in pairs and (8, "777", 3) in pairs


def test_find_series_row_matches_imdb_enriched_candidate(db):
    """直测：仅补 imdb 身份的候选行同样按标题+年份命中。"""
    lib = _seed(db, source_ids="7")
    source8 = db.query(em.UpstreamSource).filter_by(id=8).one()
    row = em.MediaItem(guid="pa070-imdb-cand", library_id=lib.id,
                       item_type="series", name="庆余年",
                       production_year="2019", imdb_id="tt1234567")
    db.add(row)
    db.commit()
    record = agg.UpstreamCatalogRecord(
        source_id="8", upstream_item_id="777", name="庆余年",
        item_type="Series", production_year="2019")
    key = agg.aggregate_key_for(record)
    found = als._find_series_row(db, lib, source8, "777", record, key)
    assert found is not None and found.id == row.id


# ---------------------------------------------------------------------------
# 门与边界：mixed 仍 blocked、映射关仍 blocked
# ---------------------------------------------------------------------------

def test_mixed_library_still_blocked(db):
    lib = _seed(db)
    lib.collection_type = "mixed"
    db.commit()
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["blocked"] == "unsupported_collection"
    assert _rows(db, lib, "series") == []


def test_series_sync_blocked_when_mapping_off(db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "0")
    lib = _seed(db)
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["blocked"] == "mapping_off"
    assert _rows(db, lib, "series") == []
