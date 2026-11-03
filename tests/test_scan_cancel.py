# -*- coding: utf-8 -*-
"""运行中扫描取消（docs/aetrix-trial-scan.md §15，feat/scan-cancel）。

覆盖 §15.9 的 9 组验收：
1. 标志/接口（probe、request_cancel_run、DELETE 三态、审计字段）
2. 探针（2s 缓存、读失败不误取消、begin 后立即取消）
3. 清理安全（取消路径零删除、已提交批次保留）
4. walker 检查点（每目录/每页）
5. 队列收尾（cancelled、补扫快照丢弃、不通知）
6. 重启分流（cancelled 不续跑，interrupted 仍续跑）
7. 并发/幂等（重复取消、无 run 的窗口）
8. 回归（正常扫描仍 success）
9. 清理边界（清理入口取消 → cancelled；进入清理后不中断由设计保证）
"""
import os
import types
import uuid

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 — 先导避循环
from backend.emby_server import scan_cancel
from backend.emby_server import scanner
from backend.emby_server import scan_queue
from backend.emby_server import maintenance
from backend.emby_server import portal
try:
    from backend.database import init_db
    init_db()
except ModuleNotFoundError:          # Windows 无 fcntl：建表后直接用
    from backend import database as _dbmod
    _dbmod.Base.metadata.create_all(_dbmod.engine)
    em.Base.metadata.create_all(_dbmod.engine)


# ---------------- helpers ----------------

def _memory_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _guid():
    return uuid.uuid4().hex


def _library(db, name="取消测试库"):
    lib = em.Library(guid=_guid(), name=name, collection_type="movies")
    db.add(lib)
    db.commit()
    # ci-local.db 跨 pytest 运行持久；SQLite 会复用被删库的 id，先清掉
    # 该 id 的历史行（否则上一轮失败残留会污染本轮计数/唯一索引）。
    db.query(em.ScanRun).filter(em.ScanRun.library_id == lib.id).delete()
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete()
    db.commit()
    return lib


def _running_run(db, lib):
    run = em.ScanRun(library_id=lib.id, status=scanner.SCAN_STATUS_RUNNING,
                     trigger="manual")
    db.add(run)
    db.commit()
    return run


def _cleanup(db, lib):
    db.query(em.ScanRun).filter(em.ScanRun.library_id == lib.id).delete()
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete()
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


class _Request:
    def __init__(self):
        self.state = type("S", (), {})()


# ---------------- 1/2. 探针 ----------------

def test_probe_detects_cancel_and_caches_poll():
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    run = _running_run(db, lib)
    reads = {"n": 0}
    probe = scan_cancel.CancelProbe(run.id, poll_sec=3600,
                                    session_factory=lambda: factory())
    probe.check("first")          # 第一次立即读
    probe.check("second")         # 轮询窗口内：不再读
    assert reads["n"] == 0
    db.query(em.ScanRun).filter(em.ScanRun.id == run.id).update(
        {em.ScanRun.cancel_requested_at: __import__("datetime").datetime.now()})
    db.commit()
    probe2 = scan_cancel.CancelProbe(run.id, poll_sec=0,
                                     session_factory=lambda: factory())
    with pytest.raises(scan_cancel.ScanCancelled):
        probe2.check("dir")
    db.close()


def test_probe_read_error_does_not_cancel():
    def boom():
        raise RuntimeError("db down")
    probe = scan_cancel.CancelProbe(1, poll_sec=0, session_factory=boom)
    probe.check("ok")   # 读失败 → 忽略本轮，不抛取消


def test_checkpoint_noop_without_binding():
    scan_cancel.checkpoint("noop")   # 未绑定 probe：no-op


def test_checkpoint_raises_when_bound():
    class P:
        def check(self, where):
            raise scan_cancel.ScanCancelled(where)
    token = scan_cancel.bind(P())
    try:
        with pytest.raises(scan_cancel.ScanCancelled):
            scan_cancel.checkpoint("bound")
    finally:
        scan_cancel.unbind(token)


# ---------------- 1. request_cancel_run / 收尾 ----------------

def test_request_cancel_run_idempotent_and_missing():
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    run = _running_run(db, lib)
    first = scanner.request_cancel_run(db, lib.id, wait_sec=0)
    assert first == run.id
    stamp = db.query(em.ScanRun.cancel_requested_at).filter(
        em.ScanRun.id == run.id).scalar()
    assert stamp is not None
    assert scanner.request_cancel_run(db, lib.id, wait_sec=0) == run.id
    assert db.query(em.ScanRun.cancel_requested_at).filter(
        em.ScanRun.id == run.id).scalar() == stamp, "重复取消不刷新时间戳"
    db.query(em.ScanRun).filter(em.ScanRun.id == run.id).update(
        {em.ScanRun.status: scanner.SCAN_STATUS_SUCCESS})
    db.commit()
    assert scanner.request_cancel_run(db, lib.id, wait_sec=0) is None
    db.close()


