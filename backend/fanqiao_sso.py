# -*- coding: utf-8 -*-
"""帆桥影院 × 帆桥机场 SSO 与资格同步核心模块（对齐面板侧契约 a85d3e0）。

协议与安全约束：
1. **共享密钥**：优先读 ``SystemConfig(key="fanqiao_shared_secret")``，回退环境变量
   ``FANQIAO_SHARED_SECRET``；长度至少 32 字符，缺失或过短 fail-closed（503），全程不打印密钥。
2. **SSO 票据（GET /sso/fanqiao?ticket=payload.sig）**：
   - ``payload`` 为无填充 base64url 编码的 UTF-8 JSON ``{sub, tier, exp, nonce}``：
     ``sub`` 为 64 位 hex，``tier`` ∈ ``{standard, premium, flagship, none}``，
     ``exp`` 为 Unix 秒（必须满足 ``now < exp <= now + 60``），``nonce`` 为 32 位 hex；
   - ``sig`` 为对 URL 中 ``payload`` 原始 base64url 字符串做 ``HMAC-SHA256(secret, payload)``
     所得 32 字节摘要的无填充 base64url；
   - ``nonce`` 用 Redis ``SET ... NX EX`` 原子防重放（无 Redis 测试态回退进程内互斥表）。
3. **资格同步 Webhook（POST /api/integrations/fanqiao/entitlements）**：
   - 校验 ``X-Fanqiao-Signature``：对原始请求体字节做 ``HMAC-SHA256`` 的 64 位小写 hex；
   - 校验 ``abs(now - sent_at) <= 300`` 秒，``len(items) <= 200``；
   - 未知 ``sub`` 先建 ``FanqiaoIdentity(user_id=None)`` 占位映射，不提前膨胀 ``web_users``；
   - ``banned=True`` 独立最高优先级：一律无权限（即使 ``tier != "none"``），停用门户与播放并吊销全部 Emby 令牌；
   - ``tier == "none"`` 或 ``expires_at <= now``：取消公益资格、回收查看行并吊销 Emby 播放令牌，禁止播放；
   - 全程幂等，支持面板侧非 2xx 自动重试。
4. **密钥轮换整体重建**：提供 ``reset_fanqiao_identities`` 清空映射表供新密钥重推。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import models, realms, register_channel
from backend.authlog import record_event
from backend.database import get_redis
from backend.security import hash_password
from backend.user_lifecycle import set_user_active
logger = logging.getLogger(__name__)

CONFIG_SECRET_KEY = "fanqiao_shared_secret"
ENV_SECRET_KEY = "FANQIAO_SHARED_SECRET"
MIN_SECRET_LENGTH = 32

MAX_TICKET_TTL_SEC = 60
NONCE_REDIS_TTL_SEC = 120
WEBHOOK_MAX_SKEW_SEC = 300
WEBHOOK_MAX_ITEMS = 200
HANDOFF_TTL_SEC = 60
PROVISIONAL_SSO_WELFARE_DAYS = 31

SSO_HANDOFF_COOKIE = "aetrix_sso_handoff"
SSO_PENDING_COOKIE = "aetrix_sso_pending"

ACTIVE_TIERS = frozenset({"standard", "premium", "flagship"})
ALL_TIERS = frozenset({"standard", "premium", "flagship", "none"})

_SUB_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_SIG_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_B64URL_NOPAD_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_NONCE_KEY_PREFIX = "aetrix:fanqiao:nonce:"
_HANDOFF_KEY_PREFIX = "aetrix:fanqiao:handoff:"

_mem_lock = threading.Lock()
_mem_nonces: dict[str, float] = {}
_mem_handoffs: dict[str, tuple[str, float]] = {}


class FanqiaoError(Exception):
    """帆桥联动基础异常（绝不携带密钥原文）。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FanqiaoNotConfiguredError(FanqiaoError):
    def __init__(self) -> None:
        super().__init__("帆桥联动未配置有效共享密钥", status_code=503)


class FanqiaoAuthError(FanqiaoError):
    def __init__(self, message: str = "签名或票据无效", status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code)


