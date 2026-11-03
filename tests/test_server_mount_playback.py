# -*- coding: utf-8 -*-
"""服务端挂载片源按库免绑定 + 115 必须中转（§16.5 / 模式 B，fix/server-mount-playback）。

产品模式：服务器统一提供 115 挂载片源，用户零网盘账号。
- 库行 `no_binding_required`（默认关）显式豁免该库的服务端挂载条目；
- `byos://` 用户自备盘条目永远要求绑定；
- 115 挂载**绝不 302** 到 115 CDN，一律服务端中转（2026-11-01 全中转路线）；
- 全局兜底开关 `playback_require_user_binding` / env（默认 1）关闭时全放行；
- 接口与上游 #1797 `binding_required_for_item` 同名同签名，便于合并/提 PR。
"""
import asyncio
import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as gate
from backend.emby_server import mount_routes
from backend.emby_server.mounts import PlayTarget
from backend.emby_server.next302_play_route import ROUTE_SERVER_MOUNT, decide_route


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


def _binding(db, user_id, status="active", provider="pan115"):
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


def _library(db, exempt=False, name=None):
    lib = em.Library(guid=f"lib-{name or id(db)}", name=name or "服务端供片库",
                     collection_type="movies", no_binding_required=exempt)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, path="115:/媒体库"):
    mount = em.StorageMount(name=f"m-{abs(hash(path))}", path=path)
    db.add(mount)
    db.commit()
    return mount


def _item(db, file_path, library_id=None, container="mkv"):
    return types.SimpleNamespace(
        id=1, file_path=file_path, library_id=library_id,
        container=container, drive_file_id="", item_type="movie")


def _plain_request():
    return types.SimpleNamespace(
        query_params={}, path_params={"item_id": "guid-1"})


def _ensure(db, user, item):
    gate.ensure_binding_playback_allowed(db, user, None, item)


# ---------------- 库级开关 ----------------

def test_library_switch_off_keeps_gate_for_mount_item(db):
    lib = _library(db, exempt=False)
    _mount(db)
    user = _user(db, "u1")
    item = _item(db, "mount://1/媒体库/电影/a.mkv", lib.id)
    with pytest.raises(HTTPException) as exc:
        _ensure(db, user, item)
    assert exc.value.status_code == 403


def test_library_switch_on_exempts_mount_item(db):
    lib = _library(db, exempt=True)
    _mount(db)
    user = _user(db, "u1")
    item = _item(db, "mount://1/媒体库/电影/a.mkv", lib.id)
    _ensure(db, user, item)


def test_switch_on_never_exempts_byos_item(db):
    lib = _library(db, exempt=True)
    user = _user(db, "u1")
    item = _item(db, "byos://9/电影/a.mkv", lib.id)
    with pytest.raises(HTTPException):
        _ensure(db, user, item)


def test_bound_user_passes_byos_item(db):
    lib = _library(db, exempt=False)
    user = _user(db, "u1")
    _binding(db, user.id)
    item = _item(db, "byos://9/电影/a.mkv", lib.id)
    _ensure(db, user, item)


def test_unknown_library_fails_closed(db):
    user = _user(db, "u1")
    item = _item(db, "mount://1/媒体库/a.mkv", library_id=None)
    with pytest.raises(HTTPException):
        _ensure(db, user, item)


# ---------------- 全局兜底开关 ----------------

def test_switch_off_allows_everything(db, monkeypatch):
    lib = _library(db, exempt=False)
    user = _user(db, "u1")
    monkeypatch.setenv("EMBY_PLAYBACK_REQUIRE_USER_BINDING", "0")
    for path in ("mount://1/a.mkv", "byos://9/a.mkv", "/srv/media/a.mkv"):
        _ensure(db, user, _item(db, path, lib.id))
    assert gate.binding_gate_enabled(db) is False


def test_system_config_overrides_env(db, monkeypatch):
    monkeypatch.setenv("EMBY_PLAYBACK_REQUIRE_USER_BINDING", "0")
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "true")
    assert gate.binding_gate_enabled(db) is True
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "false")
    assert gate.binding_gate_enabled(db) is False


