"""存量类型回填例程（scripts/backfill_movie_genres.py 扩展）。

背景：CMS 托管行 enrich 不写 genres 的存量（库 58 有 tmdb_id 的
1,614 行全缺类型）。回填数据源两级、都不许新打一轮 TMDB 搜索：
① donor——同 tmdb_id、同类型、已有 genres 的已落库行（纯本地、
零请求）；② tmdb_client.details 的两级缓存（磁盘 30 天，enrich
拉过的详情多半还在，缓存未命中才走令牌桶）。

钉死：
- donor 优先于 details；donor 按类型隔离（电影的类型不许喂剧集）；
- dry-run 只计数不写库；apply 只填空不覆盖；
- 软删除 / 合并行不作目标；--library-id 口径只碰指定库；
- 无 donor 时才调 details，kind 透传（movie / series 分开）。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from scripts.backfill_movie_genres import backfill_genres, donor_genres


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _library(db, name, collection_type="movies", kind="local"):
    lib = em.Library(guid=uuid.uuid4().hex, name=name,
                     collection_type=collection_type, kind=kind)
    db.add(lib)
    db.flush()
    return lib


def _item(db, lib, name, **kw):
    kw.setdefault("enrich_status", "done")
    kw.setdefault("item_type", "movie")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id,
        name=name, sort_name=name, **kw)
    db.add(item)
    db.flush()
    return item


def _no_details(tmdb_id, kind):  # pragma: no cover - 不应被调用的桩
    raise AssertionError("donor 可覆盖时不许调 details")


# ---------- donor 映射 ----------

def test_donor_map_basic_and_kind_isolation(db):
    local = _library(db, "本地电影")
    cms = _library(db, "在线电影", kind="applecms")
    _item(db, local, "本地版", tmdb_id="603", genres="动作,剧情")
    _item(db, cms, "托管版", tmdb_id="603", genres="")
    donors = donor_genres(db, kind="movie")
    assert donors == {"603": "动作,剧情"}
    # 剧集口径下电影 donor 不出现
    assert donor_genres(db, kind="series") == {}


def test_donor_map_ignores_blank_and_merged(db):
    local = _library(db, "本地电影")
    _item(db, local, "空白类型", tmdb_id="1", genres="  ")
    _item(db, local, "合并行", tmdb_id="2", genres="喜剧", merged_into_id=99)
    _item(db, local, "无身份", tmdb_id=None, genres="喜剧")
    assert donor_genres(db, kind="movie") == {}


# ---------- 回填主流程 ----------

def test_backfill_donor_first_dry_run_counts_without_writing(db):
    local = _library(db, "本地电影")
    cms = _library(db, "在线电影", kind="applecms")
    _item(db, local, "本地版", tmdb_id="603", genres="动作,剧情")
    target = _item(db, cms, "托管版", tmdb_id="603", genres="")
    stats = backfill_genres(db, kind="movie", dry_run=True,
                            details_fetcher=_no_details)
    assert stats["total"] == 1
    assert stats["donor_filled"] == 1
    assert stats["details_filled"] == 0
    db.refresh(target)
    assert not (target.genres or "").strip()


def test_backfill_apply_writes_only_targets_and_never_overwrites(db):
    local = _library(db, "本地电影")
    cms = _library(db, "在线电影", kind="applecms")
    donor = _item(db, local, "本地版", tmdb_id="603", genres="动作,剧情")
    target = _item(db, cms, "托管版", tmdb_id="603", genres="")
    already = _item(db, cms, "已有类型", tmdb_id="603", genres="悬疑")
    stats = backfill_genres(db, kind="movie", dry_run=False,
                            details_fetcher=_no_details)
    assert stats["total"] == 1 and stats["donor_filled"] == 1
    db.refresh(target)
    db.refresh(already)
    db.refresh(donor)
    assert target.genres == "动作,剧情"
    assert already.genres == "悬疑"
    assert donor.genres == "动作,剧情"


def test_backfill_details_fallback_and_kind_passthrough(db):
    cms = _library(db, "在线电影", kind="applecms")
    _item(db, cms, "无donor", tmdb_id="999", genres="")
    seen = {}

    def fake_details(tmdb_id, kind):
        seen["args"] = (tmdb_id, kind)
        return {"genres": [{"id": 28, "name": "动作"}]}

    stats = backfill_genres(db, kind="movie", dry_run=False,
                            details_fetcher=fake_details)
    assert seen["args"] == ("999", "movie")
    assert stats["details_filled"] == 1
    assert stats["donor_filled"] == 0


def test_backfill_still_missing_when_details_have_no_genres(db):
    cms = _library(db, "在线电影", kind="applecms")
    _item(db, cms, "无donor", tmdb_id="999", genres="")
    stats = backfill_genres(db, kind="movie", dry_run=False,
                            details_fetcher=lambda tid, kind: {})
    assert stats["still_missing"] == 1


def test_backfill_library_filter_and_exclusions(db):
    cms = _library(db, "在线电影", kind="applecms")
    other = _library(db, "别的库", kind="applecms")
    local = _library(db, "本地电影")
    _item(db, local, "本地版", tmdb_id="603", genres="动作,剧情")
    _item(db, cms, "目标", tmdb_id="603", genres="")
    _item(db, other, "别库目标", tmdb_id="603", genres="")
    _item(db, cms, "合并行", tmdb_id="603", genres="", merged_into_id=99)
    stats = backfill_genres(db, kind="movie", library_id=cms.id,
                            dry_run=True, details_fetcher=_no_details)
    assert stats["total"] == 1
    assert stats["donor_filled"] == 1


def test_backfill_series_uses_series_donors_and_kind(db):
    local_tv = _library(db, "本地剧集", collection_type="tvshows")
    cms_tv = _library(db, "国产剧", collection_type="tvshows", kind="applecms")
    _item(db, local_tv, "本地剧", item_type="series",
          tmdb_id="60300", genres="剧情")
    target = _item(db, cms_tv, "托管剧", item_type="series",
                   tmdb_id="60300", genres="")
    stats = backfill_genres(db, kind="series", dry_run=False,
                            details_fetcher=_no_details)
    assert stats["donor_filled"] == 1
    db.refresh(target)
    assert target.genres == "剧情"
