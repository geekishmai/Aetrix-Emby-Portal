# -*- coding: utf-8 -*-
"""转存工人唤醒订阅断裂时的认领兜底（2026-11-02 生产实证）。

生产故障：worker 04:56 重启后，唤醒订阅因 Redis 客户端 socket_timeout
（1 秒）每次 listen 都抛 TimeoutError，订阅线程每约 31 秒断开重连，
期间新单的即时认领通道事实上失效；主循环只剩 60 秒兜底扫。实测
认领节奏：启动突发后停 4–7 分钟一轮，跨槽 3 槽空着、13 单 pending
无人认领，user 1329 的 playback 单 2123 等了 26 分钟以上。

本文件钉死两条：
1. 订阅线程一次订阅失败（抛错）也必须唤醒主循环跑认领——断连
   本身就是一次认领触发信号，不能只等 30 秒后重连再说。
2. 端到端：订阅已断裂时，主循环仍应在数秒内认领到轮内新建的
   playback 单，而不是等满 60 秒兜底扫。

全部离线：SQLite :memory: + 桩 drive_api + 桩 Redis 客户端；
订阅重连等待用封顶 Event 加速（真实间隔 30 秒不变）。
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

from backend import database as dbmod
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


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _reset_pause_and_wake(monkeypatch):
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


class _CappedEvent(threading.Event):
    """wait 超时封顶的 Event：订阅重连的 30 秒等待在测试里按
    0.5 秒跑，真实间隔不变；is_set/set 语义与标准 Event 一致。"""

    def wait(self, timeout=None):  # noqa: D102 - 测试替身
        if timeout is None:
            return super().wait()
        return super().wait(min(timeout, 0.5))


class _BrokenPubSub:
    """每次 listen 都抛 TimeoutError 的订阅替身：复现生产里
    socket_timeout=1 秒的 Redis 客户端行为。"""

    def subscribe(self, *args, **kwargs):
        return None

    def listen(self):
        raise TimeoutError("timed out")


class _BrokenRedisClient:
    def pubsub(self):
        return _BrokenPubSub()


def _job(db, item_id=1, trigger="playback", owner_ref="u9"):
    row = em.TransferJob(item_id=item_id, trigger=trigger,
                         owner_scope="user", owner_ref=owner_ref,
                         status="pending")
    db.add(row)
    db.commit()
    return row


def test_wake_subscriber_failure_wakes_main_loop(monkeypatch):
    """订阅失败也要唤醒：listen 抛错后，进程内唤醒 Event 必须置位，
    主循环下一轮即跑认领，而不是等 30 秒重连后再说。"""
    monkeypatch.setattr(dbmod, "get_redis",
                        lambda: _BrokenRedisClient())
    stop = _CappedEvent()
    thread = threading.Thread(target=tv._wake_subscriber_loop, args=(stop,),
                              daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not tv._WAKE_EVENT.is_set():
            time.sleep(0.05)
        assert tv._WAKE_EVENT.is_set() is True
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_daemon_claims_late_job_while_subscription_broken(
        db_factory, monkeypatch):
    """端到端：订阅已断裂时，主循环应在数秒内认领轮内新建的单。

    复现 2026-11-02 现场：工人已在岗、订阅断裂、兜底扫 60 秒；
    轮内新建的 playback 单（job 2123 同款处境）必须在 10 秒内被
    认领，而不是等满 60 秒扫到。执行本身打桩为 done，只测认领。
    """
    monkeypatch.setattr(dbmod, "get_redis",
                        lambda: _BrokenRedisClient())
    monkeypatch.setattr(tv, "master_enabled", lambda: True)

    def _fake_run(db, job, factory):
        job.status = "done"
        db.commit()
        return {"outcome": "done"}

    monkeypatch.setattr(tv, "run_transfer_job_once", _fake_run)
    stop = _CappedEvent()
    state = {"last_patrol": 0.0, "last_follow_sweep": 0.0,
             "last_idle_cleanup": 0.0}
    subscriber = threading.Thread(target=tv._wake_subscriber_loop,
                                  args=(stop,), daemon=True)
    main = threading.Thread(
        target=tv._daemon_main_loop,
        args=(stop, state, db_factory, lambda: SimpleNamespace()),
        daemon=True)
    subscriber.start()
    main.start()
    try:
        # 等主循环跑完首轮空转、进入等待态，再建单（轮内到达）。
        time.sleep(1.0)
        session = db_factory()
        job = _job(session)
        job_id = job.id
        session.close()
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
        stop.set()
        subscriber.join(timeout=5)
        main.join(timeout=5)
    assert not subscriber.is_alive()
    assert not main.is_alive()