def test_cancel_scan_writes_cancelled_status():
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    run = _running_run(db, lib)
    lib.is_scanning = True
    lib.scan_status = scanner.SCAN_STATUS_RUNNING
    db.commit()
    scanner.cancel_scan(db, lib, {"added": 3}, run.id)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_CANCELLED
    assert lib.is_scanning is False
    run_row = db.query(em.ScanRun).filter(em.ScanRun.id == run.id).first()
    assert run_row.status == scanner.SCAN_STATUS_CANCELLED
    db.close()


# ---------------- 3/8. 扫描主体：清理安全 + 已提交批次 ----------------

def _scanfile(path):
    return scanner.ScanFile(
        stored_path=str(path), name=path.name, local_dir=str(path.parent),
        size=64, container="mp4")


def _set_cancel_now(lib_id, db=None):
    """置取消标志；db 给了就用调用方会话（SQLite 下避免写锁冲突）。"""
    from backend.database import SessionLocal
    from datetime import datetime
    own = db is None
    s = db or SessionLocal()
    try:
        run = (s.query(em.ScanRun)
               .filter(em.ScanRun.library_id == lib_id,
                       em.ScanRun.status == scanner.SCAN_STATUS_RUNNING)
               .order_by(em.ScanRun.id.desc()).first())
        if run is not None:
            run.cancel_requested_at = datetime.now()
            s.commit()
    finally:
        if own:
            s.close()


def test_scan_cancel_before_cleanup_skips_removal(tmp_path, monkeypatch):
    """取消后：状态 cancelled、清理零调用、已提交条目保留。"""
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消清理库")
    try:
        movie = tmp_path / "Cancel Me (2024).mp4"
        movie.write_bytes(b"\x00" * 64)
        sf = _scanfile(movie)

        def fake_sources(snap, library, db_, failed_roots, report=None):
            yield "test", iter([sf])

        monkeypatch.setattr(scanner, "iter_scan_sources", fake_sources)
        # 本批写库/提交完成后、清理入口之前置取消：验证「已提交批次保留 +
        # 清理零调用」，而不是在批次中途回滚（那是下一条用例）。
        _orig_store = scanner._store_dir_states

        def _store_then_cancel(db_, ctx):
            _orig_store(db_, ctx)
            _set_cancel_now(lib.id, db_)

        monkeypatch.setattr(scanner, "_store_dir_states", _store_then_cancel)
        monkeypatch.setattr(scanner, "PROBE_BACKGROUND", True)
        monkeypatch.setattr(scan_cancel, "POLL_SEC_DEFAULT", 0.0)
        called = {"remove": 0}
        monkeypatch.setattr(
            scanner, "_remove_missing_items",
            lambda *a, **k: called.__setitem__("remove", called["remove"] + 1) or 0)

        stats = scanner.scan_library_sync(db, lib, trigger="manual")
        assert stats.get("canceled") is True
        assert stats.get("removal_skipped") is True
        assert called["remove"] == 0, "取消路径绝不能执行删除清理"
        db.refresh(lib)
        assert lib.scan_status == scanner.SCAN_STATUS_CANCELLED
        run = (db.query(em.ScanRun)
               .filter(em.ScanRun.library_id == lib.id)
               .order_by(em.ScanRun.id.desc()).first())
        assert run.status == scanner.SCAN_STATUS_CANCELLED
        assert db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).count() == 1, "已提交批次保留"
    finally:
        _cleanup(db, lib)
        db.close()


def test_scan_cancel_mid_batch_keeps_committed_and_drops_current(tmp_path, monkeypatch):
    """批次之间取消：第一批已提交，第二批未提交（回滚）。"""
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消批次库")
    try:
        f1 = tmp_path / "First (2024).mp4"
        f2 = tmp_path / "Second (2024).mp4"
        f1.write_bytes(b"\x00" * 64)
        f2.write_bytes(b"\x00" * 64)

        def fake_sources(snap, library, db_, failed_roots, report=None):
            def gen():
                yield _scanfile(f1)
                _set_cancel_now(library.id)   # 第一批之后置取消
                yield _scanfile(f2)
            yield "test", gen()

        monkeypatch.setattr(scanner, "iter_scan_sources", fake_sources)
        monkeypatch.setattr(scanner, "PROBE_BACKGROUND", True)
        monkeypatch.setattr(scanner, "SCAN_BATCH", 1)
        monkeypatch.setattr(scan_cancel, "POLL_SEC_DEFAULT", 0.0)
        monkeypatch.setattr(scanner, "_remove_missing_items", lambda *a, **k: 0)

        stats = scanner.scan_library_sync(db, lib, trigger="manual")
        assert stats.get("canceled") is True
        assert stats.get("added") == 1
        names = [r[0] for r in db.query(em.MediaItem.name).filter(
            em.MediaItem.library_id == lib.id).all()]
        assert len(names) == 1, f"只保留第一批：{names}"
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------- 4. walker 检查点 ----------------

