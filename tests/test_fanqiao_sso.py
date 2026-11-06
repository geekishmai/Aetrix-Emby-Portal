# -*- coding: utf-8 -*-
"""帆桥影院 SSO 与资格同步 Webhook 完整回归测试（离线内存 SQLite，零外部网络依赖）。

覆盖契约（面板侧 a85d3e0 对齐）：
1. 共享密钥读取（SystemConfig 优先于 env，长度 ≥32 门禁）与日志/响应零泄露；
2. GET /sso/fanqiao：payload 原始串 HMAC-SHA256 验签、exp ≤60s 硬上限、过期拒绝、
   Redis SET NX EX 原子防重放（及 Redis 故障 fail-closed）；
3. 首次 SSO 自动建户（无邮箱、独立 fanqiao_identities 表不占 telegram_id）、赋公益资格与查看行、
   跳首页带首次设置接线账号引导（/?setup_emby=1），已设接线用户名后直跳 /；
4. POST /api/integrations/fanqiao/entitlements：原始字节流 HMAC 小写 hex、sent_at ±300s、
   单批 ≤200 条、未知 sub 占位映射与后续 SSO 转正、重试幂等；
5. tier 变化（standard -> flagship -> none）、到期禁播（含 free 公益服下拦截播放并吊销 Emby 令牌）、
   banned=True 独立最高优先级禁播禁登、以及密钥轮换映射表整体清空重建。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import fanqiao_sso, log_redact, models, register_channel, subscriptions
from backend.api.fanqiao import fanqiao_router
from backend.database import get_db
from backend.emby_server import models as emby_models
from backend.integrations import store

TEST_SECRET = "fanqiao_test_shared_secret_at_least_32_bytes_long_2026"
OTHER_SECRET = "another_different_shared_secret_over_32_bytes_long_999"


def _sub_of(uid: int, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), str(uid).encode("utf-8"), hashlib.sha256).hexdigest()


def _sign_body(raw_body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


@pytest.fixture()
def env_setup(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    emby_models.Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    seed = TestingSession()
    try:
        # 默认创建一个 free（公益服）以验证即使在公益服下，帆桥到期/封禁用户也会被严格禁播
        seed.add(models.ServerRealm(name="帆桥影院", slug="main", access_mode="free", is_active=True))
        seed.add(models.SystemConfig(key="fanqiao_shared_secret", value=TEST_SECRET))
        seed.commit()
    finally:
        seed.close()

    monkeypatch.setattr(fanqiao_sso, "get_redis", lambda: None)
    fanqiao_sso._mem_nonces.clear()
    fanqiao_sso._mem_handoffs.clear()
    store.invalidate()

    app = FastAPI()
    app.include_router(fanqiao_router)

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    try:
        yield client, TestingSession, app
    finally:
        fanqiao_sso._mem_nonces.clear()
        fanqiao_sso._mem_handoffs.clear()
        store.invalidate()
        engine.dispose()


def test_secret_loading_min_length_and_zero_leak(env_setup, monkeypatch):
    client, SessionCls, _ = env_setup
    now = int(time.time())
    t_valid = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": _sub_of(1), "tier": "standard", "exp": now + 30, "nonce": "a" * 32},
        TEST_SECRET,
    )

    # 1) SystemConfig 过短且 env 缺失 -> 503 且响应体绝不含密钥片段
    db = SessionCls()
    try:
        cfg = db.query(models.SystemConfig).filter_by(key="fanqiao_shared_secret").one()
        cfg.value = "too_short_secret_123"
        db.commit()
    finally:
        db.close()
    monkeypatch.delenv("FANQIAO_SHARED_SECRET", raising=False)

    r_short = client.get(f"/sso/fanqiao?ticket={t_valid}", follow_redirects=False)
    assert r_short.status_code == 503
    assert "too_short_secret" not in r_short.text

    # 2) 清空 SystemConfig 后自动回退读取环境变量 FANQIAO_SHARED_SECRET
    db = SessionCls()
    try:
        db.query(models.SystemConfig).filter_by(key="fanqiao_shared_secret").delete()
        db.commit()
    finally:
        db.close()
    monkeypatch.setenv("FANQIAO_SHARED_SECRET", TEST_SECRET)

    r_env_ok = client.get(f"/sso/fanqiao?ticket={t_valid}", follow_redirects=False)
    assert r_env_ok.status_code == 302
    assert TEST_SECRET not in r_env_ok.text

    # 3) 日志脱敏验证：URL 查询串里的 ticket= 与 JSON 里的 fanqiao_shared_secret 均被替换为 ***
    redacted_url = log_redact.redact_secrets(f"GET /sso/fanqiao?ticket={t_valid} HTTP/1.1")
    assert t_valid not in redacted_url
    assert "ticket=***" in redacted_url
    redacted_cfg = log_redact.redact_secrets(f'{{"fanqiao_shared_secret": "{TEST_SECRET}"}}')
    assert TEST_SECRET not in redacted_cfg


def test_sso_ticket_sig_expiry_60s_cap_and_redis_replay(env_setup, monkeypatch):
    client, _SessionCls, _ = env_setup
    now = int(time.time())
    sub = _sub_of(42)

    # 1) 签名错误（错误密钥 / 篡改 payload）-> 401
    bad_sig_ticket = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": now + 30, "nonce": "1" * 32},
        OTHER_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={bad_sig_ticket}", follow_redirects=False).status_code == 401

    good_ticket = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": now + 30, "nonce": "2" * 32},
        TEST_SECRET,
    )
    p_part, s_part = good_ticket.split(".")
    tampered_payload = fanqiao_sso._b64url_encode_nopad(
        json.dumps({"sub": sub, "tier": "flagship", "exp": now + 30, "nonce": "2" * 32}).encode("utf-8")
    )
    assert client.get(f"/sso/fanqiao?ticket={tampered_payload}.{s_part}", follow_redirects=False).status_code == 401
    assert client.get(f"/sso/fanqiao?ticket={p_part}=.{s_part}", follow_redirects=False).status_code == 401

    # 2) 票据已过期（exp <= now）与超过 60 秒安全上限（exp = now + 61）均拒绝 401
    expired_ticket = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": now - 1, "nonce": "3" * 32},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={expired_ticket}", follow_redirects=False).status_code == 401

    over_60_ticket = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": now + 65, "nonce": "4" * 32},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={over_60_ticket}", follow_redirects=False).status_code == 401

    # 3) 真实 Redis SET NX EX 语义检验：首次放行 302，重放同一票据拒绝 401，Redis 抛错时 fail-closed 503
    redis_kv: dict[str, tuple[str, int]] = {}

    class FakeRedis:
        def set(self, key: str, val: str, nx: bool = False, ex: int = 0):
            if nx and key in redis_kv:
                return False
            redis_kv[key] = (val, ex)
            return True

        def setex(self, key: str, ttl: int, val: str):
            redis_kv[key] = (val, ttl)

        def getdel(self, key: str):
            item = redis_kv.pop(key, None)
            return item[0] if item else None

    monkeypatch.setattr(fanqiao_sso, "get_redis", lambda: FakeRedis())

    exact_60_ticket = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": int(time.time()) + 60, "nonce": "5" * 32},
        TEST_SECRET,
    )
    r1 = client.get(f"/sso/fanqiao?ticket={exact_60_ticket}", follow_redirects=False)
    assert r1.status_code == 302
    assert f"aetrix:fanqiao:nonce:{'5' * 32}" in redis_kv

    # 重放同一票据 -> 401
    r2 = client.get(f"/sso/fanqiao?ticket={exact_60_ticket}", follow_redirects=False)
    assert r2.status_code == 401

    # Redis 故障时 fail-closed 503，绝不放行
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(fanqiao_sso, "get_redis", lambda: BrokenRedis())
    t_redis_err = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": int(time.time()) + 30, "nonce": "6" * 32},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={t_redis_err}", follow_redirects=False).status_code == 503


def test_sso_first_login_onboarding_vs_returning_user_redirect(env_setup):
    client, SessionCls, _ = env_setup
    sub = _sub_of(1001)
    t1 = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "premium", "exp": int(time.time()) + 45, "nonce": "a1" * 16},
        TEST_SECRET,
    )

    # 1) 首次登录：自动建户（无邮箱、不碰 telegram_id）、设公益资格与查看行、跳首页带 setup_emby=1 引导
    resp = client.get(f"/sso/fanqiao?ticket={t1}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?setup_emby=1"
    assert fanqiao_sso.SSO_HANDOFF_COOKIE in resp.cookies

    # 一次性换取门户 JWT 会话，二次重复换取必须 401
    ex1 = client.post("/api/user/auth/sso-session")
    assert ex1.status_code == 200
    body1 = ex1.json()
    assert body1["access_token"]
    assert body1["refresh_token"]
    assert body1["first_login"] is True
    assert body1["setup_emby_needed"] is True
    assert body1["user"]["is_welfare"] is True
    assert body1["user"]["email"] is None
    assert client.post("/api/user/auth/sso-session").status_code == 401

    # 校验数据库：独立映射表 fanqiao_identities 已绑定，telegram_id 保持 None，接线员查看权限已开通
    db = SessionCls()
    try:
        ident = db.query(models.FanqiaoIdentity).filter_by(sub=sub).one()
        user = db.query(models.WebUser).filter_by(id=ident.user_id).one()
        assert user.telegram_id is None
        assert user.email is None
        assert user.register_channel == register_channel.FANQIAO
        assert user.is_welfare is True
        assert user.welfare_expires_at is not None and user.welfare_expires_at > datetime.now()
        granted, _unlock = subscriptions.view_grant(db, user)
        assert granted is True
        assert subscriptions.can_play(db, user) is True

        # 模拟用户已在「接线员」页设好接线用户名
        user.emby_username = "fq_player_1001"
        db.commit()
    finally:
        db.close()

    # 2) 已设好接线用户名后再次 SSO 登录：直接跳 "/" 首页，不再弹首次引导
    t2 = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "premium", "exp": int(time.time()) + 45, "nonce": "b2" * 16},
        TEST_SECRET,
    )
    resp2 = client.get(f"/sso/fanqiao?ticket={t2}", follow_redirects=False)
    assert resp2.status_code == 302
    assert resp2.headers["location"] == "/"
    ex2 = client.post("/api/user/auth/sso-session")
    assert ex2.status_code == 200
    assert ex2.json()["first_login"] is False
    assert ex2.json()["setup_emby_needed"] is False


def test_webhook_signature_skew_cap_idempotency_and_placeholder_promotion(env_setup):
    client, SessionCls, _ = env_setup
    now = int(time.time())
    future_exp = now + 86400 * 30
    sub_unknown = _sub_of(2001)
    sub_banned_pre = _sub_of(2002)

    # 1) 签名错误或篡改原始 JSON 空白字符 -> 401
    payload = {
        "items": [
            {"sub": sub_unknown, "tier": "standard", "expires_at": future_exp, "banned": False},
            {"sub": sub_banned_pre, "tier": "premium", "expires_at": future_exp, "banned": True},
        ],
        "sent_at": now,
    }
    raw_compact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    raw_pretty = json.dumps(payload, indent=2).encode("utf-8")
    sig_compact = _sign_body(raw_compact)

    # 用 compact 的签名发 pretty 的 body -> 必须因原始字节不同而 401
    assert client.post(
        "/api/integrations/fanqiao/entitlements",
        content=raw_pretty,
        headers={"X-Fanqiao-Signature": sig_compact, "Content-Type": "application/json"},
    ).status_code == 401

    # 2) sent_at 偏差超过 300 秒 -> 401
    stale_raw = json.dumps({"items": [], "sent_at": now - 305}).encode("utf-8")
    assert client.post(
        "/api/integrations/fanqiao/entitlements",
        content=stale_raw,
        headers={"X-Fanqiao-Signature": _sign_body(stale_raw)},
    ).status_code == 401

    # 3) 单次超过 200 条 -> 400
    over_items = [{"sub": _sub_of(5000 + i), "tier": "standard", "expires_at": future_exp, "banned": False} for i in range(201)]
    over_raw = json.dumps({"items": over_items, "sent_at": now}).encode("utf-8")
    assert client.post(
        "/api/integrations/fanqiao/entitlements",
        content=over_raw,
        headers={"X-Fanqiao-Signature": _sign_body(over_raw)},
    ).status_code == 400

    # 4) 未知 sub 首次推送：先建 user_id=None 占位映射，不提前建 WebUser；重放重试完全幂等
    r_ok1 = client.post(
        "/api/integrations/fanqiao/entitlements",
        content=raw_compact,
        headers={"X-Fanqiao-Signature": sig_compact},
    )
    assert r_ok1.status_code == 200
    assert r_ok1.json()["placeholders_created"] == 2

    # 面板重试同一批次 -> 幂等 200，placeholders_created == 0
    r_ok2 = client.post(
        "/api/integrations/fanqiao/entitlements",
        content=raw_compact,
        headers={"X-Fanqiao-Signature": sig_compact},
    )
    assert r_ok2.status_code == 200
    assert r_ok2.json()["placeholders_created"] == 0

    db = SessionCls()
    try:
        assert db.query(models.WebUser).count() == 0
        ph = db.query(models.FanqiaoIdentity).filter_by(sub=sub_unknown).one()
        assert ph.user_id is None
        assert ph.tier == "standard"
    finally:
        db.close()

    # 5) 已被占位标记 banned=True 的 sub 尝试 SSO 登录 -> 立即 403 拦截
    t_banned = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub_banned_pre, "tier": "premium", "exp": int(time.time()) + 30, "nonce": "c3" * 16},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={t_banned}", follow_redirects=False).status_code == 403

    # 6) 正常占位 sub 首次 SSO 登录 -> 占位行自动转正绑定新建 WebUser，且保留 Webhook 下发的精确到期时间
    t_promote = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub_unknown, "tier": "standard", "exp": int(time.time()) + 30, "nonce": "d4" * 16},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={t_promote}", follow_redirects=False).status_code == 302
    db = SessionCls()
    try:
        promoted = db.query(models.FanqiaoIdentity).filter_by(sub=sub_unknown).one()
        assert promoted.user_id is not None
        u = db.query(models.WebUser).filter_by(id=promoted.user_id).one()
        assert abs(u.welfare_expires_at.timestamp() - future_exp) < 2
    finally:
        db.close()


def test_tier_transitions_expiry_ban_disable_playback_and_mapping_reset(env_setup):
    client, SessionCls, app = env_setup
    sub = _sub_of(3001)

    # 1) 用户通过 SSO 登录建户，模拟签发一枚活跃 Emby 客户端播放令牌
    t_init = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "standard", "exp": int(time.time()) + 30, "nonce": "e5" * 16},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={t_init}", follow_redirects=False).status_code == 302

    db = SessionCls()
    try:
        ident = db.query(models.FanqiaoIdentity).filter_by(sub=sub).one()
        uid = ident.user_id
        db.add(emby_models.EmbyApiToken(token="tok_hash_1", user_id=uid, device_id="dev1", is_revoked=False))
        db.commit()
    finally:
        db.close()

    def _push(item_dict: dict):
        body = json.dumps({"items": [item_dict], "sent_at": int(time.time())}).encode("utf-8")
        res = client.post(
            "/api/integrations/fanqiao/entitlements",
            content=body,
            headers={"X-Fanqiao-Signature": _sign_body(body)},
        )
        assert res.status_code == 200
        return res.json()

    # 2) 套餐升级 standard -> flagship：保持可播，到期时间更新
    new_exp = int(time.time()) + 86400 * 90
    _push({"sub": sub, "tier": "flagship", "expires_at": new_exp, "banned": False})
    db = SessionCls()
    try:
        u = db.query(models.WebUser).filter_by(id=uid).one()
        assert u.is_welfare is True
        assert subscriptions.can_play(db, u) is True
        subscriptions.ensure_playback_allowed(db, u)
    finally:
        db.close()

    # 3) 套餐到期 / 降为非合格档（tier="none", banned=False）：即使在 free 公益服也必须立即禁播 + 吊销 Emby 令牌
    _push({"sub": sub, "tier": "none", "expires_at": int(time.time()) - 10, "banned": False})
    db = SessionCls()
    try:
        u = db.query(models.WebUser).filter_by(id=uid).one()
        tok = db.query(emby_models.EmbyApiToken).filter_by(token="tok_hash_1").one()
        assert u.is_welfare is False
        assert u.is_active is True  # 非封禁：仅回收播放资格，不误封门户账号
        assert tok.is_revoked is True
        assert subscriptions.can_play(db, u) is False
        with pytest.raises( Exception ):
            subscriptions.ensure_playback_allowed(db, u)
    finally:
        db.close()

    # 4) 用户续费恢复（tier="standard", future expires_at）：播放权限立即恢复
    _push({"sub": sub, "tier": "standard", "expires_at": new_exp, "banned": False})
    db = SessionCls()
    try:
        u = db.query(models.WebUser).filter_by(id=uid).one()
        db.add(emby_models.EmbyApiToken(token="tok_hash_2", user_id=uid, device_id="dev1", is_revoked=False))
        db.commit()
        assert u.is_welfare is True
        assert subscriptions.can_play(db, u) is True
    finally:
        db.close()

    # 5) 独立封禁标志 banned=True（即便 tier 仍传 flagship 且未到期！）：一律无权限，停用账号、bump token_version、吊销令牌、拒 SSO
    _push({"sub": sub, "tier": "flagship", "expires_at": new_exp, "banned": True})
    db = SessionCls()
    try:
        u = db.query(models.WebUser).filter_by(id=uid).one()
        tok2 = db.query(emby_models.EmbyApiToken).filter_by(token="tok_hash_2").one()
        assert u.is_active is False
        assert u.is_welfare is False
        assert (u.token_version or 0) >= 1
        assert tok2.is_revoked is True
        assert subscriptions.can_play(db, u) is False
    finally:
        db.close()

    t_banned_login = fanqiao_sso.build_sso_ticket_for_test(
        {"sub": sub, "tier": "flagship", "exp": int(time.time()) + 30, "nonce": "f6" * 16},
        TEST_SECRET,
    )
    assert client.get(f"/sso/fanqiao?ticket={t_banned_login}", follow_redirects=False).status_code == 403

    # 6) 轮换共享密钥后整体重建映射表：清空 fanqiao_identities 且保留存量 WebUser
    db = SessionCls()
    try:
        out = fanqiao_sso.reset_fanqiao_identities(db)
        assert out["deleted_mappings"] == 1
        assert db.query(models.FanqiaoIdentity).count() == 0
        assert db.query(models.WebUser).filter_by(id=uid).count() == 1
    finally:
        db.close()
