# -*- coding: utf-8 -*-
"""苹果 CMS 多源元数据合并填充回归（2026-11-02 站主亲点）。

口径：
- 采集：``cms_entry_to_record`` 把 list 负载现成带的海报/地区/
  分类名/备注/评分/演员/导演/简介透传到记录（不参与配对/聚合键，
  同步不为采集多打 detail 请求）。
- 合行填空：建行落字段；重见（含跨源去重命中同一行）时只填行上
  为空的字段，绝不覆盖已有非空值；填过的行标
  ``metadata_source="applecms"``（暂定填充）。身份字段
  （tmdb_id/imdb_id）仍归 enrich，同步不碰。
- 优先级：TMDB 仍是权威——带 applecms 标记的行在 TMDB
  apply/apply_details 时不受「只填空」保护，海报/简介/评分/类型/
  国家被 TMDB 盖过，标记转正为 "tmdb"；真实来源（tmdb/nfo 等）
  的只填空保护不变。
- 导演不落行：现有人物模型只有演员一种类型，导演只采集进记录。
"""

from __future__ import annotations

import json
import urllib.parse
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em
from backend.emby_server.tmdb import TmdbClient


# ---------------------------------------------------------------------------
# 采集层
# ---------------------------------------------------------------------------

def test_record_collects_source_metadata():
    entry = {
        "vod_id": 42, "vod_name": "沙丘", "vod_year": "2021",
        "type_name": "科幻片",
        "vod_pic": "http://img.example/dune.jpg",
        "vod_area": "美国", "vod_remarks": "正片",
        "vod_score": "8.5",
        "vod_actor": "提莫西·查拉梅,丽贝卡·弗格森",
        "vod_director": "丹尼斯·维伦纽瓦",
        "vod_blurb": "保罗的成长之旅。",
    }
    record = acs.cms_entry_to_record(entry, source_id="7")
    assert record.poster_url == "http://img.example/dune.jpg"
    assert record.area == "美国"
    assert record.category_name == "科幻片"
    assert record.remarks == "正片"
    assert record.score == 8.5
    assert record.actors == ("提莫西·查拉梅", "丽贝卡·弗格森")
    assert record.directors == ("丹尼斯·维伦纽瓦",)
    assert record.blurb == "保罗的成长之旅。"
    # 配对面不变：身份恒空、键仍是标题+年份
    assert record.tmdb_id is None and record.imdb_id is None


def test_record_score_zero_is_missing_and_blurb_falls_back_to_content():
    entry = {
        "vod_id": 1, "vod_name": "无名片", "vod_year": "2020",
        "vod_score": "0.0",
        "vod_content": "<p>一段<b>带标签</b>的简介</p>",
    }
    record = acs.cms_entry_to_record(entry, source_id="7")
    assert record.score is None  # CMS 的 0.0 是「暂无评分」占位
    assert record.blurb == "一段 带标签 的简介"
    assert record.poster_url == "" and record.actors == ()


# ---------------------------------------------------------------------------
# 同步合行填空
# ---------------------------------------------------------------------------

CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
]


def _entry(vod_id, name, **kw):
    import zlib
    entry = {
        "vod_id": vod_id, "vod_name": name, "vod_year": "2021",
        "type_id": 2, "type_name": "动作片", "vod_serial": "0",
        "vod_time": "2024-05-01 10:00:00",
        "vod_douban_id": str(
            100000 + zlib.crc32(f"{name}|2021".encode()) % 900000),
    }
    entry.update(kw)
    return entry


def _page(entries):
    return {"code": 1, "page": 1, "pagecount": 1, "limit": 20,
            "total": len(entries), "class": CLASS_TREE, "list": entries}


class FakeHttpGet:
    """按 host 路由的可变桩：测试中途可换页内容（模拟下一轮同步）。"""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        host = urllib.parse.urlparse(url).netloc
        return 200, json.loads(json.dumps(self.pages[host],
                                          ensure_ascii=False))


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


def _seed(db):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-movies", name="CMS电影",
        collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids="7,8",
        cms_type_map=json.dumps({"电影": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def _row(db, lib, name="沙丘"):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id,
                    em.MediaItem.name == name)
            .one())


def _people(db, item_id):
    return (db.query(em.EmbyPerson)
            .filter(em.EmbyPerson.item_id == item_id)
            .order_by(em.EmbyPerson.sort_order)
            .all())


