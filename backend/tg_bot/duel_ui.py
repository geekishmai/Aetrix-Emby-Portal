"""骰子对决的群面板文案与编辑。

进行中只编辑原面板（倒计时至少隔 8 秒一改，避开 Telegram 编辑限频）。
结束收成一行结果，再按 bot_msg_autodelete_seconds 自毁。骰子动画是
Telegram 原生消息，没法编辑进面板，发出后按同一自毁时间删掉。
醒目结果就是收成的那一行，不再另发一条，避免两套结果刷屏。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from backend import models
from backend.duel import (
    PANEL_EDIT_INTERVAL,
    TIMEOUT_SECONDS,
    split_pot,
)
from backend.tg_bot import ephemeral, layout, sender

logger = logging.getLogger(__name__)

SILENT = "__silent__"
PANEL = "__duel_panel__"


def _name(user) -> str:
    if user is None:
        return "玩家"
    return html.escape(str(user.username or f"玩家{user.id}"))


def _user(db, user_id: int | None):
    if not user_id:
        return None
    return db.query(models.WebUser).filter(models.WebUser.id == int(user_id)).first()


def remaining_seconds(duel, now: datetime | None = None) -> int:
    now = now or datetime.now()
    if duel.expires_at is None:
        return 0
    return max(0, int((duel.expires_at - now).total_seconds()))


def open_text(duel, initiator_name: str, *, now: datetime | None = None) -> str:
    """进行中面板。第一行写清怎么玩、押多少、赢多少、还剩几秒。"""
    _rake, prize = split_pot(duel.stake, duel.rake_pct)
    left = remaining_seconds(duel, now)
    summary = f"押 {duel.stake} 应战，胜者拿 {prize}，剩 {left} 秒"
    return layout.card(
        layout.title(summary),
        [
            layout.row("🎲", "玩法说明", "两人各掷一次骰子，点数大的赢，平局各退"),
            layout.row("💰", "押注能量", f"<b>{int(duel.stake)}</b> 能量"),
            layout.row("🏆", "胜者可得", f"<b>{prize}</b> 能量（系统抽 {int(duel.rake_pct)}%）"),
            layout.row("⏰", "剩余秒数", f"<b>{left}</b> 秒，无人应战全额退回"),
            layout.row("👥", "参与人数", f"<b>1</b> / 2 · 发起人 <b>{initiator_name}</b>"),
        ],
    )


def open_markup(duel_id: int, stake: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": f"应战 {int(stake)}", "callback_data": f"duel_accept:{int(duel_id)}"},
        ]]
    }


def result_line(db, duel) -> str:
    """终态一行。谁赢了多少，或为什么退回。"""
    initiator = _name(_user(db, duel.initiator_id))
    challenger = _name(_user(db, duel.challenger_id))
    stake = int(duel.stake or 0)
    if duel.status == "settled" and duel.winner_id:
        winner = initiator if duel.winner_id == duel.initiator_id else challenger
        loser = challenger if duel.winner_id == duel.initiator_id else initiator
        win_dice = duel.initiator_dice if duel.winner_id == duel.initiator_id else duel.challenger_dice
        lose_dice = duel.challenger_dice if duel.winner_id == duel.initiator_id else duel.initiator_dice
        return (
            f"🎉 {winner} {win_dice} 点胜 {loser} {lose_dice} 点，"
            f"赢走 {int(duel.prize)} 能量（系统抽 {int(duel.rake)}）"
        )
    if duel.status == "tie":
        return f"🤝 平局（都是 {duel.initiator_dice} 点），{initiator} 与 {challenger} 各退 {stake} 能量"
    if duel.status == "failed":
        return f"⚠️ 掷骰失败，押注已退回（各 {stake} 能量）"
    if duel.status == "refunded" and duel.initiator_dice and duel.challenger_dice:
        return f"⚠️ 今日赢取已到上限，本局押注已退回（各 {stake} 能量）"
    return f"⌛ {TIMEOUT_SECONDS} 秒无人应战，{stake} 能量已退回 {initiator}"


def _edit_ok(ok: bool, err: str | None) -> bool:
    if ok:
        return True
    return bool(err) and "not modified" in str(err).lower()


def publish_terminal(db, duel) -> bool:
    """把终态收成一行并按现有自毁规则挂删除。编辑失败留给下一拍重试。"""
    if duel is None or duel.status not in ("settled", "tie", "refunded", "failed"):
        return False
    if duel.panel_finalized:
        return True
    if not duel.message_id:
        duel.panel_finalized = True
        db.commit()
        return True
    line = result_line(db, duel)
    ok, err = sender.edit_message_text(
        db, int(duel.chat_id), int(duel.message_id), line, {"inline_keyboard": []},
    )
    if not _edit_ok(ok, err):
        logger.warning("duel terminal edit failed id=%s err=%s", duel.id, err)
        db.rollback()
        return False
    delay = ephemeral.bot_msg_delay_seconds(db)
    if delay > 0:
        ephemeral.schedule_delete(int(duel.chat_id), int(duel.message_id), delay)
    fresh = db.query(models.Duel).filter(models.Duel.id == duel.id).first()
    if fresh is not None:
        fresh.panel_finalized = True
        db.commit()
    return True


def refresh_open_panels(db, now: datetime | None = None) -> int:
    """进行中的面板刷新剩余秒数。距上次编辑不足 8 秒的跳过。"""
    now = now or datetime.now()
    rows = (
        db.query(models.Duel)
        .filter(models.Duel.status == "open", models.Duel.message_id.isnot(None))
        .all()
    )
    updated = 0
    for duel in rows:
        if duel.last_panel_edit_at is not None:
            age = (now - duel.last_panel_edit_at).total_seconds()
            if age < PANEL_EDIT_INTERVAL:
                continue
        initiator = _name(_user(db, duel.initiator_id))
        text = open_text(duel, initiator, now=now)
        ok, err = sender.edit_message_text(
            db, int(duel.chat_id), int(duel.message_id), text,
            open_markup(duel.id, duel.stake),
        )
        if not _edit_ok(ok, err):
            logger.debug("duel countdown edit skipped id=%s err=%s", duel.id, err)
            db.rollback()
            continue
        fresh = db.query(models.Duel).filter(models.Duel.id == duel.id).first()
        if fresh is not None and fresh.status == "open":
            fresh.last_panel_edit_at = now
            db.commit()
            updated += 1
        else:
            db.rollback()
    return updated


def tick(db, now: datetime | None = None) -> list[str]:
    """调度一拍：先动账，再补终态面板，最后刷新倒计时。"""
    from backend import duel as duel_svc

    now = now or datetime.now()
    actions = duel_svc.run_scheduled_tick(db, now=now)
    pending = (
        db.query(models.Duel)
        .filter(
            models.Duel.status.in_(("settled", "tie", "refunded", "failed")),
            models.Duel.panel_finalized == False,  # noqa: E712
            models.Duel.message_id.isnot(None),
        )
        .all()
    )
    for duel in pending:
        if publish_terminal(db, duel):
            actions.append(f"panel:{duel.id}")
    refreshed = refresh_open_panels(db, now=now)
    if refreshed:
        actions.append(f"refresh:{refreshed}")
    return actions
