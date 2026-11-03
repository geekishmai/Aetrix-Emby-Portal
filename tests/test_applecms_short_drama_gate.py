# -*- coding: utf-8 -*-
"""苹果 CMS 同步短剧质量门回归（2026-11-02 站主令：竖版短剧过滤）。

口径：
- 只认源站分类证据：分类名自身或祖先命中短剧标记（子类沿
  type_pid 回溯）、条目 type_name、分类串 vod_class、备注
  vod_remarks；片名不作证据（综艺拿「短剧」当节目名不许误杀），
  分辨率不作证据（CMS 条目恒 0）。
- 判据与补源侧（#1775）同一套：``applecms_backfill`` 的
  ``SHORT_DRAMA_TOKENS`` / ``class_short_drama_ids`` /
  ``_is_short_drama_entry`` 是仓库内唯一落点（含「竖版」一词），
  同步侧只复用、不另造第二套。
- 命中即跳过并计数（stats["skipped_short_drama"]），不建行、
  不挂映射；电影/剧集两条路径同一道门，剧集路径在拉 detail
  前先判、detail 证据明确的再复检一道。
- 正常电影/剧集不受影响（计数为 0、照常建行）。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_backfill as abf
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------

CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
    {"type_id": 30, "type_pid": 0, "type_name": "短剧大全"},
    {"type_id": 31, "type_pid": 30, "type_name": "现代言情"},
    {"type_id": 32, "type_pid": 30, "type_name": "AI漫剧"},
    {"type_id": 40, "type_pid": 0, "type_name": "国产剧"},
    {"type_id": 50, "type_pid": 0, "type_name": "竖屏剧场"},
]


def test_class_short_drama_ids_follows_pid_chain():
    ids = abf.class_short_drama_ids({"class": CLASS_TREE})
    # 短剧大全自身 + 两个子类（祖先命中）+ 竖屏剧场；正常类不在内
    assert ids == {30, 31, 32, 50}


def test_class_short_drama_ids_without_pid_falls_back_to_own_name():
    ids = abf.class_short_drama_ids({"class": [
        {"type_id": 54, "type_name": "爽文短剧"},
        {"type_id": 65, "type_name": "反转爽剧"},
        {"type_id": 1, "type_name": "电影"},
    ]})
    assert ids == {54}


def test_is_short_drama_evidence_fields():
    short_ids = {30, 31}
    assert abf._is_short_drama_entry(
        {"type_id": 31, "type_name": "现代言情"}, short_ids)
    assert abf._is_short_drama_entry(
        {"type_id": 2, "type_name": "动作片", "vod_class": "竖屏短剧"},
        short_ids)
    assert abf._is_short_drama_entry(
        {"type_id": 2, "type_name": "动作片", "vod_remarks": "竖屏"},
        short_ids)
    # 「竖版」同在共享词表内（与补源侧 #1775 同口径，防两套漂移）
    assert abf._is_short_drama_entry(
        {"type_id": 2, "type_name": "动作片", "vod_class": "竖版"},
        short_ids)
    # 片名含短剧不算证据（综艺节目名），分类全正常 → 不判
    assert not abf._is_short_drama_entry(
        {"type_id": 5, "type_name": "大陆综艺", "vod_name": "短剧X家族",
         "vod_class": "真人秀", "vod_remarks": "更新至第8期"}, short_ids)
    assert not abf._is_short_drama_entry(
        {"type_id": 2, "type_name": "动作片", "vod_class": "动作"},
        short_ids)


# ---------------------------------------------------------------------------
# 同步集成（电影路径）
# ---------------------------------------------------------------------------

def _entry(vod_id, name, *, year="2021", type_id=2, type_name="动作片",
           serial="0", vod_class="动作", vod_remarks=""):
    return {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": type_id, "type_name": type_name, "vod_serial": serial,
        "vod_time": "2024-05-01 10:00:00",
        "vod_pic": "http://img.example/x.jpg",
        "vod_content": "简介文本", "vod_class": vod_class,
        "vod_remarks": vod_remarks,
    }


MOVIE_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 5,
    "class": CLASS_TREE,
    "list": [
        _entry(101, "正常电影"),
        # 短剧子类（祖先=短剧大全）：顶级名回溯后不在电影白名单，
        # 但本例白名单为空（全收），靠短剧门拦下并单独计数
        _entry(102, "偏爱短剧", type_id=31, type_name="现代言情"),
        # 分类串证据：类型是动作片，vod_class 明写竖屏短剧
        _entry(103, "伪装短剧", vod_class="竖屏短剧"),
        # 片名含短剧、分类证据全无 → 不许误杀
        _entry(104, "短剧X家族", type_id=2, type_name="动作片"),
        _entry(105, "微短剧专场", type_id=50, type_name="竖屏剧场"),
    ],
}


class _MovieHttp:
    def __call__(self, url, headers=None, timeout=None):
        return 200, json.loads(json.dumps(MOVIE_PAGE, ensure_ascii=False))


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


def _seed(db, *, collection="movies", type_map=None):
    db.add(em.UpstreamSource(
        id=7, name="源7", source_type="applecms",
        base_url="http://s7.example", enabled=True,
        user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid=f"lib-cms-{collection}", name="CMS库",
        collection_type=collection, is_enabled=True,
        kind="applecms", upstream_source_ids="7",
        cms_type_map=json.dumps(type_map, ensure_ascii=False)
        if type_map is not None else None)
    db.add(lib)
    db.commit()
    return lib


def test_movie_sync_skips_short_drama_and_counts(db):
    lib = _seed(db)  # 无白名单（全收）：短剧门是唯一拦截
    stats = als.sync_library(db, lib, http_get=_MovieHttp())
    assert stats["blocked"] is None
    assert stats["skipped_short_drama"] == 3
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .order_by(em.MediaItem.name).all())
    assert [r.name for r in rows] == ["正常电影", "短剧X家族"]
    assert stats["added"] == 2
    # 进度写回明细带短剧计数
    detail = json.loads(lib.scan_stats)["cms_sync"]
    assert detail["skipped_short_drama"] == 3


# ---------------------------------------------------------------------------
# 同步集成（剧集路径：list 门 + detail 复检）
# ---------------------------------------------------------------------------

def _detail(vod_id, name, episodes, **extra):
    play_url = "#".join(f"{t}${a}" for t, a in episodes)
    d = {"vod_id": vod_id, "vod_name": name, "vod_year": "2019",
         "vod_play_from": "直链线路", "vod_play_url": play_url}
    d.update(extra)
    return d


def _eps(prefix, count):
    return [(f"第{i}集", f"https://cdn.example.com/{prefix}/e{i}.m3u8")
            for i in range(1, count + 1)]


SERIES_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 3,
    "class": CLASS_TREE,
    "list": [
        _entry(201, "正常剧", type_id=40, type_name="国产剧",
               vod_class="古装"),
        _entry(202, "短剧本剧", type_id=31, type_name="现代言情",
               vod_class="言情"),
        # list 看不出（国产剧），detail 分类串明写竖屏短剧 → 复检拦
        _entry(203, "藏得很深", type_id=40, type_name="国产剧",
               vod_class="古装"),
    ],
}

SERIES_DETAILS = {
    "201": _detail(201, "正常剧", _eps("ok", 2)),
    "202": _detail(202, "短剧本剧", _eps("sd", 60)),
    "203": _detail(203, "藏得很深", _eps("deep", 80),
                   type_id=40, type_name="国产剧",
                   vod_class="竖屏短剧"),
}


class _SeriesHttp:
    def __call__(self, url, headers=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "list":
            return 200, json.loads(json.dumps(SERIES_PAGE,
                                              ensure_ascii=False))
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            payload = SERIES_DETAILS[vod_id]
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


def test_series_sync_skips_short_drama_before_and_after_detail(db):
    lib = _seed(db, collection="tvshows")
    stats = als.sync_library(db, lib, http_get=_SeriesHttp())
    assert stats["blocked"] is None
    # 202 在 list 阶段拦（不拉 detail），203 在 detail 复检拦
    assert stats["skipped_short_drama"] == 2
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id,
                    em.MediaItem.item_type == "series").all())
    assert [r.name for r in rows] == ["正常剧"]
    assert stats["added"] == 1
