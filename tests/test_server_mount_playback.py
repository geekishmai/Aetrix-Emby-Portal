# -*- coding: utf-8 -*-
"""服务端挂载片源按库免绑定（R2 重贴版，2026-11-04）。

上游 #1797 提供 ``binding_required_for_item`` / ``sign_allowed_for_item``
单一入口（CMS 库免绑定）；本 fork 在该接口上并入库级开关
``no_binding_required``：开启后该库的服务端挂载（``mount://``）条目免绑定，
``byos://`` 用户自备盘条目永远要求绑定。

next302 门户选路已被上游整体删除（user_frontend 2026-10-13 起无网页播放器，
播放统一走 Emby 协议 PlaybackInfo/video_stream）——本文件不再有
ROUTE_SERVER_MOUNT 相关断言。
"""
import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as gate


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="盘",
        credential_id=cred.id, status=status)
    db.add(binding)
    db.commit()
    return binding


def _library(db, exempt=False, cms_sources="", name=None):
    lib = em.Library(guid=f"lib-{name or id(db)}", name=name or "服务端供片库",
                     collection_type="movies", no_binding_required=exempt,
                     upstream_source_ids=cms_sources)
    db.add(lib)
    db.commit()
    return lib


def _item(file_path, library_id=None):
    return types.SimpleNamespace(
        id=1, file_path=file_path, library_id=library_id,
        container="mkv", drive_file_id="", item_type="movie")


def _ensure(db, user, item):
    gate.ensure_binding_playback_allowed_for_item(db, user, item, None)


# ---------------- 库级开关 ----------------

def test_library_switch_off_keeps_gate_for_mount_item(db):
    lib = _library(db, exempt=False)
    user = _user(db, "u1")
    item = _item("mount://1/媒体库/电影/a.mkv", lib.id)
    with pytest.raises(HTTPException) as exc:
        _ensure(db, user, item)
    assert exc.value.status_code == 403


def test_library_switch_on_exempts_mount_item(db):
    lib = _library(db, exempt=True)
    user = _user(db, "u1")
    item = _item("mount://1/媒体库/电影/a.mkv", lib.id)
    _ensure(db, user, item)


def test_switch_on_never_exempts_byos_item(db):
    lib = _library(db, exempt=True)
    user = _user(db, "u1")
    item = _item("byos://9/电影/a.mkv", lib.id)
    with pytest.raises(HTTPException):
        _ensure(db, user, item)


def test_bound_user_passes_byos_item(db):
    lib = _library(db, exempt=False)
    user = _user(db, "u1")
    _binding(db, user.id)
    item = _item("byos://9/电影/a.mkv", lib.id)
    _ensure(db, user, item)


def test_cms_library_exempt_keeps_upstream_semantics(db):
    """上游 #1797：CMS 库（upstream_source_ids 非空）免绑定。"""
    lib = _library(db, exempt=False, cms_sources="3")
    user = _user(db, "u1")
    item = _item("/hosted/no-file.mkv", lib.id)
    assert gate.binding_required_for_item(db, item) is False
    _ensure(db, user, item)


def test_unknown_library_fails_closed(db):
    user = _user(db, "u1")
    item = _item("mount://1/媒体库/a.mkv", library_id=None)
    assert gate.binding_required_for_item(db, item) is True
    with pytest.raises(HTTPException):
        _ensure(db, user, item)


# ---------------- 全局兜底开关 ----------------

def test_switch_off_allows_everything(db, monkeypatch):
    lib = _library(db, exempt=False)
    user = _user(db, "u1")
    monkeypatch.setenv("EMBY_PLAYBACK_REQUIRE_USER_BINDING", "0")
    for path in ("mount://1/a.mkv", "byos://9/a.mkv", "/srv/media/a.mkv"):
        _ensure(db, user, _item(path, lib.id))
    assert gate.binding_gate_enabled(db) is False


def test_system_config_overrides_env(db, monkeypatch):
    monkeypatch.setenv("EMBY_PLAYBACK_REQUIRE_USER_BINDING", "0")
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "true")
    assert gate.binding_gate_enabled(db) is True
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "false")
    assert gate.binding_gate_enabled(db) is False


# ---------------- 签名轨 ----------------

def test_sign_allowed_for_exempt_mount_without_binding(db):
    lib = _library(db, exempt=True)
    user = _user(db, "u1")
    mount_item = _item("mount://1/a.mkv", lib.id)
    byos_item = _item("byos://9/a.mkv", lib.id)
    assert gate.sign_allowed_for_item(db, user.id, mount_item) is True
    assert gate.sign_allowed_for_item(db, user.id, byos_item) is False
    _binding(db, user.id)
    assert gate.sign_allowed_for_item(db, user.id, byos_item) is True


def test_sign_requires_user_id(db):
    lib = _library(db, exempt=True)
    assert gate.sign_allowed_for_item(db, None, _item("mount://1/a.mkv", lib.id)) is False


# ---------------- 后台库开关保存 ----------------

def test_update_library_toggle_no_binding_required(db):
    from backend.emby_server import portal
    lib = _library(db, exempt=False)
    req = portal.LibraryUpdate(no_binding_required=True)
    portal.update_library(lib.id, req, None, db)
    db.refresh(lib)
    assert lib.no_binding_required is True
    portal.update_library(lib.id, portal.LibraryUpdate(no_binding_required=False),
                          None, db)
    db.refresh(lib)
    assert lib.no_binding_required is False
