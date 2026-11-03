# -*- coding: utf-8 -*-
"""转存 failed/retriable 滞留单的周期回队（2026-11-03 生产实证）。

生产故障：金丝雀 job 1174（user 602）2026-10-30 以 failed/retriable
落终态后 4 天无人再拾——工人只认领 pending、租约回收只管 running、
门禁重触发只在用户再次点播同片时发生。全队列同形态滞留 77 单
（failed/retriable 且 next_retry_at 为空、超 24h 无更新），其中
53 单落点还是已下线的 pan115/quark。另有 100 单 failed/no_binding
落在已下线落点上，错误文案还写着“重绑后可重建”，与删线事实不符。

本文件钉死四条：
1. 滞留（超回队等待期）的 failed/retriable 单被周期扫回 pending，
   重置口径与重绑复活（revive_binding_failed_jobs）一致：attempts
   清零、错误/重试时间/租约清掉、reschedule_count +1；回队后走
   既有认领链（认领前复核判落点下线/无绑定、执行器真跑），不另
   起第二套调度。
2. 回队有硬上限：reschedule_count 达上限后永久留在 failed，
   不无限回队；未滞留（< 等待期）、带 next_retry_at、其它
   error_kind、剧集预热探针单都不碰。
3. failed/no_binding 且落点已下线的行被纠正为 provider_removed
   明确终态（状态仍 failed）；落点还活着的 no_binding 行不碰
   （重绑复活口径不变）。
4. 回队后的单经 claim_due_jobs 实际流转：落点下线 → 认领前
   provider_removed 秒判（不烧 attempts）；无绑定 → no_binding
   秒判；绑定在 → 被认领执行。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_pause_and_wake():
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _backdate(db, job_id, when):
    db.query(em.TransferJob).filter_by(id=job_id).update(
        {"updated_at": when}, synchronize_session=False)
    db.commit()
    db.expire_all()


def _job(db, *, item_id=1, trigger="playback", owner_ref="9",
         dest="userdrive:9", status="failed", error_kind="retriable",
         attempts=4, next_retry_at=None, reschedule_count=0,
         stale_hours=25):
    row = em.TransferJob(
        item_id=item_id, trigger=trigger, provider="drive",
        owner_scope="user", owner_ref=owner_ref, dest_drive_id=dest,
        status=status, attempts=attempts, error_kind=error_kind,
        error_detail="上游 502" if error_kind else None,
        next_retry_at=next_retry_at,
        reschedule_count=reschedule_count)
    db.add(row)
    db.commit()
    if stale_hours is not None:
        _backdate(db, row.id, datetime.now() - timedelta(hours=stale_hours))
    return db.query(em.TransferJob).filter_by(id=row.id).one()


def _fresh(db, job_id):
    db.expire_all()
    return db.query(em.TransferJob).filter_by(id=job_id).one()


# ---------------------------------------------------------------------------
# 1. 滞留 retriable 回队
# ---------------------------------------------------------------------------

def test_stale_retriable_job_is_rescheduled(db_factory):
    db = db_factory()
    job = _job(db)
    assert tv.reschedule_stale_retriable_jobs(db) == 1
    row = _fresh(db, job.id)
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.error_kind is None
    assert row.error_detail is None
    assert row.next_retry_at is None
    assert row.lease_expires_at is None
    assert row.claimed_by is None
    assert row.reschedule_count == 1


def test_fresh_retriable_job_untouched(db_factory):
    db = db_factory()
    job = _job(db, stale_hours=1)
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == "retriable"
    assert row.reschedule_count == 0


def test_retriable_at_cap_untouched(db_factory):
    db = db_factory()
    job = _job(db, reschedule_count=tv.retriable_reschedule_max())
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == "retriable"


def test_retriable_with_next_retry_at_untouched(db_factory):
    db = db_factory()
    job = _job(db, next_retry_at=datetime.now() + timedelta(hours=1))
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    assert _fresh(db, job.id).status == "failed"


@pytest.mark.parametrize("kind", ["dead", "auth_error",
                                  "insufficient_space", "item_gone"])
def test_other_kinds_not_rescheduled(db_factory, kind):
    db = db_factory()
    job = _job(db, error_kind=kind)
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == kind


def test_series_preheat_not_rescheduled(db_factory):
    db = db_factory()
    job = _job(db, trigger=tv.TRIGGER_SERIES_PREHEAT)
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    assert _fresh(db, job.id).status == "failed"


def test_second_cycle_increments_until_cap(db_factory):
    db = db_factory()
    cap = tv.retriable_reschedule_max()
    job = _job(db, reschedule_count=cap - 1)
    assert tv.reschedule_stale_retriable_jobs(db) == 1
    row = _fresh(db, job.id)
    assert row.reschedule_count == cap
    # 再失败落回 failed/retriable 并再次滞留：已达上限，不再回队。
    row.status = "failed"
    row.error_kind = "retriable"
    row.attempts = 3
    db.commit()
    _backdate(db, row.id, datetime.now() - timedelta(hours=25))
    assert tv.reschedule_stale_retriable_jobs(db) == 0
    assert _fresh(db, job.id).status == "failed"


# ---------------------------------------------------------------------------
# 2. 已下线落点的 no_binding 明确终态
# ---------------------------------------------------------------------------

def test_removed_dest_no_binding_closed_as_provider_removed(db_factory):
    db = db_factory()
    job = _job(db, error_kind="no_binding", dest="quark:user-9",
               attempts=0)
    assert tv.close_removed_dest_no_binding_jobs(db) == 1
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == tv.PROVIDER_REMOVED_KIND
    assert row.error_detail == tv.PROVIDER_REMOVED_DETAIL


def test_live_dest_no_binding_untouched(db_factory):
    db = db_factory()
    job = _job(db, error_kind="no_binding", dest="userdrive:9",
               attempts=0)
    assert tv.close_removed_dest_no_binding_jobs(db) == 0
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == "no_binding"


def test_removed_dest_other_kinds_untouched_by_close(db_factory):
    db = db_factory()
    job = _job(db, error_kind="auth_error", dest="pan115:user-9")
    assert tv.close_removed_dest_no_binding_jobs(db) == 0
    assert _fresh(db, job.id).error_kind == "auth_error"


# ---------------------------------------------------------------------------
# 3. 回队后走既有认领链流转
# ---------------------------------------------------------------------------

def test_rescheduled_removed_dest_terminalized_at_claim(db_factory):
    db = db_factory()
    job = _job(db, dest="pan115:user-9")
    assert tv.reschedule_stale_retriable_jobs(db) == 1
    assert tv.claim_due_jobs(db) == []
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == tv.PROVIDER_REMOVED_KIND
    assert row.attempts == 0  # 认领前秒判，不烧 attempts


def test_rescheduled_unbound_job_fails_no_binding_at_claim(db_factory):
    db = db_factory()
    job = _job(db, dest="userdrive:9")
    assert tv.reschedule_stale_retriable_jobs(db) == 1
    assert tv.claim_due_jobs(db) == []
    row = _fresh(db, job.id)
    assert row.status == "failed"
    assert row.error_kind == "no_binding"
    assert row.attempts == 0


def test_rescheduled_bound_job_is_claimed(db_factory):
    db = db_factory()
    db.add(em.UserStorageBinding(user_id=9, provider="drive",
                                 account_label="test", credential_id=1,
                                 status="active"))
    db.commit()
    job = _job(db, dest="userdrive:9")
    assert tv.reschedule_stale_retriable_jobs(db) == 1
    claimed = tv.claim_due_jobs(db)
    assert claimed == [job.id]
    row = _fresh(db, job.id)
    assert row.status == "running"