def _b64url_encode_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode_nopad(segment: str) -> bytes:
    if not segment or "=" in segment or not _B64URL_NOPAD_RE.fullmatch(segment):
        raise FanqiaoAuthError("票据编码格式无效")
    pad_len = (-len(segment)) % 4
    try:
        return base64.urlsafe_b64decode((segment + ("=" * pad_len)).encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise FanqiaoAuthError("票据解码失败") from exc


def get_shared_secret(db: Session) -> str:
    """读取并校验共享密钥（SystemConfig 优先于环境变量，长度至少 32）。"""
    secret = ""
    try:
        row = (
            db.query(models.SystemConfig)
            .filter(models.SystemConfig.key == CONFIG_SECRET_KEY)
            .first()
        )
        if row is not None and row.value and str(row.value).strip():
            secret = str(row.value).strip()
    except Exception:  # noqa: BLE001
        secret = ""

    if not secret:
        secret = (os.getenv(ENV_SECRET_KEY, "") or "").strip()

    if len(secret) < MIN_SECRET_LENGTH:
        raise FanqiaoNotConfiguredError()
    return secret


def build_sso_ticket_for_test(payload: dict[str, Any], secret: str) -> str:
    """按面板侧 a85d3e0 契约构造签名票据（供联调与测试验证协议一致性）。"""
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = _b64url_encode_nopad(payload_bytes)
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode_nopad(digest)
    return f"{payload_b64}.{sig_b64}"


def _consume_nonce_once(nonce: str, ttl_sec: int = NONCE_REDIS_TTL_SEC) -> bool:
    """原子消费一次性 nonce：成功消费返回 True，重放返回 False。"""
    key = f"{_NONCE_KEY_PREFIX}{nonce}"
    client = get_redis()
    if client is not None:
        try:
            ok = client.set(key, "1", nx=True, ex=max(60, int(ttl_sec)))
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            # Redis 已配置但异常时 fail-closed，避免重放绕过
            logger.error("帆桥 SSO nonce 写入 Redis 失败，拒绝放行（类型=%s）", type(exc).__name__)
            raise FanqiaoError("服务繁忙，请稍后重试", status_code=503) from None

    now_m = time.monotonic()
    with _mem_lock:
        expired = [k for k, exp_at in _mem_nonces.items() if exp_at <= now_m]
        for k in expired:
            _mem_nonces.pop(k, None)
        if nonce in _mem_nonces:
            return False
        _mem_nonces[nonce] = now_m + max(60, int(ttl_sec))
        return True


def store_handoff_payload(payload_dict: dict[str, Any], ttl_sec: int = HANDOFF_TTL_SEC) -> str:
    """保存 60 秒一次性门户登录态交接载荷，返回随机不透明 handoff code。"""
    code = secrets.token_hex(24)
    raw = json.dumps(payload_dict, ensure_ascii=False)
    key = f"{_HANDOFF_KEY_PREFIX}{code}"
    client = get_redis()
    if client is not None:
        try:
            client.setex(key, max(10, int(ttl_sec)), raw)
            return code
        except Exception as exc:  # noqa: BLE001
            logger.warning("帆桥 SSO handoff 写 Redis 失败，回退进程内暂存（类型=%s）", type(exc).__name__)

    now_m = time.monotonic()
    with _mem_lock:
        expired = [k for k, (_v, exp_at) in _mem_handoffs.items() if exp_at <= now_m]
        for k in expired:
            _mem_handoffs.pop(k, None)
        _mem_handoffs[code] = (raw, now_m + max(10, int(ttl_sec)))
    return code


def consume_handoff_payload(code: str) -> Optional[dict[str, Any]]:
    """一次性消费门户登录态交接载荷（读取即销毁）。"""
    clean_code = (code or "").strip()
    if not clean_code or not re.fullmatch(r"[0-9a-f]{48}", clean_code):
        return None
    key = f"{_HANDOFF_KEY_PREFIX}{clean_code}"
    raw: Any = None
    client = get_redis()
    if client is not None:
        try:
            if hasattr(client, "getdel"):
                raw = client.getdel(key)
            else:
                raw = client.get(key)
                if raw is not None:
                    client.delete(key)
        except Exception:  # noqa: BLE001
            raw = None

    if raw is None:
        now_m = time.monotonic()
        with _mem_lock:
            entry = _mem_handoffs.pop(clean_code, None)
            if entry is not None and entry[1] > now_m:
                raw = entry[0]

    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001
        return None


def verify_and_consume_ticket(
    ticket: str,
    secret: str,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, Any]:
    """校验 SSO ticket 签名、有效期（≤60s）并原子消费 nonce 防重放。"""
    raw_ticket = (ticket or "").strip()
    if not raw_ticket or raw_ticket.count(".") != 1:
        raise FanqiaoAuthError("票据格式无效")

    payload_b64, sig_b64 = raw_ticket.split(".", 1)
    if not payload_b64 or not sig_b64 or "=" in payload_b64 or "=" in sig_b64:
        raise FanqiaoAuthError("票据编码无效")
    if not (_B64URL_NOPAD_RE.fullmatch(payload_b64) and _B64URL_NOPAD_RE.fullmatch(sig_b64)):
        raise FanqiaoAuthError("票据字符无效")

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_sig = _b64url_encode_nopad(expected_digest)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise FanqiaoAuthError("票据签名校验失败")

    payload_bytes = _b64url_decode_nopad(payload_b64)
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FanqiaoAuthError("票据载荷不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise FanqiaoAuthError("票据载荷结构无效")

    sub = str(data.get("sub") or "").strip().lower()
    if not _SUB_HEX_RE.fullmatch(sub):
        raise FanqiaoAuthError("票据 sub 格式无效（需 64 位 hex）")

    tier = str(data.get("tier") or "").strip().lower()
    if tier not in ALL_TIERS:
        raise FanqiaoAuthError("票据 tier 无效")

    exp_raw = data.get("exp")
    if isinstance(exp_raw, bool) or not isinstance(exp_raw, int):
        raise FanqiaoAuthError("票据 exp 必须为整数时间戳")

    now_val = float(time.time() if now_ts is None else now_ts)
    now_sec = int(now_val)
    if exp_raw <= now_sec:
        raise FanqiaoAuthError("登录票据已过期，请返回帆桥面板重新点击进入")
    if exp_raw > now_sec + MAX_TICKET_TTL_SEC:
        raise FanqiaoAuthError("票据有效期超出 60 秒安全上限")

    nonce = str(data.get("nonce") or "").strip().lower()
    if not _NONCE_HEX_RE.fullmatch(nonce):
        raise FanqiaoAuthError("票据 nonce 格式无效（需 32 位 hex）")

    ttl = max(NONCE_REDIS_TTL_SEC, (exp_raw - now_sec) + 30)
    if not _consume_nonce_once(nonce, ttl_sec=ttl):
        raise FanqiaoAuthError("登录票据已被使用，请勿重复提交")

    return {"sub": sub, "tier": tier, "exp": exp_raw, "nonce": nonce}


def verify_webhook_signature(raw_body: bytes, sig_header: Optional[str], secret: str) -> None:
    """校验 Webhook 请求头 X-Fanqiao-Signature（原始 body 的 HMAC-SHA256 小写 hex）。"""
    provided = (sig_header or "").strip().lower()
    if not _SIG_HEX_RE.fullmatch(provided):
        raise FanqiaoAuthError("缺少或非法的 X-Fanqiao-Signature 签名头")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest().lower()
    if not hmac.compare_digest(expected, provided):
        raise FanqiaoAuthError("X-Fanqiao-Signature 签名校验失败")


def _generate_unique_portal_username(db: Session, sub: str) -> str:
    """自动生成无冲突的门户用户名（同时避开 WebUser.username 与 emby_username 大小写不敏感空间）。"""
    base = f"fq_{sub[:10]}"
    candidate = base
    for attempt in range(20):
        if attempt > 0:
            candidate = f"fq_{sub[:8]}_{secrets.token_hex(2)}"
        low = candidate.lower()
        taken = (
            db.query(models.WebUser.id)
            .filter(
                or_(
                    func.lower(models.WebUser.username) == low,
                    func.lower(models.WebUser.emby_username) == low,
                )
            )
            .first()
        )
        if taken is None:
            return candidate
    return f"fq_{secrets.token_hex(8)}"


def _revoke_user_emby_tokens(db: Session, user_id: int) -> None:
    from backend.emby_server import models as emby_models

    db.query(emby_models.EmbyApiToken).filter(
        emby_models.EmbyApiToken.user_id == user_id,
        emby_models.EmbyApiToken.is_revoked == False,  # noqa: E712
    ).update(
        {emby_models.EmbyApiToken.is_revoked: True},
        synchronize_session=False,
    )


def _sync_user_view_unlock(
    db: Session,
    user_id: int,
    *,
    entitled: bool,
    expires_at: Optional[datetime],
    now_dt: datetime,
) -> None:
    """同步当前服的 EmbyViewUnlock（使合格用户在公益服可直接在接线员页设置播放账号密码，失效时同步回收）。"""
    try:
        realm_id = realms.active_realm_id(db)
    except Exception:  # noqa: BLE001
        realm_id = 1

    rows = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user_id)
        .all()
    )
    if entitled:
        target_row = next((r for r in rows if r.realm_id == realm_id), None)
        if target_row is None:
            db.add(
                models.EmbyViewUnlock(
                    user_id=user_id,
                    realm_id=realm_id,
                    unlocked_at=now_dt,
                    expires_at=expires_at,
                    points_spent=0,
                )
            )
        else:
            # 仅不缩短用户自费购买的更晚查看行（points_spent > 0）
            if (target_row.points_spent or 0) == 0 or (
                target_row.expires_at is not None
                and (expires_at is None or expires_at > target_row.expires_at)
            ):
                target_row.expires_at = expires_at
    else:
        past = now_dt - timedelta(seconds=1)
        for r in rows:
            if (r.points_spent or 0) == 0 and (r.expires_at is None or r.expires_at > now_dt):
                r.expires_at = past


