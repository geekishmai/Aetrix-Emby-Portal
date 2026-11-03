# -*- coding: utf-8 -*-
"""已合并副本的 enrich 终态收口（2026-11-04，38 条 retrying 副本实证）。

背景：`_claim_batch` 的 not_merged 谓词（`merged_into_id.is_(None)`）永久排除
副本行。合并发生在副本已排队（pending）或已停泊（retrying）之后时，这些行
永远不会被认领，也不会从 backlog 消失（health_report 照计）。

钉住的行为：
- `merge_group` / `_follow_series_episodes`：合并写 `merged_into_id` 的同时把
  pending/retrying 副本收终态 done，并清认领租约与退避时间；
- `maintenance.repair_merged_enrich_status`：存量修复，幂等，支持 dry_run；
- 启动维护自动执行（两个进程都可安全跑）。
"""
import os
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import maintenance
from backend.emby_server import merge_versions_worker as mvw


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _movie(db, guid, **kw):
    it = em.MediaItem(guid=guid, library_id=1, item_type="movie",
                      name=kw.pop("name", guid), file_path="/x",
                      **kw)
    db.add(it)
    db.commit()
    return it


# ---------------- merge_group 收终态 ----------------

def test_merge_group_finalizes_pending_and_retrying():
    """合并时 pending/retrying 副本收 done，租约与退避清空。"""
    primary = SimpleNamespace(id=1, merged_into_id=None, name="主",
                              enrich_status="done", poster_path=None,
                              primary_image_url=None, tmdb_id="",
                              overview="", item_type="movie")
    pending = SimpleNamespace(id=2, merged_into_id=None, name="p",
                              enrich_status="pending", enrich_claimed_at=None,
                              enrich_claim_token=None,
                              enrich_next_retry_at=datetime(2026, 11, 5),
                              poster_path=None, primary_image_url=None,
                              tmdb_id="", overview="", item_type="movie")
    retrying = SimpleNamespace(id=3, merged_into_id=None, name="r",
                               enrich_status="retrying",
                               enrich_claimed_at="2026-11-03",
                               enrich_claim_token="tok",
                               enrich_next_retry_at=datetime(2026, 11, 5),
                               poster_path=None, primary_image_url=None,
                               tmdb_id="", overview="", item_type="movie")
    n = mvw.merge_group(None, [primary, pending, retrying])
    assert n == 2
    for item in (pending, retrying):
        assert item.merged_into_id == 1
        assert item.enrich_status == "done"
        assert item.enrich_claimed_at is None
        assert item.enrich_claim_token is None
        assert item.enrich_next_retry_at is None
    assert primary.enrich_status == "done"


def test_merge_group_keeps_done_untouched():
    """已终态（done/failed）副本不被重复改写语义（merged 照写，status 不动）。"""
    done_copy = SimpleNamespace(id=2, merged_into_id=None, name="d",
                                enrich_status="done", enrich_claimed_at=None,
                                enrich_claim_token=None,
                                enrich_next_retry_at=None,
                                poster_path=None, primary_image_url=None,
                                tmdb_id="", overview="", item_type="movie")
    primary = SimpleNamespace(id=1, merged_into_id=None, name="主",
                              enrich_status="done", poster_path=None,
                              primary_image_url=None, tmdb_id="",
                              overview="", item_type="movie")
    assert mvw.merge_group(None, [primary, done_copy]) == 1
    assert done_copy.enrich_status == "done"


# ---------------- episode follow 收终态 ----------------

def test_follow_series_episodes_finalizes_retrying(db):
    p = em.MediaItem(guid="s-p", library_id=1, item_type="series",
                     name="主剧", file_path="/p", tmdb_id="123")
    d = em.MediaItem(guid="s-d", library_id=1, item_type="series",
                     name="副剧", file_path="/d", tmdb_id="123")
    db.add_all([p, d])
    db.commit()

    def _ep(guid, series, s_no, e_no, status="retrying"):
        row = em.MediaItem(
            guid=guid, library_id=1, item_type="episode", name=guid,
            series_id=series.id, season_number=s_no, episode_number=e_no,
            file_path=f"/{guid}", enrich_status=status,
            enrich_claim_token="tok" if status == "retrying" else None,
            enrich_next_retry_at=None)
        db.add(row)
        db.commit()
        return row

    p_e1 = _ep("pe1", p, 1, 1, status="done")
    d_e1 = _ep("de1", d, 1, 1)
    group = [p, d]
    mvw.merge_group(db, group)
    mvw._follow_series_episodes(db, group)
    db.commit()
    db.refresh(d_e1)
    assert d_e1.merged_into_id == p_e1.id
    assert d_e1.enrich_status == "done"
    assert d_e1.enrich_claim_token is None


# ---------------- 存量修复（dry-run / 幂等） ----------------

def test_repair_dry_run_counts_without_write(db):
    lib = em.Library(guid="l1", name="库", collection_type="movies")
    db.add(lib)
    db.commit()
    _movie(db, "main", name="主")
    _movie(db, "copy-pending", name="p",
           merged_into_id=1, enrich_status="pending")
    _movie(db, "copy-retrying", name="r",
           merged_into_id=1, enrich_status="retrying",
           enrich_claim_token="tok")
    _movie(db, "independent", name="独立", enrich_status="pending")  # 未合并：不动
    _movie(db, "copy-done", name="d",
           merged_into_id=1, enrich_status="done")                    # 已终态：不动
    counts = maintenance.repair_merged_enrich_status(db, dry_run=True)
    assert counts["scanned"] == 2
    assert counts["by_status"] == {"pending": 1, "retrying": 1}
    assert counts["repaired"] == 0
    rows = {r.guid: r.enrich_status for r in db.query(em.MediaItem).all()}
    assert rows["copy-pending"] == "pending"
    assert rows["copy-retrying"] == "retrying"


def test_repair_writes_final_status_and_is_idempotent(db):
    lib = em.Library(guid="l2", name="库", collection_type="movies")
    db.add(lib)
    db.commit()
    copy = _movie(db, "copy", name="副本", merged_into_id=1,
                  enrich_status="retrying", enrich_claim_token="tok",
                  enrich_next_retry_at=datetime(2026, 11, 5))
    first = maintenance.repair_merged_enrich_status(db)
    assert first["repaired"] == 1
    db.refresh(copy)
    assert copy.enrich_status == "done"
    assert copy.enrich_claim_token is None
    assert copy.enrich_next_retry_at is None
    second = maintenance.repair_merged_enrich_status(db)
    assert second["scanned"] == 0 and second["repaired"] == 0, "幂等：第二轮零改动"


def test_startup_maintenance_runs_repair(db, monkeypatch):
    """启动维护接线：repair 出现在结果里且真的收口存量。

    run_startup_maintenance 函数体内 `from backend.database import
    SessionLocal` → monkeypatch backend.database.SessionLocal 即生效。
    """
    import backend.database as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", lambda: db)
    lib = em.Library(guid="l3", name="库", collection_type="movies")
    db.add(lib)
    db.commit()
    _movie(db, "copy2", name="副本2", merged_into_id=1,
           enrich_status="pending")
    result = maintenance.run_startup_maintenance(scan_owner=False)
    assert result["merged_enrich_repaired"] == 1
    row = db.query(em.MediaItem).filter(em.MediaItem.guid == "copy2").first()
    assert row.enrich_status == "done"
