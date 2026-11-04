"""115 接口调用计数：按接口分类累计到 Redis 哈希，跨进程可读；Redis 不可用时不影响调用。"""
from __future__ import annotations

import pytest

from backend import database
from backend.emby_server import transfer115


class _FakeRedis:
    def __init__(self):
        self.hashes: dict = {}

    def pipeline(self):
        return _FakePipe(self)

    def hgetall(self, key):
        return {k.encode(): str(v).encode() for k, v in self.hashes.get(key, {}).items()}


class _FakePipe:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def hincrby(self, key, field, amount):
        self.ops.append((key, field, amount))

    def execute(self):
        for key, field, amount in self.ops:
            bucket = self.redis.hashes.setdefault(key, {})
            bucket[field] = bucket.get(field, 0) + amount


@pytest.fixture()
def fake_redis(monkeypatch):
    monkeypatch.setenv("PAN115_GUARD_ENABLED", "0")
    fake = _FakeRedis()
    monkeypatch.setattr(database, "get_redis", lambda: fake)
    return fake


def _client(monkeypatch, fail_urls=()):
    def fake_http_json(method, url, **kwargs):
        if any(part in url for part in fail_urls):
            raise transfer115.Pan115Error("上游挂了")
        return {"state": True}

    monkeypatch.setattr(transfer115, "_http_json", fake_http_json)
    return transfer115.Pan115Client("UID=x", ua="t-ua")


def test_calls_counted_by_kind(fake_redis, monkeypatch):
    client = _client(monkeypatch)
    client._call("GET", f"{transfer115.PAN115_WEBAPI_BASE}/files", params={"cid": "0"})
    client._call("GET", f"{transfer115.PAN115_WEBAPI_BASE}/files", params={"cid": "1"})
    client._call("POST", f"{transfer115.PAN115_PROAPI_BASE}/app/chrome/downurl")
    client._call("GET", f"{transfer115.PAN115_WEBAPI_BASE}/files/download", params={"pickcode": "p"})
    client._call("POST", f"{transfer115.PAN115_WEBAPI_BASE}/files/add")
    assert transfer115.call_stats() == {"list": 2, "download_url": 2, "other": 1, "total": 5}


def test_errors_counted_and_reraised(fake_redis, monkeypatch):
    client = _client(monkeypatch, fail_urls=("downurl",))
    with pytest.raises(transfer115.Pan115Error):
        client._call("POST", f"{transfer115.PAN115_PROAPI_BASE}/app/chrome/downurl")
    assert transfer115.call_stats() == {"download_url": 1, "total": 1, "errors": 1, "errors_download_url": 1}


def test_redis_unavailable_does_not_break_calls(monkeypatch):
    monkeypatch.setenv("PAN115_GUARD_ENABLED", "0")
    monkeypatch.setattr(database, "get_redis", lambda: None)
    client = _client(monkeypatch)
    assert client._call("GET", f"{transfer115.PAN115_WEBAPI_BASE}/files") == {"state": True}
    assert transfer115.call_stats() == {}
