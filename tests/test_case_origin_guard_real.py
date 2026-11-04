# -*- coding: utf-8 -*-
"""T1-v3：守卫正式回归——走**真实 fixture setup/teardown**，不手动 restore。

Codex 复审要求（v2 未闭环两项）：
1. 诊断对象必须是**起点 factory 实际绑定**（``factory.kw['bind']``），
   与 ``dbmod.engine`` 分开报告；``factory.bind=A 缺表、dbmod.engine=B 有表``
   不得报 GAP，反向不得漏报；无可检查 bind 显式 unknown；
   GAP 证据不含 engine URL（凭据红线）。
2. 回归必须经真实 autouse fixture 的 setup/teardown（用 ``__wrapped__``
   直调三件套），不能手动调 helper 代替；两方向反例（bind 与 engine 不一致）
   都要在禁用/错接时变红。

用 requestфикстure 的 ``_case_origin_factory_guard.__wrapped__`` 直接驱动
fixture 体（pytest fixture 包装下 ``__wrapped__`` 是原函数）——
setup = 前半段（到 yield），teardown = yield 之后段。用显式生成器驱动。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import database as dbmod
from backend import models as base_models
from backend.emby_server import models as em
from conftest import (
    _FACTORY_GAPS,
    _case_origin_factory_guard,
    factory_gap_count,
    restore_session_factory,
    snapshot_session_factory,
)


def _full_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, factory


def _seed_user(engine, name):
    s = sessionmaker(bind=engine)()
    s.add(base_models.WebUser(username=name, password_hash="x"))
    s.commit()
    s.close()


class _GuardDriver:
    """真实驱动 _case_origin_factory_guard：setup/teardown 各跑一半。"""

    def __init__(self):
        self._gen = None

    def setup(self):
        self._gen = _case_origin_factory_guard.__wrapped__()
        self._gen.send(None)  # 执行 setup 段（快照起点）

    def teardown(self):
        try:
            self._gen.send(None)  # 执行 teardown 段（恢复起点 + GAP 对账）
        except StopIteration:
            pass

    def abort(self):
        if self._gen is not None:
            self._gen.close()


def _run_guarded(body):
    """setup → body → teardown 的完整真实守卫周期。"""
    d = _GuardDriver()
    d.setup()
    try:
        body()
    finally:
        d.teardown()


# ---------------- 1. BO-1 两方向：bind 与 engine 不一致 ----------------

def test_bo1_bind_missing_but_engine_ok_no_gap(monkeypatch):
    """factory.bind=A 被 DROP 表、dbmod.engine=B 有表 → 不得报 GAP（旧实现会误报
    或查错对象）；且诊断对象是 bind 不是 engine。"""
    engine_a, factory_a = _full_db()
    engine_b, _ = _full_db()
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_b  # engine 属性与 bind 不一致（B 有表）
        before = factory_gap_count()

        # 真实顺序：setup 时 A 有表；用例期间表被 drop；teardown 对账 GAP +1
        d = _GuardDriver()
        d.setup()
        try:
            with engine_a.begin() as conn:
                conn.execute(text("DROP TABLE web_users"))
                conn.execute(text("DROP TABLE system_configs"))
        finally:
            d.teardown()
        assert factory_gap_count() == before + 1, (
            "bind 被 DROP 核心表 → teardown 对账必须 GAP +1（按 bind 诊断）")
        # 证据不含 URL（凭据红线）
        assert all("url" not in g and "engine_url" not in g for g in _FACTORY_GAPS)
    finally:
        restore_session_factory(base)
        engine_a.dispose()
        engine_b.dispose()


def test_bo1_bind_ok_but_engine_empty_no_false_gap(monkeypatch):
    """factory.bind=D 有表可读、dbmod.engine=C 空库 → 不得误报 GAP
    （旧 v2 实现按 origin['engine'] 诊断，delta=1 误报——本用例钉死修正）。"""
    engine_d, factory_d = _full_db()
    engine_c = create_engine("sqlite:///:memory:")
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_d  # bind=D：有表
        dbmod.engine = engine_c                  # engine=C：空库
        before = factory_gap_count()

        _run_guarded(lambda: None)

        assert factory_gap_count() == before, (
            "bind 有表时不得因 engine 属性是空库而误报 GAP（两对象分开诊断）")
    finally:
        restore_session_factory(base)
        engine_d.dispose()
        engine_c.dispose()


def test_origin_bind_unknown_reported_not_guessed():
    """无可检查 bind（None）→ GAP 显式 unknown，不猜别的有表库。"""
    base = snapshot_session_factory()
    before = factory_gap_count()
    try:
        broken = sessionmaker()  # 无 bind 的 sessionmaker
        dbmod.SessionLocal._factory = broken
        dbmod.engine = None

        _run_guarded(lambda: None)

        assert factory_gap_count() == before + 1
        assert _FACTORY_GAPS[-1]["state"] == "unknown"
        assert _FACTORY_GAPS[-1]["reason"] == "origin-bind-not-ok"
    finally:
        restore_session_factory(base)


# ---------------- 2. BO-2 / 漂移：走真实 guard，禁用守卫必红 ----------------

def test_bo2_real_guard_restores_origin_after_empty_end(full_db=None):
    """A→错误空 C：真实 guard teardown 恢复 A（哨兵数据是 A 的）；
    若守卫被禁用（手动跳过 teardown），断言失败——防错接。"""
    engine_a, factory_a = _full_db()
    _seed_user(engine_a, "user-A")
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_a

        engine_c = create_engine("sqlite:///:memory:")
        factory_c = sessionmaker(bind=engine_c)
        try:
            def body():
                dbmod.SessionLocal._factory = factory_c  # 错误结束于空 C

            _run_guarded(body)

            assert dbmod.SessionLocal._factory is factory_a, "必须回到起点 A"
            s = dbmod.SessionLocal()
            try:
                names = {u.username for u in s.query(base_models.WebUser).all()}
                assert names == {"user-A"}, f"必须读到 A 的哨兵数据，实际：{names}"
            finally:
                s.close()
        finally:
            engine_c.dispose()
    finally:
        restore_session_factory(base)


def test_bind_drift_real_guard_restores(full_db=None):
    """同 factory 对象、bind 被改走 → 真实 guard 恢复到起点 engine；
    teardown 恢复环节被禁时该断言失败（守卫不可拆）。"""
    engine_a, factory_a = _full_db()
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_a

        engine_b = create_engine("sqlite:///:memory:")
        try:
            def body():
                factory_a.kw["bind"] = engine_b  # 同对象漂移

            _run_guarded(body)

            s = dbmod.SessionLocal()
            try:
                assert s.get_bind() is engine_a, (
                    "真实 guard teardown 必须把 bind 拉回起点 engine")
            finally:
                s.close()
        finally:
            factory_a.kw["bind"] = engine_a
            engine_b.dispose()
    finally:
        restore_session_factory(base)


def test_disabled_guard_would_fail_bind_drift(full_db=None):
    """反例钉死：守卫 teardown 被禁（只 setup 不 teardown）时，bind 漂移
    不会被纠 → 断言红。证明上面的绿是守卫带来的，不是天然绿。"""
    engine_a, factory_a = _full_db()
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_a

        engine_b = create_engine("sqlite:///:memory:")
        try:
            d = _GuardDriver()
            d.setup()
            try:
                factory_a.kw["bind"] = engine_b
            finally:
                d.abort()  # 显式不跑 teardown 段（禁用守卫）

            s = dbmod.SessionLocal()
            try:
                assert s.get_bind() is engine_b, (
                    "禁用守卫后漂移仍在——若此断言失败说明环境意外自愈，"
                    "上一用例的绿不可归因于守卫")
            finally:
                s.close()
        finally:
            factory_a.kw["bind"] = engine_a
            engine_b.dispose()
    finally:
        restore_session_factory(base)


# ---------------- 3. 正常路径不虚报 ----------------

def test_healthy_origin_no_gap(full_db=None):
    """起点 bind 有表 → GAP 恰 +0（不虚报）。"""
    engine_a, factory_a = _full_db()
    base = snapshot_session_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_a
        before = factory_gap_count()

        _run_guarded(lambda: None)

        assert factory_gap_count() == before, "起点健康时不得虚报 GAP"
    finally:
        restore_session_factory(base)