def _apply_identity_state_to_user(
    db: Session,
    user: models.WebUser,
    identity: models.FanqiaoIdentity,
    *,
    now_dt: datetime,
) -> str:
    """把 FanqiaoIdentity 的 (tier, expires_at, banned) 落到关联 WebUser。

    返回状态标签：``"banned"`` | ``"entitled"`` | ``"expired_or_none"``。
    """
    if identity.banned:
        set_user_active(db, user, False)
        user.is_welfare = False
        if user.welfare_expires_at is None or user.welfare_expires_at > now_dt:
            user.welfare_expires_at = now_dt - timedelta(seconds=1)
        _sync_user_view_unlock(db, user.id, entitled=False, expires_at=None, now_dt=now_dt)
        return "banned"

    # 未封禁：若此前因封禁被停用，则恢复门户可登录态（不恢复已吊销的旧令牌）
    if not user.is_active:
        set_user_active(db, user, True)

    entitled = (
        identity.tier in ACTIVE_TIERS
        and (identity.expires_at is None or identity.expires_at > now_dt)
    )
    if entitled:
        user.is_welfare = True
        user.welfare_expires_at = identity.expires_at
        user.welfare_grant_channel = register_channel.FANQIAO
        user.welfare_granted_at = now_dt
        _sync_user_view_unlock(
            db, user.id, entitled=True, expires_at=identity.expires_at, now_dt=now_dt
        )
        return "entitled"

    user.is_welfare = False
    user.welfare_expires_at = (
        identity.expires_at
        if identity.expires_at is not None and identity.expires_at <= now_dt
        else (now_dt - timedelta(seconds=1))
    )
    _sync_user_view_unlock(db, user.id, entitled=False, expires_at=None, now_dt=now_dt)
    _revoke_user_emby_tokens(db, user.id)
    return "expired_or_none"


