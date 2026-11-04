"""群内骰子对决（/duel）。

规则：发起人先扣押注（冻结），群里第一个点「应战」的人押同额，自己不能
应自己。机器人用 Telegram sendDice 🎲 给双方各掷一次，点数大的拿走奖池，
系统按比例抽成（默认 5%，向下取整，零头留在奖池里）；平局各退、不抽成。
30 秒无人应战，发起人全额退回。一个群同时只开一局。

账务走 backend.api.economy（duel_stake / duel_win / duel_refund），
ref_id 带对局 id。发起幂等键 tg:<update_id>，应战幂等键
tgcq:<callback id>，poller 重放只认已有的一局，不重复扣、不重复发奖。
并发应战靠「status=open 才改得动」的条件更新，第二个人拿不到行。

资格沿用老虎机口径（公益服、未停用、未拉黑、号龄），但不看老虎机开关。
每日赢取上限按「拿到手里的奖金」计；结算时若赢家会超上限，双方全额退回，
不抽成、不让差额凭空消失。

开关 bot_duel_enabled 默认关。超时固定 30 秒（需求写死，不做可配）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
PANEL_EDIT_INTERVAL = 8
ROLLING_STALE_SECONDS = 90

TERMINAL_STATUSES = ("settled", "tie", "refunded", "failed")

REASON_MESSAGES = {
    "disabled": "骰子对决未开启",
    "not_group": "请在群里发起骰子对决",
    "unbound": "请先绑定账号后再玩骰子对决",
    "bad_amount": "请发送 /duel <能量>，例如 /duel 50",
    "below_min": "单注不能低于 {min_stake} 能量",
    "above_max": "单注不能高于 {max_stake} 能量",
    "bad_config": "对决注额配置无效，请让管理员检查上下限",
    "prize_empty": "抽成过高，胜者拿不到奖金",
    "prize_over_cap": "这一注的奖金超过每日赢取上限",
    "daily_cap": "今日赢取已达上限，明天再来",
    "busy": "本群已有一局对决进行中",
    "insufficient": "能量不足",
    "account_disabled": "账号已被停用，无法参加对决",
    "not_free_realm": "骰子对决仅在公益服开放",
    "blacklisted": "你暂无对决资格",
    "too_new": "账号注册满 {min_days} 天后才能参加对决",
    "self": "不能应战自己的对决",
    "taken": "已经有人应战了",
    "expired": "这局已过期，押注已退回",
    "closed": "这局对决已经结束",
    "rolling": "正在掷骰，请稍候",
    "settled": "这局已经结算过了",
    "bad_dice": "骰子结果无效，押注已退回",
}


def reason_message(reason: str, **kw) -> str:
    text = REASON_MESSAGES.get(reason, "系统繁忙，请稍后再试")
    try:
        return text.format(**kw)
    except (KeyError, IndexError):
        return text


def is_enabled(db: Session) -> bool:
    """总开关 bot_duel_enabled，默认关。"""
    return economy._get_bool_config(db, "bot_duel_enabled", False)


def stake_bounds(db: Session) -> tuple[int, int]:
    lo = max(1, economy._get_int_config(db, "bot_duel_min", 10))
    hi = max(1, economy._get_int_config(db, "bot_duel_max", 200))
    return lo, hi


def rake_pct(db: Session) -> int:
    return min(100, max(0, economy._get_int_config(db, "bot_duel_rake_pct", 5)))


def daily_win_cap(db: Session) -> int:
    """每日赢取上限（0 = 不限制），默认 500。只计真正拿到的奖金。"""
    return max(0, economy._get_int_config(db, "bot_duel_daily_win_cap", 500))


def split_pot(stake: int, pct: int) -> tuple[int, int]:
    """奖池 = 押注 × 2。抽成向下取整，返回 (抽成, 胜者所得)。"""
    pot = int(stake) * 2
    rake = pot * int(pct) // 100
    return rake, pot - rake


def _today_win_sum(db: Session, user_id: int) -> int:
    today = economy._today_start_utc_naive()
    total = (
        db.query(func.coalesce(func.sum(models.PointsLog.amount), 0))
        .filter(
            models.PointsLog.user_id == user_id,
            models.PointsLog.type == "duel_win",
            models.PointsLog.created_at >= today,
            models.PointsLog.amount > 0,
        )
        .scalar()
    )
    return int(total or 0)


def _cap_blocks(db: Session, user_id: int, prize: int) -> bool:
    cap = daily_win_cap(db)
    if cap <= 0:
        return False
    if prize > cap:
        return True
    return _today_win_sum(db, user_id) + prize > cap


def check_eligibility(db: Session, user: models.WebUser) -> dict:
    """返回 {"ok": True} 或 {"ok": False, "reason": ...}。不看总开关。"""
    if not getattr(user, "is_active", True):
        return {"ok": False, "reason": "account_disabled"}
    from backend import subscriptions

    if not subscriptions.is_free_realm(db):
        return {"ok": False, "reason": "not_free_realm"}
    from backend import lottery

    if lottery.is_blacklisted(db, user.id):
        return {"ok": False, "reason": "blacklisted"}
    min_days = lottery.min_account_age_days(db)
    if min_days > 0:
        created = getattr(user, "created_at", None)
        if created is not None and (datetime.now() - created).days < min_days:
            return {"ok": False, "reason": "too_new", "min_days": min_days}
    return {"ok": True}


def _by_key(db: Session, key: str | None) -> models.Duel | None:
    if not key:
        return None
    return (
        db.query(models.Duel)
        .filter(models.Duel.idempotency_key == key)
        .first()
    )


def _refund_stake(db: Session, user_id: int, stake: int, duel_id: int) -> None:
    """按 ref_id 退回押注。同一对局同一人只退一次。"""
    ref = f"duel:{duel_id}:refund:{user_id}"
    exists = (
        db.query(models.PointsLog.id)
        .filter(models.PointsLog.ref_id == ref, models.PointsLog.type == "duel_refund")
        .first()
    )
    if exists:
        return
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None or stake <= 0:
        logger.warning("duel refund skipped: duel=%s user=%s", duel_id, user_id)
        return
    economy._add_points(
        db, user, int(stake), "duel_refund",
        f"骰子对决退回 {int(stake)} 能量", ref,
    )


def _release_live(db: Session, chat_id: int, duel_id: int) -> None:
    (
        db.query(models.DuelLive)
        .filter(
            models.DuelLive.chat_id == int(chat_id),
            models.DuelLive.duel_id == int(duel_id),
        )
        .delete(synchronize_session=False)
    )


def _load(db: Session, duel_id: int) -> models.Duel | None:
    return db.query(models.Duel).filter(models.Duel.id == int(duel_id)).first()


def create_duel(
    db: Session,
    user: models.WebUser,
    chat_id: int,
    stake: int,
    *,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    """开局并冻结发起人押注。

    返回 {"ok": True, "duel": row, "duplicate": bool} 或
    {"ok": False, "reason": ...}。duplicate 为 poller 重放命中已有局。
    """
    if not is_enabled(db):
        return {"ok": False, "reason": "disabled"}
    eligibility = check_eligibility(db, user)
    if not eligibility.get("ok"):
        return eligibility
    try:
        stake = int(stake)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_amount"}
    if stake <= 0:
        return {"ok": False, "reason": "bad_amount"}
    lo, hi = stake_bounds(db)
    if lo > hi:
        return {"ok": False, "reason": "bad_config"}
    if stake < lo:
        return {"ok": False, "reason": "below_min", "min_stake": lo}
    if stake > hi:
        return {"ok": False, "reason": "above_max", "max_stake": hi}
    pct = rake_pct(db)
    rake, prize = split_pot(stake, pct)
    if prize <= 0:
        return {"ok": False, "reason": "prize_empty"}
    cap = daily_win_cap(db)
    if cap > 0 and prize > cap:
        return {"ok": False, "reason": "prize_over_cap"}

    existing = _by_key(db, idempotency_key)
    if existing is not None:
        return {"ok": True, "duel": existing, "duplicate": True}

    now = now or datetime.now()
    economy.lock_user_row(db, user.id)
    try:
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            db.rollback()
            return {"ok": True, "duel": existing, "duplicate": True}
        if (
            db.query(models.DuelLive.chat_id)
            .filter(models.DuelLive.chat_id == int(chat_id))
            .first()
        ):
            db.rollback()
            return {"ok": False, "reason": "busy"}
        if _cap_blocks(db, user.id, prize):
            db.rollback()
            return {"ok": False, "reason": "daily_cap"}
        row = models.Duel(
            chat_id=int(chat_id),
            initiator_id=user.id,
            stake=stake,
            rake_pct=pct,
            status="open",
            idempotency_key=idempotency_key,
            created_at=now,
            expires_at=now + timedelta(seconds=TIMEOUT_SECONDS),
        )
        db.add(row)
        db.flush()
        try:
            economy._spend_points(
                db, user, stake, "duel_stake",
                f"骰子对决押注 {stake} 能量",
                ref_id=f"duel:{row.id}:stake:{user.id}",
            )
        except economy.InsufficientPoints:
            db.rollback()
            return {"ok": False, "reason": "insufficient"}
        db.add(models.DuelLive(chat_id=int(chat_id), duel_id=row.id))
        db.commit()
        db.refresh(row)
        return {"ok": True, "duel": row, "duplicate": False}
    except IntegrityError:
        db.rollback()
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            logger.info("duel replay hit key=%s id=%s", idempotency_key, existing.id)
            return {"ok": True, "duel": existing, "duplicate": True}
        if (
            db.query(models.DuelLive.chat_id)
            .filter(models.DuelLive.chat_id == int(chat_id))
            .first()
        ):
            return {"ok": False, "reason": "busy"}
        raise


def attach_panel(db: Session, duel_id: int, message_id: int) -> None:
    row = _load(db, duel_id)
    if row is None:
        return
    row.message_id = int(message_id)
    row.last_panel_edit_at = datetime.now()
    db.commit()


def fail_unannounced(db: Session, duel_id: int) -> None:
    """面板没发出去：进行中的局全额退回，避免押注冻在看不见的局里。"""
    row = _load(db, duel_id)
    if row is None or row.status != "open" or row.message_id:
        return
    _refund_stake(db, row.initiator_id, row.stake, row.id)
    row.status = "refunded"
    row.settled_at = datetime.now()
    row.panel_finalized = True
    _release_live(db, row.chat_id, row.id)
    db.commit()


def _diagnose_accept(db: Session, duel_id: int, user_id: int, accept_key: str) -> dict:
    row = _load(db, duel_id)
    if row is None:
        return {"ok": False, "reason": "closed"}
    if row.accept_key == accept_key:
        return {"ok": True, "duel": row, "duplicate": True}
    if row.status == "open" and row.initiator_id == user_id:
        return {"ok": False, "reason": "self"}
    if row.status == "open" and row.expires_at and row.expires_at <= datetime.now():
        expired = expire_if_due(db, duel_id)
        return {"ok": False, "reason": "expired", "duel": expired}
    if row.status == "rolling" and row.accept_key == accept_key:
        return {"ok": True, "duel": row, "duplicate": True}
    if row.status == "rolling":
        return {"ok": False, "reason": "taken"}
    if row.status in TERMINAL_STATUSES:
        return {"ok": False, "reason": "closed", "duel": row}
    return {"ok": False, "reason": "closed", "duel": row}


def accept_duel(
    db: Session,
    user: models.WebUser,
    duel_id: int,
    accept_key: str,
    *,
    now: datetime | None = None,
) -> dict:
    """应战：条件更新抢到 open 行才扣同额押注。"""
    eligibility = check_eligibility(db, user)
    if not eligibility.get("ok"):
        return eligibility
    now = now or datetime.now()
    try:
        claimed = (
            db.query(models.Duel)
            .filter(
                models.Duel.id == int(duel_id),
                models.Duel.status == "open",
                models.Duel.initiator_id != user.id,
                models.Duel.expires_at > now,
            )
            .update(
                {
                    models.Duel.status: "rolling",
                    models.Duel.challenger_id: user.id,
                    models.Duel.accept_key: accept_key,
                    models.Duel.accepted_at: now,
                },
                synchronize_session="fetch",
            )
        )
        if claimed != 1:
            db.rollback()
            return _diagnose_accept(db, duel_id, user.id, accept_key)
        row = _load(db, duel_id)
        if row is None:
            db.rollback()
            return {"ok": False, "reason": "closed"}
        _rake, prize = split_pot(row.stake, row.rake_pct)
        economy.lock_user_row(db, user.id)
        if _cap_blocks(db, user.id, prize):
            db.rollback()
            return {"ok": False, "reason": "daily_cap"}
        try:
            economy._spend_points(
                db, user, int(row.stake), "duel_stake",
                f"骰子对决应战 {int(row.stake)} 能量",
                ref_id=f"duel:{row.id}:stake:{user.id}",
            )
        except economy.InsufficientPoints:
            db.rollback()
            return {"ok": False, "reason": "insufficient"}
        db.commit()
        db.refresh(row)
        return {"ok": True, "duel": row, "duplicate": False}
    except IntegrityError:
        db.rollback()
        return _diagnose_accept(db, duel_id, user.id, accept_key)


def settle_duel(
    db: Session,
    duel_id: int,
    initiator_dice: int,
    challenger_dice: int,
    *,
    now: datetime | None = None,
) -> dict:
    """按两枚骰子结算。已不是 rolling 的重入只返回现状，不重复发奖。"""
    if not (isinstance(initiator_dice, int) and 1 <= initiator_dice <= 6):
        return {"ok": False, "reason": "bad_dice"}
    if not (isinstance(challenger_dice, int) and 1 <= challenger_dice <= 6):
        return {"ok": False, "reason": "bad_dice"}
    now = now or datetime.now()
    # 先把 rolling 改成 settling（同一事务里再写成终态）。第二个结算者
    # 改不到行，就不会重复发奖。事务没提交就崩了会回到 rolling，残局
    # 超时后由 abort 退回。
    claimed = (
        db.query(models.Duel)
        .filter(models.Duel.id == int(duel_id), models.Duel.status == "rolling")
        .update({models.Duel.status: "settling"}, synchronize_session="fetch")
    )
    if claimed != 1:
        db.rollback()
        existing = _load(db, duel_id)
        if existing is None:
            return {"ok": False, "reason": "closed"}
        return {"ok": True, "duel": existing, "duplicate": True}
    row = _load(db, duel_id)
    if row is None or row.challenger_id is None:
        db.rollback()
        return {"ok": False, "reason": "closed"}
    for uid in sorted({row.initiator_id, row.challenger_id}):
        economy.lock_user_row(db, uid)
    db.refresh(row)
    if row.status != "settling":
        db.rollback()
        return {"ok": True, "duel": row, "duplicate": True}

    row.initiator_dice = int(initiator_dice)
    row.challenger_dice = int(challenger_dice)
    row.settled_at = now
    rake, prize = split_pot(row.stake, row.rake_pct)
    if initiator_dice == challenger_dice:
        _refund_stake(db, row.initiator_id, row.stake, row.id)
        _refund_stake(db, row.challenger_id, row.stake, row.id)
        row.status = "tie"
        row.rake = 0
        row.prize = 0
        row.winner_id = None
    else:
        winner_id = row.initiator_id if initiator_dice > challenger_dice else row.challenger_id
        if _cap_blocks(db, winner_id, prize):
            _refund_stake(db, row.initiator_id, row.stake, row.id)
            _refund_stake(db, row.challenger_id, row.stake, row.id)
            row.status = "refunded"
            row.rake = 0
            row.prize = 0
            row.winner_id = None
        else:
            winner = db.query(models.WebUser).filter(models.WebUser.id == winner_id).first()
            if winner is not None and prize > 0:
                economy._add_points(
                    db, winner, int(prize), "duel_win",
                    f"骰子对决获胜 +{int(prize)} 能量",
                    ref_id=f"duel:{row.id}:win",
                )
            row.status = "settled"
            row.winner_id = winner_id
            row.rake = int(rake)
            row.prize = int(prize)
    _release_live(db, row.chat_id, row.id)
    db.commit()
    db.refresh(row)
    logger.info(
        "duel settled id=%s status=%s dice=%s/%s prize=%s",
        row.id, row.status, initiator_dice, challenger_dice, row.prize,
    )
    return {"ok": True, "duel": row, "duplicate": False}


def abort_roll(db: Session, duel_id: int, *, reason: str = "dice_failed",
               now: datetime | None = None, stale_before: datetime | None = None) -> models.Duel | None:
    """掷骰失败或 rolling 卡住：双方退回。stale_before 给定时只收过期残局。"""
    now = now or datetime.now()
    query = db.query(models.Duel).filter(
        models.Duel.id == int(duel_id),
        models.Duel.status == "rolling",
    )
    if stale_before is not None:
        query = query.filter(models.Duel.accepted_at <= stale_before)
    claimed = query.update(
        {models.Duel.status: "failed", models.Duel.settled_at: now},
        synchronize_session="fetch",
    )
    if claimed != 1:
        db.rollback()
        return None
    row = _load(db, duel_id)
    if row is None:
        db.rollback()
        return None
    _refund_stake(db, row.initiator_id, row.stake, row.id)
    if row.challenger_id:
        _refund_stake(db, row.challenger_id, row.stake, row.id)
    _release_live(db, row.chat_id, row.id)
    db.commit()
    db.refresh(row)
    logger.info("duel aborted id=%s reason=%s", row.id, reason)
    return row


def expire_if_due(db: Session, duel_id: int, *, now: datetime | None = None) -> models.Duel | None:
    """无人应战且已过期：退回发起人。没到期或已被抢走则返回 None。"""
    now = now or datetime.now()
    claimed = (
        db.query(models.Duel)
        .filter(
            models.Duel.id == int(duel_id),
            models.Duel.status == "open",
            models.Duel.expires_at <= now,
        )
        .update(
            {models.Duel.status: "refunded", models.Duel.settled_at: now},
            synchronize_session="fetch",
        )
    )
    if claimed != 1:
        db.rollback()
        return None
    row = _load(db, duel_id)
    if row is None:
        db.rollback()
        return None
    _refund_stake(db, row.initiator_id, row.stake, row.id)
    _release_live(db, row.chat_id, row.id)
    db.commit()
    db.refresh(row)
    logger.info("duel expired id=%s stake=%s", row.id, row.stake)
    return row


def run_scheduled_tick(db: Session, now: datetime | None = None) -> list[str]:
    """超时退回、卡住的掷骰退回、补编辑终态面板、刷新倒计时。

    面板编辑由调用方注入（tg_bot.duel_ui.tick），这里只动账。
    返回动作 id 列表，供调度日志使用。
    """
    now = now or datetime.now()
    actions: list[str] = []
    due_ids = [
        row_id for (row_id,) in db.query(models.Duel.id).filter(
            models.Duel.status == "open",
            models.Duel.expires_at <= now,
        ).all()
    ]
    for duel_id in due_ids:
        if expire_if_due(db, duel_id, now=now) is not None:
            actions.append(f"expire:{duel_id}")
    stale_before = now - timedelta(seconds=ROLLING_STALE_SECONDS)
    stale_ids = [
        row_id for (row_id,) in db.query(models.Duel.id).filter(
            models.Duel.status == "rolling",
            models.Duel.accepted_at.isnot(None),
            models.Duel.accepted_at <= stale_before,
        ).all()
    ]
    for duel_id in stale_ids:
        if abort_roll(db, duel_id, reason="stale", now=now, stale_before=stale_before) is not None:
            actions.append(f"abort:{duel_id}")
    return actions
