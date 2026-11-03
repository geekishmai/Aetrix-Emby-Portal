# -*- coding: utf-8 -*-
"""服务端挂载片源不设绑定门（§16.5，fix/server-mount-playback）。

产品模式：服务器统一提供 115 挂载片源，用户零网盘账号。门禁收窄为：
- 服务端挂载（``mount://``）条目 → 不需要用户绑定（服务端中继出流）；
- 用户自备盘（``byos://`` / 用户副本）→ 仍需 active 绑定；
- 兜底开关 ``playback_require_user_binding`` / env
  ``EMBY_PLAYBACK_REQUIRE_USER_BINDING=0`` → 全站播放不再要求绑定。

原有 ``test_playback_gate.py`` / ``test_mount_file_binding_gate.py`` 的条目
没有 ``file_path``（不是服务端挂载），行为必须逐字不变。
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
from backend.emby_server import mount_routes
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


def _item(file_path):
    return types.SimpleNamespace(id=1, file_path=file_path)


MOUNT_ITEM = _item("mount://3/媒体库/电影/a.mkv")
BYOS_ITEM = _item("byos://9/电影/a.mkv")
PLAIN_ITEM = _item("/srv/media/a.mkv")


def _plain_request():
    return types.SimpleNamespace(
        query_params={}, path_params={"item_id": "guid-1"})


# ---------------- 门禁豁免 ----------------

def test_unbound_user_can_play_server_mount_item(db):
    user = _user(db, "u1")
    gate.ensure_binding_playback_allowed(db, user, None, MOUNT_ITEM)


def test_unbound_user_still_blocked_on_byos_item(db):
    user = _user(db, "u1")
    with pytest.raises(HTTPException) as exc:
        gate.ensure_binding_playback_allowed(db, user, None, BYOS_ITEM)
    assert exc.value.status_code == 403
    assert exc.value.detail == gate.GATE_MESSAGE


def test_unbound_user_still_blocked_on_plain_item(db):
    user = _user(db, "u1")
    with pytest.raises(HTTPException):
        gate.ensure_binding_playback_allowed(db, user, None, PLAIN_ITEM)


def test_bound_user_passes_byos_item(db):
    user = _user(db, "u1")
    _binding(db, user.id)
    gate.ensure_binding_playback_allowed(db, user, None, BYOS_ITEM)


# ---------------- 兜底开关 ----------------

def test_switch_off_allows_everything(db, monkeypatch):
    user = _user(db, "u1")
    monkeypatch.setenv("EMBY_PLAYBACK_REQUIRE_USER_BINDING", "0")
    for item in (MOUNT_ITEM, BYOS_ITEM, PLAIN_ITEM):
        gate.ensure_binding_playback_allowed(db, user, None, item)
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


# ---------------- 签名签发 ----------------

def test_can_issue_sign_for_mount_item_without_binding(db):
    user = _user(db, "u1")
    assert gate.can_issue_play_sign(db, user.id, MOUNT_ITEM) is True
    assert gate.can_issue_play_sign(db, user.id, BYOS_ITEM) is False
    _binding(db, user.id)
    assert gate.can_issue_play_sign(db, user.id, BYOS_ITEM) is True


def test_can_issue_sign_when_switch_off(db, monkeypatch):
    user = _user(db, "u1")
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "false")
    assert gate.can_issue_play_sign(db, user.id, PLAIN_ITEM) is True
    assert gate.can_issue_play_sign(db, None, PLAIN_ITEM) is False


# ---------------- next302 选路 ----------------

def test_decide_route_server_mount_wins_over_bind_guide():
    verdict = decide_route(bound=False, has_user_ready_copy=False,
                           server_mount=True)
    assert verdict["route"] == ROUTE_SERVER_MOUNT
    assert verdict["shared"] is True


def test_decide_route_byos_unbound_still_bind_guide():
    verdict = decide_route(bound=False, has_user_ready_copy=False)
    assert verdict["route"] == "bind_guide"


# ---------------- mounted File 端点 ----------------

@pytest.fixture()
def _mounted(monkeypatch):
    item = types.SimpleNamespace(drive_file_id="fid-1",
                                 file_path="mount://3/媒体库/a.mkv")
    monkeypatch.setattr(mount_routes, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(mount_routes, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(mount_routes, "_play_target", lambda db, item:
                        types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url",
                        lambda fid: "https://example.invalid/dl/" + fid)


def test_mounted_file_allows_unbound_user_for_server_mount(db, _mounted):
    user = _user(db, "u1")
    resp = mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302


def test_mounted_file_blocks_unbound_byos(db, _mounted, monkeypatch):
    user = _user(db, "u1")
    # 同一个 fixture 的 item 改成 byos 路径：仍必须 403
    import backend.emby_server.mount_routes as mr
    byos = types.SimpleNamespace(drive_file_id="fid-1",
                                 file_path="byos://9/媒体库/a.mkv")
    monkeypatch.setattr(mr, "_require_visible_item",
                        lambda db, user, item_id: byos)
    with pytest.raises(HTTPException) as exc:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert exc.value.status_code == 403


# ---------------- next302 play_status 动作映射 ----------------

def _item_row(db, file_path):
    lib = em.Library(guid="lib-sm", name="服务端供片库",
                     collection_type="movies")
    db.add(lib)
    db.flush()
    row = em.MediaItem(guid="item-sm", library_id=lib.id, item_type="movie",
                       name="服务端片源", file_path=file_path)
    db.add(row)
    db.commit()
    return row


def test_play_status_server_mount_returns_play(db, monkeypatch):
    from backend.api import next302
    row = _item_row(db, "mount://3/媒体库/电影/a.mkv")
    user = _user(db, "u1")
    monkeypatch.setattr(next302, "require_search_enabled", lambda: None)
    monkeypatch.setattr(next302, "_scope", lambda db_, user_: (None, True))
    out = next302.play_status(row.id, user, db)
    assert out["route"] == ROUTE_SERVER_MOUNT
    assert out["action"] == "play"
    assert out["server_mount"] is True


def test_play_status_switch_off_allows_plain_item(db, monkeypatch):
    from backend.api import next302
    row = _item_row(db, "/srv/media/a.mkv")
    user = _user(db, "u1")
    monkeypatch.setattr(next302, "require_search_enabled", lambda: None)
    monkeypatch.setattr(next302, "_scope", lambda db_, user_: (None, True))
    monkeypatch.setattr(gate.store, "get_value", lambda *a, **k: "false")
    out = next302.play_status(row.id, user, db)
    assert out["route"] == ROUTE_SERVER_MOUNT
    assert out["action"] == "play"
