# -*- coding: utf-8 -*-
"""帆桥影院 SSO 登录入口与资格同步 Webhook 路由。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend import fanqiao_sso, models
from backend.api.admin_core import _audit, get_current_admin
from backend.authlog import client_ip, user_agent
from backend.database import get_db

logger = logging.getLogger(__name__)

fanqiao_router = APIRouter(tags=["帆桥联动"])


def _is_secure_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip().lower()
    return proto == "https"


@fanqiao_router.get("/sso/fanqiao")
def sso_fanqiao_entry(
    request: Request,
    ticket: str = Query("", description="帆桥一次性签名登录票据 payload.sig"),
    db: Session = Depends(get_db),
):
    """帆桥面板单点登录入口：验签 + 防重放 -> 查找/创建门户用户 -> 设资格 -> 签发门户会话跳首页。"""
    try:
        secret = fanqiao_sso.get_shared_secret(db)
        verified = fanqiao_sso.verify_and_consume_ticket(ticket, secret)
        outcome = fanqiao_sso.complete_sso_login(
            db,
            verified,
            client_ip=client_ip(request),
            user_agent=user_agent(request),
        )
    except fanqiao_sso.FanqiaoError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    secure_cookie = _is_secure_request(request)
    resp = RedirectResponse(url=outcome["redirect_url"], status_code=302)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.set_cookie(
        key=fanqiao_sso.SSO_HANDOFF_COOKIE,
        value=outcome["handoff_code"],
        max_age=fanqiao_sso.HANDOFF_TTL_SEC,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    resp.set_cookie(
        key=fanqiao_sso.SSO_PENDING_COOKIE,
        value="1",
        max_age=fanqiao_sso.HANDOFF_TTL_SEC,
        httponly=False,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    return resp


@fanqiao_router.post("/api/user/auth/sso-session")
def exchange_sso_session(request: Request, response: Response):
    """门户前端落地页一次性换取 SSO 登录态（消费 HttpOnly 交接 Cookie，JWT 不进 URL）。"""
    code = request.cookies.get(fanqiao_sso.SSO_HANDOFF_COOKIE, "")
    payload = fanqiao_sso.consume_handoff_payload(code)
    response.delete_cookie(key=fanqiao_sso.SSO_HANDOFF_COOKIE, path="/")
    response.delete_cookie(key=fanqiao_sso.SSO_PENDING_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    if not payload:
        raise HTTPException(status_code=401, detail="单点登录会话不存在或已过期")
    return payload


@fanqiao_router.post("/api/integrations/fanqiao/entitlements")
async def sync_fanqiao_entitlements(
    request: Request,
    db: Session = Depends(get_db),
):
    """帆桥面板资格同步 Webhook：校验原始请求体 HMAC-SHA256 与 sent_at 窗口，幂等批量更新资格/到期/封禁。"""
    raw_body = await request.body()
    sig_header = request.headers.get("X-Fanqiao-Signature")
    try:
        secret = fanqiao_sso.get_shared_secret(db)
        fanqiao_sso.verify_webhook_signature(raw_body, sig_header, secret)
        try:
            body_obj = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise fanqiao_sso.FanqiaoError("请求体不是合法 UTF-8 JSON", status_code=400) from exc
        stats = fanqiao_sso.sync_entitlements_batch(db, body_obj)
    except fanqiao_sso.FanqiaoError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None

    return {"success": True, **stats}


@fanqiao_router.post("/api/admin/integrations/fanqiao/reset-mappings")
def admin_reset_fanqiao_mappings(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """共享密钥轮换后整体清空 sub 映射表（仅限超级管理员）。"""
    out = fanqiao_sso.reset_fanqiao_identities(db)
    _audit(db, current_admin, "reset_fanqiao_mappings", "system", None, out)
    db.commit()
    return {"success": True, **out}
