"""metadata_source：元数据来源必须可观测。

以前「刮没刮干净」只能靠 `last_scraped_at IS NULL` 反推，而那把「从没试过」和
「试过但没拿到」混为一谈——生产排查时因此误判过。补上来源标记后，
一条 SQL 就能回答「哪些条目没刮干净」「有多少是 NFO 来的」。

取值口径：
- ``nfo``      文字来自本地 NFO（用户自己整理的，最权威）
- ``tmdb``     文字与图都来自 TMDB
- ``tmdb_img`` NFO 给文字、TMDB 补图（B 方案最常见的组合）
- ``none``     跑过补全但没拿到数据
- ``None``     历史数据未标记
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from conftest import restore_session_factory, snapshot_session_factory  # noqa: E402
from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory（见 backend/database.py 的说明）。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

from backend.emby_server import nfo as nfo_lib  # noqa: E402
from backend.emby_server import tmdb  # noqa: E402


def _item(**kw):
    base = dict(
        tmdb_id=None, last_scraped_at=None, overview=None, community_rating=None,
        primary_image_url=None, poster_path=None, backdrop_path=None,
        backdrop_image_url=None, aliases="", genres="", name="x", sort_name="x",
        original_title=None, production_year=None, official_rating=None,
        studios="", metadata_source=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_tmdb_search_marks_source_tmdb():
    it = _item()
    tmdb.TmdbClient().apply(it, {"id": 1, "name": "n", "overview": "o",
                                 "vote_average": 7.5}, "series")
    assert it.metadata_source == "tmdb"


def test_nfo_marks_source_nfo():
    it = _item()
    nfo_lib.apply_nfo(it, {"title": "n", "plot": "p", "tmdb_id": 5}, "series")
    assert it.metadata_source == "nfo"


def test_tmdb_images_over_nfo_text_marks_tmdb_img():
    """B 方案：NFO 管文字、TMDB 补图 —— 组合必须单独标记，不能被当成纯 TMDB"""
    it = _item(metadata_source="nfo")
    tmdb.TmdbClient().apply_images(it, {"poster_path": "/p.jpg", "backdrop_path": None})
    assert it.metadata_source == "tmdb_img"


def test_apply_details_does_not_downgrade_nfo():
    """详情只补缺项，绝不能把 NFO 的来源标记改写掉"""
    it = _item(metadata_source="nfo")
    tmdb.TmdbClient().apply_details(it, {"id": 1, "overview": "o", "vote_average": 7.0,
                                         "external_ids": {}, "alternative_titles": {}})
    assert it.metadata_source == "nfo", it.metadata_source


def test_images_without_nfo_stay_tmdb():
    it = _item(metadata_source="tmdb")
    tmdb.TmdbClient().apply_images(it, {"poster_path": "/p.jpg", "backdrop_path": None})
    assert it.metadata_source == "tmdb", it.metadata_source


def test_incomplete_scrape_recorded_as_none():
    """跑过但没刮到 → 显式记 none，与「历史未标记」(NULL) 区分"""
    from backend.emby_server import enrich_worker, models as em

    # 本用例自建一个库并设为当前 factory（用完还原）。不能用顶层
    # `from backend.database import SessionLocal`（拿到 import 期快照），
    # 也不能依赖 _real_session_local（其它测试文件可能改过它）——
    # 两者都会让本用例与测试文件的执行顺序耦合。
    own_engine = _ce("sqlite:///:memory:")
    original = snapshot_session_factory()
    _dbmod.configure_session_local(_sm(bind=own_engine))
    em.Base.metadata.create_all(bind=own_engine)
    db = _dbmod.SessionLocal()
    try:
        lib = em.Library(guid="L" * 32, name="库", collection_type="tvshows", paths="")
        db.add(lib)
        db.flush()
        it = em.MediaItem(guid="m" * 32, library_id=lib.id, item_type="series",
                          name="没命中的剧", overview="", tmdb_id=None,
                          enrich_status="enriching", enrich_attempts=0,
                          enrich_next_retry_at=None)
        db.add(it)
        db.flush()
        enrich_worker._enrich_apply(db, it, {"tmdb_hit": None, "tmdb_details": None,
                                             "poster": None, "fanart": None,
                                             "external_subs": [], "nfo_data": None})
        db.commit()
        assert it.enrich_status == "pending", it.enrich_status
        assert it.metadata_source == "none", it.metadata_source
    finally:
        db.close()
        restore_session_factory(original)
        own_engine.dispose()
