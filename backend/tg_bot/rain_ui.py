"""红包雨面板。

一场雨只发一条面板，份数落下或被人抢走时改这条，不连发 N 条消息。
个人抢到多少只弹给本人。结束收成一行（谁抢得最多），再按群消息自毁时间删。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from sqlalchemy import func

from backend import models
from backend.rain import released_count
from backend.tg_bot import ephemeral, layout, sender

logger = logging.getLogger(__name__)

SILENT = "__silent__"
PANEL = "__rain_panel__"


def _name(user) -> str:
    if user is None:
        return "玩家"
    return html.escape(str(user.username or f"玩家{user.id}"))


def _packet(db, rain):
    return db.query(models.RedPacket).filter(models.RedPacket.id == rain.packet_id).first()


def _participants(db, packet_id: int) -> int:
    total = (
        db.query(func.count(func.distinct(models.RedPacketClaim.user_id)))
        .filter(models.RedPacketClaim.packet_id == int(packet_id))
        .scalar()
    )
    return int(total or 0)


def open_text(db, rain, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    packet = _packet(db, rain)
    released = released_count(rain, now)
    remaining = int(packet.remaining_count) if packet is not None else int(rain.packet_count)
    taken = int(rain.packet_count) - remaining
    if int(rain.interval_sec or 0) <= 0 or released >= int(rain.packet_count):
        nxt = "已经全部落下"
    else:
        nxt = f"每 {int(rain.interval_sec)} 秒再落 1 份"
    summary = f"红包雨：总额 {int(rain.total_amount)}，共 {int(rain.packet_count)} 份，已落下 {released} 份"
    return layout.card(
        layout.title(summary),
        [
            layout.row("🧧", "玩法说明", "系统出的红包，点「抢」领一份，先到先得"),
            layout.row("💰", "红包总额", f"<b>{int(rain.total_amount)}</b> 能量，分成 <b>{int(rain.packet_count)}</b> 份"),
            layout.row("📦", "剩余份数", f"已抢 <b>{taken}</b> 份，还剩 <b>{remaining}</b> 份"),
            layout.row("⏰", "落下进度", nxt),
            layout.row("👥", "参与人数", f"<b>{_participants(db, rain.packet_id)}</b> 人"),
        ],
    )


def open_markup(rain_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "抢", "callback_data": f"rain_grab:{int(rain_id)}"},
        ]]
    }


def result_line(db, rain) -> str:
    packet = _packet(db, rain)
    if packet is None:
        return "红包雨结束"
    top = (
        db.query(models.RedPacketClaim, models.WebUser)
        .join(models.WebUser, models.WebUser.id == models.RedPacketClaim.user_id)
        .filter(models.RedPacketClaim.packet_id == packet.id)
        .order_by(models.RedPacketClaim.amount.desc(), models.RedPacketClaim.id.asc())
        .first()
    )
    people = _participants(db, packet.id)
    if top is None:
        return "红包雨结束，没有人抢到"
    claim, user = top
    return (
        f"🎉 红包雨结束：{_name(user)} 抢到最多 {int(claim.amount)} 能量，"
        f"共 {people} 人参与"
    )


def _edit_ok(ok: bool, err: str | None) -> bool:
    if ok:
        return True
    return bool(err) and "not modified" in str(err).lower()


def publish_terminal(db, rain) -> bool:
    if rain is None or rain.status not in ("done", "cancelled"):
        return False
    if rain.panel_finalized:
        return True
    if not rain.message_id:
        rain.panel_finalized = True
        db.commit()
        return True
    line = result_line(db, rain)
    ok, err = sender.edit_message_text(
        db, int(rain.chat_id), int(rain.message_id), line, {"inline_keyboard": []},
    )
    if not _edit_ok(ok, err):
        logger.warning("rain terminal edit failed id=%s err=%s", rain.id, err)
        db.rollback()
        return False
    delay = ephemeral.bot_msg_delay_seconds(db)
    if delay > 0:
        ephemeral.schedule_delete(int(rain.chat_id), int(rain.message_id), delay)
    fresh = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == rain.id).first()
    if fresh is not None:
        fresh.panel_finalized = True
        db.commit()
    return True


def refresh_panel(db, rain, *, now: datetime | None = None) -> None:
    if rain is None or rain.status != "running" or not rain.message_id:
        return
    now = now or datetime.now()
    text = open_text(db, rain, now=now)
    ok, err = sender.edit_message_text(
        db, int(rain.chat_id), int(rain.message_id), text, open_markup(rain.id),
    )
    if not _edit_ok(ok, err):
        db.rollback()
        return
    fresh = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == rain.id).first()
    if fresh is not None and fresh.status == "running":
        fresh.last_panel_edit_at = now
        fresh.shown_released = released_count(fresh, now)
        db.commit()
    else:
        db.rollback()


def after_claim(db, rain_id: int | None) -> None:
    """有人抢到之后：没抢完就改剩余份数，抢完就收成一行。"""
    if not rain_id:
        return
    from backend import rain as rain_svc

    finished = rain_svc.finish_if_drained(db, int(rain_id))
    rain = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == int(rain_id)).first()
    if rain is None:
        return
    if finished is not None or rain.status != "running":
        publish_terminal(db, rain)
        return
    refresh_panel(db, rain)


def _send_missing_panels(db) -> list[str]:
    """定时开的雨还没发面板时补发。命令开的雨由路由当场发。"""
    from backend import rain as rain_svc

    rows = (
        db.query(models.RedPacketRain)
        .filter(
            models.RedPacketRain.status == "running",
            models.RedPacketRain.message_id.is_(None),
        )
        .all()
    )
    actions = []
    for rain in rows:
        text = open_text(db, rain)
        ok, err, mid = sender.send_message_ex(db, int(rain.chat_id), text, open_markup(rain.id))
        if ok and mid:
            rain_svc.attach_panel(db, rain.id, mid)
            actions.append(f"panel:{rain.id}")
        else:
            logger.warning("rain panel send failed id=%s err=%s", rain.id, err)
            rain_svc.fail_unannounced(db, rain.id)
            actions.append(f"cancel:{rain.id}")
    return actions


def tick(db, now_sh: datetime | None = None, *, now: datetime | None = None) -> list[str]:
    from backend import rain as rain_svc

    now = now or datetime.now()
    actions = list(rain_svc.run_scheduled_starts(db, now_sh))
    actions.extend(_send_missing_panels(db))
    running = (
        db.query(models.RedPacketRain)
        .filter(models.RedPacketRain.status == "running")
        .all()
    )
    for rain in running:
        finished = rain_svc.finish_if_drained(db, rain.id, now=now)
        if finished is not None:
            publish_terminal(db, finished)
            actions.append(f"done:{rain.id}")
            continue
        if not rain.message_id:
            continue
        released = released_count(rain, now)
        if released == int(rain.shown_released or 0):
            continue
        if rain.last_panel_edit_at is not None:
            age = (now - rain.last_panel_edit_at).total_seconds()
            if age < 3:
                continue
        refresh_panel(db, rain, now=now)
        actions.append(f"refresh:{rain.id}")
    pending = (
        db.query(models.RedPacketRain)
        .filter(
            models.RedPacketRain.status.in_(("done", "cancelled")),
            models.RedPacketRain.panel_finalized == False,  # noqa: E712
            models.RedPacketRain.message_id.isnot(None),
        )
        .all()
    )
    for rain in pending:
        if publish_terminal(db, rain):
            actions.append(f"panel-end:{rain.id}")
    return actions
