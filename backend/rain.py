"""群内红包雨。

管理员在群里发 /rain，或按后台设定的钟点，在群里下一场红包雨。
钱由系统出，不扣管理员。总额、份数、落下间隔、每日赢取上限都在
公益配置里，总开关默认关。

一场雨底下是一个普通红包，抢的时候仍走 welfare_redpacket.claim_packet
（随机金额、行锁、一人一次）。份数按间隔放开：开场就能抢 1 份，
之后每隔 N 秒再放开 1 份。一个群同时只一场。

记账类型 redpacket_rain，和用户自己发的红包分开。过期没抢完的
直接作废，不退给挂名的管理员。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)

PACKET_TTL_HOURS = 24
LAST_SCHEDULE_KEY = "bot_rain_last_day"

REASON_MESSAGES = {
    "disabled": "红包雨未开启",
    "not_group": "请在群里发起红包雨",
    "not_staff": "仅管理员可以发起红包雨",
    "unbound": "请先绑定账号后再抢红包雨",
    "bad_config": "红包雨配置无效：总额不能小于份数",
    "busy": "本群已有一场红包雨",
    "not_free_realm": "红包雨仅在公益服开放",
    "account_disabled": "账号已被停用",
    "blacklisted": "你暂无红包雨资格",
    "too_new": "账号注册满 {min_days} 天后才能抢红包雨",
    "not_open": "下一份还没落下",
    "daily_cap": "今日红包雨赢取已达上限",
    "closed": "红包雨已经结束",
}


def reason_message(reason: str, **kw) -> str:
    text = REASON_MESSAGES.get(reason, "系统繁忙，请稍后再试")
    try:
        return text.format(**kw)
    except (KeyError, IndexError):
        return text


def is_enabled(db: Session) -> bool:
    return economy._get_bool_config(db, "bot_rain_enabled", False)


def total_amount(db: Session) -> int:
    return max(1, economy._get_int_config(db, "bot_rain_total", 100))


def packet_count(db: Session) -> int:
    return min(100, max(1, economy._get_int_config(db, "bot_rain_count", 10)))


def interval_seconds(db: Session) -> int:
    return min(3600, max(0, economy._get_int_config(db, "bot_rain_interval_sec", 3)))


def daily_win_cap(db: Session) -> int:
    return max(0, economy._get_int_config(db, "bot_rain_daily_win_cap", 200))


def schedule_clock(db: Session) -> tuple[int, int] | None:
    """后台填写的每天开场时间，空或写错就不当成定时。"""
    raw = str(economy._get_config(db, "bot_rain_schedule", "") or "").strip()
    matched = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not matched:
        return None
    hour, minute = int(matched.group(1)), int(matched.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def released_count(rain, now: datetime | None = None) -> int:
    """此刻已经落下、可以抢的份数。间隔 0 表示一开始就全部落下。"""
    now = now or datetime.now()
    total = int(rain.packet_count or 0)
    interval = int(rain.interval_sec or 0)
    if total <= 0:
        return 0
    if interval <= 0:
        return total
    started = rain.started_at or now
    elapsed = max(0, (now - started).total_seconds())
    return min(total, 1 + int(elapsed // interval))


def _today_win_sum(db: Session, user_id: int) -> int:
    today = economy._today_start_utc_naive()
    total = (
        db.query(func.coalesce(func.sum(models.PointsLog.amount), 0))
        .filter(
            models.PointsLog.user_id == user_id,
            models.PointsLog.type == "redpacket_rain",
            models.PointsLog.created_at >= today,
            models.PointsLog.amount > 0,
        )
        .scalar()
    )
    return int(total or 0)


def assert_share_available(db: Session, packet, now: datetime | None = None) -> None:
    """还没落到这一份时拒绝。调用方须已锁住红包行。"""
    rain = (
        db.query(models.RedPacketRain)
        .filter(models.RedPacketRain.id == int(packet.rain_id))
        .first()
    )
    if rain is None or rain.status != "running":
        raise ValueError(reason_message("closed"))
    released = released_count(rain, now)
    taken = int(packet.total_count) - int(packet.remaining_count)
    if taken >= released:
        raise ValueError(reason_message("not_open"))


def assert_claimer(db: Session, user: models.WebUser) -> None:
    """抢的人要在公益服、没被拉黑、号龄够。不看是不是管理员。"""
    from backend import lottery, subscriptions

    if not subscriptions.is_free_realm(db):
        raise ValueError(reason_message("not_free_realm"))
    if lottery.is_blacklisted(db, user.id):
        raise ValueError(reason_message("blacklisted"))
    min_days = lottery.min_account_age_days(db)
    if min_days > 0:
        created = getattr(user, "created_at", None)
        if created is not None and (datetime.now() - created).days < min_days:
            raise ValueError(reason_message("too_new", min_days=min_days))


def assert_within_daily_cap(db: Session, user_id: int, amount: int) -> None:
    cap = daily_win_cap(db)
    if cap <= 0:
        return
    if _today_win_sum(db, user_id) + int(amount) > cap:
        raise ValueError(reason_message("daily_cap"))


def _by_key(db: Session, key: str | None):
    if not key:
        return None
    return (
        db.query(models.RedPacketRain)
        .filter(models.RedPacketRain.idempotency_key == key)
        .first()
    )


def _live(db: Session, chat_id: int):
    return (
        db.query(models.RedPacketRainLive)
        .filter(models.RedPacketRainLive.chat_id == int(chat_id))
        .first()
    )


def start_rain(
    db: Session,
    admin: models.WebUser,
    chat_id: int,
    *,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    """开一场雨并建系统出资的红包。不扣管理员能量。"""
    if not is_enabled(db):
        return {"ok": False, "reason": "disabled"}
    if not getattr(admin, "is_active", True):
        return {"ok": False, "reason": "account_disabled"}
    if not bool(getattr(admin, "is_staff", False)):
        return {"ok": False, "reason": "not_staff"}
    from backend import subscriptions

    if not subscriptions.is_free_realm(db):
        return {"ok": False, "reason": "not_free_realm"}
    total = total_amount(db)
    count = packet_count(db)
    interval = interval_seconds(db)
    if total < count:
        return {"ok": False, "reason": "bad_config"}
    existing = _by_key(db, idempotency_key)
    if existing is not None:
        return {"ok": True, "rain": existing, "duplicate": True}
    if _live(db, chat_id) is not None:
        return {"ok": False, "reason": "busy"}

    now = now or datetime.now()
    packet_key = f"rainpkt:{idempotency_key}" if idempotency_key else None
    try:
        packet = models.RedPacket(
            sender_id=admin.id,
            total_amount=total,
            total_count=count,
            remaining_amount=total,
            remaining_count=count,
            expires_at=now + timedelta(hours=PACKET_TTL_HOURS),
            idempotency_key=packet_key,
            funding="system",
        )
        db.add(packet)
        db.flush()
        rain = models.RedPacketRain(
            chat_id=int(chat_id),
            packet_id=packet.id,
            total_amount=total,
            packet_count=count,
            interval_sec=interval,
            status="running",
            idempotency_key=idempotency_key,
            created_by=admin.id,
            created_at=now,
            started_at=now,
            shown_released=1 if interval > 0 else count,
        )
        db.add(rain)
        db.flush()
        packet.rain_id = rain.id
        db.add(models.RedPacketRainLive(chat_id=int(chat_id), rain_id=rain.id))
        db.commit()
        db.refresh(rain)
        return {"ok": True, "rain": rain, "duplicate": False}
    except IntegrityError:
        db.rollback()
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            logger.info("rain replay hit key=%s id=%s", idempotency_key, existing.id)
            return {"ok": True, "rain": existing, "duplicate": True}
        if _live(db, chat_id) is not None:
            return {"ok": False, "reason": "busy"}
        raise


def attach_panel(db: Session, rain_id: int, message_id: int) -> None:
    rain = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == int(rain_id)).first()
    if rain is None:
        return
    rain.message_id = int(message_id)
    rain.last_panel_edit_at = datetime.now()
    db.commit()


def fail_unannounced(db: Session, rain_id: int) -> None:
    """面板没发出去：把系统红包清零并结束，不能留一份没人看得见的奖。"""
    rain = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == int(rain_id)).first()
    if rain is None or rain.message_id or rain.status != "running":
        return
    packet = db.query(models.RedPacket).filter(models.RedPacket.id == rain.packet_id).first()
    if packet is not None and int(packet.remaining_count or 0) == int(packet.total_count or 0):
        packet.remaining_amount = 0
        packet.remaining_count = 0
    rain.status = "cancelled"
    rain.panel_finalized = True
    (
        db.query(models.RedPacketRainLive)
        .filter(models.RedPacketRainLive.rain_id == rain.id)
        .delete(synchronize_session=False)
    )
    db.commit()


def finish_if_drained(db: Session, rain_id: int, *, now: datetime | None = None):
    """份数抢完或红包过期时收场。返回结束后的场次，没结束返回 None。"""
    now = now or datetime.now()
    rain = db.query(models.RedPacketRain).filter(models.RedPacketRain.id == int(rain_id)).first()
    if rain is None or rain.status != "running":
        return None
    packet = db.query(models.RedPacket).filter(models.RedPacket.id == rain.packet_id).first()
    if packet is None:
        return None
    expired = bool(packet.expires_at and packet.expires_at <= now)
    if int(packet.remaining_count or 0) > 0 and not expired:
        return None
    rain.status = "done"
    (
        db.query(models.RedPacketRainLive)
        .filter(
            models.RedPacketRainLive.chat_id == rain.chat_id,
            models.RedPacketRainLive.rain_id == rain.id,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    db.refresh(rain)
    return rain


def _first_staff(db: Session):
    return (
        db.query(models.WebUser)
        .filter(models.WebUser.is_staff == True, models.WebUser.is_active == True)  # noqa: E712
        .order_by(models.WebUser.id)
        .first()
    )


def run_scheduled_starts(db: Session, now_sh: datetime | None = None) -> list[str]:
    """到了设定钟点，在启用群里各开一场。同一天不重复开。

    没配群或没有管理员时不写「今天已开过」，方便补上配置后当天还能开。
    """
    if not is_enabled(db):
        return []
    clock = schedule_clock(db)
    if clock is None:
        return []
    if now_sh is None:
        from backend.tg_bot.rankpush import _now_shanghai

        now_sh = _now_shanghai()
    if (now_sh.hour, now_sh.minute) < clock:
        return []
    day = now_sh.date().isoformat()
    from backend.integrations import store

    if str(store.read_value(db, LAST_SCHEDULE_KEY, "") or "") == day:
        return []
    groups = []
    raw = str(economy._get_config(db, "bot_group_ids", "") or "")
    from backend.tg_bot import policy

    groups = sorted(policy.parse_group_ids(raw))
    if not groups:
        logger.info("rain schedule skipped: no bot_group_ids")
        return ["no_group"]
    admin = _first_staff(db)
    if admin is None:
        logger.info("rain schedule skipped: no staff")
        return ["no_admin"]
    actions: list[str] = []
    for chat_id in groups:
        result = start_rain(
            db, admin, chat_id,
            idempotency_key=f"sched:{day}:{chat_id}",
            now=datetime.now(),
        )
        if result.get("ok"):
            actions.append(f"start:{chat_id}")
        else:
            actions.append(f"{result.get('reason')}:{chat_id}")
    store.write_values(db, {LAST_SCHEDULE_KEY: day})
    db.commit()
    return actions
