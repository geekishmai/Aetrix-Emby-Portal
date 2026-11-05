# -*- coding: utf-8 -*-
"""用户自备盘绑定入口总开关（user_storage_binding_enabled）。

模式 B（服务端统一供片）下关掉绑定入口：providers 空列表 + enabled=false、
新绑定被拒绝（115/夸克与 Drive OAuth）、已有绑定行不删除；默认开、读失败
回默认开（入口控制不是安全门禁）。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.emby_server import user_bindings as ub
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
def _reset_cache():
    store.invalidate(ub.KEY_USER_STORAGE_BINDING_ENABLED)
    yield
    store.invalidate(ub.KEY_USER_STORAGE_BINDING_ENABLED)


def _set_switch(db, value):
    db.add(base_models.SystemConfig(
        key=ub.KEY_USER_STORAGE_BINDING_ENABLED, value=value))
    db.commit()
    store.invalidate(ub.KEY_USER_STORAGE_BINDING_ENABLED)


def _mount(db, name, mount_type, is_enabled=True):
    row = em.StorageMount(name=name, mount_type=mount_type, path="",
                          is_enabled=is_enabled)
    db.add(row)
    db.commit()
    return row


def _binding_row(db, user_id=7):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    row = em.UserStorageBinding(
        user_id=user_id, provider="pan115", account_label="我的115",
        credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return base_models.WebUser(id=user_id, username=f"u{user_id}",
                               password_hash="x", is_active=True)


# ---------------- 默认与读取失败 ----------------

def test_default_on(db):
    assert ub.user_storage_binding_enabled(db) is True


def test_store_read_failure_defaults_on(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(store, "get_value", boom)
    assert ub.user_storage_binding_enabled(db) is True


# ---------------- providers ----------------

def test_switch_off_providers_empty_even_with_mounts(db):
    _mount(db, "m-115", "115")
    _mount(db, "m-drive", "drive")
    assert ub.list_binding_providers(db) == ["drive", "pan115"]
    _set_switch(db, "false")
    assert ub.list_binding_providers(db) == []


def test_providers_endpoint_reports_enabled_flag(db):
    _mount(db, "m-115", "115")
    user = _user()
    body = portal.list_storage_binding_providers(request_user=user, db=db)
    assert body == {"providers": ["pan115"], "enabled": True}
    _set_switch(db, "false")
    body = portal.list_storage_binding_providers(request_user=user, db=db)
    assert body == {"providers": [], "enabled": False}


# ---------------- 新绑定拒绝 ----------------

def test_switch_off_rejects_create_binding(db):
    _set_switch(db, "false")
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, "pan115", "我的115", "UID=1_A1_1;CID=x")
    assert excinfo.value.kind == "unsupported"
    assert "无需绑定" in excinfo.value.message
    assert db.query(em.UserStorageBinding).count() == 0


def test_switch_on_still_validates_provider(db):
    """开关开时不被总开关拦：照常走到 provider 校验。"""
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, "bogus", "x", "cookie")
    assert excinfo.value.kind == "validation"


def test_switch_off_rejects_drive_oauth_new_binding(db, monkeypatch):
    from backend.emby_server import drive_oauth

    called = {"n": 0}

    def fake_start(*a, **k):
        called["n"] += 1
        return {"authorization_url": "https://accounts.google.com/x"}

    monkeypatch.setattr(drive_oauth, "start_drive_oauth", fake_start)
    _set_switch(db, "false")
    request = Request({"type": "http", "method": "POST",
                       "path": "/storage-bindings/drive/oauth/start",
                       "headers": [], "client": ("127.0.0.1", 1),
                       "url": "https://v2.example.com/api/user/emby/"
                              "storage-bindings/drive/oauth/start"})
    with pytest.raises(portal.HTTPException) as excinfo:
        portal.start_drive_oauth_binding(
            body=portal.DriveOAuthStart(account_label="谷歌盘"),
            request=request, response=Response(), request_user=_user(), db=db)
    assert excinfo.value.status_code == 403
    assert called["n"] == 0


def test_switch_off_allows_rebind_of_existing_drive_row(db, monkeypatch):
    """重绑已有行保留入口（已有绑定不删除、可修复）。"""
    from backend.emby_server import drive_oauth

    monkeypatch.setattr(
        drive_oauth, "start_drive_oauth",
        lambda *a, **k: {"authorization_url": "https://accounts.google.com/x"})
    _set_switch(db, "false")
    request = Request({"type": "http", "method": "POST",
                       "path": "/storage-bindings/drive/oauth/start",
                       "headers": [], "client": ("127.0.0.1", 1),
                       "url": "https://v2.example.com/api/user/emby/"
                              "storage-bindings/drive/oauth/start"})
    body = portal.start_drive_oauth_binding(
        body=portal.DriveOAuthStart(account_label="谷歌盘",
                                    rebind_binding_id=3),
        request=request, response=Response(), request_user=_user(), db=db)
    assert body["authorization_url"].startswith("https://accounts.google.com/")


# ---------------- 已有绑定行保留 ----------------

def test_switch_off_keeps_existing_binding_rows(db):
    row = _binding_row(db)
    _set_switch(db, "false")
    rows = ub.list_bindings(db, 7)
    assert [r["id"] for r in rows] == [row.id]
    assert db.query(em.UserStorageBinding).count() == 1
    assert row.status == "active"