def test_switch_read_failure_defaults_on(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config down")
    monkeypatch.setattr(gate.store, "get_value", boom)
    assert gate.binding_gate_enabled(db) is True


# ---------------- 上游同接口 ----------------

def test_binding_required_for_item_interface(db):
    lib = _library(db, exempt=True)
    assert gate.binding_required_for_item(
        db, _item(db, "mount://1/a.mkv", lib.id)) is False
    assert gate.binding_required_for_item(
        db, _item(db, "byos://9/a.mkv", lib.id)) is True
    assert gate.binding_required_for_item(db, None) is True


def test_binding_required_for_upstream_uses_mapping(db):
    lib = _library(db, exempt=True)
    item = em.MediaItem(guid="map-item", library_id=lib.id, item_type="movie",
                        name="片", file_path="mount://1/a.mkv")
    db.add(item)
    db.flush()
    mapping = em.UpstreamItemMapping(
        source_id=1, upstream_item_id="up-1", local_item_id=item.id,
        confidence=1.0, evidence="tmdb")
    db.add(mapping)
    db.commit()
    assert gate.binding_required_for_upstream(db, 1, "up-1") is False
    # 查不到映射 → fail-closed
    assert gate.binding_required_for_upstream(db, 1, "missing") is True


# ---------------- 签名签发 ----------------

def test_can_issue_sign_for_exempt_mount_without_binding(db):
    lib = _library(db, exempt=True)
    user = _user(db, "u1")
    mount_item = _item(db, "mount://1/a.mkv", lib.id)
    byos_item = _item(db, "byos://9/a.mkv", lib.id)
    assert gate.can_issue_play_sign(db, user.id, mount_item) is True
    assert gate.can_issue_play_sign(db, user.id, byos_item) is False
    _binding(db, user.id)
    assert gate.can_issue_play_sign(db, user.id, byos_item) is True


# ---------------- next302 选路（AL-1 P2） ----------------

def test_decide_route_server_mount_wins_over_bind_guide():
    verdict = decide_route(bound=False, has_user_ready_copy=False,
                           server_mount=True)
    assert verdict["route"] == ROUTE_SERVER_MOUNT
    assert verdict["shared"] is True


def test_decide_route_byos_unbound_still_bind_guide():
    verdict = decide_route(bound=False, has_user_ready_copy=False)
    assert verdict["route"] == "bind_guide"


def _item_row(db, file_path, library_id):
    row = em.MediaItem(guid=f"item-{abs(hash(file_path))}",
                       library_id=library_id, item_type="movie",
                       name="服务端片源", file_path=file_path)
    db.add(row)
    db.commit()
    return row


def _play_status(db, monkeypatch, row, user):
    from backend.api import next302
    monkeypatch.setattr(next302, "require_search_enabled", lambda: None)
    monkeypatch.setattr(next302, "_scope", lambda db_, user_: (None, True))
    return next302.play_status(row.id, user, db)


def test_play_status_exempt_mount_is_play(db, monkeypatch):
    lib = _library(db, exempt=True)
    row = _item_row(db, "mount://1/媒体库/a.mkv", lib.id)
    user = _user(db, "u1")
    out = _play_status(db, monkeypatch, row, user)
    assert out["route"] == ROUTE_SERVER_MOUNT
    assert out["action"] == "play"
    assert out["server_mount"] is True


def test_play_status_switch_off_byos_not_server_mount(db, monkeypatch):
    """AL-1 P2：全局开关关闭时，byos 条目不能被误判成 server_mount。"""
    lib = _library(db, exempt=False)
    row = _item_row(db, "byos://9/媒体库/a.mkv", lib.id)
    user = _user(db, "u1")
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "false")
    out = _play_status(db, monkeypatch, row, user)
    assert out["route"] != ROUTE_SERVER_MOUNT
    assert out["action"] != "play"


# ---------------- mounted File：115 必须中转，绝不 302 ----------------

@pytest.fixture()
def _mounted_env(monkeypatch):
    def _setup(item, target):
        monkeypatch.setattr(mount_routes, "_require_visible_item",
                            lambda db, user, item_id: item)
        monkeypatch.setattr(mount_routes, "ensure_playback_allowed",
                            lambda db, user: None)
        monkeypatch.setattr(mount_routes, "_play_target", lambda db, it: target)
    return _setup


def _call_mounted(user, db):
    return asyncio.run(
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db))


def test_mounted_file_drive_mount_still_302(db, monkeypatch, _mounted_env):
    lib = _library(db, exempt=True)
    _mount(db, path="drive:folder-1")
    item = types.SimpleNamespace(
        id=1, file_path="mount://1/电影/a.mkv", library_id=lib.id,
        container="mkv", drive_file_id="fid-1")
    _mounted_env(item, PlayTarget(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url",
                        lambda fid: "https://example.invalid/dl/" + fid)
    user = _user(db, "u1")
    resp = _call_mounted(user, db)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.invalid/dl/")


def test_mounted_file_115_mount_goes_relay_never_302(db, monkeypatch, _mounted_env):
    lib = _library(db, exempt=True)
    _mount(db, path="115:/媒体库")
    item = types.SimpleNamespace(
        id=1, file_path="mount://1/电影/a.mkv", library_id=lib.id,
        container="mkv", drive_file_id="fid-115")
    target = PlayTarget(kind="url", value="https://115cdn.example/secret",
                        headers={"Cookie": "UID=secret", "User-Agent": "UA"})
    _mounted_env(item, target)
    calls = {}

    async def fake_relay(url, request, headers, media_type, **kwargs):
        calls.update(url=url, headers=headers, media_type=media_type)
        from fastapi.responses import Response
        return Response(content=b"relay", media_type=media_type)

    from backend.emby_server import streaming
    monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_relay)
    user = _user(db, "u1")
    resp = _call_mounted(user, db)
    assert calls["url"] == "https://115cdn.example/secret"
    assert calls["headers"]["Cookie"] == "UID=secret"
    assert getattr(resp, "status_code", None) == 200
    assert not hasattr(resp, "headers") or "location" not in resp.headers


def test_mounted_file_unbound_byos_blocked(db, monkeypatch, _mounted_env):
    lib = _library(db, exempt=True)
    item = types.SimpleNamespace(
        id=1, file_path="byos://9/电影/a.mkv", library_id=lib.id,
        container="mkv", drive_file_id="")
    _mounted_env(item, PlayTarget(kind="url", value=""))
    user = _user(db, "u1")
    with pytest.raises(HTTPException) as exc:
        _call_mounted(user, db)
    assert exc.value.status_code == 403


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
