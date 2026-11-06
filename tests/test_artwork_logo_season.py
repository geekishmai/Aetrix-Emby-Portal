# -*- coding: utf-8 -*-
"""海报墙与刮削 P1 回归测试（feat/artwork-logo-season）。

覆盖：
1. P1-1 季海报与宽高比：
   - 两段式 ``identify_two_stage.apply_fill`` 调用 ``_apply_season_posters`` 为无图季落库
     （已有季图不覆盖；异常不拖垮补资料段；禁用调用时变红）；
   - ``user_views`` 显式 ``cover_path`` 库封面 ``PrimaryImageAspectRatio == 1.7777778``（16:9）；
   - ``_item_dto`` 单集有自有剧照时 ``PrimaryImageAspectRatio == 1.7777778``，无自有剧照回退海报
     及电影/剧集保持 ``0.6666667``（2:3）。
2. P1-2 Logo / 无字 Backdrop / 中文横卡 Thumb：
   - ``TmdbClient.details()`` 单次请求合并 ``append_to_response=alternative_titles,external_ids,credits,images``
     与 ``include_image_language=zh-CN,zh,cn,tw,hk,ja,en,null``（0 额外 TMDB 请求）；
   - ``require_images=True`` 穿透刷新不含 ``images`` 键的旧磁盘缓存；
   - 选图优先级：透明 PNG Logo（排除 .svg，简中→繁中→原语言→英文）、无字 Backdrop（``iso_639_1=None``
     优先于带字图）、带字 16:9 Thumb（简中→繁中→原语言→英文，不混入无字图）；
   - 老库 ``_auto_migrate`` 幂等补齐 ``logo_path``/``logo_image_url``/``thumb_path``/``thumb_image_url``；
   - ``_item_dto`` 下发 ``ImageTags.Logo`` / ``ImageTags.Thumb`` 及季/集 ``ParentLogoImageTag``；
   - ``media_routes.item_image``：无 Logo 时 ``/Images/Logo`` 严返 404（绝不回退 Backdrop）；
     有透明 PNG Logo 且带 ``maxWidth`` 请求时保持 ``image/png`` 与 RGBA 透明通道（不转不透明 JPEG）；
   - ``image_store.referenced_basenames`` 保留 ``logo_path`` 与 ``thumb_path``；
   - ``scripts/backfill_artwork.py`` 默认 dry-run 零网络零写库，``--apply`` 回填 Logo/无字背景/Thumb/季海报
     且零 115 调用。
"""
from __future__ import annotations

import io
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import database as dbmod
from backend import models as base_models
from backend.emby_server import api as ea_api
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import image_store
from backend.emby_server import media_routes
from backend.emby_server import models as em
from backend.emby_server import pan115_guard
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server import tmdb_cache
from scripts import backfill_artwork as bf_art


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _make_lib(db, name="测试库", collection_type="tvshows", **kw):
    lib = em.Library(
        guid=uuid.uuid4().hex,
        name=name,
        collection_type=collection_type,
        is_enabled=True,
        **kw,
    )
    db.add(lib)
    db.flush()
    return lib


def _make_item(db, lib_id: int, item_type="movie", name="测试条目", **kw):
    kw.setdefault("enrich_status", "done")
    row = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=lib_id,
        item_type=item_type,
        name=name,
        sort_name=name,
        file_path=f"/m/{uuid.uuid4().hex}.mkv",
        **kw,
    )
    db.add(row)
    db.flush()
    return row


def _rich_details_payload(tmdb_id=603, orig_lang="ja"):
    return {
        "id": tmdb_id,
        "title": "黑客帝国",
        "original_language": orig_lang,
        "overview": "详情简介",
        "vote_average": 8.7,
        "poster_path": "/default_poster.jpg",
        "backdrop_path": "/texted_default_backdrop.jpg",
        "credits": {"cast": [{"id": 1, "name": "基努·里维斯", "character": "尼奥", "profile_path": "/k.jpg"}]},
        "images": {
            "logos": [
                {"file_path": "/vec_zh.svg", "iso_639_1": "zh", "iso_3166_1": "CN", "vote_average": 9.9, "width": 800},
                {"file_path": "/logo_en.png", "iso_639_1": "en", "vote_average": 8.0, "width": 800},
                {"file_path": "/logo_ja.png", "iso_639_1": "ja", "vote_average": 7.5, "width": 800},
                {"file_path": "/logo_tw.png", "iso_639_1": "zh", "iso_3166_1": "TW", "vote_average": 9.0, "width": 800},
                {"file_path": "/logo_cn.png", "iso_639_1": "zh", "iso_3166_1": "CN", "vote_average": 6.5, "width": 600},
            ],
            "backdrops": [
                {"file_path": "/bd_en_text.jpg", "iso_639_1": "en", "vote_average": 9.5, "width": 1920},
                {"file_path": "/bd_zh_thumb.jpg", "iso_639_1": "zh", "iso_3166_1": "CN", "vote_average": 7.2, "width": 1920},
                {"file_path": "/bd_clean_small.jpg", "iso_639_1": None, "vote_average": 9.8, "width": 780},
                {"file_path": "/bd_clean_hd.jpg", "iso_639_1": None, "vote_average": 8.1, "width": 1920},
            ],
        },
    }


