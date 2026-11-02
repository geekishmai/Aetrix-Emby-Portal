# -*- coding: utf-8 -*-
"""Pan115Client 改名口回归：改名走 webapi files/edit。
_http_json 一律桩替身，不连真实 115。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import transfer115

COOKIE = "UID=987654_ab12cd; CID=abc; SEID=def"


def _client(monkeypatch, handler):
    calls = []

    def fake_http(method, url, *, params=None, data=None, cookie="",
                  timeout=30, ua=""):
        calls.append({"method": method, "url": url, "params": params,
                      "data": data})
        return handler(calls[-1])

    monkeypatch.setattr(transfer115, "_http_json", fake_http)
    return transfer115.Pan115Client(COOKIE), calls


def test_rename_file_payload(monkeypatch):
    def handler(call):
        assert call["url"].endswith("/files/edit")
        assert call["data"] == {"fid": "f9", "file_name": "relay.mkv"}
        return {"state": True}

    client, _ = _client(monkeypatch, handler)
    assert client.rename_file("f9", "relay.mkv")["state"] is True
