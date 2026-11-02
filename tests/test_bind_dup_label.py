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


