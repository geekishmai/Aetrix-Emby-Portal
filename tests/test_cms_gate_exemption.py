# -*- coding: utf-8 -*-
"""无界（CMS）片库播放免绑定门禁（2026-11-03 用户亲点定版）。

定版口径：「无界片库免绑定，本地库门禁照旧」。
- 条目所属库是 CMS 库（upstream_source_ids 经
  applecms_library.parse_upstream_source_ids 解析后非空，与 #1793
  体检修复同一份定义）→ 播放不要求绑定；
- 本地/挂载库条目 → 门禁原样：未绑定 403 + 原文案；
- 判定只放 playback_gate 一处（binding_required_for_item /
  binding_required_for_upstream / sign_allowed_for_item），调用点不
  各写一份；
- 查不到库 / 条目无 library_id / 无映射 → 需要门禁（fail-closed）。

覆盖：判定入口矩阵、ensure 按条目/按上游口径、详情签名判定矩阵，
以及真 TestClient 全链路（PlaybackInfo、上游取流口、详情线路签名）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as gate


# ---------------------------------------------------------------- 单元层


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


def _library(db, name, upstream_source_ids=""):
    lib = em.Library(guid=uuid.uuid4().hex, name=name,
                     upstream_source_ids=upstream_source_ids)
    db.add(lib)
    db.commit()
    return lib


def _item(db, lib_id, name="片", file_path=None):
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib_id, item_type="movie",
        name=name, file_path=file_path)
    db.add(item)
    db.commit()
    return item


def _mapping(db, item_id, source_id, upstream_item_id):
    row = em.UpstreamItemMapping(
        local_item_id=item_id, source_id=source_id,
        upstream_item_id=upstream_item_id, confidence=0.95,
        evidence="tmdb")
    db.add(row)
    db.commit()
    return row


def _blocked(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as excinfo:
        fn(*args, **kwargs)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == gate.GATE_MESSAGE


def test_binding_required_cms_item_is_false(db):
    lib = _library(db, "cms库", upstream_source_ids="7,9")
    item = _item(db, lib.id)
    assert gate.binding_required_for_item(db, item) is False


def test_binding_required_local_item_is_true(db):
    lib = _library(db, "本地库", upstream_source_ids="")
    item = _item(db, lib.id)
    assert gate.binding_required_for_item(db, item) is True


def test_binding_required_blank_source_ids_is_true(db):
    lib = _library(db, "空白库", upstream_source_ids=" , ,")
    item = _item(db, lib.id)
    assert gate.binding_required_for_item(db, item) is True


def test_binding_required_fail_closed(db):
    assert gate.binding_required_for_item(db, None) is True
    # library_id 列 NOT NULL：无库条目只可能是未落库的瞬时对象，
    # 用命名空间对象锁住防御分支（getattr 取不到/为空 → 需要门禁）。
    orphan = types.SimpleNamespace(library_id=None)
    assert gate.binding_required_for_item(db, orphan) is True
    no_attr = types.SimpleNamespace()
    assert gate.binding_required_for_item(db, no_attr) is True
    dangling = _item(db, 999999)
    assert gate.binding_required_for_item(db, dangling) is True


def test_ensure_for_item_unbound_cms_passes(db):
    lib = _library(db, "cms库", upstream_source_ids="7")
    item = _item(db, lib.id)
    user = _user(db, "free")
    gate.ensure_binding_playback_allowed_for_item(db, user, item)


def test_ensure_for_item_unbound_local_blocked_same_message(db):
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "free")
    _blocked(gate.ensure_binding_playback_allowed_for_item, db, user, item)


def test_ensure_for_item_bound_local_passes(db):
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "bound")
    _binding(db, user.id)
    gate.ensure_binding_playback_allowed_for_item(db, user, item)


def test_ensure_for_item_bound_cms_passes(db):
    lib = _library(db, "cms库", upstream_source_ids="7")
    item = _item(db, lib.id)
    user = _user(db, "bound")
    _binding(db, user.id)
    gate.ensure_binding_playback_allowed_for_item(db, user, item)


def test_ensure_for_item_keeps_play_sign_consumption(db, monkeypatch):
    # 本地条目 + 已下发签名消费：与 ensure 原口径一致，不追溯。
    lib = _library(db, "本地库")
    item = _item(db, lib.id)
    user = _user(db, "free")
    monkeypatch.setattr(
        "backend.emby_server.play_sign.hotlink_enabled", lambda db: True)
    monkeypatch.setattr(
        "backend.emby_server.play_sign.verify_play_sign",
        lambda uid, item_id, exp, sign: True)
    request = types.SimpleNamespace(
        query_params={"sign": "s", "exp": "9999999999", "uid": "7"},
        path_params={"item_id": "guid-1"})
    gate.ensure_binding_playback_allowed_for_item(db, user, item, request)


def test_binding_required_upstream_matrix(db):
    cms_lib = _library(db, "cms库", upstream_source_ids="7")
    local_lib = _library(db, "本地库")
    cms_item = _item(db, cms_lib.id)
    local_item = _item(db, local_lib.id)
    _mapping(db, cms_item.id, 7, "up-cms-1")
    _mapping(db, local_item.id, 7, "up-local-1")
    _mapping(db, cms_item.id, 8, "up-mixed-1")
    _mapping(db, local_item.id, 8, "up-mixed-1")
    assert gate.binding_required_for_upstream(db, 7, "up-cms-1") is False
    assert gate.binding_required_for_upstream(db, 7, "up-local-1") is True
    # 同一上游条目同时映射到 CMS 与本地条目 → fail-closed 需要门禁
    assert gate.binding_required_for_upstream(db, 8, "up-mixed-1") is True
    # 无映射 → 需要门禁
    assert gate.binding_required_for_upstream(db, 7, "up-none") is True


def test_ensure_for_upstream_unbound(db):
    cms_lib = _library(db, "cms库", upstream_source_ids="7")
    local_lib = _library(db, "本地库")
    cms_item = _item(db, cms_lib.id)
    local_item = _item(db, local_lib.id)
    _mapping(db, cms_item.id, 7, "up-cms-1")
    _mapping(db, local_item.id, 7, "up-local-1")
    user = _user(db, "free")
    gate.ensure_binding_playback_allowed_for_upstream(
        db, user, 7, "up-cms-1")
    _blocked(gate.ensure_binding_playback_allowed_for_upstream,
             db, user, 7, "up-local-1")
    _blocked(gate.ensure_binding_playback_allowed_for_upstream,
             db, user, 7, "up-none")


def test_sign_allowed_matrix(db):
    cms_lib = _library(db, "cms库", upstream_source_ids="7")
    local_lib = _library(db, "本地库")
    cms_item = _item(db, cms_lib.id)
    local_item = _item(db, local_lib.id)
    bound = _user(db, "bound")
    _binding(db, bound.id)
    free = _user(db, "free")
    assert gate.sign_allowed_for_item(db, bound.id, local_item) is True
    assert gate.sign_allowed_for_item(db, bound.id, cms_item) is True
    assert gate.sign_allowed_for_item(db, free.id, cms_item) is True
    assert gate.sign_allowed_for_item(db, free.id, local_item) is False
    assert gate.sign_allowed_for_item(db, None, cms_item) is False
    assert gate.sign_allowed_for_item(db, 0, cms_item) is False


# ---------------------------------------------------------------- 全链路

import re
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import play_sign, upstream_sources as us
from backend.security import hash_password

init_db()

PW = "CmsFree#2026"
BOUND = "cmsfree_bound"
FREE = "cmsfree_free"
CMS_LIB_NAME = "免绑CMS库"
LOCAL_LIB_NAME = "免绑本地库"
SRC_EMBY_NAME = "免绑上游源"
SRC_CMS_NAME = "免绑CMS源"
ITEM_NAMES = ["免绑CMS托管片", "免绑CMS线路片", "免绑本地片"]


def _guid() -> str:
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_(ITEM_NAMES)).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name.in_([SRC_EMBY_NAME, SRC_CMS_NAME])
    ).delete(synchronize_session=False)
    db.query(em.Library).filter(
        em.Library.name.in_([CMS_LIB_NAME, LOCAL_LIB_NAME])).delete()
    for uname in (BOUND, FREE):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == u.id).delete()
            db.query(em.MountCredential).filter(
                em.MountCredential.owner_user_id == u.id).delete()
            db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    users = {}
    for uname in (BOUND, FREE):
        u = models.WebUser(
            username=uname, password_hash=hash_password(PW),
            is_active=True, is_staff=True, emby_username=uname,
            emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=users[BOUND],
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(
        user_id=users[BOUND], provider="drive", account_label="种子盘",
        credential_id=cred.id, status="active"))
    src_emby = us.create_source(
        db, name=SRC_EMBY_NAME, source_type="emby",
        base_url="https://upfree.example.com", auth_method="api_key")
    us.enable_source(db, src_emby)
    src_cms = em.UpstreamSource(
        name=SRC_CMS_NAME, source_type="applecms",
        base_url="https://cmsfree.example.com", auth_method="none",
        enabled=True)
    db.add(src_cms)
    db.flush()
    cms_lib = em.Library(
        guid=_guid(), name=CMS_LIB_NAME, collection_type="movies",
        is_enabled=True, upstream_source_ids=str(src_cms.id))
    local_lib = em.Library(
        guid=_guid(), name=LOCAL_LIB_NAME, collection_type="movies",
        is_enabled=True)
    db.add_all([cms_lib, local_lib])
    db.flush()
    cms_hosted = em.MediaItem(
        guid=_guid(), library_id=cms_lib.id, item_type="movie",
        name="免绑CMS托管片", file_path=None,
        duration_ticks=118 * 60 * 10_000_000)
    cms_line = em.MediaItem(
        guid=_guid(), library_id=cms_lib.id, item_type="movie",
        name="免绑CMS线路片",
        file_path="mount://3/电影/免绑CMS线路片/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=118 * 60 * 10_000_000,
        probe_status="failed", probe_attempts=99)
    local_movie = em.MediaItem(
        guid=_guid(), library_id=local_lib.id, item_type="movie",
        name="免绑本地片",
        file_path="mount://3/电影/免绑本地片/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=118 * 60 * 10_000_000,
        probe_status="failed", probe_attempts=99)
    db.add_all([cms_hosted, cms_line, local_movie])
    db.flush()
    db.add_all([
        em.UpstreamItemMapping(
            local_item_id=cms_hosted.id, source_id=src_cms.id,
            upstream_item_id="cms-vod-1", confidence=0.95,
            evidence="tmdb"),
        em.UpstreamItemMapping(
            local_item_id=cms_line.id, source_id=src_emby.id,
            upstream_item_id="up-cms-line-1", confidence=0.95,
            evidence="tmdb"),
        em.UpstreamItemMapping(
            local_item_id=local_movie.id, source_id=src_emby.id,
            upstream_item_id="up-local-1", confidence=0.95,
            evidence="tmdb"),
    ])
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "cms_hosted": cms_hosted.guid, "cms_line": cms_line.guid,
            "local_movie": local_movie.guid, "src_emby": src_emby.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}CmsFreeTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}cmsfree-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(client, seed):
    return _token(client, BOUND)


@pytest.fixture(scope="module")
def free_token(client, seed):
    return _token(client, FREE)


def _h(token):
    return {"X-Emby-Token": token}


def _qs(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _upstream_line(body):
    return [s for s in body.get("MediaSources") or []
            if ":upstream" in str(s.get("Id", ""))]


@pytest.fixture()
def upstream_env(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "_default_http_range_probe",
        lambda url, headers=None, timeout=None: (206, None))


def test_playbackinfo_unbound_cms_hosted_200_with_line(
        client, free_token, seed):
    r = client.post(f"/emby/Items/{seed['cms_hosted']}/PlaybackInfo",
                    headers=_h(free_token), json={})
    assert r.status_code == 200, r.text
    lines = _upstream_line(r.json())
    assert len(lines) == 1, r.json().get("MediaSources")
    q = _qs(lines[0]["DirectStreamUrl"])
    assert play_sign.verify_play_sign(
        int(q["uid"]), seed["cms_hosted"], int(q["exp"]), q["sign"])


def test_playbackinfo_unbound_cms_line_item_200(
        client, free_token, seed):
    r = client.post(f"/emby/Items/{seed['cms_line']}/PlaybackInfo",
                    headers=_h(free_token), json={})
    assert r.status_code == 200, r.text
    assert len(_upstream_line(r.json())) == 1


def test_playbackinfo_unbound_local_403_same_message(
        client, free_token, seed):
    r = client.post(f"/emby/Items/{seed['local_movie']}/PlaybackInfo",
                    headers=_h(free_token), json={})
    assert r.status_code == 403, r.text
    assert gate.GATE_MESSAGE in r.text


def test_playbackinfo_bound_local_still_200(client, bound_token, seed):
    r = client.post(f"/emby/Items/{seed['local_movie']}/PlaybackInfo",
                    headers=_h(bound_token), json={})
    assert r.status_code == 200, r.text
    assert len(_upstream_line(r.json())) == 1


def test_detail_unbound_cms_line_signed(client, free_token, seed):
    r = client.get(f"/emby/Items/{seed['cms_hosted']}",
                   headers=_h(free_token))
    assert r.status_code == 200, r.text
    lines = _upstream_line(r.json())
    assert len(lines) == 1, r.json().get("MediaSources")
    q = _qs(lines[0]["DirectStreamUrl"])
    assert int(q["uid"]) == seed["free_id"]
    assert play_sign.verify_play_sign(
        int(q["uid"]), seed["cms_hosted"], int(q["exp"]), q["sign"])


def test_detail_unbound_local_line_unsigned(client, free_token, seed):
    r = client.get(f"/emby/Items/{seed['local_movie']}",
                   headers=_h(free_token))
    assert r.status_code == 200, r.text
    lines = _upstream_line(r.json())
    assert len(lines) == 1
    q = _qs(lines[0]["DirectStreamUrl"])
    assert "sign" not in q and "exp" not in q and "uid" not in q


def test_upstream_stream_unbound_cms_redirects(
        client, free_token, seed, upstream_env):
    r = client.get(
        f"/emby/Videos/{seed['cms_line']}/upstream/stream",
        headers=_h(free_token), follow_redirects=False)
    assert r.status_code == 302, r.text
    assert "upfree.example.com" in r.headers.get("location", "")


def test_upstream_stream_unbound_local_blocked(
        client, free_token, seed, upstream_env):
    r = client.get(
        f"/emby/Videos/{seed['local_movie']}/upstream/stream",
        headers=_h(free_token), follow_redirects=False)
    assert r.status_code == 403, r.text
    assert "location" not in r.headers


def test_upstream_stream_bound_local_still_redirects(
        client, bound_token, seed, upstream_env):
    r = client.get(
        f"/emby/Videos/{seed['local_movie']}/upstream/stream",
        headers=_h(bound_token), follow_redirects=False)
    assert r.status_code == 302, r.text