def complete_sso_login(
    db: Session,
    verified_payload: dict[str, Any],
    *,
    client_ip: str = "",
    user_agent: str = "",
    now_ts: Optional[float] = None,
) -> dict[str, Any]:
    """根据已验签 SSO 票据查找/创建门户用户、同步资格并签发一次性交接登录态。"""
    sub = verified_payload["sub"]
    tier = verified_payload["tier"]
    now_val = float(time.time() if now_ts is None else now_ts)
    now_dt = datetime.fromtimestamp(now_val)

    identity = (
        db.query(models.FanqiaoIdentity)
        .filter(models.FanqiaoIdentity.sub == sub)
        .first()
    )
    if identity is not None and identity.banned:
        raise FanqiaoError("该账号已被封禁，无法登录", status_code=403)

    user: Optional[models.WebUser] = None
    first_login = False
    if identity is not None and identity.user_id is not None:
        user = db.query(models.WebUser).filter(models.WebUser.id == identity.user_id).first()
        if user is None:
            identity.user_id = None
        elif not user.is_active:
            raise FanqiaoError("该账号已被停用，无法登录", status_code=403)

    if user is None:
        first_login = True
        username = _generate_unique_portal_username(db, sub)
        user = models.WebUser(
            username=username,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            email=None,
            is_active=True,
            register_channel=register_channel.FANQIAO,
        )
        db.add(user)
        db.flush()

        if identity is None:
            identity = models.FanqiaoIdentity(
                sub=sub,
                user_id=user.id,
                tier=tier,
                banned=False,
            )
            db.add(identity)
        else:
            identity.user_id = user.id

    identity.tier = tier
    identity.last_sso_at = now_dt
    if tier in ACTIVE_TIERS:
        # 若 Webhook 尚未送达权威到期时间（或旧时间已过期但本次 SSO 票据已升回有效档），先给临时有效期等 Webhook 刷新
        if identity.expires_at is None and identity.last_synced_at is not None:
            # Webhook 曾明确推送过 expires_at=None（永久套餐）
            pass
        elif identity.expires_at is None or identity.expires_at <= now_dt:
            identity.expires_at = now_dt + timedelta(days=PROVISIONAL_SSO_WELFARE_DAYS)

    _apply_identity_state_to_user(db, user, identity, now_dt=now_dt)
    user.last_login_at = now_dt

    db.commit()
    db.refresh(user)
    record_event(
        db,
        username=user.username,
        user_id=user.id,
        ip=client_ip,
        agent=user_agent,
        success=True,
        reason="fanqiao_sso",
    )
    from backend.api.emby_portal import _issue_auth_response

    auth_resp = _issue_auth_response(user, db)
    setup_emby_needed = not bool((user.emby_username or "").strip())

    handoff_dict = auth_resp.model_dump() if hasattr(auth_resp, "model_dump") else auth_resp.dict()
    handoff_dict["first_login"] = first_login
    handoff_dict["setup_emby_needed"] = setup_emby_needed
    handoff_code = store_handoff_payload(handoff_dict, ttl_sec=HANDOFF_TTL_SEC)

    return {
        "handoff_code": handoff_code,
        "first_login": first_login,
        "setup_emby_needed": setup_emby_needed,
        "redirect_url": "/?setup_emby=1" if setup_emby_needed else "/",
        "user_id": user.id,
        "username": user.username,
    }


