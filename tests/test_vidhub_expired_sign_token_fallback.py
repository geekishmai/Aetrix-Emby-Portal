# -*- coding: utf-8 -*-
"""回归测试：客户端（如 VidHub 继续观看）缓存旧 DirectPlayUrl 致 play_sign 过期后的鉴权与门禁行为。

生产根因（2026-11-07 线上日志实证）：
VidHub 会把昨日详情/PlaybackInfo 返回的 DirectPlayUrl（同时含长期有效 `api_key` 与
短期 `uid/exp/sign`）持久化在客户端本地库；次日点继续观看时直接请求该缓存 URL、不重发
PlaybackInfo。旧版 `get_play_user` 见 `exp` 过期即一刀切抛 403，忽略了同请求携带的本人
有效 `EmbyApiToken`。

锁定安全与兼容契约：
1. 未篡改但自然过期的 `play_sign` + 本人有效 `EmbyApiToken`（`api_key` 或 `X-Emby-Token`）
   → `get_play_user` 回退成功返回本人；且下游门禁因 `verify_play_sign=False` 仍按新播放
   完整执行实时绑定/付费墙门禁（无豁免穿透）。
2. 自然过期的 `play_sign` 但无有效令牌 / 令牌已吊销 / 令牌属于其他用户 → 仍抛 403。
3. 签名或 `exp` 被篡改（HMAC 不匹配），即使同时携带本人有效令牌 → 仍抛 403。
4. 二级清单定位口（带 `?u=`）过期 → 不走令牌回退，仍抛 403。
5. 重新请求 `PlaybackInfo`（含命中缓存基础体场景）始终重新签发新鲜 `exp > now`。
"""
from __future__ import annotations

import os
import secrets
import time
import uuid

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import auth, models as em, play_sign, playback_gate
from backend.emby_server.auth import hash_emby_token
from backend.security import hash_password

init_db()


def _make_request(item_guid: str, query: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": f"/emby/Videos/{item_guid}/stream.mkv",
        "query_string": query.encode("utf-8"),
        "headers": headers or [],
        "client": ("127.0.0.1", 51234),
        "server": ("emby.fluxquata.com", 443),
        "path_params": {"item_id": item_guid},
    }
    return Request(scope)


@pytest.fixture()
def token_env():
    db = SessionLocal()
    created_uids: list[int] = []
    try:
        u1_name = f"vh_u1_{uuid.uuid4().hex[:8]}"
        u2_name = f"vh_u2_{uuid.uuid4().hex[:8]}"
        u1 = models.WebUser(
            username=u1_name,
            password_hash=hash_password("pw123456"),
            is_active=True,
            emby_username=u1_name,
            emby_password=hash_password("pw123456"),
        )
        u2 = models.WebUser(
            username=u2_name,
            password_hash=hash_password("pw123456"),
            is_active=True,
            emby_username=u2_name,
            emby_password=hash_password("pw123456"),
        )
        db.add_all([u1, u2])
        db.flush()
        created_uids.extend([u1.id, u2.id])

        raw_tok1 = secrets.token_hex(20)
        raw_tok2 = secrets.token_hex(20)
        db.add_all(
            [
                em.EmbyApiToken(
                    token=hash_emby_token(raw_tok1),
                    user_id=u1.id,
                    device_id="vh-dev-1",
                    app_name="VidHub",
                    app_version="1.7.9",
                    expires_at=play_sign.token_expiry_default(),
                ),
                em.EmbyApiToken(
                    token=hash_emby_token(raw_tok2),
                    user_id=u2.id,
                    device_id="vh-dev-2",
                    app_name="VidHub",
                    app_version="1.7.9",
                    expires_at=play_sign.token_expiry_default(),
                ),
            ]
        )
        db.commit()
        yield db, u1, raw_tok1, u2, raw_tok2
    finally:
        if created_uids:
            db.query(em.EmbyApiToken).filter(em.EmbyApiToken.user_id.in_(created_uids)).delete(
                synchronize_session=False
            )
            db.query(models.WebUser).filter(models.WebUser.id.in_(created_uids)).delete(
                synchronize_session=False
            )
            db.commit()
        db.close()


def test_expired_authentic_sign_with_same_user_valid_api_key_succeeds_and_runs_live_gate(
    token_env, monkeypatch
):
    db, u1, tok1, _u2, _tok2 = token_env
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda _db: True)
    item_guid = uuid.uuid4().hex
    expired_exp = int(time.time()) - 14 * 3600
    authentic_old_sign = play_sign.make_play_sign(u1.id, item_guid, expired_exp)

    req = _make_request(
        item_guid,
        f"static=true&MediaSourceId={item_guid}&api_key={tok1}&uid={u1.id}&exp={expired_exp}&sign={authentic_old_sign}",
    )
    resolved = auth.get_play_user(req, db, None)
    assert resolved.id == u1.id

    # 下游门禁不把已过期签名当成已发放豁免凭证（_request_uses_issued_play_sign 必须为 False）
    assert playback_gate._request_uses_issued_play_sign(req, db) is False


def test_expired_sign_without_token_or_with_other_user_token_rejected_403(token_env, monkeypatch):
    db, u1, _tok1, _u2, tok2 = token_env
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda _db: True)
    item_guid = uuid.uuid4().hex
    expired_exp = int(time.time()) - 3600
    old_sign = play_sign.make_play_sign(u1.id, item_guid, expired_exp)

    # 无 api_key：纯签名链接过期必须 403
    req_no_tok = _make_request(item_guid, f"uid={u1.id}&exp={expired_exp}&sign={old_sign}")
    with pytest.raises(HTTPException) as ei1:
        auth.get_play_user(req_no_tok, db, None)
    assert ei1.value.status_code == 403

    # 带了其他用户的有效 api_key：uid 不一致必须 403
    req_wrong_user = _make_request(
        item_guid, f"api_key={tok2}&uid={u1.id}&exp={expired_exp}&sign={old_sign}"
    )
    with pytest.raises(HTTPException) as ei2:
        auth.get_play_user(req_wrong_user, db, None)
    assert ei2.value.status_code == 403


def test_tampered_sign_or_secondary_locator_never_falls_back(token_env, monkeypatch):
    db, u1, tok1, _u2, _tok2 = token_env
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda _db: True)
    item_guid = uuid.uuid4().hex
    expired_exp = int(time.time()) - 600
    old_sign = play_sign.make_play_sign(u1.id, item_guid, expired_exp)
    tampered_sign = ("0" if old_sign[0] != "0" else "1") + old_sign[1:]

    # 篡改签名即使带本人有效 api_key 也必须 403
    req_tampered = _make_request(
        item_guid, f"api_key={tok1}&uid={u1.id}&exp={expired_exp}&sign={tampered_sign}"
    )
    with pytest.raises(HTTPException) as ei1:
        auth.get_play_user(req_tampered, db, None)
    assert ei1.value.status_code == 403

    # 二级定位参数 ?u= 不走令牌回退
    req_locator = _make_request(
        item_guid, f"u=abc.def&api_key={tok1}&uid={u1.id}&exp={expired_exp}&sign={old_sign}"
    )
    with pytest.raises(HTTPException) as ei2:
        auth.get_play_user(req_locator, db, None)
    assert ei2.value.status_code == 403
