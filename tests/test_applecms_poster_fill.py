# -*- coding: utf-8 -*-
"""苹果 CMS 源站海报（vod_pic）只填空回归（2026-11-02 图片兼容根因二）。

定案：fetcher/sync 全链此前不解析 ``vod_pic``，CMS 托管行海报全靠
TMDB enrich 回填，没对上身份的行永远无图。修法口径：

- ``cms_entry_poster`` 带出 vod_pic（list 与 detail 载荷都认），
  只认 http(s) 绝对地址，相对/协议相对/垃圾值一律空串不猜；
- 同步落行只填空：行无 poster_path 且无 primary_image_url 才写
  源站图；已有 TMDB/本地图的行绝不覆盖，重跑不换图；
- 剧行走 detail 的 vod_pic（非凡等源 list 不带图），集行不单独填。
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

PIC_A = "https://img.example/poster-a.jpg"
PIC_B = "https://img.example/poster-b.jpg"
TMDB_PIC = "https://image.tmdb.org/t/p/original/tmdb.jpg"


# ---------------------------------------------------------------------------
# 纯函数：cms_entry_poster
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry,expected", [
    ({"vod_pic": PIC_A}, PIC_A),
    ({"vod_pic": "  " + PIC_A + "  "}, PIC_A),
    ({"vod_pic": "http://img.example/x.jpg"}, "http://img.example/x.jpg"),
    ({"vod_pic": ""}, ""),
    ({"vod_pic": None}, ""),
    ({}, ""),
    ({"vod_pic": "/upload/vod/x.jpg"}, ""),          # 相对路径不猜
    ({"vod_pic": "//img.example/x.jpg"}, ""),        # 协议相对不猜
    ({"vod_pic": "ftp://img.example/x.jpg"}, ""),
    (None, ""),
    ("not-a-dict", ""),
])
def test_cms_entry_poster_shapes(entry, expected):
    assert acs.cms_entry_poster(entry) == expected


# ---------------------------------------------------------------------------
# 同步落行（电影 + 剧集共用夹具）
# ---------------------------------------------------------------------------

CLASS_TREE_MOVIES = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
]
CLASS_TREE_TV = [
    {"type_id": 1, "type_pid": 0, "type_name": "电视剧"},
    {"type_id": 2, "type_pid": 1, "type_name": "国产剧"},
]


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


def _seed_source(db, sid=7, host="s7.example"):
    db.add(em.UpstreamSource(
        id=sid, name=f"源{sid}", source_type="applecms",
        base_url=f"http://{host}", enabled=True,
        user_agent="Mozilla/5.0 (test)"))
    db.commit()


class _FakeHttp:
    """按 ac 路由的 provide 桩：pages 可替换以便重跑换图。"""

    def __init__(self, list_payload, details=None):
        self.list_payload = list_payload
        self.details = details or {}

    def __call__(self, url, headers=None, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        ac = (query.get("ac") or [""])[0]
        if ac == "list":
            return 200, json.loads(json.dumps(
                self.list_payload, ensure_ascii=False))
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            payload = self.details.get(vod_id)
            if payload is None:
                return 200, {"code": 1, "page": 1, "pagecount": 0, "list": []}
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


def _movie_entry(vod_id, name, pic=PIC_A, **kw):
    import zlib
    entry = {"vod_id": vod_id, "vod_name": name, "vod_year": "2021",
             "type_id": 2, "type_name": "动作片", "vod_serial": "0",
             "vod_time": "2024-05-01 10:00:00",
             "vod_douban_id": str(
                 100000 + zlib.crc32(f"{name}|2021".encode()) % 900000)}
    if pic is not None:
        entry["vod_pic"] = pic
    entry.update(kw)
    return entry


def _movie_lib(db):
    lib = em.Library(
        guid="lib-poster-movies", name="CMS电影", collection_type="movies",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map=json.dumps({"电影": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def _movie_page(entries):
    return {"code": 1, "page": 1, "pagecount": 1, "limit": 20,
            "total": len(entries), "class": CLASS_TREE_MOVIES,
            "list": entries}


def _row(db, lib, name):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id,
                    em.MediaItem.name == name).first())


def test_movie_sync_fills_poster_from_vod_pic(db):
    _seed_source(db)
    lib = _movie_lib(db)
    page = _movie_page([_movie_entry(101, "有图电影"),
                        _movie_entry(102, "无图电影", pic=None)])
    stats = als.sync_library(db, lib, http_get=_FakeHttp(page))
    row = _row(db, lib, "有图电影")
    assert row is not None and row.primary_image_url == PIC_A
    bare = _row(db, lib, "无图电影")
    assert bare is not None and not bare.primary_image_url \
        and not bare.poster_path
    assert stats["posters_filled"] == 1


def test_movie_sync_never_overwrites_existing_image(db):
    _seed_source(db)
    lib = _movie_lib(db)
    page = _movie_page([_movie_entry(101, "沙丘")])
    als.sync_library(db, lib, http_get=_FakeHttp(page))
    row = _row(db, lib, "沙丘")
    assert row.primary_image_url == PIC_A

    # 模拟 enrich 已把图换成 TMDB 图：源站换图重跑也不许覆盖
    row.primary_image_url = TMDB_PIC
    db.commit()
    page2 = _movie_page([_movie_entry(101, "沙丘", pic=PIC_B)])
    stats2 = als.sync_library(db, lib, http_get=_FakeHttp(page2))
    db.expire_all()
    row2 = _row(db, lib, "沙丘")
    assert row2.primary_image_url == TMDB_PIC
    assert stats2["posters_filled"] == 0


def test_series_sync_fills_series_poster_from_detail(db):
    _seed_source(db)
    lib = em.Library(
        guid="lib-poster-tv", name="国产剧", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map=json.dumps({"电视剧": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    entry = {"vod_id": 202, "vod_name": "庆余年", "vod_year": "2019",
             "type_id": 2, "type_name": "国产剧", "vod_serial": "",
             "vod_time": "2024-05-01 10:00:00"}  # list 条目无图（非凡形态）
    page = {"code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 1,
            "class": CLASS_TREE_TV, "list": [entry]}
    import zlib
    detail = {"vod_id": 202, "vod_name": "庆余年", "vod_year": "2019",
              "vod_play_from": "直链线路",
              "vod_play_url": "第1集$https://cdn.example.com/e1.m3u8",
              "vod_pic": PIC_A,
              "vod_douban_id": str(
                  100000 + zlib.crc32("庆余年|2019".encode()) % 900000)}
    stats = als.sync_library(db, lib, http_get=_FakeHttp(
        page, details={"202": detail}), sleep_fn=lambda s: None)
    series = (db.query(em.MediaItem)
              .filter(em.MediaItem.library_id == lib.id,
                      em.MediaItem.item_type == "series").first())
    assert series is not None and series.primary_image_url == PIC_A
    assert stats["posters_filled"] == 1
    episodes = (db.query(em.MediaItem)
                .filter(em.MediaItem.library_id == lib.id,
                        em.MediaItem.item_type == "episode").all())
    assert episodes and all(not ep.primary_image_url for ep in episodes)