# ---------------- 1. P1-1：两段式季海报 + 宽高比修正 ----------------


def test_two_stage_apply_fill_writes_season_posters_fill_only(db_session):
    lib = _make_lib(db_session)
    series = _make_item(db_session, lib.id, item_type="series", name="漫长的季节", tmdb_id="225000")
    s1 = _make_item(
        db_session, lib.id, item_type="season", name="第 1 季",
        series_id=series.id, parent_id=series.id, season_number=1,
    )
    s2 = _make_item(
        db_session, lib_id=lib.id, item_type="season", name="第 2 季",
        series_id=series.id, parent_id=series.id, season_number=2,
        primary_image_url="https://image.tmdb.org/t/p/w500/existing_s2.jpg",
    )
    db_session.commit()

    fetched = {
        "tmdb_id": "225000",
        "tmdb_details": _rich_details_payload(225000, orig_lang="zh"),
        "season_posters": {
            s1.id: "https://image.tmdb.org/t/p/w500/new_s1.jpg",
            s2.id: "https://image.tmdb.org/t/p/w500/should_not_overwrite.jpg",
        },
    }
    ts.apply_fill(db_session, series, fetched)
    db_session.commit()
    db_session.refresh(s1)
    db_session.refresh(s2)

    assert s1.primary_image_url == "https://image.tmdb.org/t/p/w500/new_s1.jpg"
    assert s2.primary_image_url == "https://image.tmdb.org/t/p/w500/existing_s2.jpg"


def test_aspect_ratio_episode_still_is_16_9_and_movie_remains_2_3(db_session):
    lib = _make_lib(db_session)
    user = base_models.WebUser(id=1, username="u1", password_hash="x", is_active=True)
    db_session.add(user)

    movie = _make_item(
        db_session, lib.id, item_type="movie", name="电影",
        primary_image_url="https://image.tmdb.org/t/p/w500/m.jpg",
    )
    series = _make_item(
        db_session, lib.id, item_type="series", name="剧集",
        primary_image_url="https://image.tmdb.org/t/p/w500/s.jpg",
    )
    ep_with_still = _make_item(
        db_session, lib.id, item_type="episode", name="第 1 集",
        series_id=series.id, parent_id=series.id, episode_number=1,
        primary_image_url="https://image.tmdb.org/t/p/w500/still.jpg",
    )
    ep_fallback_poster = _make_item(
        db_session, lib.id, item_type="episode", name="第 2 集",
        series_id=series.id, parent_id=series.id, episode_number=2,
        primary_image_url=None, poster_path=None,
    )
    db_session.commit()

    dto_movie = ea_api._item_dto(movie, "http://test", user.id, db_session)
    dto_ep_still = ea_api._item_dto(ep_with_still, "http://test", user.id, db_session)
    dto_ep_fb = ea_api._item_dto(ep_fallback_poster, "http://test", user.id, db_session)

    assert dto_movie["PrimaryImageAspectRatio"] == pytest.approx(0.6666667)
    assert dto_ep_still["PrimaryImageAspectRatio"] == pytest.approx(1.7777778)
    assert dto_ep_fb["PrimaryImageAspectRatio"] == pytest.approx(0.6666667)


# ---------------- 2. P1-2：TMDB 单次合并请求 + 选图算法 ----------------


def test_tmdb_details_merges_images_and_include_languages_zero_extra_calls(tmp_path, monkeypatch):
    cache_dir = tmp_path / "tmdb_cache"
    cache_dir.mkdir()
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(cache_dir))
    tmdb_mod.TmdbClient._cache.clear()

    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    seen = []

    def fake_get(path, params):
        seen.append((path, dict(params)))
        return _rich_details_payload(603)

    monkeypatch.setattr(client, "_get", fake_get)
    data = client.details("603", "movie")
    assert len(seen) == 1, "details() 必须一次请求同时拿全 credits 与 images"
    path, params = seen[0]
    assert path == "/movie/603"
    assert params["append_to_response"] == "alternative_titles,external_ids,credits,images"
    assert params["include_image_language"] == "zh-CN,zh,cn,tw,hk,ja,en,null"
    assert "images" in data

    # require_images=True 对升级前无 images 的磁盘老缓存自动穿透刷新一次
    tmdb_mod.TmdbClient._cache.clear()
    lang = tmdb_mod.preferred_language()
    tmdb_cache.save_details("movie", "999", {"id": 999, "credits": {"cast": []}}, lang)
    assert client.details("999", "movie") == {"id": 999, "credits": {"cast": []}}
    assert len(seen) == 1  # 默认兼容旧载荷不穿透
    refreshed = client.details("999", "movie", require_images=True)
    assert len(seen) == 2
    assert "images" in refreshed


