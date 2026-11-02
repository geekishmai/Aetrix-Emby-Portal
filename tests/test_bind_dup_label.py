"""重复标签绑定：并发/重试不再 500（幂等复用或 409 人话提示）。

根因（2026-11-02 生产实证）：create_binding 的重名预检只拦得住
顺序提交；验活窗口约数秒内两次提交都会通过预检，第二行的 INSERT
撞 uq_user_storage_binding_label，IntegrityError 未捕获 → 500。
本文件锁死修后口径：
- 同用户同 provider 同 label 已有绑定：
  - 凭据一致（夸克轮换字段 __puus/__pus 不计）→ 幂等返回已有行，
    不建第二行、不重验；
  - 凭据不同 → BindingError kind=conflict（路由映射 409），文案说人话；
- INSERT 撞唯一约束的竞态分支同样走上述口径，永不 500。
夸克网络全部用桩替身，不碰真实夸克。
"""

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import quarkdrive
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import unseal_secret
from backend.integrations import store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _quark_switch_cache_isolation():
    keys = [ub.KEY_QUARK_BIND_ENABLED]
    vip_key = getattr(ub, "KEY_QUARK_BIND_VIP_CHECK", None)
    if vip_key:
        keys.append(vip_key)
    for key in keys:
        store.invalidate(key)
    yield
    for key in keys:
        store.invalidate(key)


FAKE_COOKIE = "__puus=old-token; __pus=abc; other=1"
ROTATED_COOKIE = "__puus=new-token; __pus=abc; other=1"
OTHER_COOKIE = "__puus=other-token; __pus=xyz; other=2"


class _StubQuarkClient:
    calls = {"check": 0, "list_root": 0}
    behavior = "ok"
    rotate_to = None
    vip_result = "vip"

    def __init__(self, cookie):
        self.cookie = cookie

    def check(self):
        type(self).calls["check"] += 1
        if type(self).behavior == "auth_error":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        if type(self).rotate_to:
            self.cookie = type(self).rotate_to
        return {"ok": True}

    def list_root(self):
        type(self).calls["list_root"] += 1
        if type(self).behavior == "auth_error":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []

    def list_dir(self, pdir_fid, page_size=100):
        if type(self).behavior == "auth_error":
            raise quarkdrive.QuarkAuthError("夸克登录态失效")
        return []

    def vip_status(self):
        return type(self).vip_result


@pytest.fixture()
def _stub_client(monkeypatch):
    _StubQuarkClient.calls = {"check": 0, "list_root": 0}
    _StubQuarkClient.behavior = "ok"
    _StubQuarkClient.rotate_to = None
    _StubQuarkClient.vip_result = "vip"
    monkeypatch.setattr(ub.quarkdrive, "QuarkClient", _StubQuarkClient)
    return _StubQuarkClient


def _bind(db, user_id=7, label="1", cookie=FAKE_COOKIE):
    return ub.create_binding(db, user_id, "quark", label, cookie)


def _count_bindings(db):
    return db.query(em.UserStorageBinding).count()


def test_same_label_same_cookie_idempotent(db, _stub_client):
    first = _bind(db)
    assert first.status == "active"
    calls_after_first = dict(_StubQuarkClient.calls)
    again = _bind(db)
    assert again.id == first.id
    assert again.status == "active"
    assert _count_bindings(db) == 1
    # 幂等返回已有行：不重验、不建第二行凭据
    assert _StubQuarkClient.calls == calls_after_first
    assert db.query(em.MountCredential).count() == 1


def test_same_label_rotated_cookie_idempotent(db, _stub_client):
    _StubQuarkClient.rotate_to = ROTATED_COOKIE
    first = _bind(db)
    assert first.status == "active"
    # 库里存的是轮换后的 Cookie；原 Cookie 重提仍算同一凭据
    stored = unseal_secret(db.query(em.MountCredential).filter(
        em.MountCredential.id == first.credential_id).first().sealed_credential)
    assert stored == ROTATED_COOKIE
    again = _bind(db, cookie=FAKE_COOKIE)
    assert again.id == first.id
    assert _count_bindings(db) == 1


def test_same_label_different_cookie_conflict(db, _stub_client):
    _bind(db)
    with pytest.raises(ub.BindingError) as excinfo:
        _bind(db, cookie=OTHER_COOKIE)
    assert excinfo.value.kind == "conflict"
    assert "换个名字" in excinfo.value.message
    assert _count_bindings(db) == 1


def test_same_label_same_cookie_failed_row_returned_as_is(db, _stub_client):
    _StubQuarkClient.behavior = "auth_error"
    failed = _bind(db)
    assert failed.status == "expired"
    again = _bind(db)
    assert again.id == failed.id
    assert again.status == "expired"
    assert _count_bindings(db) == 1


