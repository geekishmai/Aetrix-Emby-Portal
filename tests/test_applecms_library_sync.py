# -*- coding: utf-8 -*-
"""苹果 CMS 库同步工人回归（新媒体库类型一期·同步单）。

口径（2026-11-02 定案）：
- 同步只拉 provide ``ac=list`` 分页，**不拉 detail**（播放地址绝不
  落库、元数据由 TMDB 补全链补）；行建成托管行（guid=hosted_guid、
  upstream_source_id=主来源、probe_status=done、enrich_status 保持
  pending 交给 enrich 补身份），对不上身份的行靠现行货架规矩不上架。
- 去重标题+年份键（aggregate_key_for）：跨源同片一行多映射。
- 一期只做电影：cms_type_map 非空 = 白名单（只有标 in 的顶级
  分类收），电影/剧集边界靠它；vod_serial>1 的剧集形态另跳过
  计数（试点 3 家该字段全空/全 0，不作数）。
- 映射开关关 = 整批 blocked 不建行（没映射的行播不了，不建孤儿行）。
- 增量走 h=24 窗口、全量不带 h；进度写回库行 scan_stats/last_scan_at。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server.upstream_import import hosted_guid


def _maps_for(db, item_id):
    return (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id == item_id)
            .all())


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
    {"type_id": 5, "type_pid": 0, "type_name": "综艺"},
]


def _fake_douban(name, year):
    """夹具豆瓣号：按片名+年份派生，同片跨源同号（认亲汇合口径）。"""
    import zlib
    return str(100000 + zlib.crc32(f"{name}|{year}".encode()) % 900000)


def _entry(vod_id, name, *, year="2021", type_id=2, serial="0",
           vod_time="2024-05-01 10:00:00"):
    return {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": type_id, "type_name": "动作片", "vod_serial": serial,
        "vod_time": vod_time, "vod_pic": "http://img.example/x.jpg",
        "vod_content": "简介文本", "vod_class": "动作",
        "vod_douban_id": _fake_douban(name, year),
    }


SOURCE7_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 4,
    "class": CLASS_TREE,
    "list": [
        _entry(101, "沙丘"),
        _entry(102, "庆余年", serial="36"),               # 剧集形态
        _entry(103, "快乐大本营", type_id=5, serial="1"),  # 综艺（map out）
        _entry(104, "无年份片", year=""),
    ],
}

SOURCE8_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 2,
    "class": CLASS_TREE,
    "list": [
        _entry("55", "沙丘"),       # 与源 7 同片（标题+年份键去重）
        _entry("56", "流浪地球", year="2019"),
    ],
}


class FakeHttpGet:
    """按 base_url 路由的 provide 桩；记录全部请求 URL 供断言。"""

    def __init__(self):
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        host = urllib.parse.urlparse(url).netloc
        payload = {"s7.example": SOURCE7_PAGE,
                   "s8.example": SOURCE8_PAGE}[host]
        # fetcher 的 http_get 契约：返回 (status, 已解析的 dict)
        return 200, json.loads(json.dumps(payload, ensure_ascii=False))


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


def _seed(db, *, type_map=None):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-movies", name="CMS电影",
        collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids="7,8",
        cms_type_map=json.dumps(type_map, ensure_ascii=False)
        if type_map is not None else None)
    db.add(lib)
    db.commit()
    return lib


def _movies(db, lib):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .order_by(em.MediaItem.name)
            .all())


def test_sync_builds_hosted_rows_and_dedupes(db):
    lib = _seed(db, type_map={"电影": "in"})
    http = FakeHttpGet()
    stats = als.sync_library(db, lib, http_get=http)
    assert stats["blocked"] is None
    rows = _movies(db, lib)
    # 沙丘（跨源去重一行）+ 无年份片 + 流浪地球 = 3 行
    assert [r.name for r in rows] == ["无年份片", "沙丘", "流浪地球"]
    dune = next(r for r in rows if r.name == "沙丘")
    assert dune.upstream_source_id == 7  # 先到源为主来源
    assert dune.guid == hosted_guid(7, "101")
    assert dune.probe_status == "done"
    assert dune.enrich_status == "pending"  # 身份交给 enrich 补 TMDB
    assert dune.tmdb_id is None
    assert dune.production_year == 2021
    maps = _maps_for(db, dune.id)
    assert {(m.source_id, m.upstream_item_id) for m in maps} == {
        (7, "101"), (8, "55")}
    assert all(m.confidence == 0.7 and m.evidence == "title+year"
               for m in maps)
    assert stats["added"] == 3 and stats["mapped"] == 4
    assert stats["skipped_series_shape"] == 1
    assert stats["skipped_category"] == 1
    assert stats["seen_by_source"] == {
        7: ["101", "102", "103", "104"], 8: ["55", "56"]}
    # detail 一次都不许拉：全部请求都是 ac=list
    assert http.urls and all("ac=list" in u for u in http.urls)
    # 进度写回库行
    db.refresh(lib)
    assert lib.last_scan_at is not None
    saved = json.loads(lib.scan_stats)
    assert saved["added"] == 3 and saved["cms_sync"]["mapped"] == 4


def test_sync_rerun_is_idempotent(db):
    lib = _seed(db, type_map={"电影": "in"})
    als.sync_library(db, lib, http_get=FakeHttpGet())
    stats2 = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats2["added"] == 0 and stats2["existing"] == 4  # 按条目计：沙丘两源各记一次
    assert len(_movies(db, lib)) == 3
    total_maps = sum(
        len(_maps_for(db, r.id)) for r in _movies(db, lib))
    assert total_maps == 4


def test_sync_blocked_when_mapping_off(db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "0")
    lib = _seed(db)
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["blocked"] == "mapping_off"
    assert _movies(db, lib) == []


def test_incremental_uses_h24_window_full_does_not(db):
    lib = _seed(db)
    http_inc = FakeHttpGet()
    als.sync_library(db, lib, http_get=http_inc)
    assert all("h=24" in u for u in http_inc.urls)
    http_full = FakeHttpGet()
    als.sync_library(db, lib, http_get=http_full, full=True)
    assert all("h=" not in u.split("ac=list")[-1] or "h=24" not in u
               for u in http_full.urls)


def test_type_map_allowlist_and_top_category_resolution(db):
    # 非空映射 = 白名单（试点实测定的形态）：只有标 in 的顶级分类
    # 收，没写到/out 的一律跳过；type_id 子类要沿分类树回溯到顶级
    # 名再查表——这里 {"电影": "out"} 下动作片（子类，顶级=电影）
    # 没标 in，应被跳过；不填映射时全收由其它用例覆盖。
    lib = _seed(db, type_map={"电影": "out"})
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    names = [r.name for r in _movies(db, lib)]
    assert "沙丘" not in names and "流浪地球" not in names
    assert stats["skipped_category"] >= 3


def test_dedupe_via_name_candidate_path_across_runs(db):
    """跨轮去重候选路径回归（试点实测抓到的崩溃）：第二轮换源喂
    同片时 run_cache 是空的，必须走「按名查候选行→对键」路径，
    不许崩、要挂上第二条映射而不是建重复行。"""
    lib = _seed(db, type_map={"电影": "in"})
    lib.upstream_source_ids = "7"
    db.commit()
    als.sync_library(db, lib, http_get=FakeHttpGet())
    lib.upstream_source_ids = "7,8"
    db.commit()
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == []
    dunes = [r for r in _movies(db, lib) if r.name == "沙丘"]
    assert len(dunes) == 1
    assert {(m.source_id, m.upstream_item_id)
            for m in _maps_for(db, dunes[0].id)} == {(7, "101"), (8, "55")}
    assert stats["dedupe_hits"] >= 1


def test_round_gate_off_by_default(db, monkeypatch):
    monkeypatch.delenv("APPLECMS_LIBRARY_SYNC_ENABLED", raising=False)
    _seed(db)
    assert als.sync_enabled() is False
    assert als.run_library_sync_round(
        session_factory=lambda: db, http_get=FakeHttpGet()) == []


def test_sync_quality_gate_blocks_gun_and_lowdef(db):
    """worker 同步路径质量门：枪版/低清条目不建行、不记映射。

    与扩抓 lane 同口径（片名/备注信号）；「枪」字正常标题与
    单词内嵌 CAM 不误杀；无信号不杀。
    """
    lib = _seed(db, type_map={"电影": "in"})
    page = {
        "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 8,
        "class": CLASS_TREE,
        "list": [
            _entry(201, "干净片"),
            _entry(202, "某片枪版"),
            dict(_entry(203, "某片"), vod_remarks="影院版"),
            dict(_entry(204, "某片"), vod_remarks="TELESYNC"),
            _entry(205, "某片标清"),
            dict(_entry(206, "某片"), vod_remarks="SD"),
            _entry(207, "枪神传说"),
            _entry(208, "CAMELOT行动"),
        ],
    }
    empty = {"code": 1, "page": 1, "pagecount": 1, "limit": 20,
             "total": 0, "class": CLASS_TREE, "list": []}

    class GateHttpGet:
        def __call__(self, url, headers=None, timeout=None):
            host = urllib.parse.urlparse(url).netloc
            payload = page if host == "s7.example" else empty
            return 200, json.loads(json.dumps(payload, ensure_ascii=False))

    stats = als.sync_library(db, lib, http_get=GateHttpGet())
    assert {r.name for r in _movies(db, lib)} == {
        "干净片", "枪神传说", "CAMELOT行动"}
    assert stats["skipped_quality_gun"] == 3
    assert stats["skipped_quality_lowdef"] == 2
    assert stats["added"] == 3
    assert stats["mapped"] == 3


def test_process_entry_series_mode_gate_before_detail(db):
    """剧集路径的门在 detail 拉取之前：枪版剧行不耗 detail 请求。"""
    lib = _seed(db, type_map={"电影": "in"})
    source = (db.query(em.UpstreamSource)
              .filter(em.UpstreamSource.id == 7).first())
    stats = als._zero_stats(lib.id)

    def _boom(url, headers=None, timeout=None):
        raise AssertionError("detail must not be fetched for gated entries")

    als._process_entry(db, lib, source, _entry(301, "某剧枪版"),
                       stats, {}, type_map={"电影": "in"}, tops={},
                       seen=[], series_mode=True, http_get=_boom,
                       headers={}, do_sleep=lambda s: None, delay=0.0)
    assert stats["skipped_quality_gun"] == 1
    assert _movies(db, lib) == []


# ---------------------------------------------------------------------------
# 入库标题尾年份解析（错认根因：黏尾年份无人剥离，production_year 为空
# → enrich 不带年份搜 TMDB、年份门跳过，绑到同名旧片）
# ---------------------------------------------------------------------------

def _record(name, year):
    return acs.cms_entry_to_record(
        {"vod_id": "1", "vod_name": name, "vod_year": year},
        source_id="7")


def test_cms_title_glued_year_fills_year_and_cleans_name():
    rec = _record("生化危机：爆发夜2026", "")
    assert rec.name == "生化危机：爆发夜"
    assert rec.production_year == 2026


def test_cms_title_glued_year_equal_to_vod_year_stripped():
    rec = _record("生化危机：爆发夜2026", "2026")
    assert rec.name == "生化危机：爆发夜"
    assert rec.production_year == 2026


def test_cms_title_year_conflict_keeps_original():
    """vod_year 与尾年份冲突：名与年份都不动，留待后续口径。"""
    rec = _record("生化危机：爆发夜2026", "2002")
    assert rec.name == "生化危机：爆发夜2026"
    assert rec.production_year == 2002


@pytest.mark.parametrize("raw,clean", [
    ("某片（2024）", "某片"),
    ("某片[2024]", "某片"),
    ("某片 (2024)", "某片"),
    ("某片 2024", "某片"),
])
def test_cms_title_year_forms_stripped(raw, clean):
    rec = _record(raw, "")
    assert rec.name == clean
    assert rec.production_year == 2024


def test_cms_title_out_of_range_year_not_stripped():
    """「银翼杀手2049」的 2049 在合理区间外，是片名本身，不剥。"""
    rec = _record("银翼杀手2049", "")
    assert rec.name == "银翼杀手2049"
    assert rec.production_year is None


def test_cms_title_non_trailing_digits_untouched():
    rec = _record("2012世界末日", "")
    assert rec.name == "2012世界末日"
    assert rec.production_year is None
    rec2 = _record("某剧第二季", "")
    assert rec2.name == "某剧第二季"
    assert rec2.production_year is None
    rec3 = _record("2026", "")
    assert rec3.name == "2026"
    assert rec3.production_year is None


# ---------------------------------------------------------------------------
# PA-062：电影版 _find_row 去重（与剧集版 PA-070 同构：候选比对剥身份）
# ---------------------------------------------------------------------------

def test_enriched_movie_row_still_dedupes_new_source(db):
    """已补 tmdb 身份的电影行＋新源同标题+年份条目 → 不建新行、只挂映射。

    与剧集版 PA-070 回归同构：第二轮只喂新源，绕开同轮 run_cache，
    让 _find_row 的候选比对真正跑到。
    """
    lib = _seed(db, type_map={"电影": "in"})
    lib.upstream_source_ids = "7"
    db.commit()
    als.sync_library(db, lib, http_get=FakeHttpGet())
    dune = next(r for r in _movies(db, lib) if r.name == "沙丘")
    dune.tmdb_id = "438631"
    db.commit()
    lib.upstream_source_ids = "8"
    db.commit()
    stats2 = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats2["added"] == 1  # 只有流浪地球是新行，沙丘不许重建
    dunes = [r for r in _movies(db, lib) if r.name == "沙丘"]
    assert len(dunes) == 1
    assert {(m.source_id, m.upstream_item_id)
            for m in _maps_for(db, dunes[0].id)} == {(7, "101"), (8, "55")}


def test_find_row_matches_imdb_enriched_candidate(db):
    """直测：仅补 imdb 身份的候选行同样按标题+年份命中。"""
    lib = _seed(db)
    source8 = db.query(em.UpstreamSource).filter_by(id=8).one()
    row = em.MediaItem(guid="pa062-imdb-cand", library_id=lib.id,
                       item_type="movie", name="沙丘",
                       production_year=2021, imdb_id="tt1160419")
    db.add(row)
    db.commit()
    record = agg.UpstreamCatalogRecord(
        source_id="8", upstream_item_id="55", name="沙丘",
        item_type="Movie", production_year=2021)
    key = agg.aggregate_key_for(record)
    found = als._find_row(db, lib, source8, "55", record, key)
    assert found is not None and found.id == row.id


@pytest.mark.parametrize("cand_year,rec_year", [(None, 2021), (2021, None)])
def test_find_row_year_one_side_empty_still_matches(db, cand_year, rec_year):
    """年份一空一有且标题相同视为同片（年份以非空为准）。"""
    lib = _seed(db)
    source8 = db.query(em.UpstreamSource).filter_by(id=8).one()
    row = em.MediaItem(guid=f"pa062-year-{cand_year}", library_id=lib.id,
                       item_type="movie", name="沙丘",
                       production_year=cand_year)
    db.add(row)
    db.commit()
    record = agg.UpstreamCatalogRecord(
        source_id="8", upstream_item_id="55", name="沙丘",
        item_type="Movie", production_year=rec_year)
    key = agg.aggregate_key_for(record)
    found = als._find_row(db, lib, source8, "55", record, key)
    assert found is not None and found.id == row.id


def test_find_row_year_conflict_does_not_match(db):
    """年份两边都有且不同 = 另一部片（重拍/续作），不许误并。"""
    lib = _seed(db)
    source8 = db.query(em.UpstreamSource).filter_by(id=8).one()
    row = em.MediaItem(guid="pa062-year-conflict", library_id=lib.id,
                       item_type="movie", name="沙丘",
                       production_year=1984)
    db.add(row)
    db.commit()
    record = agg.UpstreamCatalogRecord(
        source_id="8", upstream_item_id="55", name="沙丘",
        item_type="Movie", production_year=2021)
    key = agg.aggregate_key_for(record)
    assert als._find_row(db, lib, source8, "55", record, key) is None


class _OnePageHttp:
    """单页 provide 桩（脏名存量行自愈用例专用）。"""

    def __init__(self, page):
        self.page = page
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return 200, json.loads(json.dumps(self.page, ensure_ascii=False))


def test_sync_heals_dirty_glued_year_row(db):
    """存量脏名行（标题黏尾年份、年份空）被干净记录认出并刷新，不建新行。"""
    lib = _seed(db, type_map={"电影": "in"})
    lib.upstream_source_ids = "7"
    dirty = em.MediaItem(
        guid="pa062-dirty-row", library_id=lib.id, item_type="movie",
        name="生化危机：爆发夜2026", production_year=None,
        upstream_source_id=7, probe_status="done")
    db.add(dirty)
    db.commit()
    page = {"code": 1, "page": 1, "pagecount": 1, "limit": 20,
            "total": 1, "class": CLASS_TREE,
            "list": [_entry(900, "生化危机：爆发夜", year="2026")]}
    stats = als.sync_library(db, lib, http_get=_OnePageHttp(page))
    assert stats["added"] == 0
    rows = _movies(db, lib)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == dirty.id
    assert row.name == "生化危机：爆发夜"
    assert row.production_year == 2026
    assert {(m.source_id, m.upstream_item_id)
            for m in _maps_for(db, row.id)} == {(7, "900")}