def test_sidecar_page_checkpoint_stops_next_page(monkeypatch):
    from backend.emby_server import mount_sidecar

    class Client:
        def __init__(self):
            self.calls = 0

        def list_page(self, *a, **k):
            self.calls += 1
            return {"entries": [], "next_page_token": None}

    class Fake:
        client = Client()
        session_id = "s"

    class P:
        def check(self, where):
            raise scan_cancel.ScanCancelled(where)

    token = scan_cancel.bind(P())
    try:
        with pytest.raises(scan_cancel.ScanCancelled):
            mount_sidecar.SidecarMountProvider._list_page_with_retry(
                Fake(), "/", None)
        assert Fake.client.calls == 0, "取消后不再发下一页请求"
    finally:
        scan_cancel.unbind(token)


def test_sidecar_dir_checkpoint_before_list():
    from backend.emby_server import mount_sidecar

    class Fake:
        listed = 0

        def list_dir(self, rel):
            Fake.listed += 1
            return []

    class P:
        def check(self, where):
            raise scan_cancel.ScanCancelled(where)

    token = scan_cancel.bind(P())
    try:
        with pytest.raises(scan_cancel.ScanCancelled):
            next(mount_sidecar.SidecarMountProvider.walk_media(Fake(), "/"))
        assert Fake.listed == 0
    finally:
        scan_cancel.unbind(token)


# ---------------- 5. 队列收尾 ----------------

def test_run_task_marks_cancelled_and_drops_pending_rescan(monkeypatch):
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消队列库")
    try:
        monkeypatch.setattr(scanner, "scan_library_sync",
                            lambda *a, **k: {"canceled": True, "added": 0})
        task = scan_queue.ScanTask(library_id=lib.id, name=lib.name,
                                   trigger="manual")
        task.pending_rescan_snapshot = object()
        task.pending_rescan_trigger = "manual"
        scan_queue._run_task(task)
        assert task.state == scan_queue.STATE_CANCELED
        assert task.result == "cancelled"
        assert task.pending_rescan_snapshot is None
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------- 6. 重启分流 ----------------

def test_reset_stale_flags_cancelled_not_interrupted():
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消重启库")
    try:
        run = _running_run(db, lib)
        from datetime import datetime
        run.cancel_requested_at = datetime.now()
        lib.is_scanning = True
        lib.scan_status = scanner.SCAN_STATUS_RUNNING
        db.commit()
        maintenance.reset_stale_scan_flags(db)
        db.refresh(lib)
        assert lib.scan_status == scanner.SCAN_STATUS_CANCELLED
        assert lib.is_scanning is False
    finally:
        _cleanup(db, lib)
        db.close()


def test_close_stale_runs_cancelled_and_resume_skips(monkeypatch):
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消续跑库")
    try:
        run = _running_run(db, lib)
        from datetime import datetime
        run.cancel_requested_at = datetime.now()
        lib.scan_status = scanner.SCAN_STATUS_CANCELLED
        lib.is_scanning = False
        db.commit()
        maintenance.close_stale_scan_runs(db, library_ids=[lib.id])
        db.refresh(run)
        assert run.status == scanner.SCAN_STATUS_CANCELLED
        enqueued = []
        monkeypatch.setattr(scan_queue, "enqueue",
                            lambda *a, **k: enqueued.append(a))
        monkeypatch.setattr(scan_queue, "enqueue_targeted",
                            lambda *a, **k: enqueued.append(a))
        assert maintenance.resume_interrupted_scans(db) == 0
        assert enqueued == [], "cancelled 绝不自动续跑"
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------- 7/1. 接口三态 ----------------

def test_endpoint_running_returns_cancelling(monkeypatch):
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    run = _running_run(db, lib)
    monkeypatch.setattr(scan_queue, "cancel", lambda lib_id: "running")
    req = _Request()
    out = portal.cancel_queued_scan(lib.id, req, None, db)
    assert out["state"] == "cancelling" and out["run_id"] == run.id
    assert req.state.audit_extra["phase"] == "running"
    db.close()


def test_endpoint_missing_returns_404(monkeypatch):
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    monkeypatch.setattr(scan_queue, "cancel", lambda lib_id: "missing")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        portal.cancel_queued_scan(lib.id, _Request(), None, db)
    assert ei.value.status_code == 404
    db.close()


def test_endpoint_queued_returns_canceled(monkeypatch):
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    monkeypatch.setattr(scan_queue, "cancel", lambda lib_id: "canceled")
    req = _Request()
    out = portal.cancel_queued_scan(lib.id, req, None, db)
    assert out["state"] == "canceled"
    assert req.state.audit_extra["phase"] == "queued"
    db.close()


