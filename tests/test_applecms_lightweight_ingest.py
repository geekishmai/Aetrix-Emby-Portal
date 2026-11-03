# -*- coding: utf-8 -*-
"""无界源头收紧回归（2026-11-03 轻便优先整改·子单一）。

口径：
- 入库先认亲、再落库：同步拿到源站现成身份信号（``vod_douban_id``，
  解析复用 ``applecms_identity_harvest.parse_douban_id``）才建行，
  行上 ``external_ids`` 带豆瓣号；
- 同片第二家副本（同豆瓣号）只新增/更新 ``upstream_item_mappings``
  汇合到已有行，不建新条目行；
- 无任何可用身份信号 → 不建正式条目行、不挂映射、不留毒丸状态，
  下一轮同步（源站补上信号后）仍可正常重试入库；
- 剧集季/集行跟随所属 series 的认亲结果：series 无信号整树不建，
  有信号时集行随剧行一并落库。
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


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
    {"type_id": 6, "type_pid": 0, "type_name": "电视剧"},
    {"type_id": 7, "type_pid": 6, "type_name": "国产剧"},
]

DOUBAN_DUNE = "1234567"


def _entry(vod_id, name, *, year="2021", type_id=2, douban=None):
    entry = {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": type_id, "type_name": "动作片", "vod_serial": "0",
        "vod_pic": "http://img.example/x.jpg",
    }
    if douban is not None:
        entry["vod_douban_id"] = douban
    return entry


def _page(entries):
    return {"code": 1, "page": 1, "pagecount": 1, "limit": 20,
            "total": len(entries), "class": CLASS_TREE, "list": entries}


class MovieHttp:
    """电影库 provide 桩：按 host 返回可变 list 页（模拟源站补信号）。"""

    def __init__(self, pages):
        self.pages = pages  # {host: page dict}
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        host = urllib.parse.urlparse(url).netloc
        payload = self.pages[host]
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


def _seed_sources(db):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))


def _seed_movie_lib(db, sources="7,8"):
    _seed_sources(db)
    lib = em.Library(
        guid="lib-light-movies", name="无界电影",
        collection_type="movies", is_enabled=True, kind="applecms",
        upstream_source_ids=sources,
        cms_type_map=json.dumps({"电影": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def _rows(db, lib):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .order_by(em.MediaItem.id)
            .all())


def _maps(db):
    return db.query(em.UpstreamItemMapping).all()


def _douban_of(row):
    try:
        ids = json.loads(row.external_ids or "")
    except (TypeError, ValueError):
        return None
    return ids.get("douban") if isinstance(ids, dict) else None


def test_movie_with_douban_id_ingests_with_identity(db):
    """①带豆瓣号新片入库即带身份。"""
    lib = _seed_movie_lib(db, sources="7")
    http = MovieHttp({
        "s7.example": _page([_entry(101, "沙丘", douban=DOUBAN_DUNE)]),
        "s8.example": _page([]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    rows = _rows(db, lib)
    assert len(rows) == 1
    assert _douban_of(rows[0]) == DOUBAN_DUNE
    assert stats["added"] == 1 and stats["mapped"] == 1
    assert stats["skipped_no_identity"] == 0


def test_second_source_copy_merges_mapping_only(db):
    """②同片第二家副本（同豆瓣号、标题写法不同）只加映射不加行。"""
    lib = _seed_movie_lib(db)
    http = MovieHttp({
        "s7.example": _page([_entry(101, "沙丘", douban=DOUBAN_DUNE)]),
        # 第二家标题写法不同：标题+年份键对不上，只能靠豆瓣号认亲汇合。
        "s8.example": _page([_entry("55", "沙丘 Dune", douban=DOUBAN_DUNE)]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    rows = _rows(db, lib)
    assert len(rows) == 1
    assert rows[0].name == "沙丘"  # 汇合不改已有行标题
    assert _douban_of(rows[0]) == DOUBAN_DUNE
    maps = _maps(db)
    assert {(m.source_id, m.upstream_item_id) for m in maps} == {
        (7, "101"), (8, "55")}
    assert all(m.local_item_id == rows[0].id for m in maps)
    assert stats["added"] == 1 and stats["mapped"] == 2


def test_movie_without_identity_signal_skips_and_retries(db):
    """③无信号片不建行；源站补上信号后下一轮仍可重试入库。"""
    lib = _seed_movie_lib(db, sources="7")
    page = _page([_entry(101, "无信号片")])
    http = MovieHttp({"s7.example": page, "s8.example": _page([])})
    stats = als.sync_library(db, lib, http_get=http)
    assert _rows(db, lib) == []
    assert _maps(db) == []
    assert stats["added"] == 0
    assert stats["skipped_no_identity"] == 1
    # 再跑一轮仍无信号：依旧不建行、不产生毒丸（无行无映射无残留）。
    stats2 = als.sync_library(db, lib, http_get=http)
    assert _rows(db, lib) == [] and _maps(db) == []
    assert stats2["skipped_no_identity"] == 1
    # 源站补上豆瓣号后，同一 vod 下一轮正常入库带身份。
    page["list"][0]["vod_douban_id"] = "7654321"
    stats3 = als.sync_library(db, lib, http_get=http)
    rows = _rows(db, lib)
    assert len(rows) == 1 and _douban_of(rows[0]) == "7654321"
    assert stats3["added"] == 1


def test_conflicting_douban_id_does_not_merge_or_create(db):
    """投票纪律：新副本豆瓣号与已有行冲突 → 不汇合、不建新行。"""
    lib = _seed_movie_lib(db)
    http = MovieHttp({
        "s7.example": _page([_entry(101, "沙丘", douban=DOUBAN_DUNE)]),
        "s8.example": _page([_entry("55", "沙丘", douban="9999999")]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    rows = _rows(db, lib)
    assert len(rows) == 1
    assert _douban_of(rows[0]) == DOUBAN_DUNE
    assert {(m.source_id, m.upstream_item_id) for m in _maps(db)} == {
        (7, "101")}
    assert stats["skipped_identity_conflict"] == 1


def test_existing_stock_row_without_identity_still_maps(db):
    """存量行（已在库、无身份）重见时行为不改坏：仍挂映射不重建。"""
    lib = _seed_movie_lib(db, sources="7")
    stock = em.MediaItem(
        guid="stock-row", library_id=lib.id, item_type="movie",
        name="存量片", production_year=2020, upstream_source_id=7,
        probe_status="done")
    db.add(stock)
    db.commit()
    http = MovieHttp({
        "s7.example": _page([_entry(201, "存量片", year="2020")]),
        "s8.example": _page([]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    rows = _rows(db, lib)
    assert len(rows) == 1 and rows[0].id == stock.id
    assert len(_maps(db)) == 1
    assert stats["added"] == 0 and stats["skipped_no_identity"] == 0


# ---------------------------------------------------------------------------
# 剧集：季/集行跟随 series 认亲结果
# ---------------------------------------------------------------------------

def _series_detail(vod_id, name, *, douban=None, episodes=2):
    detail = {
        "vod_id": vod_id, "vod_name": name, "vod_year": "2019",
        "type_id": 7, "type_name": "国产剧",
        "vod_play_from": "直链线路",
        "vod_play_url": "#".join(
            f"第{i}集$https://cdn.example.com/e{i}.m3u8"
            for i in range(1, episodes + 1)),
    }
    if douban is not None:
        detail["vod_douban_id"] = douban
    return detail


class SeriesHttp:
    """剧集库 provide 桩：list 走页、detail 按 vod_id 返回详情 dict。"""

    def __init__(self, list_entries, details):
        self.list_page = _page(list_entries)
        self.details = details  # {vod_id str: detail dict}

    def __call__(self, url, headers=None, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        ac = (query.get("ac") or ["list"])[0]
        if ac == "detail":
            vod_id = (query.get("ids") or query.get("id") or [""])[0]
            detail = self.details.get(str(vod_id))
            payload = {"code": 1, "list": [detail] if detail else []}
            return 200, json.loads(json.dumps(payload, ensure_ascii=False))
        return 200, json.loads(
            json.dumps(self.list_page, ensure_ascii=False))


def _seed_series_lib(db):
    _seed_sources(db)
    lib = em.Library(
        guid="lib-light-series", name="无界国产剧",
        collection_type="tvshows", is_enabled=True, kind="applecms",
        upstream_source_ids="7",
        cms_type_map=json.dumps({"电视剧": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def _series_entry(vod_id, name):
    return {"vod_id": vod_id, "vod_name": name, "vod_year": "2019",
            "type_id": 7, "type_name": "国产剧", "vod_serial": "2"}


def test_series_with_douban_id_builds_tree_with_identity(db):
    lib = _seed_series_lib(db)
    http = SeriesHttp(
        [_series_entry(301, "庆余年")],
        {"301": _series_detail(301, "庆余年", douban="3333333")})
    stats = als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    rows = _rows(db, lib)
    by_type = {}
    for r in rows:
        by_type.setdefault(r.item_type, []).append(r)
    assert len(by_type.get("series", [])) == 1
    assert _douban_of(by_type["series"][0]) == "3333333"
    assert len(by_type.get("season", [])) == 1
    assert len(by_type.get("episode", [])) == 2
    assert stats["episodes_added"] == 2


def test_series_without_identity_signal_builds_nothing(db):
    lib = _seed_series_lib(db)
    http = SeriesHttp(
        [_series_entry(302, "无信号剧")],
        {"302": _series_detail(302, "无信号剧")})
    stats = als.sync_library(db, lib, http_get=http, sleep_fn=lambda s: None)
    assert _rows(db, lib) == []
    assert _maps(db) == []
    assert stats["skipped_no_identity"] == 1
    assert stats["episodes_added"] == 0