def test_create_row_lands_source_fields_and_marks_provisional(db):
    lib = _seed(db)
    http = FakeHttpGet({
        "s7.example": _page([_entry(
            101, "沙丘",
            vod_pic="http://img.example/dune.jpg",
            vod_area="美国", vod_remarks="正片", vod_score="8.5",
            vod_actor="甲演员,乙演员", vod_director="某导演",
            vod_blurb="源站简介。")]),
        "s8.example": _page([]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    assert stats["added"] == 1
    row = _row(db, lib)
    assert row.primary_image_url == "http://img.example/dune.jpg"
    assert row.overview == "源站简介。"
    assert row.community_rating == 8.5
    assert row.countries == "美国"
    assert row.genres == "动作片"
    assert row.tagline == "正片"
    assert row.metadata_source == "applecms"  # 暂定填充标记
    # 身份仍归 enrich：同步不碰
    assert row.tmdb_id is None and row.imdb_id is None
    assert row.enrich_status == "pending"
    people = _people(db, row.id)
    assert [p.name for p in people] == ["甲演员", "乙演员"]
    assert all(p.person_tmdb_id == "" for p in people)
    # 同步只拉 list，detail 一次都不许打
    assert http.urls and all("ac=list" in u for u in http.urls)


def test_cross_source_merge_fills_union_without_overwrite(db):
    lib = _seed(db)
    # 源 7 先到：只有海报+评分；源 8 后到同片：简介/地区/演员齐全，
    # 但海报/评分不同——后到值只能填空缺，不许盖先到的非空值。
    http = FakeHttpGet({
        "s7.example": _page([_entry(
            101, "沙丘",
            vod_pic="http://img.example/from7.jpg", vod_score="7.0")]),
        "s8.example": _page([_entry(
            "55", "沙丘",
            vod_pic="http://img.example/from8.jpg", vod_score="9.9",
            vod_area="美国", vod_blurb="源8简介。",
            vod_actor="甲演员")]),
    })
    stats = als.sync_library(db, lib, http_get=http)
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id).all())
    assert len(rows) == 1  # 跨源去重一行
    assert stats["dedupe_hits"] == 1
    row = rows[0]
    assert row.primary_image_url == "http://img.example/from7.jpg"
    assert row.community_rating == 7.0
    assert row.overview == "源8简介。"       # 空缺被源 8 补上
    assert row.countries == "美国"
    assert [p.name for p in _people(db, row.id)] == ["甲演员"]
    assert row.metadata_source == "applecms"


def test_refresh_never_overwrites_tmdb_supplied_fields(db):
    lib = _seed(db)
    http = FakeHttpGet({
        "s7.example": _page([_entry(101, "沙丘", vod_blurb="源站简介。")]),
        "s8.example": _page([]),
    })
    als.sync_library(db, lib, http_get=http)
    row = _row(db, lib)
    # 模拟 enrich 已命中 TMDB 并落权威数据
    row.metadata_source = "tmdb"
    row.tmdb_id = "438631"
    row.overview = "TMDB 简介"
    row.primary_image_url = "http://img.example/tmdb.jpg"
    row.community_rating = 8.4
    db.commit()
    # 下一轮源站带了不同值重见：一个都不许盖
    http.pages["s7.example"] = _page([_entry(
        101, "沙丘", vod_blurb="源站新简介", vod_score="1.0",
        vod_pic="http://img.example/cms-new.jpg")])
    als.sync_library(db, lib, http_get=http)
    db.refresh(row)
    assert row.overview == "TMDB 简介"
    assert row.primary_image_url == "http://img.example/tmdb.jpg"
    assert row.community_rating == 8.4
    assert row.metadata_source == "tmdb"
    assert row.tmdb_id == "438631"


def test_people_not_duplicated_on_rerun(db):
    lib = _seed(db)
    http = FakeHttpGet({
        "s7.example": _page([_entry(101, "沙丘", vod_actor="甲演员,乙演员")]),
        "s8.example": _page([_entry("55", "沙丘", vod_actor="丙演员")]),
    })
    als.sync_library(db, lib, http_get=http)
    als.sync_library(db, lib, http_get=http)
    row = _row(db, lib)
    # 先写者赢（与 enrich 人物落库同口径）：源 8 的演员不合并进来
    assert [p.name for p in _people(db, row.id)] == ["甲演员", "乙演员"]


# ---------------------------------------------------------------------------
# TMDB 优先级：暂定填充可被盖过，真实来源仍受只填空保护
# ---------------------------------------------------------------------------

def _fake_item(**kw):
    base = dict(
        tmdb_id=None, last_scraped_at=None, metadata_source=None,
        overview=None, community_rating=None, poster_path=None,
        primary_image_url=None, backdrop_path=None,
        backdrop_image_url=None, name="沙丘", aliases="", genres="",
        countries="", languages="", production_year=2021, imdb_id=None,
        metadata_runtime_ticks=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_tmdb_apply_overrides_provisional_fill():
    item = _fake_item(
        metadata_source="applecms", overview="源站简介",
        community_rating=5.0, genres="动作片", countries="美国",
        primary_image_url="http://img.example/cms.jpg")
    hit = {
        "id": 438631, "title": "沙丘", "release_date": "2021-09-15",
        "overview": "TMDB 简介", "vote_average": 8.4,
        "genre_ids": [878], "poster_path": "/tmdb-poster.jpg",
    }
    TmdbClient().apply(item, hit, "movie")
    assert item.tmdb_id == "438631"
    assert item.metadata_source == "tmdb"
    assert item.overview == "TMDB 简介"
    assert item.community_rating == 8.4
    assert item.genres == "科幻"
    assert item.primary_image_url.endswith("/tmdb-poster.jpg")


def test_tmdb_apply_details_overrides_provisional_fill():
    item = _fake_item(
        metadata_source="applecms", overview="源站简介",
        community_rating=5.0, genres="动作片", countries="美国")
    data = {
        "overview": "TMDB 详情简介", "vote_average": 8.4,
        "genres": [{"id": 878, "name": "科幻"}],
        "origin_country": ["US"],
        "external_ids": {"imdb_id": "tt1160419"},
        "release_date": "2021-09-15",
    }
    TmdbClient().apply_details(item, data)
    assert item.metadata_source == "tmdb"
    assert item.overview == "TMDB 详情简介"
    assert item.community_rating == 8.4
    assert item.genres == "科幻"
    assert item.countries == "US"
    assert item.imdb_id == "tt1160419"


def test_fill_only_still_protects_real_tmdb_source():
    # 回归：豁免只对 applecms 暂定填充——真实刮削成果仍只填空。
    item = _fake_item(
        metadata_source="tmdb", overview="已有简介",
        community_rating=7.7, genres="剧情", countries="FR")
    data = {
        "overview": "另一份简介", "vote_average": 9.9,
        "genres": [{"id": 878, "name": "科幻"}],
        "origin_country": ["US"], "release_date": "2021-09-15",
    }
    TmdbClient().apply_details(item, data)
    assert item.overview == "已有简介"
    assert item.community_rating == 7.7
    assert item.genres == "剧情"
    assert item.countries == "FR"