def sync_entitlements_batch(
    db: Session,
    payload: Any,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, int]:
    """批量幂等同步帆桥资格、到期与封禁状态（未知 sub 建占位映射）。"""
    if not isinstance(payload, dict):
        raise FanqiaoError("请求体必须为 JSON 对象", status_code=400)

    sent_at = payload.get("sent_at")
    if isinstance(sent_at, bool) or not isinstance(sent_at, int):
        raise FanqiaoError("sent_at 必须为整数 Unix 时间戳", status_code=400)

    now_val = float(time.time() if now_ts is None else now_ts)
    if abs(now_val - float(sent_at)) > WEBHOOK_MAX_SKEW_SEC:
        raise FanqiaoAuthError("sent_at 时间偏差超过 300 秒允许范围")

    items = payload.get("items")
    if not isinstance(items, list):
        raise FanqiaoError("items 必须为数组", status_code=400)
    if len(items) > WEBHOOK_MAX_ITEMS:
        raise FanqiaoError(f"单次推送条数超出上限（最多 {WEBHOOK_MAX_ITEMS} 条）", status_code=400)

    # 先全量校验输入项，防止脏载荷半途报错污染会话；同批重复 sub 以最后一条为准（幂等）
    dedup_items: dict[str, dict[str, Any]] = {}
    for idx, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise FanqiaoError(f"items[{idx}] 必须为对象", status_code=400)
        sub = str(raw_item.get("sub") or "").strip().lower()
        if not _SUB_HEX_RE.fullmatch(sub):
            raise FanqiaoError(f"items[{idx}].sub 必须为 64 位 hex", status_code=400)
        tier = str(raw_item.get("tier") or "").strip().lower()
        if tier not in ALL_TIERS:
            raise FanqiaoError(f"items[{idx}].tier 非法：{tier!r}", status_code=400)
        banned = raw_item.get("banned")
        if not isinstance(banned, bool):
            raise FanqiaoError(f"items[{idx}].banned 必须为布尔值", status_code=400)
        exp_raw = raw_item.get("expires_at")
        if exp_raw is not None:
            if isinstance(exp_raw, bool) or not isinstance(exp_raw, int) or exp_raw < 0:
                raise FanqiaoError(f"items[{idx}].expires_at 必须为正整数时间戳或 null", status_code=400)
        dedup_items[sub] = {
            "sub": sub,
            "tier": tier,
            "banned": banned,
            "expires_at": datetime.fromtimestamp(float(exp_raw)) if exp_raw is not None else None,
        }

    now_dt = datetime.fromtimestamp(now_val)
    stats = {
        "received": len(items),
        "processed": len(dedup_items),
        "placeholders_created": 0,
        "users_updated": 0,
        "entitled": 0,
        "disabled_playback": 0,
        "banned": 0,
    }
    if not dedup_items:
        return stats

    subs_list = list(dedup_items.keys())
    existing_identities = {
        row.sub: row
        for row in db.query(models.FanqiaoIdentity)
        .filter(models.FanqiaoIdentity.sub.in_(subs_list))
        .all()
    }
    user_ids = [row.user_id for row in existing_identities.values() if row.user_id is not None]
    users_by_id = (
        {
            u.id: u
            for u in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
        }
        if user_ids
        else {}
    )

    for sub, spec in dedup_items.items():
        identity = existing_identities.get(sub)
        if identity is None:
            identity = models.FanqiaoIdentity(
                sub=sub,
                user_id=None,
                tier=spec["tier"],
                expires_at=spec["expires_at"],
                banned=spec["banned"],
                last_synced_at=now_dt,
            )
            db.add(identity)
            stats["placeholders_created"] += 1
        else:
            identity.tier = spec["tier"]
            identity.expires_at = spec["expires_at"]
            identity.banned = spec["banned"]
            identity.last_synced_at = now_dt

        bound_user = users_by_id.get(identity.user_id) if identity.user_id is not None else None
        if bound_user is not None:
            outcome = _apply_identity_state_to_user(db, bound_user, identity, now_dt=now_dt)
            stats["users_updated"] += 1
            if outcome == "banned":
                stats["banned"] += 1
                stats["disabled_playback"] += 1
            elif outcome == "entitled":
                stats["entitled"] += 1
            else:
                stats["disabled_playback"] += 1
        else:
            if spec["banned"]:
                stats["banned"] += 1
            elif spec["tier"] in ACTIVE_TIERS and (
                spec["expires_at"] is None or spec["expires_at"] > now_dt
            ):
                stats["entitled"] += 1

    db.commit()
    return stats


