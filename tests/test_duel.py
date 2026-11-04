"""群内骰子对决：押注冻结、应战、sendDice 结算、超时退回、幂等与并发抢占。"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import duel, models
from backend.api import welfare_admin
from backend import config_schema
from backend.tg_bot import callbacks, command_scopes, duel_ui, ephemeral, handlers, poller, router, sender

TG_A = 930001
TG_B = 930002
GROUP = -100930001
CJK4 = re.compile(r"^[一-鿿]{4}$")
LABEL_RE = re.compile(r"· \S+ <b>([^<]+)</b> \|")


class _FakeTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(name="公益服", slug="main", access_mode="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_timers(monkeypatch):
    monkeypatch.setattr(ephemeral, "Timer", _FakeTimer)
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    yield
    router._user_cmd_limits.clear()
    router._group_windows.clear()


def _user(db, tg=TG_A, name="alice", points=1000, created_at=None, staff=False):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=staff, is_active=True,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cfg(db, key, value):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _enable(db):
    _cfg(db, "bot_duel_enabled", "true")


def _balance(db, user_id):
    db.expire_all()
    return int(db.query(models.WebUser.points).filter(models.WebUser.id == user_id).scalar() or 0)


def _logs(db, user_id, type_):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == type_,
    ).all()


def _labels(text):
    return LABEL_RE.findall(text)


# ---------- 纯规则 ----------

def test_split_pot_floors_rake():
    assert duel.split_pot(50, 5) == (5, 95)
    assert duel.split_pot(1, 5) == (0, 2)
    assert duel.split_pot(10, 0) == (0, 20)


def test_switch_defaults_off_and_registered():
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_duel_enabled"] == "false"
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_duel_min"] == "10"
    assert welfare_admin.WELFARE_CONFIG_KEYS["bot_duel_rake_pct"] == "5"
    schema = config_schema.WELFARE_CONFIG_SCHEMA
    assert schema["bot_duel_enabled"] == ("bool",)
    assert config_schema.normalize("bot_duel_enabled", schema["bot_duel_enabled"], False) == "false"
    assert config_schema.normalize("bot_duel_rake_pct", schema["bot_duel_rake_pct"], 5) == "5"


def test_disabled_by_default(db):
    user = _user(db)
    res = duel.create_duel(db, user, GROUP, 50, idempotency_key="tg:1")
    assert res["ok"] is False and res["reason"] == "disabled"
    assert _balance(db, user.id) == 1000


def test_open_panel_copy_and_button(db):
    _enable(db)
    user = _user(db)
    res = duel.create_duel(db, user, GROUP, 50, idempotency_key="tg:2")
    assert res["ok"] and not res["duplicate"]
    text = duel_ui.open_text(res["duel"], "alice")
    assert text.startswith("▎<b>押 50 应战，胜者拿 95，剩 ")
    assert "秒" in text.split("\n", 1)[0]
    for label in _labels(text):
        assert CJK4.match(label), label
    markup = duel_ui.open_markup(res["duel"].id, 50)
    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "应战 50"
    assert button["callback_data"] == f"duel_accept:{res['duel'].id}"


# ---------- 账务 ----------

def test_create_freezes_stake_and_replay_does_not_double_charge(db):
    _enable(db)
    user = _user(db)
    first = duel.create_duel(db, user, GROUP, 50, idempotency_key="tg:9")
    second = duel.create_duel(db, user, GROUP, 50, idempotency_key="tg:9")
    assert first["ok"] and second["ok"] and second["duplicate"]
    assert first["duel"].id == second["duel"].id
    assert _balance(db, user.id) == 950
    assert len(_logs(db, user.id, "duel_stake")) == 1
    assert db.query(models.DuelLive).count() == 1


def test_bounds_and_daily_cap(db):
    _enable(db)
    user = _user(db)
    low = duel.create_duel(db, user, GROUP, 1)
    assert low["reason"] == "below_min"
    high = duel.create_duel(db, user, GROUP, 9999)
    assert high["reason"] == "above_max"
    _cfg(db, "bot_duel_daily_win_cap", "10")
    blocked = duel.create_duel(db, user, GROUP, 50)
    assert blocked["reason"] == "prize_over_cap"
    assert _balance(db, user.id) == 1000


def test_one_live_duel_per_chat(db):
    _enable(db)
    alice = _user(db)
    bob = _user(db, TG_B, "bob")
    assert duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:a")["ok"]
    busy = duel.create_duel(db, bob, GROUP, 50, idempotency_key="tg:b")
    assert busy["reason"] == "busy"
    assert _balance(db, bob.id) == 1000


def test_cannot_accept_self_and_second_challenger_does_not_pay(db):
    _enable(db)
    alice = _user(db)
    bob = _user(db, TG_B, "bob")
    cara = _user(db, TG_B + 1, "cara")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:c")
    duel_id = created["duel"].id
    self_res = duel.accept_duel(db, alice, duel_id, "tgcq:self")
    assert self_res["reason"] == "self"
    assert _balance(db, alice.id) == 950
    ok = duel.accept_duel(db, bob, duel_id, "tgcq:bob")
    assert ok["ok"] and not ok["duplicate"]
    assert _balance(db, bob.id) == 950
    late = duel.accept_duel(db, cara, duel_id, "tgcq:cara")
    assert late["reason"] == "taken"
    assert _balance(db, cara.id) == 1000
    replay = duel.accept_duel(db, bob, duel_id, "tgcq:bob")
    assert replay["ok"] and replay["duplicate"]
    assert _balance(db, bob.id) == 950
    assert len(_logs(db, bob.id, "duel_stake")) == 1


def test_insufficient_challenger_leaves_duel_open(db):
    _enable(db)
    alice = _user(db)
    bob = _user(db, TG_B, "bob", points=10)
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:d")
    res = duel.accept_duel(db, bob, created["duel"].id, "tgcq:poor")
    assert res["reason"] == "insufficient"
    assert _balance(db, bob.id) == 10
    assert _balance(db, alice.id) == 950
    row = db.query(models.Duel).one()
    assert row.status == "open" and row.challenger_id is None


def test_higher_dice_takes_pot_minus_rake(db):
    _enable(db)
    alice = _user(db, name="alice")
    bob = _user(db, TG_B, "bob")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:e")
    assert duel.accept_duel(db, bob, created["duel"].id, "tgcq:e")["ok"]
    settled = duel.settle_duel(db, created["duel"].id, 6, 2)
    assert settled["ok"] and settled["duel"].status == "settled"
    assert settled["duel"].winner_id == alice.id
    assert settled["duel"].prize == 95 and settled["duel"].rake == 5
    assert _balance(db, alice.id) == 1045
    assert _balance(db, bob.id) == 950
    assert _balance(db, alice.id) + _balance(db, bob.id) == 1995
    again = duel.settle_duel(db, created["duel"].id, 6, 2)
    assert again["duplicate"]
    assert len(_logs(db, alice.id, "duel_win")) == 1
    assert db.query(models.DuelLive).count() == 0
    line = duel_ui.result_line(db, settled["duel"])
    assert line.startswith("🎉 alice 6 点胜 bob 2 点，赢走 95 能量")
    assert "\n" not in line


def test_tie_refunds_both_without_rake(db):
    _enable(db)
    alice = _user(db)
    bob = _user(db, TG_B, "bob")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:f")
    duel.accept_duel(db, bob, created["duel"].id, "tgcq:f")
    settled = duel.settle_duel(db, created["duel"].id, 4, 4)
    assert settled["duel"].status == "tie"
    assert _balance(db, alice.id) == 1000
    assert _balance(db, bob.id) == 1000
    assert _logs(db, alice.id, "duel_win") == []
    assert len(_logs(db, alice.id, "duel_refund")) == 1


def test_timeout_refunds_initiator(db, monkeypatch):
    _enable(db)
    alice = _user(db, name="alice")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:g")
    row = created["duel"]
    row.expires_at = datetime.now() - timedelta(seconds=1)
    row.message_id = 77
    db.commit()
    edited = []
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda *a, **kw: edited.append(a[3] if len(a) > 3 else kw.get("text")) or (True, None),
    )
    actions = duel_ui.tick(db)
    assert any(item.startswith("expire:") for item in actions)
    assert _balance(db, alice.id) == 1000
    assert db.query(models.DuelLive).count() == 0
    assert edited and "无人应战" in edited[0] and "alice" in edited[0]
    fresh = db.query(models.Duel).one()
    assert fresh.status == "refunded" and fresh.panel_finalized is True


def test_dice_failure_refunds_both(db):
    _enable(db)
    alice = _user(db)
    bob = _user(db, TG_B, "bob")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:h")
    duel.accept_duel(db, bob, created["duel"].id, "tgcq:h")
    aborted = duel.abort_roll(db, created["duel"].id, reason="dice_failed")
    assert aborted.status == "failed"
    assert _balance(db, alice.id) == 1000
    assert _balance(db, bob.id) == 1000


def test_countdown_edit_is_rate_limited(db, monkeypatch):
    _enable(db)
    alice = _user(db)
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:i")
    row = created["duel"]
    row.message_id = 8
    row.last_panel_edit_at = datetime.now() - timedelta(seconds=30)
    db.commit()
    calls = []
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda *a, **kw: calls.append(a) or (True, None),
    )
    assert duel_ui.refresh_open_panels(db) == 1
    assert duel_ui.refresh_open_panels(db) == 0
    assert len(calls) == 1
    assert "应战 50" in str(calls[0])


def test_eligibility_mirrors_slots_gates(db):
    _enable(db)
    banned = _user(db)
    db.add(models.LotteryBlacklist(user_id=banned.id, reason="t"))
    db.commit()
    assert duel.create_duel(db, banned, GROUP, 50)["reason"] == "blacklisted"
    _cfg(db, "lottery_min_account_age_days", "30")
    newbie = _user(db, TG_B, "new", created_at=datetime.now())
    res = duel.create_duel(db, newbie, GROUP, 50)
    assert res["reason"] == "too_new"
    realm = db.query(models.ServerRealm).one()
    realm.access_mode = "paid"
    db.commit()
    old = _user(db, TG_B + 3, "old", created_at=datetime.now() - timedelta(days=60))
    assert duel.create_duel(db, old, GROUP, 50)["reason"] == "not_free_realm"


# ---------- 按钮与命令 ----------

def _cq(data, cq_id="cq-duel", tg=TG_B, chat=GROUP):
    return {
        "id": cq_id,
        "from": {"id": tg, "first_name": "Bob"},
        "data": data,
        "message": {"message_id": 55, "chat": {"id": chat, "type": "supergroup"}},
    }


def test_unbound_click_is_toast_only(db, monkeypatch):
    _enable(db)
    alice = _user(db)
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:j")
    answers = []
    dice = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: answers.append(kw.get("text") or (a[2] if len(a) > 2 else "")) or (True, None),
    )
    monkeypatch.setattr(sender, "send_dice", lambda *a, **kw: dice.append(1) or (True, None, 6, 1))
    callbacks.handle_callback(db, _cq(f"duel_accept:{created['duel'].id}", tg=999999))
    assert answers and "绑定" in answers[-1]
    assert dice == []
    assert _balance(db, alice.id) == 950
    assert db.query(models.Duel).one().status == "open"


def test_callback_settle_uses_native_dice(db, monkeypatch):
    _enable(db)
    alice = _user(db, name="alice")
    bob = _user(db, TG_B, "bob")
    created = duel.create_duel(db, alice, GROUP, 50, idempotency_key="tg:k")
    duel.attach_panel(db, created["duel"].id, 55)
    answers = []
    dice_emoji = []
    edited = []
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda *a, **kw: answers.append(kw.get("text") or "") or (True, None),
    )

    def fake_dice(db_, chat_id, emoji="🎰"):
        dice_emoji.append(emoji)
        return True, None, 6 if len(dice_emoji) == 1 else 1, 100 + len(dice_emoji)

    monkeypatch.setattr(sender, "send_dice", fake_dice)
    monkeypatch.setattr(
        sender, "edit_message_text",
        lambda *a, **kw: edited.append(a[3]) or (True, None),
    )
    callbacks.handle_callback(db, _cq(f"duel_accept:{created['duel'].id}"))
    assert dice_emoji == ["🎲", "🎲"]
    assert _balance(db, alice.id) == 1045
    assert _balance(db, bob.id) == 950
    assert edited and "赢走 95 能量" in edited[-1] and "\n" not in edited[-1]
    assert any("赢了" in (text or "") or "输了" in (text or "") for text in answers)
    # 同一个按钮重放不再掷骰、不再发奖
    callbacks.handle_callback(db, _cq(f"duel_accept:{created['duel'].id}", cq_id="cq-duel"))
    assert dice_emoji == ["🎲", "🎲"]
    assert len(_logs(db, alice.id, "duel_win")) == 1


def test_router_group_command_sends_panel_once(db, monkeypatch):
    _enable(db)
    _user(db)
    sent = []

    def fake_ex(db_, chat_id, text, reply_markup=None, **kw):
        sent.append((text, reply_markup))
        return True, None, 42

    monkeypatch.setattr(sender, "send_message_ex", fake_ex)
    monkeypatch.setattr(sender, "send_message", lambda *a, **kw: sent.append(("plain", a[2])) or (True, None))
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    update = {
        "update_id": 501,
        "message": {
            "message_id": 7,
            "text": "/duel 50",
            "from": {"id": TG_A, "first_name": "Alice"},
            "chat": {"id": GROUP, "type": "supergroup"},
        },
    }
    router.dispatch(db, update)
    assert len(sent) == 1
    text, markup = sent[0]
    assert "押 50 应战" in text
    assert markup["inline_keyboard"][0][0]["text"] == "应战 50"
    row = db.query(models.Duel).one()
    assert row.message_id == 42
    assert _balance(db, row.initiator_id) == 950
    # 同一 update 重放不双花、不再发面板
    router._user_cmd_limits.clear()
    router.dispatch(db, update)
    assert len(sent) == 1
    assert _balance(db, row.initiator_id) == 950


def test_router_private_and_disabled(db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda *a, **kw: sent.append(a[2]) or (True, None),
    )
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda *a, **kw: sent.append(a[2]) or (True, None, 1),
    )
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    _user(db)
    router.dispatch(db, {
        "update_id": 1,
        "message": {
            "message_id": 1, "text": "/duel 50",
            "from": {"id": TG_A, "first_name": "A"},
            "chat": {"id": TG_A, "type": "private"},
        },
    })
    assert sent and "群里" in sent[-1]
    router._user_cmd_limits.clear()
    router.dispatch(db, {
        "update_id": 2,
        "message": {
            "message_id": 2, "text": "/duel 50",
            "from": {"id": TG_A, "first_name": "A"},
            "chat": {"id": GROUP, "type": "group"},
        },
    })
    assert "未开启" in sent[-1]
    assert db.query(models.Duel).count() == 0


def test_duel_command_is_group_scoped():
    assert ("duel", "[群/私聊] 骰子对决") in poller.BOT_COMMANDS
    assert "duel" in command_scopes.GROUP_COMMANDS
    names = {n for n, _ in command_scopes.group_commands(poller.BOT_COMMANDS)}
    assert "duel" in names
