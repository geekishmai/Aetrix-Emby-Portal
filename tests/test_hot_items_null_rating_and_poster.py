# -*- coding: utf-8 -*-
"""回归测试：列表接口 SortBy=CommunityRating DESC 将 NULL 排末尾，且支持 HasPrimaryImage 过滤。

生产根因（2026-11-07 线上复现）：
首页「各库热门」请求 `/emby/Users/{uid}/Items?SortBy=CommunityRating,DateCreated&SortOrder=Descending`。
PostgreSQL 下 `DESC` 默认 `NULLS FIRST`，导致刚入库尚未刮削（`community_rating=NULL`、
`poster_path=NULL`、`ImageTags={}`）的《Mr Nice Guy（一个好人）》《Badges of Fury（不二神探）》
《Flirting Scholar（唐伯虎点秋香）》排在 `community_rating=8.9` 的有海报条目之前，占满前 16 条，
前端显示整排「暂无海报」。
"""
from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from starlette.requests import Request

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import api as eapi, models as em
from backend.security import hash_password

init_db()


def test_community_rating_desc_puts_null_last_and_has_primary_image_filters_posterless():
    db = SessionLocal()
    lib_guid = uuid.uuid4().hex
    created_item_ids: list[int] = []
    user_id = None
    lib_id = None
    try:
        uname = f"hot_u_{uuid.uuid4().hex[:8]}"
        user = models.WebUser(
            username=uname,
            password_hash=hash_password("pw123456"),
            is_active=True,
            is_staff=True,
            emby_username=uname,
            emby_password=hash_password("pw123456"),
        )
        db.add(user)
        db.flush()
        user_id = user.id

        lib = em.Library(guid=lib_guid, name="热门排序测试库", collection_type="movies", is_enabled=True)
        db.add(lib)
        db.flush()
        lib_id = lib.id

        scraped_good = em.MediaItem(
            guid=uuid.uuid4().hex,
            library_id=lib.id,
            item_type="movie",
            name="一个好人",
            tmdb_id="10622",
            community_rating=6.6,
            poster_path="/data/images/good.jpg",
            primary_image_url="https://image.tmdb.org/t/p/w500/good.jpg",
            backdrop_image_url="https://image.tmdb.org/t/p/w1280/good_bg.jpg",
            file_path="/media/good.mkv",
        )
        unscraped_null = em.MediaItem(
            guid=uuid.uuid4().hex,
            library_id=lib.id,
            item_type="movie",
            name="Mr Nice Guy 10bit MNHD",
            community_rating=None,
            poster_path=None,
            primary_image_url=None,
            file_path="/media/unscraped.mkv",
        )
        high_rated = em.MediaItem(
            guid=uuid.uuid4().hex,
            library_id=lib.id,
            item_type="movie",
            name="功夫",
            tmdb_id="9470",
            community_rating=8.3,
            primary_image_url="https://image.tmdb.org/t/p/w500/kungfu.jpg",
            file_path="/media/kungfu.mkv",
        )
        db.add_all([scraped_good, unscraped_null, high_rated])
        db.commit()
        created_item_ids = [scraped_good.id, unscraped_null.id, high_rated.id]

        # 1. 不带 Filters=HasPrimaryImage 时：NULL 评分也必须排在有分条目末尾，且有海报行必带 ImageTags.Primary
        qs1 = (
            f"ParentId={lib_guid}&IncludeItemTypes=Movie,Series"
            f"&SortBy=CommunityRating,DateCreated&SortOrder=Descending&Recursive=true"
        )
        req1 = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/emby/Users/me/Items",
                "query_string": qs1.encode("utf-8"),
                "headers": [],
                "server": ("emby.fluxquata.com", 443),
                "scheme": "https",
            }
        )
        res1 = eapi._query_items(req1, user, db, "https://emby.fluxquata.com")
        names1 = [it["Name"] for it in res1["Items"]]
        assert names1 == ["功夫", "一个好人", "Mr Nice Guy 10bit MNHD"]
        assert res1["Items"][0]["ImageTags"].get("Primary") == "1"
        assert res1["Items"][1]["ImageTags"].get("Primary") == "1"
        assert res1["Items"][1]["BackdropImageTags"] == ["1"]

        # Verify generated SQL contains IS NULL ordering (critical for PostgreSQL where DESC is NULLS FIRST)
        # 2. 带 Filters=HasPrimaryImage（LibraryHotRow 口径）：无海报条目直接从 SQL 排除
        qs2 = f"{qs1}&Filters=HasPrimaryImage"
        req2 = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/emby/Users/me/Items",
                "query_string": qs2.encode("utf-8"),
                "headers": [],
                "server": ("emby.fluxquata.com", 443),
                "scheme": "https",
            }
        )
        res2 = eapi._query_items(req2, user, db, "https://emby.fluxquata.com")
        names2 = [it["Name"] for it in res2["Items"]]
        assert names2 == ["功夫", "一个好人"]
        assert all(it["ImageTags"].get("Primary") == "1" for it in res2["Items"])
    finally:
        if created_item_ids:
            db.query(em.ItemFacet).filter(em.ItemFacet.item_id.in_(created_item_ids)).delete(
                synchronize_session=False
            )
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(created_item_ids)).delete(
                synchronize_session=False
            )
        if lib_id is not None:
            db.query(em.Library).filter(em.Library.id == lib_id).delete()
        if user_id is not None:
            db.query(models.WebUser).filter(models.WebUser.id == user_id).delete()
        db.commit()
        db.close()
