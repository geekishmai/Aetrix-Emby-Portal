"""公网 /api/health 只回状态：内部指标（115 调用计数、版本、体检明细）只给本机监控。"""
from __future__ import annotations

import asyncio

from starlette.requests import Request

import backend.main as main


def _request(headers: dict) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/api/health", "headers": raw})


def _fake_health():
    return {"status": "healthy", "level": "ok", "issues": [], "metrics": {"database": "ok"}}


def test_public_request_gets_status_only(monkeypatch):
    monkeypatch.setattr(main, "_collect_health", _fake_health)
    body = asyncio.run(main.health_check(_request({"X-Aetrix-Public": "1"})))
    assert set(body) == {"status", "timestamp"}
    assert body["status"] == "healthy"


def test_local_request_keeps_full_metrics(monkeypatch):
    monkeypatch.setattr(main, "_collect_health", _fake_health)
    body = asyncio.run(main.health_check(_request({})))
    assert "pan115_calls" in body and "health_metrics" in body
