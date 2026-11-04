"""每周游戏榜：红包、老虎机、骰子对决、红包雨的净赢能量前十。

和观影周榜同一时段（周日 21:00，上海时间）发到同一个主群，海报沿用
观影榜的票根样式。只统计本周台账净额大于 0 的人。

没有可过期的称号体系（现在只有白名单固定称号，写在 codes.py，
不能按一周失效）。所以第一名只出现在海报上，不给账号加称号，
也不另发能量。开关默认关。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func

from backend import models
from backend.api import economy
from backend.integrations import store
from backend.tg_bot import layout, rank_poster, rankpush, sender

logger = logging.getLogger(__name__)

ENABLED_KEY = "bot_gamerank_enabled"
LAST_KEY = "bot_gamerank_last_weekly"

# 净赢 = 这些台账金额相加（支出是负数）。红包手续费算成本。
GAME_TYPES = (
    "redpacket",
    "redpacket_fee",
    "redpacket_rain",
    "slots_bet",
    "slots_win",
    "slots_jackpot",
    "slots_refund",
    "duel_stake",
    "duel_win",
    "duel_refund",
)


def is_enabled(db) -> bool:
    return economy._get_bool_config(db, ENABLED_KEY, False)


def weekly_rows(db, limit: int = 10) -> tuple[list[dict], datetime]:
    """本周净赢前 N。返回 (行, 上海周一 0 点)。净额不是正数的不上榜。"""
    from backend.emby_server.portal import leaderboard_window

    start_sh, start_utc = leaderboard_window("week")
    summed = (
        db.query(
            models.WebUser.id.label("user_id"),
            models.WebUser.username.label("username"),
            func.sum(models.PointsLog.amount).label("net"),
        )
        .join(models.PointsLog, models.PointsLog.user_id == models.WebUser.id)
        .filter(
            models.PointsLog.type.in_(GAME_TYPES),
            models.PointsLog.created_at >= start_utc,
        )
        .group_by(models.WebUser.id, models.WebUser.username)
        .having(func.sum(models.PointsLog.amount) > 0)
        .order_by(func.sum(models.PointsLog.amount).desc(), models.WebUser.id.asc())
        .limit(int(limit))
        .all()
    )
    rows = [
        {"user_id": int(row.user_id), "username": row.username or "", "net": int(row.net or 0)}
        for row in summed
    ]
    return rows, start_sh


def _caption(rows: list[dict], start_sh: datetime) -> str:
    return layout.card(
        layout.title("本周游戏榜"),
        [
            layout.row("📅", "统计周期", f"{start_sh.month}月{start_sh.day}日当周"),
            layout.row("🏆", "上榜人数", f"{len(rows)} 人 · 按净赢能量"),
            layout.row("🎮", "计入项目", "红包、老虎机、骰子对决、红包雨"),
        ],
    )


def push_game_rank(db) -> dict:
    """生成并推送本周游戏榜。返回 {pushed, reason, rows}。

    reason: ok / disabled / empty / no_group / send_failed。
    不发能量、不加称号。
    """
    if not is_enabled(db):
        return {"pushed": False, "reason": "disabled", "rows": []}
    rows, start_sh = weekly_rows(db)
    if not rows:
        logger.info("gamerank 本周无人净赢，跳过推送")
        return {"pushed": False, "reason": "empty", "rows": []}
    group_id = rankpush.target_group_id(db)
    if group_id is None:
        logger.warning("gamerank 未配置交流群（bot_group_ids），跳过推送")
        return {"pushed": False, "reason": "no_group", "rows": rows}
    poster_path = None
    try:
        poster_path = rank_poster.render_rank_poster(
            "week", rows, start_sh,
            headline="本周游戏榜",
            value_of=lambda row: f"+{int(row.get('net') or 0)}",
            session_label="周日 21:00 场",
            foot="Aetrix · 每周日 21:00 更新",
        )
        ok, err = sender.send_photo(
            db, group_id, _caption(rows, start_sh), photo_path=poster_path,
        )
    except Exception:
        logger.exception("gamerank 海报生成或发送失败")
        ok, err = False, "exception"
    finally:
        if poster_path is not None:
            try:
                poster_path.unlink(missing_ok=True)
            except OSError:
                pass
    if not ok:
        logger.warning("gamerank 推送失败 err=%s", err)
        return {"pushed": False, "reason": "send_failed", "rows": rows}
    logger.info("gamerank 已推送 group=%s rows=%s", group_id, len(rows))
    return {"pushed": True, "reason": "ok", "rows": rows}


def run_scheduled_tick(db, now_sh: datetime | None = None) -> list[str]:
    """周日 21:00 后推一次。时间和观影周榜用同一组常量，避免两套钟。"""
    now = now_sh or rankpush._now_shanghai()
    if not is_enabled(db):
        return []
    if now.weekday() != rankpush.WEEKLY_PUSH_WEEKDAY:
        return []
    if (now.hour, now.minute) < (rankpush.WEEKLY_PUSH_HOUR, rankpush.WEEKLY_PUSH_MINUTE):
        return []
    monday = rankpush.period_key("week")
    if str(store.read_value(db, LAST_KEY, "") or "") == monday:
        return []
    result = push_game_rank(db)
    if result["reason"] in ("ok", "empty"):
        store.write_values(db, {LAST_KEY: monday})
        db.commit()
        return [f"week:{result['reason']}" if not result["pushed"] else "week:pushed"]
    return [f"week:{result['reason']}"]
