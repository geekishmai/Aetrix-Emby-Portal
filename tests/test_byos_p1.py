"""BYOS P1 绑定管理（2026-11-02 二期删除后的存续口径）。

原 P1 范围里的 115 用户绑盘线（建绑验活/直链变体/凭据隔离）已随
二期整体删除，本文件只保留仍然成立的钉子：
- user_storage_bindings / mount_credentials 的模型约束（provider
  枚举含存量 pan115/quark 值，存量数据不删、CHECK 不收窄）；
- 已下线 provider（pan115/quark）建绑一律拒绝（unsupported +
  下线文案、不落行）；Drive 明文凭据仍拒绝、指引 OAuth 口；
- 解绑删双行（外键强制口径）；
- 管理员侧 Pan115Mount 的 cookie_override 实例仍不读写共享目录
  缓存（共享挂载保留能力）；
- 变体解析器对显式请求变体的通用口径（保留逻辑）。
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import play_variants as pv
from backend.emby_server import user_bindings as ub


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


def _mount115(db):
    m = em.StorageMount(name="m115", mount_type="115", path="115:/")
    db.add(m)
    db.commit()
    return m


# ---------- 模型约束 ----------



def test_provider_and_status_check_constraints(db):
    cred = em.MountCredential(mount_id=0, scope="user", owner_user_id=7,
                              sealed_credential="x", key_id="fernet-v1")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=7, provider="bogus",
                                 account_label="a", credential_id=cred.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(em.UserStorageBinding(user_id=7, provider="pan115",
                                 account_label="a", credential_id=cred.id,
                                 status="bogus"))
    with pytest.raises(IntegrityError):
        db.commit()


# ---------- 凭据行约束（P1 实测修） ----------

def test_mount_attached_credential_unique_still_enforced(db):
    # 挂载附属的用户行仍受（一挂载一 scope 一用户一行）约束；
    # 只有 mount_id=NULL 的绑定行豁免（NULL 在唯一约束里互不相等）。
    # 注：服级行 owner_user_id=NULL 本就不受该约束限（#1230 原语义，
    # 本 PR 不改）；这里钉的是用户行那一半。
    db.add(em.MountCredential(mount_id=5, scope="user",
                              owner_user_id=7, sealed_credential="x"))
    db.commit()
    db.add(em.MountCredential(mount_id=5, scope="user",
                              owner_user_id=7, sealed_credential="y"))
    with pytest.raises(IntegrityError):
        db.commit()


# ---------- 建绑与验活 ----------



def test_create_binding_removed_provider_rejected(db):
    # 二期删除口径：pan115/quark 建绑在任何凭据处理之前拒绝
    # （unsupported + 下线文案、不落行、不验活）。原「空凭据拒绝」
    # 用例随 Cookie 型建绑口一起消失，拒绝点前移到 provider 门。
    for provider in ("pan115", "quark"):
        with pytest.raises(ub.BindingError) as excinfo:
            ub.create_binding(db, 7, provider, "旧盘", "SOME-CREDENTIAL")
        assert excinfo.value.kind == "unsupported"
        assert excinfo.value.message == ub.REMOVED_PROVIDER_MESSAGE
    assert db.query(em.UserStorageBinding).count() == 0










def test_create_binding_drive_rejected_in_p1(db):
    # P2 已上线：Drive 仍不收明文凭据，拒绝文案改为指引 OAuth 口
    # （行为不变：kind=unsupported、不落行）。
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, "drive", "盘", "whatever")
    assert excinfo.value.kind == "unsupported"
    assert "oauth/start" in excinfo.value.message
    assert db.query(em.UserStorageBinding).count() == 0


# ---------- 解绑与重验 ----------



@pytest.fixture()
def fk_db():
    """外键强制开启的隔离库：生产 PG 强制外键，默认 SQLite 不强制，
    解绑删凭据撞外键的 bug 就是这么漏网的（2026-10-26 生产 500）。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_delete_binding_fk_enforced_removes_both_rows(fk_db):
    user = base_models.WebUser(username="u1", password_hash="x")
    fk_db.add(user)
    fk_db.commit()
    cred = em.MountCredential(mount_id=None, scope="user", owner_user_id=user.id,
                              sealed_credential="", key_id="fernet-v1", status="invalid")
    fk_db.add(cred)
    fk_db.flush()
    b = em.UserStorageBinding(user_id=user.id, provider="drive", account_label="谷歌盘",
                              credential_id=cred.id, status="invalid")
    fk_db.add(b)
    fk_db.commit()
    ub.delete_binding(fk_db, user.id, b.id)
    assert fk_db.query(em.UserStorageBinding).count() == 0
    assert fk_db.query(em.MountCredential).filter(
        em.MountCredential.id == cred.id).count() == 0








# ---------- 115 直链解析 ----------









def test_byos_provider_instance_bypasses_shared_list_cache(db):
    # 绑定凭据实例不读写共享目录缓存（键不含凭据，复用即串用户）
    m = _mount115(db)
    p = mount_lib.Pan115Mount(m, db=db, cookie_override="COOKIE-U")
    assert p._byos_no_cache is True
    assert p._cookie() == "COOKIE-U"
    p2 = mount_lib.Pan115Mount(m, db=db)
    assert p2._byos_no_cache is False


# ---------- 变体清单 ----------



def test_resolve_requested_variant_byos(db):
    assert pv.resolve_requested_variant(
        {"variant": "byos115_direct"}, ["mount_relay", "byos115_direct"]) == (
        "byos115_direct", None)
    assert pv.resolve_requested_variant(
        {"variant": "byos115_direct"}, ["mount_relay"]) == (None, 409)
    assert pv.resolve_requested_variant(
        {"variant": "nope"}, ["mount_relay"]) == (None, 404)
