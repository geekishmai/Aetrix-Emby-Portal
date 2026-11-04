# -*- coding: utf-8 -*-
"""健康检查 fail-closed v3（2026-11-04 服务器实测后定稿）。

服务器实测前提（Claude 提供）：``127.0.0.1:18000`` 映射的是 aetrix-web 的
nginx，不是 api。nginx 对 ``/api/`` 一律设置
``X-Forwarded-For: $proxy_add_x_forwarded_for`` → 本机监控（scan-watch /
aetrix-daily / verify-library）经 18000 访问时**一定带 XFF**（值为 docker 网关
172.21.0.1），api 看到的直连对端是 web 容器（172.21.0.6）。

判定顺序（两端点同口径）：
1. 有效管理员凭据 → 完整内容（管理后台经公网访问系统健康页）；
2. 带 ``X-Aetrix-Public`` → 只回 ``{status, health_level}``；
3. 否则取来源链（直连对端 IP + XFF 每一跳），**全部**落在可信网段
   （默认 127.0.0.0/8、::1、compose 子网 172.21.0.0/16，可用
   ``HEALTH_FULL_CIDRS`` 覆盖）→ 完整内容；任何一跳是公网 IP → 只回状态。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from starlette.requests import Request

import backend.main as main
from backend import security
from backend.models import WebUser

MINIMAL = {"status", "health_level"}

# 生产 compose 子网内的两个角色：nginx（web）与 api
WEB_IP = "172.21.0.6"
GATEWAY_IP = "172.21.0.1"


def _request(*, host="203.0.113.9", headers=None, path="/api/health") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": raw, "client": (host, 54321),
    })


def _fake_health():
    return {"status": "healthy", "level": "ok", "issues": [],
            "metrics": {"database": "ok"}}


@pytest.fixture()
def fake_health(monkeypatch):
    monkeypatch.setattr(main, "_collect_health", _fake_health)
    monkeypatch.setattr(main, "_collect_observability",
                        lambda: {"enabled": False})
    monkeypatch.setattr(main, "_runtime_report", lambda: {})
    monkeypatch.setattr(main, "_redis_status", lambda: {"ok": True})
    monkeypatch.setattr(
        "backend.emby_server.transfer115.call_stats", lambda: {"total": 1})
    monkeypatch.delenv("HEALTH_FULL_CIDRS", raising=False)
    return _fake_health


def _admin(monkeypatch, *, is_staff=True, active=True):
    user = WebUser(id=7, username="admin", password_hash="x",
                   is_staff=is_staff, is_active=active, token_version=0)
    payload = {"sub": "7", "type": "access", "tv": 0, "jti": "j1",
               "exp": datetime.now() + timedelta(hours=1)}

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return user

    class _DB:
        def query(self, *a, **k):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(main, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(security, "decode_token",
                        lambda token, expected_type=None: payload)
    monkeypatch.setattr(security, "enforce_token_freshness",
                        lambda db, payload, user: None)
    return {"Authorization": "Bearer admin-jwt"}


def _full(body):
    return "pan115_calls" in body and "version" in body


# ---------------- 1. 本机经 nginx（XFF 只有网关地址） ----------------

def test_local_via_nginx_keeps_full(fake_health):
    """本机监控经 18000→nginx→api：直连对端=web 容器，XFF=网关，全可信。"""
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP, headers={"X-Forwarded-For": GATEWAY_IP})))
    assert _full(body)


def test_local_via_nginx_multi_internal_hops_keep_full(fake_health):
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP,
        headers={"X-Forwarded-For": f"{GATEWAY_IP}, {WEB_IP}"})))
    assert _full(body)


def test_direct_loopback_without_proxy_headers_keeps_full(fake_health):
    body = asyncio.run(main.health_check(_request(host="127.0.0.1")))
    assert _full(body)


def test_direct_ipv6_loopback_keeps_full(fake_health):
    body = asyncio.run(main.health_check(_request(host="::1")))
    assert _full(body)


# ---------------- 2. 公网经 Caddy + nginx ----------------

def test_public_via_caddy_marker_gets_minimal(fake_health):
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP,
        headers={"X-Aetrix-Public": "1",
                 "X-Forwarded-For": f"203.0.113.9, {GATEWAY_IP}"})))
    assert set(body) == MINIMAL


def test_public_xff_contains_public_ip_gets_minimal(fake_health):
    """Caddy 标记头丢失时，XFF 里的真实客户端公网 IP 仍让判定落到最小。"""
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP, headers={"X-Forwarded-For": "203.0.113.9"})))
    assert set(body) == MINIMAL


def test_public_xff_with_port_suffix_gets_minimal(fake_health):
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP, headers={"X-Forwarded-For": "203.0.113.9:54321"})))
    assert set(body) == MINIMAL


# ---------------- 3. 伪造 XFF（只填内网 IP） ----------------

def test_forged_internal_xff_from_public_peer_gets_minimal(fake_health):
    """直连对端是公网 IP、XFF 伪造成内网：仍只回状态（不能靠伪造提权）。"""
    body = asyncio.run(main.health_check(_request(
        host="203.0.113.9",
        headers={"X-Forwarded-For": f"{GATEWAY_IP}, {WEB_IP}"})))
    assert set(body) == MINIMAL


def test_forged_public_marker_never_upgrades(fake_health):
    """任何来源带 X-Aetrix-Public → 最小（伪造不构成提权）。"""
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP,
        headers={"X-Aetrix-Public": "1",
                 "X-Forwarded-For": GATEWAY_IP})))
    assert set(body) == MINIMAL


# ---------------- 4. 管理员凭据 ----------------

def test_admin_token_over_public_path_keeps_full(fake_health, monkeypatch):
    headers = _admin(monkeypatch)
    headers.update({"X-Aetrix-Public": "1",
                    "X-Forwarded-For": "203.0.113.9"})
    body = asyncio.run(main.health_check(_request(host=WEB_IP, headers=headers)))
    assert _full(body)


def test_non_admin_token_gets_minimal(fake_health, monkeypatch):
    """非管理员凭据 + 公网链路（XFF 含公网 IP）→ 只回状态。"""
    headers = _admin(monkeypatch, is_staff=False)
    headers["X-Forwarded-For"] = "203.0.113.9"
    body = asyncio.run(main.health_check(
        _request(host=WEB_IP, headers=headers)))
    assert set(body) == MINIMAL


def test_inactive_admin_token_gets_minimal(fake_health, monkeypatch):
    headers = _admin(monkeypatch, active=False)
    headers["X-Forwarded-For"] = "203.0.113.9"
    body = asyncio.run(main.health_check(
        _request(host=WEB_IP, headers=headers)))
    assert set(body) == MINIMAL


def test_invalid_token_gets_minimal(fake_health):
    body = asyncio.run(main.health_check(_request(
        host=WEB_IP,
        headers={"Authorization": "Bearer not-a-jwt",
                 "X-Forwarded-For": "203.0.113.9"})))
    assert set(body) == MINIMAL


# ---------------- 5. HEALTH_FULL_CIDRS 白名单 ----------------

def test_env_cidrs_add_custom_network(fake_health, monkeypatch):
    monkeypatch.setenv("HEALTH_FULL_CIDRS", "100.64.0.0/10")
    body = asyncio.run(main.health_check(_request(host="100.64.1.2")))
    assert _full(body)


def test_env_cidrs_replace_defaults(fake_health, monkeypatch):
    """env 覆盖默认：只列回环时，compose 子网不再可信。"""
    monkeypatch.setenv("HEALTH_FULL_CIDRS", "127.0.0.0/8")
    assert set(asyncio.run(main.health_check(_request(
        host=WEB_IP, headers={"X-Forwarded-For": GATEWAY_IP})))) == MINIMAL
    assert _full(asyncio.run(main.health_check(_request(host="127.0.0.1"))))


def test_env_cidrs_invalid_entries_ignored(fake_health, monkeypatch):
    monkeypatch.setenv("HEALTH_FULL_CIDRS", "not-a-cidr,10.9.0.0/16")
    assert _full(asyncio.run(main.health_check(_request(host="10.9.1.2"))))
    assert set(asyncio.run(main.health_check(_request(host="10.8.1.2")))) == MINIMAL


# ---------------- 6. detailed 端点同口径 ----------------

def test_detailed_public_gets_minimal(fake_health):
    body = main.detailed_health_check(_request(path="/api/health/detailed"))
    assert set(body) == MINIMAL


def test_detailed_caddy_path_gets_minimal(fake_health):
    body = main.detailed_health_check(_request(
        host=WEB_IP, path="/api/health/detailed",
        headers={"X-Aetrix-Public": "1"}))
    assert set(body) == MINIMAL


def test_detailed_local_via_nginx_keeps_full(fake_health):
    body = main.detailed_health_check(_request(
        host=WEB_IP, path="/api/health/detailed",
        headers={"X-Forwarded-For": GATEWAY_IP}))
    assert "services" in body


def test_detailed_admin_over_public_keeps_full(fake_health, monkeypatch):
    headers = _admin(monkeypatch)
    headers.update({"X-Aetrix-Public": "1", "X-Forwarded-For": "203.0.113.9"})
    body = main.detailed_health_check(_request(
        host=WEB_IP, path="/api/health/detailed", headers=headers))
    assert "services" in body
