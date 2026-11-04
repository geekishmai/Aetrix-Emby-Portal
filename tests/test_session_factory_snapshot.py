# -*- coding: utf-8 -*-
"""SessionLocal factory 换绑的快照/恢复 helper 回归（T1，2026-11-05）。

根因复现：``original = dbmod.SessionLocal``（保存的是**代理对象**）再
``configure_session_local(original)`` 还原——防递归保护把代理判为误用，
回退到 ``_real_session_local``（bind 到 backend.database **首次 import 时的
engine**，测试进程里通常是 0 表的命令行 DATABASE_URL 库）。后果：该文件
teardown 之后，所有依赖全局 ``SessionLocal`` 的测试文件全部
``no such table``（全量 95 个环境类 ERROR 的根因；单文件跑全绿）。

本文件钉死：
1. 事故形态确实存在（守卫断言，防有人"顺手优化"掉 conftest helper）；
2. conftest 的 ``snapshot_session_factory`` / ``restore_session_factory``
   语义正确（换绑→快照→再换绑→恢复 = 回到快照时的 factory）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import database as dbmod
from conftest import restore_session_factory, snapshot_session_factory


def _tmp_factory():
    engine = create_engine("sqlite:///:memory:")
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_accidental_proxy_restore_falls_back_to_real():
    """事故形态守卫：传代理给 configure 会回退 _real（不是恢复原指向）。"""
    assert isinstance(dbmod.SessionLocal, dbmod._SessionLocalProxy), \
        "SessionLocal 必须仍是代理（若生产改了实现，本文件的守卫语义需重审）"

    real = dbmod._real_session_local
    token = dbmod.SessionLocal._factory
    own_engine, own = _tmp_factory()
    try:
        dbmod.configure_session_local(own)
        assert dbmod.SessionLocal._factory is own

        # 事故写法：把代理原样传回
        dbmod.configure_session_local(dbmod.SessionLocal)
        assert dbmod.SessionLocal._factory is real, \
            "防递归保护必须把代理判为误用并回退 _real（本断言钉住事故形态）"
    finally:
        dbmod.SessionLocal._factory = token
        own_engine.dispose()


def test_snapshot_restore_round_trip():
    """conftest helper：换绑→快照→再换绑→恢复，factory 精确回到快照值。

    v2：快照是 dict（factory/engine/DATABASE_URL 三件），restore 三件一起还原
    （QA BO-3：只还原 factory 会把 engine/env 残留在他人 tmp 库）。"""
    base_token = snapshot_session_factory()
    engine_a, factory_a = _tmp_factory()
    engine_b, factory_b = _tmp_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        token_a = snapshot_session_factory()
        assert token_a["factory"] is factory_a

        dbmod.SessionLocal._factory = factory_b
        assert dbmod.SessionLocal._factory is factory_b

        restore_session_factory(token_a)
        assert dbmod.SessionLocal._factory is factory_a, \
            "restore 必须恢复快照时的 factory，而不是回退 _real"
    finally:
        restore_session_factory(base_token)
        engine_a.dispose()
        engine_b.dispose()


def test_snapshot_restore_covers_engine_and_env():
    """BO-3 P2：victim 三件一起改（factory/engine/DATABASE_URL），快照/恢复
    必须三件一起还原，engine 与 env 不得残留他人 tmp 库。"""
    base_token = snapshot_session_factory()
    engine_a, factory_a = _tmp_factory()
    try:
        dbmod.SessionLocal._factory = factory_a
        dbmod.engine = engine_a
        os.environ["DATABASE_URL"] = "sqlite:///T1-v2-probe-a.db"
        token = snapshot_session_factory()

        # 模拟另一个文件的三件改绑
        engine_b, factory_b = _tmp_factory()
        try:
            dbmod.SessionLocal._factory = factory_b
            dbmod.engine = engine_b
            os.environ["DATABASE_URL"] = "sqlite:///T1-v2-probe-b.db"
            assert dbmod.engine is engine_b

            restore_session_factory(token)
            assert dbmod.SessionLocal._factory is factory_a
            assert dbmod.engine is engine_a, "engine 必须随快照一起还原"
            assert os.environ["DATABASE_URL"] == "sqlite:///T1-v2-probe-a.db", \
                "DATABASE_URL 必须随快照一起还原"
        finally:
            engine_b.dispose()
    finally:
        restore_session_factory(base_token)
        engine_a.dispose()


def test_restore_returns_to_global_session():
    """恢复后 SessionLocal() 产出的 session 绑定到快照 factory 的 engine。"""
    base_token = snapshot_session_factory()
    engine, factory = _tmp_factory()
    try:
        dbmod.SessionLocal._factory = factory
        token = snapshot_session_factory()

        dbmod.SessionLocal._factory = dbmod._real_session_local  # 模拟被顶掉
        restore_session_factory(token)

        s = dbmod.SessionLocal()
        try:
            assert s.get_bind() is engine, \
                "恢复后新建的 session 必须连回快照 factory 的 engine"
        finally:
            s.close()
    finally:
        restore_session_factory(base_token)
        engine.dispose()
