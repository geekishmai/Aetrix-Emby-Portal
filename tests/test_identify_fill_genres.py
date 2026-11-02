"""补资料段补 genres（CMS 托管行 enrich 后无类型定案修复）。

定案：本地行 genres 来自扫描期 scanner 直调 TmdbClient.apply /
apply_details；CMS 托管行无本地文件、不走扫描，只走 enrich 两段式
写库，而补资料段白名单 FILL_FIELDS 与候选抽取都不含 genres ——
TMDB 已命中、简介/评分/海报都落了，类型永远为空（生产库 58：
tmdb 1614 行 genres 0 行）。

钉死：
① 补资料段从 TMDB 详情 genres 落类型（与 apply_details 同一解析）；
② 详情缺时用搜索命中 genre_ids 经同一映射表翻译；
③ 只填空不覆盖；未绑定条目仍拒绝；
④ 候选抽取：详情优先于命中、最多 4 个、去重。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em


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


def _make_cms_movie(db, **kw):
    """CMS 托管电影行：无本地文件、带 upstream_source_id。"""
    lib = em.Library(guid=uuid.uuid4().hex, name="在线电影",
                     collection_type="movies", kind="applecms")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name="某片", sort_name="某片", file_path=None,
        production_year=2021, upstream_source_id=7, **kw)
    db.add(item)
    db.flush()
    return item


def _fetched_with_details_genres():
    return {
        "tmdb_id": "603",
        "tmdb_details": {
            "overview": "详情简介",
            "vote_average": 8.7,
            "genres": [{"id": 28, "name": "动作"}, {"id": 18, "name": "剧情"}],
            "imdb_id": "tt0133093",
        },
    }


# ---------- ① 详情 genres 落库 ----------

def test_fill_genres_from_details(db):
    item = _make_cms_movie(db, tmdb_id="603")
    filled = ts.apply_fill(db, item, _fetched_with_details_genres())
    assert "genres" in filled
    assert item.genres == "动作,剧情"


def test_fill_genres_fill_only_never_overwrites(db):
    item = _make_cms_movie(db, tmdb_id="603", genres="已有类型")
    filled = ts.apply_fill(db, item, _fetched_with_details_genres())
    assert "genres" not in filled
    assert item.genres == "已有类型"


def test_fill_genres_blank_string_counts_as_missing(db):
    item = _make_cms_movie(db, tmdb_id="603", genres="  ")
    filled = ts.apply_fill(db, item, _fetched_with_details_genres())
    assert "genres" in filled
    assert item.genres == "动作,剧情"


# ---------- ② 命中 genre_ids 垫后 ----------

def test_fill_genres_from_hit_genre_ids_when_no_details(db):
    item = _make_cms_movie(db, tmdb_id="603")
    fetched = {"tmdb_hit": {"id": 603, "genre_ids": [28, 35]}}
    filled = ts.apply_fill(db, item, fetched)
    assert "genres" in filled
    assert item.genres == "动作,喜剧"


def test_fill_genres_details_preferred_over_hit(db):
    item = _make_cms_movie(db, tmdb_id="603")
    fetched = _fetched_with_details_genres()
    fetched["tmdb_hit"] = {"id": 603, "genre_ids": [35]}
    ts.apply_fill(db, item, fetched)
    assert item.genres == "动作,剧情"


def test_fill_genres_unknown_hit_id_not_written_as_number(db):
    item = _make_cms_movie(db, tmdb_id="603")
    fetched = {"tmdb_hit": {"id": 603, "genre_ids": [999999]}}
    filled = ts.apply_fill(db, item, fetched)
    assert "genres" not in filled
    assert not (item.genres or "").strip()


# ---------- ③ 未绑定仍拒绝 ----------

def test_fill_genres_refuses_unbound_item(db):
    item = _make_cms_movie(db)
    assert ts.apply_fill(db, item, _fetched_with_details_genres()) == []
    assert not (item.genres or "").strip()


# ---------- ④ 候选抽取口径 ----------

def test_candidates_genres_capped_at_four_and_deduped():
    fetched = {"tmdb_details": {"genres": [
        {"id": 28, "name": "动作"}, {"id": 28, "name": "动作"},
        {"id": 18, "name": "剧情"}, {"id": 35, "name": "喜剧"},
        {"id": 80, "name": "犯罪"}, {"id": 53, "name": "惊悚"},
    ]}}
    candidates = ts.fill_candidates_from_fetched(fetched)
    assert candidates.get("genres") == "动作,剧情,喜剧,犯罪"


def test_candidates_no_genres_evidence_no_key():
    candidates = ts.fill_candidates_from_fetched({"tmdb_details": {"overview": "x"}})
    assert "genres" not in candidates
