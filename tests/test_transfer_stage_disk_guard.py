# -*- coding: utf-8 -*-
"""转存暂存磁盘防护（2026-11-02 生产事故单）。

事故：2026-11-02 15:00 生产磁盘跌至 9% 剩余、health down；主因
worker 容器 /tmp/aetrix-transfer-stage 占 12G——跨盘转存把整个
文件先下到服务器暂存再上传，文件多大吃多大盘，多单并发大文件
把 74G 系统盘撑爆。既有 TRANSFER_STAGE_MAX_TOTAL_BYTES 只在
_acquire_source_stage 新建暂存时按估算把关（单文件事前估算、
并发竞态可穿透），盘上实占超帽后新单仍照常认领、继续堆暂存。

本文件钉死两道新防线：
1. 认领护栏：暂存目录实占 ≥ 上限时，claim_due_jobs 本轮压住
   跨盘单不认领（留 pending、不烧 attempts、不判终态），同
   provider 单不经暂存照常认领；暂存回落后下一轮自动恢复。
2. 引用式清道：sweep_stage_orphans 按任务引用删残留——对应
   转存单已终态（done/failed）或不存在、且文件 mtime 超过宽限
   才删；非终态（pending/running）单引用的暂存文件绝不删，
   含执行器实测 size 与条目登记 size 漂移时靠签名＋sidecar
   真实 size 重算键兜底；mp-<job_id>.json 按行存在性同判。

全部离线：SQLite :memory: + tmp_path 暂存目录，不碰生产数据、
不碰 Redis、不碰真实网盘。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


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
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture(autouse=True)
def _clean_state():
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


_counter = [0]


def _item(db, fid="origfid", size=2000):
    _counter[0] += 1
    row = em.MediaItem(guid=f"sg-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id=fid,
                       file_path="mount://3/电影/a.mkv", size=size)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, user_id=7, cross=True, status="pending", **kw):
    dest = f"pan115:user-{user_id}" if cross else f"userdrive:{user_id}"
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _age(path, age_sec):
    t = time.time() - age_sec
    os.utime(path, (t, t))


def _stage_pair(stage_dir, key, size, nbytes=300, age_sec=7200):
    """在暂存目录落一对 st-<key>.bin/.json 并回拨 mtime。"""
    stage_dir.mkdir(parents=True, exist_ok=True)
    bin_path = stage_dir / f"st-{key}.bin"
    meta_path = stage_dir / f"st-{key}.json"
    bin_path.write_bytes(b"x" * nbytes)
    meta_path.write_text(json.dumps(
        {"key": key, "size": size, "prefix": nbytes}), encoding="utf-8")
    _age(str(bin_path), age_sec)
    _age(str(meta_path), age_sec)
    return bin_path, meta_path


# ---------- ① 认领护栏：暂存超限压住跨盘单 ----------

def test_claim_holds_cross_jobs_when_stage_over_limit(db, monkeypatch,
                                                      tmp_path):
    """暂存实占 ≥ 上限：跨盘单本轮不认领（pending、attempts 不动），
    同 provider 单照常认领；清掉暂存后跨盘单恢复认领。
    旧码无护栏：跨盘单第一轮即被认领 → 红。"""
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(stage_dir))
    monkeypatch.setenv("TRANSFER_STAGE_MAX_TOTAL_BYTES", "1000")
    # 盘上实占 1500 字节 ≥ 上限 1000（别的单留下的整份暂存）。
    (stage_dir / ("st-" + "f" * 64 + ".bin")).write_bytes(b"x" * 1500)

    _binding(db, 7, "pan115")
    _binding(db, 7, "drive")
    item = _item(db)
    cross = _job(db, item.id, cross=True)
    same = _job(db, item.id, cross=False)

    claimed = tv.claim_due_jobs(db, limit=4)
    assert cross.id not in claimed
    assert same.id in claimed
    db.refresh(cross)
    assert cross.status == "pending"
    assert cross.attempts == 0

    # 暂存回落后自动恢复：下一轮跨盘单被认领。
    (stage_dir / ("st-" + "f" * 64 + ".bin")).unlink()
    claimed = tv.claim_due_jobs(db, limit=4)
    assert cross.id in claimed
    db.refresh(cross)
    assert cross.status == "running"


def test_claim_proceeds_when_stage_under_limit(db, monkeypatch, tmp_path):
    """暂存实占低于上限时护栏不介入：跨盘单照常认领（防误伤）。"""
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(stage_dir))
    monkeypatch.setenv("TRANSFER_STAGE_MAX_TOTAL_BYTES", "100000")
    (stage_dir / ("st-" + "f" * 64 + ".bin")).write_bytes(b"x" * 1500)

    _binding(db, 7, "pan115")
    item = _item(db)
    cross = _job(db, item.id, cross=True)
    claimed = tv.claim_due_jobs(db, limit=4)
    assert cross.id in claimed


# ---------- ② 清道：活单文件绝不删 ----------

def test_sweep_keeps_live_job_stage_files(db, tmp_path):
    """pending 活单引用的暂存对：mtime 远超宽限也不删。"""
    stage_dir = tmp_path / "stage"
    item = _item(db, fid="livefid", size=2000)
    _job(db, item.id, cross=True, status="pending")
    key = tv._stage_key("drive", "shared1", "livefid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000)
    stats = tv.sweep_stage_orphans(db, directory=str(stage_dir))
    assert bin_path.exists() and meta_path.exists()
    assert stats["removed_files"] == 0
    assert stats["kept_live_files"] >= 2


def test_sweep_keeps_running_job_sidecar_and_stage(db, tmp_path):
    """running 活单：mp 分片会话与暂存对都不删。"""
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True)
    item = _item(db, fid="runfid", size=2000)
    job = _job(db, item.id, cross=True, status="running")
    key = tv._stage_key("drive", "shared1", "runfid", 2000)
    bin_path, meta_path = _stage_pair(stage_dir, key, 2000)
    sidecar = stage_dir / f"mp-{job.id}.json"
    sidecar.write_text("{}", encoding="utf-8")
    _age(str(sidecar), 7200)
    stats = tv.sweep_stage_orphans(db, directory=str(stage_dir))
    assert bin_path.exists() and meta_path.exists() and sidecar.exists()
    assert stats["removed_files"] == 0


def test_sweep_keeps_live_stage_despite_size_drift(db, tmp_path):
    """执行器实测 size（3000）与条目登记 size（2000）漂移：暂存键
    按实测 size 生成、与 _cross_content_key 的登记口径对不上，
    清道靠签名＋sidecar 真实 size 重算键兜底，活单文件仍不删。"""
    stage_dir = tmp_path / "stage"
    item = _item(db, fid="driftfid", size=2000)
    _job(db, item.id, cross=True, status="pending")
    drift_key = tv._stage_key("drive", "shared1", "driftfid", 3000)
    bin_path, meta_path = _stage_pair(stage_dir, drift_key, 3000)
    stats = tv.sweep_stage_orphans(db, directory=str(stage_dir))
    assert bin_path.exists() and meta_path.exists()
    assert stats["removed_files"] == 0


# ---------- ③ 清道：终态/无主残留回收 ----------

def test_sweep_removes_terminal_and_ownerless_leftovers(db, tmp_path):
    """终态单的暂存对、无任何任务的暂存对、终态/不存在任务的
    mp 会话都回收并计数；宽限内的新文件不动。"""
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True)

    done_item = _item(db, fid="donefid", size=2000)
    done_job = _job(db, done_item.id, cross=True, status="done")
    done_key = tv._stage_key("drive", "shared1", "donefid", 2000)
    done_bin, done_meta = _stage_pair(stage_dir, done_key, 2000,
                                      nbytes=300)

    ghost_key = tv._stage_key("drive", "shared1", "ghostfid", 5000)
    ghost_bin, ghost_meta = _stage_pair(stage_dir, ghost_key, 5000,
                                        nbytes=100)

    failed_item = _item(db, fid="failfid", size=2000)
    failed_job = _job(db, failed_item.id, cross=True, status="failed")
    dead_sidecar = stage_dir / f"mp-{failed_job.id}.json"
    dead_sidecar.write_text("{}", encoding="utf-8")
    _age(str(dead_sidecar), 7200)
    gone_sidecar = stage_dir / "mp-999999.json"
    gone_sidecar.write_text("{}", encoding="utf-8")
    _age(str(gone_sidecar), 7200)

    # 宽限内的无主新文件：刚落盘，引用判定窗口内一律不动。
    young_key = tv._stage_key("drive", "shared1", "youngfid", 5000)
    young_bin, young_meta = _stage_pair(stage_dir, young_key, 5000,
                                        nbytes=100, age_sec=0)

    stats = tv.sweep_stage_orphans(db, directory=str(stage_dir))
    assert not done_bin.exists() and not done_meta.exists()
    assert not ghost_bin.exists() and not ghost_meta.exists()
    assert not dead_sidecar.exists() and not gone_sidecar.exists()
    assert young_bin.exists() and young_meta.exists()
    assert stats["removed_files"] == 6
    assert stats["removed_bytes"] >= 400
    assert done_job.status == "done"  # 清道只删文件，不动任务行


def test_sweep_missing_directory_is_noop(db, tmp_path):
    """暂存目录不存在（容器刚重建）：清道静默回零，不抛。"""
    stats = tv.sweep_stage_orphans(db, directory=str(tmp_path / "nope"))
    assert stats == {"removed_files": 0, "removed_bytes": 0,
                     "kept_live_files": 0}
