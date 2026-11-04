"""SessionLocal 代理：import 期绑定不再导致「查旧库」。

## 现象

``test_enrich_worker::test_get_progress`` 偶发 ``assert 0 == 2``，且**只在某些
执行顺序下**出现。

## 根因

20+ 个模块写的是 ``from backend.database import SessionLocal`` —— import 期就把
引用复制一份。此后 ``database.SessionLocal`` 被重建（测试隔离库），这些模块
手里的旧引用不跟着变，于是查到建库之前的那个库，表现为读到空表。

## 修法

``database.SessionLocal`` 改为可调用代理，在**调用时刻**解析当前生效的
sessionmaker。本文件就是证明它确实有效的回归测试。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from backend import database as dbmod  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from conftest import restore_session_factory, snapshot_session_factory
import pytest  # noqa: E402
from contextlib import suppress  # noqa: E402


def test_sessionlocal_is_callable_proxy():
    """代理本身必须能当 sessionmaker 用"""
    assert callable(dbmod.SessionLocal)
    s = dbmod.SessionLocal()
    try:
        assert hasattr(s, "query")
    finally:
        s.close()


def _with_factory(tmpdb):
    """把 database.SessionLocal 换成指向 tmpdb 的 factory，跑完后还原。"""
    engine = create_engine(f"sqlite:///{tmpdb}")
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    original = snapshot_session_factory()
    dbmod.configure_session_local(factory)
    return engine, original


def test_early_importer_sees_reassigned_sessionlocal():
    """核心回归：模拟「先 import 模块，再重设 SessionLocal」

    这正是测试踩坑的顺序（旧实现下这里拿到的是重设前的旧库，assert 0 == 2）。
    本测试不依赖 pytest 的执行顺序：只断言「重设后产出的 session 连到新库」。
    """
    from backend.emby_server import enrich_worker  # noqa: E402

    engine, original = _with_factory(f"{_tmp}_alt")
    try:
        s = enrich_worker.SessionLocal()
        try:
            assert s.get_bind() is engine, \
                "早导入的模块必须看到重设后的 SessionLocal，否则会查旧库"
        finally:
            s.close()
    finally:
        dbmod.configure_session_local()
        engine.dispose()


def test_all_early_importers_follow_reassignment():
    """所有早导入的模块都必须跟着重设走（行为断言，不检验实现细节）"""
    from backend.emby_server import enrich_worker, scan_queue  # noqa: E402

    engine, _orig = _with_factory(f"{_tmp}_alt_p")
    try:
        for mod in (enrich_worker, scan_queue):
            s = mod.SessionLocal()
            try:
                assert s.get_bind() is engine, f"{mod.__name__} 还在查旧库"
            finally:
                s.close()
    finally:
        dbmod.configure_session_local()
        engine.dispose()


def test_get_db_uses_current_factory():
    """get_db 是 FastAPI 依赖注入入口，也必须跟着重设走"""
    engine, original = _with_factory(f"{_tmp}_alt2")
    try:
        gen = dbmod.get_db()
        s = next(gen)
        try:
            assert s.get_bind() is engine
        finally:
            s.close()
            with suppress(StopIteration):
                next(gen)
    finally:
        dbmod.configure_session_local()
        engine.dispose()


def test_proxy_does_not_recurse_when_untouched():
    """代理转发当前目标（不能自己调自己）。

    T1 后全局 factory 由 conftest 守卫维持「最后已知良好」值，不再恒等于
    _real_session_local（先前文件的换绑/恢复会把指向留在某个有表的全局库）。
    本用例钉的语义是「代理转发、不自递归」：把目标指到哨兵 factory，
    SessionLocal() 必须产出哨兵的 session。"""
    if not isinstance(dbmod.SessionLocal, dbmod._SessionLocalProxy):
        pytest.skip("当前 SessionLocal 不是代理")
    engine = create_engine("sqlite:///:memory:")
    sentinel = sessionmaker(bind=engine)
    token = dbmod.SessionLocal._factory
    try:
        dbmod.SessionLocal._factory = sentinel
        s = dbmod.SessionLocal()
        try:
            assert s.get_bind() is engine
        finally:
            s.close()
    finally:
        dbmod.SessionLocal._factory = token
        engine.dispose()


def test_proxy_object_is_never_replaced():
    """关键不变量：代理对象本身恒定，configure 只换目标

    早期版本把真 factory 赋给 SessionLocal，等于把代理顶掉、转发能力作废——
    这条测试就是防那个设计漏洞复发的。
    """
    from backend.emby_server import enrich_worker  # noqa: E402

    before = dbmod.SessionLocal
    engine, _ = _with_factory(f"{_tmp}_identity")
    try:
        assert dbmod.SessionLocal is before, "configure 不得替换代理对象本身"
        assert enrich_worker.SessionLocal is before, \
            "各模块 import 到的必须始终是同一个代理"
    finally:
        dbmod.configure_session_local()
        engine.dispose()