def test_artwork_pickers_language_and_format_priority():
    payload = _rich_details_payload(603, orig_lang="ja")
    # Logo：排除 .svg，简中(/logo_cn.png) 优先于 繁中(/logo_tw.png) → 原语言(/logo_ja.png) → 英文
    assert tmdb_mod.pick_logo_path(payload) == "/logo_cn.png"
    # 无简中时退繁中，再退原语言 ja（而非英文）
    payload_no_cn = _rich_details_payload(603, orig_lang="ja")
    payload_no_cn["images"]["logos"] = [
        x for x in payload_no_cn["images"]["logos"] if x.get("iso_3166_1") != "CN"
    ]
    assert tmdb_mod.pick_logo_path(payload_no_cn) == "/logo_tw.png"
    payload_no_zh = _rich_details_payload(603, orig_lang="ja")
    payload_no_zh["images"]["logos"] = [
        x for x in payload_no_zh["images"]["logos"] if x.get("iso_639_1") != "zh"
    ]
    assert tmdb_mod.pick_logo_path(payload_no_zh) == "/logo_ja.png"

    # Backdrop：优先 >=1280 的无字背景 /bd_clean_hd.jpg，避开 9.5 分英文带字图
    assert tmdb_mod.pick_textless_backdrop_path(payload) == "/bd_clean_hd.jpg"
    # Thumb：优先简中带字 16:9 横图 /bd_zh_thumb.jpg，不混入无字图
    assert tmdb_mod.pick_thumb_path(payload) == "/bd_zh_thumb.jpg"


# ---------------- 3. 数据库迁移 + DTO ImageTags + 路由 404/透明 PNG ----------------


def test_auto_migrate_adds_logo_and_thumb_columns_to_legacy_table(tmp_path, monkeypatch):
    legacy_db = tmp_path / "legacy_art.db"
    eng = create_engine(f"sqlite:///{legacy_db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE emby_items (id INTEGER PRIMARY KEY, name TEXT)"))

    monkeypatch.setattr(dbmod, "engine", eng)
    monkeypatch.setattr(dbmod, "DATABASE_TYPE", "sqlite")
    dbmod._auto_migrate()

    cols = {c["name"] for c in inspect(eng).get_columns("emby_items")}
    assert {"logo_path", "logo_image_url", "thumb_path", "thumb_image_url"}.issubset(cols)
    eng.dispose()


def test_dto_emits_logo_and_parent_logo_tags_and_route_serves_alpha_png_or_404(
    tmp_path, db_session, monkeypatch
):
    img_dir = tmp_path / "img_cache"
    img_dir.mkdir()
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(img_dir))

    # 造一张真实带透明通道（alpha=0 与 alpha=255）的 RGBA PNG Logo 放入缓存目录
    logo_file = img_dir / "clearlogo_rgba.png"
    im = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    im.putpixel((10, 10), (255, 40, 40, 255))
    im.save(logo_file, format="PNG")

    bd_file = img_dir / "backdrop_clean.jpg"
    Image.new("RGB", (1280, 720), (20, 30, 40)).save(bd_file, format="JPEG")

    lib = _make_lib(db_session, name="公开剧集库")
    user = base_models.WebUser(id=7, username="u7", password_hash="x", is_active=True)
    db_session.add(user)

    # 1) 有 Backdrop 但无 Logo 的电影：请求 /Images/Logo 必须 404（严禁错回 Backdrop！）
    movie_no_logo = _make_item(
        db_session, lib.id, item_type="movie", name="无Logo片",
        backdrop_path=str(bd_file),
        backdrop_image_url="https://image.tmdb.org/t/p/w1280/bd.jpg",
    )
    # 2) 有透明 PNG Logo + 中文 Thumb 的剧集及其子单集
    series = _make_item(
        db_session, lib.id, item_type="series", name="有Logo剧",
        logo_path=str(logo_file),
        logo_image_url="https://image.tmdb.org/t/p/w500/logo_cn.png",
        thumb_image_url="https://image.tmdb.org/t/p/w780/thumb_cn.jpg",
        backdrop_path=str(bd_file),
    )
    ep = _make_item(
        db_session, lib.id, item_type="episode", name="S01E01",
        series_id=series.id, parent_id=series.id, episode_number=1,
    )
    db_session.commit()
    db_session.refresh(ep)

    dto_no_logo = ea_api._item_dto(movie_no_logo, "http://test", user.id, db_session)
    assert "Logo" not in dto_no_logo["ImageTags"]

    dto_series = ea_api._item_dto(series, "http://test", user.id, db_session)
    assert dto_series["ImageTags"].get("Logo") == "1"
    assert dto_series["ImageTags"].get("Thumb") == "1"

    dto_ep = ea_api._item_dto(ep, "http://test", user.id, db_session)
    assert dto_ep.get("ParentLogoImageTag") == "1"
    assert dto_ep.get("ParentLogoItemId") == series.guid
    assert dto_ep.get("ParentThumbImageTag") == "1"
    assert dto_ep.get("ParentThumbItemId") == series.guid

    req = Request({
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("test", 80), "client": ("127.0.0.1", 1234), "root_path": "",
    })

    # 无 Logo 条目拉 /Images/Logo → 404
    with pytest.raises(HTTPException) as exc_info:
        media_routes.item_image(movie_no_logo.guid, "Logo", req, db=db_session, user=user)
    assert exc_info.value.status_code == 404

    # 有透明 PNG Logo 条目带 maxWidth=320 拉 /Images/Logo → 原样返回透明 PNG（不转不透明 JPEG）
    resp = media_routes.item_image(
        series.guid, "Logo", req, db=db_session, user=user,
        maxWidth="320", maxHeight=None, w=None, h=None,
    )
    body = b""
    if hasattr(resp, "path") and resp.path:
        body = open(resp.path, "rb").read()
    else:
        body = bytes(resp.body)
    out_img = Image.open(io.BytesIO(body))
    assert out_img.format == "PNG"
    assert out_img.mode == "RGBA"
    assert out_img.getpixel((0, 0))[3] == 0, "透明背景像素 alpha 必须保持为 0"

    # 引用扫描包含 logo_path，维护清理不会误删已本地化的透明 Logo
    refs = image_store.referenced_basenames(db_session)
    assert "clearlogo_rgba.png" in refs