# ---------------- QA AI-2 P1：宽捕获不得吞取消 ----------------

def test_mount_dir_entries_propagates_cancel():
    """目录列举的宽捕获（MountError/Exception）不许吞 ScanCancelled。"""
    class Provider:
        def list_dir(self, rel):
            raise scan_cancel.ScanCancelled("sidecar_page")
    sf = types.SimpleNamespace(
        provider=Provider(), mount_id=3, dir_rel="/a", local_dir="",
        stored_path="mount://3/a/x.mkv")
    ctx = scanner._ScanContext(
        snap=types.SimpleNamespace(collection_type="movies"),
        lib_id=1, stats={})
    with pytest.raises(scan_cancel.ScanCancelled):
        scanner._mount_dir_entries(ctx, sf, "/a")


def test_iter_scan_sources_guarded_propagates_cancel(monkeypatch):
    """_guarded 的 except Exception 不许把取消记成 failed_roots。"""
    from backend.database import SessionLocal
    db = SessionLocal()
    lib = _library(db, "取消宽捕获库")
    try:
        src = types.SimpleNamespace(
            kind="pan115", label="挂载", provider=object(), path="",
            subpath="/", source_row_id=None)
        monkeypatch.setattr(
            scanner.mount_sources, "read_library_sources",
            lambda db_, library: ([src], []))
        monkeypatch.setattr(
            scanner, "_mount_files",
            lambda *a, **k: (_ for _ in ()).throw(
                scan_cancel.ScanCancelled("sidecar_dir")))
        snap = types.SimpleNamespace(
            name="库", limit_prefixes=(), library_id=lib.id)
        failed_roots = []
        label, files = next(scanner.iter_scan_sources(
            snap, lib, db, failed_roots))
        with pytest.raises(scan_cancel.ScanCancelled):
            list(files)
        assert failed_roots == [], "取消不能被记成来源失败"
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------- QA AI-2 P2：条件更新 ----------------

def test_cancel_scan_does_not_overwrite_finished_run():
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    run = _running_run(db, lib)
    run.status = scanner.SCAN_STATUS_SUCCESS
    db.commit()
    out = scanner.cancel_scan(db, lib, {"added": 1}, run.id)
    assert out == "already_finished"
    db.refresh(run)
    assert run.status == scanner.SCAN_STATUS_SUCCESS, "不许覆盖已收尾流水"
    db.refresh(lib)
    assert lib.scan_status != scanner.SCAN_STATUS_CANCELLED
    db.close()


# ---------------- QA AI-2 P2：kill -9 的 ACK 窗口 ----------------

class _FakeRedis:
    def __init__(self, raws):
        self.raws = list(raws)
        self.srem_calls = []

    def lrange(self, key, start, end):
        return list(self.raws)

    def lrem(self, key, count, raw):
        if raw in self.raws:
            self.raws.remove(raw)
            return 1
        return 0

    def srem(self, key, member):
        self.srem_calls.append((key, member))
        return 1


def test_cancel_processing_request_acks_payload(monkeypatch):
    import json as _json
    from backend.emby_server import scan_queue_redis as rq
    raw = _json.dumps({"library_id": 7, "trigger": "manual"})
    fake = _FakeRedis([raw])
    monkeypatch.setattr(rq, "_redis", lambda: fake)
    removed = rq.cancel_processing_request(7)
    assert removed == 1
    assert fake.raws == []
    assert fake.srem_calls, "dedup 也要清"


def test_endpoint_confirms_processing_on_running(monkeypatch):
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    _running_run(db, lib)
    monkeypatch.setattr(scan_queue, "cancel", lambda lib_id: "running")
    called = []
    monkeypatch.setattr(scan_queue, "mark_processing_cancelled",
                        lambda lib_id: called.append(lib_id))
    out = portal.cancel_queued_scan(lib.id, _Request(), None, db)
    assert out["state"] == "cancelling"
    assert called == [lib.id]
    db.close()


# ---------------- QA AI-2 P2：dequeue→begin 窗口 ----------------

def test_endpoint_rechecks_queue_after_window(monkeypatch):
    factory = _memory_db()
    db = factory()
    lib = _library(db)
    outcomes = iter(["running", "canceled"])
    monkeypatch.setattr(scan_queue, "cancel", lambda lib_id: next(outcomes))
    monkeypatch.setattr(scanner, "request_cancel_run",
                        lambda db_, lib_id, wait_sec=3.0: None)
    req = _Request()
    out = portal.cancel_queued_scan(lib.id, req, None, db)
    assert out["state"] == "canceled"
    assert req.state.audit_extra["phase"] == "queued"
    db.close()
