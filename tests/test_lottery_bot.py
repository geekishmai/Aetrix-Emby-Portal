"""群抽奖 G3 测试：Bot 交互 + 回调幂等 + 公开核验接口（隔离 SQLite）。

backend.lottery 由 G1 并行任务提供，当前不存在；测试用 fake 模块注入契约实现。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend
from backend import models
from backend.tg_bot import callbacks, handlers
from backend.api import lottery as api_lottery


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username="u1", telegram_id=111):
    u = models.WebUser(username=username, password_hash="x", points=0, telegram_id=telegram_id)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def fake_lottery(monkeypatch):
    mod = types.ModuleType("backend.lottery")
    mod._enabled = True
    mod._allowed = set()
    mod._full = False
    mod._entries = set()
    mod._active_round = SimpleNamespace(
        id=7, title="测试抽奖", prize_name="月卡", draw_at=None,
        status="open", max_entries=100, entry_count=3,
    )

    def is_enabled(db):
        return mod._enabled

    def allowed_group_ids(db):
        return set(mod._allowed)

    def get_active_round(db, chat_id):
        return mod._active_round

    def join_round(db, round, user, telegram_id):
        key = (round.id, user.id)
        if key in mod._entries:
            return {"ok": False, "reason": "already"}
        if mod._full:
            return {"ok": False, "reason": "full"}
        mod._entries.add(key)
        return {"ok": True, "reason": ""}

    def verify_round(db, round_id):
        if round_id == 7:
            return {
                "round_id": 7, "title": "测试抽奖", "status": "done",
                "seed_hash": "abc123", "seed": "seed-xyz", "algorithm": "sha256",
                "entries": [1, 2, 3], "winners": [2],
            }
        raise ValueError("round not found")

    mod.is_enabled = is_enabled
    mod.allowed_group_ids = allowed_group_ids
    mod.get_active_round = get_active_round
    mod.join_round = join_round
    mod.verify_round = verify_round
    monkeypatch.setitem(sys.modules, "backend.lottery", mod)
    monkeypatch.setattr(backend, "lottery", mod, raising=False)
    return mod


@pytest.fixture()
def answers(monkeypatch):
    calls = []

    def fake_answer(db, callback_id, text, show_alert=False):
        calls.append({"callback_id": callback_id, "text": text, "show_alert": show_alert})

    monkeypatch.setattr(callbacks, "_answer_callback", fake_answer)
    return calls


def _cb(cb_id="cb1", tg_id=111, chat_id=-1001, data="lottery_join:7"):
    return {
        "id": cb_id,
        "from": {"id": tg_id},
        "message": {"chat": {"id": chat_id}},
        "data": data,
    }


# ---------- handlers.handle_lottery ----------

def test_lottery_disabled_returns_closed_text(db, fake_lottery):
    fake_lottery._enabled = False
    _make_user(db)
    res = handlers.handle_lottery(db, {"id": 111}, -1001, "", True)
    assert res == "🎲 抽奖功能暂未开启"


def test_lottery_unbound_returns_bind_guide(db, fake_lottery):
    res = handlers.handle_lottery(db, {"id": 999}, -1001, "", True)
    assert "绑定" in res


def test_lottery_group_shows_active_round_with_button(db, fake_lottery):
    _make_user(db)
    res = handlers.handle_lottery(db, {"id": 111}, -1001, "", True)
    assert isinstance(res, tuple)
    text, markup = res
    assert "测试抽奖" in text and "月卡" in text
    assert markup == {"inline_keyboard": [[{"text": "参加", "callback_data": "lottery_join:7"}]]}


def test_lottery_group_no_active_round(db, fake_lottery):
    fake_lottery._active_round = None
    _make_user(db)
    res = handlers.handle_lottery(db, {"id": 111}, -1001, "", True)
    assert res == "🎲 本群暂无进行中的抽奖，敬请期待"


def test_lottery_private_no_history(db, fake_lottery):
    _make_user(db)
    res = handlers.handle_lottery(db, {"id": 111}, 111, "", False)
    assert res == "🎲 你还没有参加过抽奖"


# ---------- callbacks.handle_callback ----------

def test_join_callback_idempotent(db, fake_lottery, answers):
    _make_user(db)
    callbacks.handle_callback(db, _cb())
    callbacks.handle_callback(db, _cb(cb_id="cb2"))
    assert len(answers) == 2
    assert "参加成功" in answers[0]["text"]
    assert "已参加" in answers[1]["text"]
    assert len(fake_lottery._entries) == 1


def test_join_callback_group_not_allowed(db, fake_lottery, answers):
    fake_lottery._allowed = {-999}
    _make_user(db)
    callbacks.handle_callback(db, _cb())
    assert len(answers) == 1
    assert "暂未开放" in answers[0]["text"]


def test_join_callback_bad_data(db, fake_lottery, answers):
    callbacks.handle_callback(db, _cb(data="lottery_join:abc"))
    callbacks.handle_callback(db, _cb(cb_id="cb2", data="lottery_join:"))
    assert len(answers) == 2
    assert all("无效" in a["text"] for a in answers)


def test_join_callback_unbound_user(db, fake_lottery, answers):
    callbacks.handle_callback(db, _cb(tg_id=999))
    assert len(answers) == 1
    assert "绑定" in answers[0]["text"]
    assert answers[0]["show_alert"] is True


def test_join_callback_full(db, fake_lottery, answers):
    fake_lottery._full = True
    _make_user(db)
    callbacks.handle_callback(db, _cb())
    assert "已满" in answers[0]["text"]


def test_join_callback_round_ended(db, fake_lottery, answers):
    fake_lottery._active_round = SimpleNamespace(
        id=7, title="测试抽奖", prize_name="月卡", draw_at=None,
        status="done", max_entries=100, entry_count=3,
    )
    _make_user(db)
    callbacks.handle_callback(db, _cb())
    assert "已结束" in answers[0]["text"]


def test_join_callback_disabled(db, fake_lottery, answers):
    fake_lottery._enabled = False
    _make_user(db)
    callbacks.handle_callback(db, _cb())
    assert "暂未开启" in answers[0]["text"]


def test_callback_ignores_other_data(db, fake_lottery, answers):
    callbacks.handle_callback(db, _cb(data="noop"))
    assert answers == []


# ---------- 公开核验接口 ----------

def test_fairness_endpoint_ok(db, fake_lottery, monkeypatch):
    monkeypatch.setattr(api_lottery, "_lottery", lambda: fake_lottery)
    data = api_lottery.round_fairness(7, db)
    assert data["seed_hash"] == "abc123"
    assert data["winners"] == [2]


def test_fairness_endpoint_404(db, fake_lottery, monkeypatch):
    monkeypatch.setattr(api_lottery, "_lottery", lambda: fake_lottery)
    with pytest.raises(HTTPException) as ei:
        api_lottery.round_fairness(999, db)
    assert ei.value.status_code == 404


def test_fairness_endpoint_503_without_module(db, monkeypatch):
    monkeypatch.setattr(api_lottery, "_lottery", lambda: None)
    with pytest.raises(HTTPException) as ei:
        api_lottery.round_fairness(7, db)
    assert ei.value.status_code == 503