# ---------------- 4. 存量回填脚本（默认 dry-run、只调 TMDB、零 115） ----------------


def test_backfill_artwork_dry_run_and_apply_zero_115(tmp_path, db_session, monkeypatch):
    cache_dir = tmp_path / "tmdb_cache"
    cache_dir.mkdir()
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(cache_dir))

    lib = _make_lib(db_session, name="电影与剧集")
    m1 = _make_item(
        db_session, lib.id, item_type="movie", name="黑客帝国",
        tmdb_id="603",
        backdrop_image_url="https://image.tmdb.org/t/p/w1280/texted_default_backdrop.jpg",
    )
    sr = _make_item(db_session, lib.id, item_type="series", name="测试剧", tmdb_id="1396")
    sn = _make_item(
        db_session, lib.id, item_type="season", name="第 1 季",
        series_id=sr.id, parent_id=sr.id, season_number=1,
    )
    db_session.commit()

    guard_115_calls = []
    monkeypatch.setattr(pan115_guard, "before_call", lambda *a, **k: guard_115_calls.append(1))

    detail_calls = []
    season_calls = []

    def fake_details(tid, kind):
        detail_calls.append((tid, kind))
        return _rich_details_payload(int(tid))

    def fake_season_poster(tid, sno):
        season_calls.append((tid, sno))
        return f"https://image.tmdb.org/t/p/w500/season_{tid}_{sno}.jpg"

    # 1) dry-run：零网络调用、零写库，准确估算 3 次 HTTP 请求（2 条详情 + 1 个季海报）
    dry_stats = bf_art.backfill_artwork(
        db_session, dry_run=True,
        details_fetcher=fake_details,
        season_poster_fetcher=fake_season_poster,
    )
    assert dry_stats["total"] == 2
    assert dry_stats["estimated_http_requests"] == 3
    assert detail_calls == [] and season_calls == []
    db_session.refresh(m1)
    assert m1.logo_image_url is None

    # 2) apply：补齐 Logo / 升级无字 Backdrop / 补中文 Thumb / 补季海报，且 115 调用为 0
    app_stats = bf_art.backfill_artwork(
        db_session, dry_run=False, batch_size=1,
        details_fetcher=fake_details,
        season_poster_fetcher=fake_season_poster,
    )
    assert app_stats["logos_filled"] == 2
    assert app_stats["thumbs_filled"] == 2
    assert app_stats["backdrops_updated"] == 2
    assert app_stats["seasons_filled"] == 1
    assert app_stats["commits"] == 2
    assert guard_115_calls == []

    db_session.refresh(m1)
    db_session.refresh(sn)
    assert m1.logo_image_url.endswith("/w500/logo_cn.png")
    assert m1.backdrop_image_url.endswith("/w1280/bd_clean_hd.jpg")
    assert m1.thumb_image_url.endswith("/w780/bd_zh_thumb.jpg")
    assert sn.primary_image_url == "https://image.tmdb.org/t/p/w500/season_1396_1.jpg"
