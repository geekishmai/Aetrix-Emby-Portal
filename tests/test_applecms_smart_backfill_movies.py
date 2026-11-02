# -*- coding: utf-8 -*-
"""苹果 CMS 智能补源·电影版回归（站主 2026-11-02「电影补源也做」）。

口径（与剧集版同一套打分/否决/质量门/探活，只换电影形态）：
- 行自身映射（集号为空）统计来源；仅剩停用源映射的电影行是目标。
- 候选直链组多单元＝剧集形态，挂到电影行就是错挂，必须拒绝。
- 年份硬否决、质量门、探活失败拒绝与剧集版一致。
- 补写只加一条映射（集号 None），电影行零新增。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_backfill_movies as bfm
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim


def _detail(vod_id, name, episodes, *, year="2019", area="中国大陆",
            type_name="剧情片", actor="演员甲,演员乙", director="导演甲",
            content="这是一个足够长的简介文本，用来做简介相似度打分。" * 2,
            remarks=""):
    play_url = "#".join(
        f"第{i}集$https://cdn.example.com/{vod_id}/e{i}.m3u8"
        for i in range(1, episodes + 1))
    return {"vod_id": vod_id, "vod_name": name, "vod_year": year,
            "vod_area": area, "type_name": type_name,
            "vod_actor": actor, "vod_director": director,
            "vod_content": content, "vod_remarks": remarks,
            "vod_play_from": "直链线路", "vod_play_url": play_url}


S7_SEARCH = {
    "决战之夜": [{"vod_id": 501, "vod_name": "决战之夜",
                  "vod_year": "2019", "type_name": "剧情片"}],
    "陷阱之夜": [{"vod_id": 502, "vod_name": "陷阱之夜",
                  "vod_year": "2024", "type_name": "剧情片"}],
    "长夜漫漫": [{"vod_id": 503, "vod_name": "长夜漫漫",
                  "vod_year": "2019", "type_name": "剧情片"}],
    "孤岛之夜": [{"vod_id": 504, "vod_name": "孤岛之夜",
                  "vod_year": "2019", "type_name": "剧情片"}],
    "雾夜": [{"vod_id": 505, "vod_name": "雾夜", "vod_year": "2019",
              "type_name": "剧情片", "vod_remarks": "标清版"}],
}
S7_DETAILS = {
    "501": _detail(501, "决战之夜", 1),
    "502": _detail(502, "陷阱之夜", 1, year="2024"),
    "503": _detail(503, "长夜漫漫", 24),
    "504": _detail(504, "孤岛之夜", 1),
    "505": _detail(505, "雾夜", 1, remarks="标清版"),
}
S8_DETAILS = {
    "901": _detail(901, "决战之夜", 1),
    "902": _detail(902, "陷阱之夜", 1),
    "903": _detail(903, "长夜漫漫", 1),
    "904": _detail(904, "孤岛之夜", 1),
    "905": _detail(905, "雾夜", 1),
}


class FakeHttpGet:
    """按 host 路由 search/detail 的 provide 桩（同剧集版口径）。"""

    def __call__(self, url, headers=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "search":
            wd = (query.get("wd") or [""])[0]
            entries = S7_SEARCH.get(wd, []) if host == "s7.example" else []
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": json.loads(json.dumps(
                             entries, ensure_ascii=False))}
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            table = S7_DETAILS if host == "s7.example" else S8_DETAILS
            payload = table.get(vod_id)
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
    engine = create_engine("sqlite://")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed_sources_and_lib(db):
    for sid, host, enabled in ((7, "s7.example", True),
                               (8, "s8.example", False)):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=enabled,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-movie", name="在线电影", collection_type="movies",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map="{}")
    db.add(lib)
    db.commit()
    return lib


def _seed_movie(db, lib, guid, name, vod8, *, year=2019):
    row = em.MediaItem(
        guid=guid, library_id=lib.id, item_type="movie", name=name,
        production_year=year)
    db.add(row)
    db.flush()
    uim.record_mapping(
        db, local_item_id=row.id, source_id=8, upstream_item_id=vod8,
        confidence=0.5, evidence="seed")
    db.commit()
    return row


def _probe_ok(url, headers, timeout):
    return (200, None)


def test_movie_mapping_sources_and_find_unplayable(db):
    lib = _seed_sources_and_lib(db)
    row_a = _seed_movie(db, lib, "a" * 32, "决战之夜", "901")
    row_b = _seed_movie(db, lib, "b" * 32, "陷阱之夜", "902")
    # B 另有一条已启用源 7 的映射 → 不是目标行
    uim.record_mapping(
        db, local_item_id=row_b.id, source_id=7, upstream_item_id="502",
        confidence=0.9, evidence="seed")
    # 集号非空的映射不算电影行来源（那是集行口径）
    uim.record_mapping(
        db, local_item_id=row_a.id, source_id=7, upstream_item_id="501",
        confidence=0.9, evidence="seed", episode_index=3)
    db.commit()
    assert bfm.movie_mapping_sources(db, row_a.id) == {8}
    assert bfm.movie_mapping_sources(db, row_b.id) == {7, 8}
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    assert {r.id for r in rows} == {row_a.id}


def test_run_backfill_movies_fills_confident_match(db):
    lib = _seed_sources_and_lib(db)
    row = _seed_movie(db, lib, "a" * 32, "决战之夜", "901")
    assert bfm.movie_mapping_sources(db, row.id) == {8}

    http = FakeHttpGet()
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    assert {r.id for r in rows} == {row.id}

    dry = bfm.run_backfill_movies(db, lib, rows, apply=False,
                                  http_get=http, http_probe=_probe_ok)
    assert dry["stats"]["filled"] == 1
    assert bfm.movie_mapping_sources(db, row.id) == {8}  # 干跑不写

    done = bfm.run_backfill_movies(db, lib, rows, apply=True,
                                   http_get=http, http_probe=_probe_ok)
    stats = done["stats"]
    assert stats["filled"] == 1 and stats["unresolved"] == 0
    assert stats["mappings_added"] == 1
    assert bfm.movie_mapping_sources(db, row.id) == {7, 8}
    # 电影行零新增；新映射集号为空
    assert (db.query(em.MediaItem)
            .filter(em.MediaItem.item_type == "movie").count()) == 1
    mapping = (db.query(em.UpstreamItemMapping)
               .filter(em.UpstreamItemMapping.local_item_id == row.id,
                       em.UpstreamItemMapping.source_id == 7).one())
    assert mapping.upstream_item_id == "501"
    assert mapping.episode_index is None
    # 审计留痕可序列化，且记下命中钥匙与档位
    json.dumps(done["audit"], ensure_ascii=False)
    gained = [a for a in done["audit"] if a["gained"]]
    assert gained[0]["gained"][0]["source_id"] == 7
    assert gained[0]["gained"][0]["tier"] == "high"


def test_run_backfill_movies_rejects_series_shape(db):
    lib = _seed_sources_and_lib(db)
    row = _seed_movie(db, lib, "c" * 32, "长夜漫漫", "903")
    http = FakeHttpGet()
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    done = bfm.run_backfill_movies(db, lib, rows, apply=True,
                                   http_get=http, http_probe=_probe_ok)
    assert done["stats"]["filled"] == 0
    assert done["stats"]["unresolved"] == 1
    assert bfm.movie_mapping_sources(db, row.id) == {8}  # 一条没加
    rejected = [c.get("rejected") for a in done["audit"]
                for attempt in a["attempts"]
                for c in attempt["candidates"]]
    assert "multi_episode_shape" in rejected


def test_run_backfill_movies_year_veto(db):
    lib = _seed_sources_and_lib(db)
    row = _seed_movie(db, lib, "d" * 32, "陷阱之夜", "902")
    http = FakeHttpGet()
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    done = bfm.run_backfill_movies(db, lib, rows, apply=True,
                                   http_get=http, http_probe=_probe_ok)
    assert done["stats"]["filled"] == 0
    assert done["stats"]["unresolved_reasons"] == {"year_mismatch": 1}
    assert bfm.movie_mapping_sources(db, row.id) == {8}


def test_run_backfill_movies_probe_failed(db):
    lib = _seed_sources_and_lib(db)
    row = _seed_movie(db, lib, "e" * 32, "孤岛之夜", "904")
    http = FakeHttpGet()
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    done = bfm.run_backfill_movies(
        db, lib, rows, apply=True, http_get=http,
        http_probe=lambda url, headers, timeout: (404, None))
    assert done["stats"]["filled"] == 0
    assert done["stats"]["unresolved_reasons"] == {"probe_failed": 1}
    assert bfm.movie_mapping_sources(db, row.id) == {8}


def test_run_backfill_movies_quality_gate(db):
    lib = _seed_sources_and_lib(db)
    row = _seed_movie(db, lib, "f" * 32, "雾夜", "905")
    http = FakeHttpGet()
    rows = bfm.find_unplayable_movies(db, [lib.id], [7])
    done = bfm.run_backfill_movies(db, lib, rows, apply=True,
                                   http_get=http, http_probe=_probe_ok)
    assert done["stats"]["filled"] == 0
    assert done["stats"]["quality_skipped"] >= 1
    assert bfm.movie_mapping_sources(db, row.id) == {8}
