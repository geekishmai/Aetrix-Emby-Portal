# -*- coding: utf-8 -*-
"""Redis 订阅与熔断器隔离（2026-11-03）。

根因：转存工人的唤醒订阅复用 ``get_redis()``（主客户端 socket_timeout=1s）
→ ``pubsub.listen()`` 每 1s 抛 TimeoutError、每轮泄漏未关闭的 pubsub 连接
→ 连接池耗尽后主链路命令失败 → ``redis_call`` 连续记失败 → 熔断器打开
（REDIS_BREAKER_FAILURES=3）→ ``get_redis()`` 返回 None → pan115_guard 退回
进程内限速，三进程叠加超账号预算（实测 4.4 次/秒）。

修复：订阅改用 ``make_redis_subscriber_client()``——独立客户端、
``socket_timeout=None``（阻塞读不超时）、独立小连接池（2 条）、
**不参与熔断计数**；每轮显式 ``pubsub.close()``。
"""
import sys
import threading
import time

import pytest

sys.path.insert(0, ".")
from backend import database  # noqa: E402


class _FakePubSub:
    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.closed = 0
        self.subscribed = []

    def subscribe(self, channel):
        self.subscribed.append(channel)

    def listen(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("Timeout reading from socket")
        # 成功路径：给出一条消息后阻塞到 stop（用空转等待）
        yield {"type": "message", "data": "wake"}
        while True:
            time.sleep(0.05)
            yield {"type": "message", "data": "idle"}

    def close(self):
        self.closed += 1


class _FakeClient:
    def __init__(self, pubsub):
        self._pubsub = pubsub

    def pubsub(self):
        return self._pubsub


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(database, "redis_breaker", database.RedisBreaker())
    yield


def test_subscriber_uses_dedicated_client_not_get_redis(monkeypatch):
    """订阅线程不得调用 get_redis（不参与熔断门控）。"""
    pubsub = _FakePubSub(fail_times=1)
    calls = {"get_redis": 0, "dedicated": 0}

    def boom_get_redis():
        calls["get_redis"] += 1
        raise AssertionError("订阅线程不应调用 get_redis（会受熔断门控）")

    def make_dedicated():
        calls["dedicated"] += 1
        return _FakeClient(pubsub)

    monkeypatch.setattr(database, "get_redis", boom_get_redis)
    monkeypatch.setattr(database, "make_redis_subscriber_client", make_dedicated)

    from backend.emby_server import transfer_variant as tv
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    tv._wake_subscriber_loop(stop)

    assert calls["get_redis"] == 0
    assert calls["dedicated"] >= 1
    assert pubsub.closed >= 1, "每轮必须显式关闭 pubsub（否则连接池泄漏）"


def test_subscriber_timeout_does_not_trip_breaker(monkeypatch):
    """订阅 TimeoutError 不推进熔断器：连续多次也不影响 get_redis。"""
    pubsub = _FakePubSub(fail_times=5)

    monkeypatch.setattr(database, "make_redis_subscriber_client",
                        lambda: _FakeClient(pubsub))
    # get_redis 仍应可用（breaker 未被订阅失败推进）
    sentinel = object()
    monkeypatch.setattr(database, "redis_client", sentinel)

    from backend.emby_server import transfer_variant as tv
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    tv._wake_subscriber_loop(stop)

    assert database.redis_breaker._consecutive == 0
    assert database.get_redis() is sentinel


def test_subscriber_client_has_no_read_timeout(monkeypatch):
    """独立客户端：socket_timeout=None、独立小池、不参与熔断。"""
    captured = {}

    class _FakeRedis:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(database, "REDIS_ENABLED", True)
    import redis as _redis
    monkeypatch.setattr(_redis, "from_url",
                        lambda url, **kw: _FakeRedis(**kw))
    database.make_redis_subscriber_client()
    assert captured.get("socket_timeout") is None
    assert captured.get("max_connections") == 2
    assert captured.get("decode_responses") is True


def test_subscriber_client_none_when_redis_disabled(monkeypatch):
    monkeypatch.setattr(database, "REDIS_ENABLED", False)
    assert database.make_redis_subscriber_client() is None