def check_fanqiao_playback_allowed(db: Session, user: Optional[models.WebUser]) -> Optional[bool]:
    """全站播放与账号卡权限钩子：

    - 非帆桥绑定用户返回 ``None``（走原有订阅/公益服规则，零行为变化）；
    - 帆桥绑定用户：``banned=True`` 或 ``tier=='none'`` 或 ``expires_at <= now`` 或 ``!user.is_active``
      一律返回 ``False``（立即禁播）；合格且未过期返回 ``True``。
    """
    if user is None or getattr(user, "id", None) is None:
        return None
    identity = (
        db.query(models.FanqiaoIdentity)
        .filter(models.FanqiaoIdentity.user_id == user.id)
        .first()
    )
    if identity is None:
        return None
    if not bool(user.is_active) or bool(identity.banned):
        return False
    if identity.tier not in ACTIVE_TIERS:
        return False
    if identity.expires_at is not None and identity.expires_at <= datetime.now():
        return False
    return True


def reset_fanqiao_identities(db: Session) -> dict[str, int]:
    """轮换共享密钥后整体清空 ``fanqiao_identities`` 映射表（供新密钥重新全量同步与 SSO 建绑）。"""
    deleted = db.query(models.FanqiaoIdentity).delete(synchronize_session=False)
    db.commit()
    return {"deleted_mappings": int(deleted or 0)}
