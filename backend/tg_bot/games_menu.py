"""群 /start 里的「游戏」入口。

只列出当前开着的群内游戏。关掉的不出现。骰子对决和红包雨默认关，
所以默认菜单里看不到它们。每周游戏榜是周日海报，不是能点的一局，不放这里。
"""
from __future__ import annotations

from backend.tg_bot import layout, redpacket_common


def enabled_entries(db) -> list[dict]:
    """已开启的游戏按钮，顺序固定。"""
    entries: list[dict] = []
    from backend import slots as slots_svc

    if slots_svc.is_enabled(db) and slots_svc.group_enabled(db):
        entries.append({"text": "🎰 老虎机", "callback_data": "menu:slots"})
    from backend import lottery

    try:
        lottery_on = bool(lottery.is_enabled(db))
    except Exception:
        lottery_on = False
    if lottery_on:
        entries.append({"text": "🎲 抽奖", "callback_data": "menu:lottery"})
    if redpacket_common.enabled(db):
        entries.append({"text": "🧧 红包", "callback_data": "menu:redpacket"})
    from backend import duel as duel_svc

    if duel_svc.is_enabled(db):
        entries.append({"text": "🎲 对决", "callback_data": "menu:duel"})
    from backend import rain as rain_svc

    if rain_svc.is_enabled(db):
        entries.append({"text": "🌧 红包雨", "callback_data": "menu:rain"})
    return entries


def any_enabled(db) -> bool:
    # 没传数据库时按默认开关：老虎机、抽奖、红包是开的，入口要在。
    if db is None:
        return True
    return bool(enabled_entries(db))


def _two_col(buttons: list[dict]) -> list[list[dict]]:
    return [list(buttons[i:i + 2]) for i in range(0, len(buttons), 2)]


def render(db) -> tuple[str, dict]:
    entries = enabled_entries(db)
    if not entries:
        text = layout.card(
            layout.title("游戏"),
            [layout.row("🎮", "当前状态", "群里的游戏都关着")],
        )
        return text, layout.back_markup()
    names = "、".join(item["text"].split(" ", 1)[-1] for item in entries)
    text = layout.card(
        layout.title("点一个游戏开始"),
        [layout.row("🎮", "正在开放", names)],
    )
    rows = _two_col(entries)
    rows.append(layout.home_row())
    return text, {"inline_keyboard": rows}