def test_different_user_same_label_independent(db, _stub_client):
    a = _bind(db, user_id=7, label="1")
    b = _bind(db, user_id=8, label="1")
    assert a.id != b.id
    assert _count_bindings(db) == 2


def test_different_label_normal_create(db, _stub_client):
    a = _bind(db, label="1")
    b = _bind(db, label="2")
    assert a.id != b.id
    assert _count_bindings(db) == 2


def test_insert_race_branch_same_cookie_returns_existing(db, _stub_client):
    """竞态分支（真实约束冲突）：_insert_binding_row 撞唯一约束时，
    回滚后按同凭据幂等返回已有行，不抛 IntegrityError、不建第二行。"""
    first = _bind(db)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=7,
        sealed_credential="x", key_id="fernet-v1", status="invalid")
    db.add(cred)
    db.flush()
    rival = em.UserStorageBinding(
        user_id=7, provider="quark", account_label="1",
        credential_id=cred.id, status="invalid")
    returned, created = ub._insert_binding_row(
        db, rival, 7, "quark", "1", FAKE_COOKIE)
    assert created is False
    assert returned.id == first.id
    assert _count_bindings(db) == 1


def test_insert_race_branch_different_cookie_conflict(db, _stub_client):
    """竞态分支（真实约束冲突）：凭据不同时 409，不 500、不建第二行。"""
    _bind(db)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=7,
        sealed_credential="x", key_id="fernet-v1", status="invalid")
    db.add(cred)
    db.flush()
    rival = em.UserStorageBinding(
        user_id=7, provider="quark", account_label="1",
        credential_id=cred.id, status="invalid")
    with pytest.raises(ub.BindingError) as excinfo:
        ub._insert_binding_row(db, rival, 7, "quark", "1", OTHER_COOKIE)
    assert excinfo.value.kind == "conflict"
    assert _count_bindings(db) == 1


def test_same_credential_helper_ignores_rotation_fields():
    assert ub._same_credential(FAKE_COOKIE, ROTATED_COOKIE) is True
    assert ub._same_credential(FAKE_COOKIE, OTHER_COOKIE) is False
    assert ub._same_credential("a=1; b=2", "b=2;a=1") is True
    assert ub._same_credential("a=1", "a=1; b=2") is False
    # 裸片段凭据也必须区分（旧实现忽略裸片段会把 COOKIE-XYZ
    # 与 COOKIE-2 误判成同一凭据，致 test_byos_p1 回归红）
    assert ub._same_credential("COOKIE-XYZ", "COOKIE-2") is False
    assert ub._same_credential("COOKIE-XYZ", "COOKIE-XYZ") is True
    assert ub._same_credential("a=1; bare", "a=1; bare") is True
    assert ub._same_credential("a=1; bare", "a=1; other") is False


def test_concurrent_double_submit_idempotent(tmp_path, _stub_client):
    """真实竞态：两线程同用户同 label 同凭据并发建绑，最终只一行。

    文件库 + busy timeout：第二线程的 INSERT 在第一线程提交前被
    唯一约束拦下（或被写锁排队到提交后），修后口径必须幂等返回
    已有行、两线程都不 500（即都不抛 IntegrityError）。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path}/race.db",
        connect_args={"check_same_thread": False, "timeout": 15})
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    entered_verify = threading.Event()

    class _SlowStub(_StubQuarkClient):
        def check(self):
            entered_verify.set()
            threading.Event().wait(0.6)
            return super().check()

    results = {}

    def worker(name):
        session = factory()
        try:
            b = ub.create_binding(session, 7, "quark", "1", FAKE_COOKIE)
            results[name] = ("ok", b.id, b.status)
        except Exception as exc:  # noqa: BLE001 — 记录后断言
            results[name] = ("error", type(exc).__name__, str(exc))
        finally:
            session.close()

    orig_client = ub.quarkdrive.QuarkClient
    ub.quarkdrive.QuarkClient = _SlowStub
    try:
        t1 = threading.Thread(target=worker, args=("t1",))
        t1.start()
        assert entered_verify.wait(timeout=10)
        t2 = threading.Thread(target=worker, args=("t2",))
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)
    finally:
        ub.quarkdrive.QuarkClient = orig_client

    assert results["t1"][0] == "ok", results
    assert results["t2"][0] == "ok", results
    assert results["t1"][1] == results["t2"][1]
    session = factory()
    try:
        assert session.query(em.UserStorageBinding).count() == 1
    finally:
        session.close()
