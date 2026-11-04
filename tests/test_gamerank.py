"""每周游戏榜：净赢前十、周日同时段、只发海报不加称号。"""
from __future__ import annotations

import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import welfare_admin
from backend import config_schema
from backend.tg_bot import gamerank, rank_poster, sender

GROUP = -100950001
CJK4 = re.compile(r"^[一-鿿]{4}$")
LABEL_RE = re.compile(r"· \S+ <b>([^<]+)</b> \|")
SUNDAY_NIGHT = datetime(2026, 11, 1, 21, 30)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, name, tg):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=0, vitality=14, is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _log(db, user, amount, type_, when=None):
    row = models.PointsLog(
        user_id=user.id, amount=amount, balance_after=0,
        type=type_, description="t",
    )
    if when is not None:
        row.created_at = when
    db.add(row)
    db.commit()


def _cfg(db, key, value):
    db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def test_switch_defaults_off():
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_gamerank_enabled"] == "false"
    assert config_schema.WELFARE_CONFIG_SCHEMA["bot_gamerank_enabled"] == ("bool",)


def test_net_win_ranks_four_games_and_drops_non_positive(db):
    alice = _user(db, "alice", 1)
    bob = _user(db, "bob", 2)
    cara = _user(db, "cara", 3)
    dave = _user(db, "dave", 4)
    eve = _user(db, "eve", 5)
    # 红包净赢含手续费：100 - 10 = 90
    _log(db, alice, 100, "redpacket")
    _log(db, alice, -10, "redpacket_fee")
    # 老虎机：下注 20，中 50，净 30
    _log(db, bob, -20, "slots_bet")
    _log(db, bob, 50, "slots_win")
    # 对决：押 50，赢回 95，净 45
    _log(db, cara, -50, "duel_stake")
    _log(db, cara, 95, "duel_win")
    # 红包雨
    _log(db, dave, 12, "redpacket_rain")
    # 只输钱的不上榜；签到不算游戏
    _log(db, eve, -40, "redpacket")
    _log(db, eve, 500, "checkin")
    # 上周的赢分不算
    _log(db, eve, 999, "slots_win", when=datetime(2020, 1, 1, 12, 0))

    rows, _start = gamerank.weekly_rows(db)
    assert [row["username"] for row in rows] == ["alice", "cara", "bob", "dave"]
    assert [row["net"] for row in rows] == [90, 45, 30, 12]


def test_top_ten_only(db):
    for i in range(12):
        user = _user(db, f"u{i}", 100 + i)
        _log(db, user, 100 - i, "redpacket_rain")
    rows, _start = gamerank.weekly_rows(db)
    assert len(rows) == 10
    assert rows[0]["net"] == 100
    assert rows[-1]["net"] == 91


def test_sunday_push_once_and_no_reward(db, monkeypatch):
    assert SUNDAY_NIGHT.weekday() == 6
    alice = _user(db, "alice", 1)
    _log(db, alice, 40, "duel_win")
    _cfg(db, "bot_gamerank_enabled", "true")
    _cfg(db, "bot_group_ids", str(GROUP))
    sent = []
    monkeypatch.setattr(
        sender, "send_photo",
        lambda *a, **kw: sent.append(a[2]) or (True, None),
    )
    before = db.query(models.PointsLog).count()
    actions = gamerank.run_scheduled_tick(db, SUNDAY_NIGHT)
    assert actions == ["week:pushed"]
    assert sent and "本周游戏榜" in sent[0]
    for label in LABEL_RE.findall(sent[0]):
        assert CJK4.match(label), label
    assert "红包雨" in sent[0]
    assert db.query(models.PointsLog).count() == before
    assert gamerank.run_scheduled_tick(db, SUNDAY_NIGHT) == []
    assert db.query(models.PointsLog).count() == before


def test_empty_and_disabled_do_not_send(db, monkeypatch):
    sent = []
    monkeypatch.setattr(sender, "send_photo", lambda *a, **kw: sent.append(1) or (True, None))
    assert gamerank.run_scheduled_tick(db, SUNDAY_NIGHT) == []
    assert sent == []
    _cfg(db, "bot_gamerank_enabled", "true")
    _cfg(db, "bot_group_ids", str(GROUP))
    actions = gamerank.run_scheduled_tick(db, SUNDAY_NIGHT)
    assert actions == ["week:empty"]
    assert sent == []
    assert gamerank.run_scheduled_tick(db, SUNDAY_NIGHT) == []


def test_poster_accepts_game_values(tmp_path):
    path = rank_poster.render_rank_poster(
        "week",
        [{"username": "alice", "net": 90}],
        datetime(2026, 10, 26),
        out_path=tmp_path / "game.png",
        headline="本周游戏榜",
        value_of=lambda row: f"+{row['net']}",
    )
    assert path.is_file() and path.stat().st_size > 1000
