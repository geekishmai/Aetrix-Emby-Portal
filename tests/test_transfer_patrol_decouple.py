# -*- coding: utf-8 -*-
"""转存巡检与认领主循环解耦（2026-11-02 生产实证）。

生产故障：worker 15:37 CST 部署重启后，守护主循环首轮即跑全量
巡检（state 初值使巡检在启动首轮到期），巡检逐行打上游活体请求
（610 行变体、逐行多次调用、单调用超时 20 秒级），主循环卡在
115/夸克巡检行 的 httpx 调用里 12 分钟以上
（py-spy 两次采样实证），期间认领 0 单、8 单 due 无人认领；
同日多次部署重启反复触发。租约回收 2026-10-31 已按同口径独立
成线程（「回收不再搭认领/主循环迭代的车」），巡检本单照此办理。

本文件钉死三条：
1. 巡检卡死（阻塞不返回）时，主循环仍能认领并完成新到的
   playback 单——巡检慢/卡只拖巡检自己，绝不占住认领主链路。
2. 巡检独立线程 _patrol_loop：启动即跑一轮、按间隔续跑，单轮
   抛错只跳过本轮、不许炸线程。
3. 总闸关时巡检不跑（与原主循环内巡检的门控口径一致）。

全部离线：SQLite :memory: + 桩 drive_api；巡检打桩为阻塞/计数
替身；长等待用封顶 Event 加速（真实间隔不变）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time
from types import SimpleNamespace

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
def _reset_pause_and_wake(monkeypatch):
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


class _CappedEvent(threading.Event):
    """wait 超时封顶的 Event：巡检线程的每日间隔等待在测试里
    按 0.5 秒跑，真实间隔不变；is_set/set 语义与标准 Event 一致。"""

    def wait(self, timeout=None):  # noqa: D102 - 测试替身
        if timeout is None:
            return super().wait()
        return super().wait(min(timeout, 0.5))


def _job(db, item_id=1, trigger="playback", owner_ref="u9"):
    row = em.TransferJob(item_id=item_id, trigger=trigger,
                         owner_scope="user", owner_ref=owner_ref,
                         status="pending")
    db.add(row)
    db.commit()
    return row


def _fake_run_done(db, job, factory):
    job.status = "done"
    db.commit()
    return {"outcome": "done"}


def test_main_loop_claims_job_while_patrol_blocked(db_factory, monkeypatch):
    """巡检阻塞不返回时，主循环仍须认领轮内新建的 playback 单。

    复现 2026-11-02 现场：工人重启首轮巡检占住主循环十几分钟，
    期间新建的单（job 2160 同款处境）无人认领。巡检已被解耦时
    本单应在数秒内被认领完成；未解耦（巡检仍在主循环里）时
    主循环卡在巡检内，断言超时变红。
    """
    monkeypatch.setattr(tv, "master_enabled", lambda: True)
    monkeypatch.setattr(tv, "run_transfer_job_once", _fake_run_done)
    patrol_release = threading.Event()
    patrol_started = threading.Event()

    def _blocking_patrol(db, factory):
        patrol_started.set()
        patrol_release.wait(timeout=30)
        return {}

    monkeypatch.setattr(tv, "patrol_once", _blocking_patrol)
    stop = _CappedEvent()
    state = {"last_patrol": 0.0, "last_follow_sweep": 0.0,
             "last_idle_cleanup": 0.0}
    threads = []
    if hasattr(tv, "_patrol_loop"):
        patrol_thread = threading.Thread(
            target=tv._patrol_loop,
            args=(stop, db_factory, lambda: SimpleNamespace()),
            daemon=True)
        patrol_thread.start()
        threads.append(patrol_thread)
    main = threading.Thread(
        target=tv._daemon_main_loop,
        args=(stop, state, db_factory, lambda: SimpleNamespace()),
        daemon=True)
    main.start()
    threads.append(main)
    try:
        # 等主循环跑完首轮空转进入等待态（解耦时巡检线程应已
        # 进入阻塞的巡检），再建单模拟轮内到达并唤醒主循环。
        time.sleep(1.0)
        if hasattr(tv, "_patrol_loop"):
            assert patrol_started.wait(timeout=5)
        session = db_factory()
        job = _job(session)
        job_id = job.id
        session.close()
        tv.notify_transfer_wakeup(job_id)
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            check = db_factory()
            try:
                row = (check.query(em.TransferJob)
                       .filter(em.TransferJob.id == job_id).one())
                status = row.status
            finally:
                check.close()
            if status == "done":
                break
            time.sleep(0.1)
        assert status == "done"
    finally:
        patrol_release.set()
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)


def test_patrol_loop_repeats_and_survives_errors(db_factory, monkeypatch):
    """巡检独立线程：启动先跑一轮、按间隔续跑，单轮抛错不炸线程。"""
    monkeypatch.setattr(tv, "master_enabled", lambda: True)
    calls = []

    def _flaky_patrol(db, factory):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(tv, "patrol_once", _flaky_patrol)
    stop = _CappedEvent()
    thread = threading.Thread(
        target=tv._patrol_loop,
        args=(stop, db_factory, lambda: SimpleNamespace()),
        daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.05)
        assert len(calls) >= 2
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_patrol_loop_respects_master_switch(db_factory, monkeypatch):
    """总闸关时巡检不跑（原主循环内巡检只在总闸开时执行的口径）。"""
    monkeypatch.setattr(tv, "master_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(tv, "patrol_once",
                        lambda db, factory: calls.append(1) or {})
    stop = _CappedEvent()
    thread = threading.Thread(
        target=tv._patrol_loop,
        args=(stop, db_factory, lambda: SimpleNamespace()),
        daemon=True)
    thread.start()
    try:
        time.sleep(1.2)
        assert calls == []
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
