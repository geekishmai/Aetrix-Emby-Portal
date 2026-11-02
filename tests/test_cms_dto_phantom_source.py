"""详情 DTO 托管行「空壳来源」回归。

2026-11-02 群反馈：Rex 播 CMS 剧《无可替代》第06集（托管行、无本地
文件）时，来源列表显示 2 条来源、一红一绿。根因：``_item_dto`` 的
full 分支对所有 movie/episode 无条件把 ``_media_source`` 本地源放在
MediaSources[0]，托管行没有 file_path，这条源是空壳——无文件、无流
信息、按其 Id 走通用 /stream 必失败，客户端据 DTO 渲染来源列表，
空壳被标红、真线路被标绿。PlaybackInfo 托管分支走
``_upstream_only_media_sources``（只有线路、无空壳）是对的，坏的
只是详情/选集 DTO 面。

锁住的口径：
- 托管行（movie/episode 且无 file_path）有可展示映射 → MediaSources
  只放上游线路：恰 1 条、Id=``{guid}:upstream:{mapping_id}``，
  不出现 guid 裸 Id 的本地空壳；
- 托管行无可展示映射 → MediaSources=[]、MediaSourceCount=0，
  DTO 组装不抛错（与 PlaybackInfo 空源口径一致）；
- 本地条目（有 file_path）带映射 → 维持原口径：主源＋线路共 2 条，
  主源 Id 为 guid 裸 Id。

测试形态照 test_upstream_detail_line.py：真 TestClient 打整 app、
种子数据直落 app 库；源用 applecms 型（合成线路不发真网络请求）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "Phantom#2026"
USER = "phantom_bound"
LIB_NAME = "空壳源测试库"
SRC_NAME = "空壳测试CMS源"
ITEM_NAMES = ["空壳托管集", "空壳无映射集", "空壳本地集"]


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
        em.UpstreamSource.name == SRC_NAME).delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.name == LIB_NAME).delete()
    u = db.query(models.WebUser).filter(
        models.WebUser.username == USER).first()
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
    user = models.WebUser(username=USER, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=USER,
                          emby_password=hash_password(PW))
    db.add(user)
    db.flush()
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=user.id, sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=user.id, provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    src = em.UpstreamSource(name=SRC_NAME, source_type="applecms",
                            base_url="https://cms.example.com",
                            auth_method="none", enabled=True)
    db.add(src)
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="tvshows",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    hosted = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="episode",
        name="空壳托管集", file_path=None,
        duration_ticks=45 * 60 * TICKS)
    hosted_no_map = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="episode",
        name="空壳无映射集", file_path=None,
        duration_ticks=45 * 60 * TICKS)
    local = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="episode",
        name="空壳本地集",
        file_path="mount://3/剧集/空壳本地剧/S01E01.mkv",
        container="mkv", size=1_000_000_000,
        duration_ticks=45 * 60 * TICKS)
    db.add_all([hosted, hosted_no_map, local])
    db.flush()
    map_hosted = em.UpstreamItemMapping(
        local_item_id=hosted.id, source_id=src.id,
        upstream_item_id="vod-1001", confidence=0.95, evidence="tmdb")
    map_local = em.UpstreamItemMapping(
        local_item_id=local.id, source_id=src.id,
        upstream_item_id="vod-1002", confidence=0.95, evidence="tmdb")
    db.add_all([map_hosted, map_local])
    db.commit()
    data = {"hosted": hosted.guid, "hosted_no_map": hosted_no_map.guid,
            "local": local.guid, "map_hosted": map_hosted.id,
            "map_local": map_local.id}
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
    return (f"MediaBrowser Client={q}PhantomTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}phantom-dev-1{q}, Version={q}1.0{q}")


@pytest.fixture(scope="module")
def token(client):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": USER, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _detail(client, token, guid):
    r = client.get(f"/emby/Items/{guid}",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text
    return r.json()


def test_hosted_episode_detail_has_only_upstream_line(client, token, seed):
    body = _detail(client, token, seed["hosted"])
    sources = body.get("MediaSources") or []
    assert len(sources) == 1, sources
    assert sources[0]["Id"] == (
        f"{seed['hosted']}:upstream:{seed['map_hosted']}")
    # 不许出现 guid 裸 Id 的本地空壳
    assert all(s["Id"] != seed["hosted"] for s in sources)
    assert body["MediaSourceCount"] == 1
    assert "Chapters" in body


def test_hosted_episode_without_mapping_has_empty_sources(
        client, token, seed):
    body = _detail(client, token, seed["hosted_no_map"])
    assert body["MediaSources"] == []
    assert body["MediaSourceCount"] == 0
    assert body["Chapters"] == []


def test_local_episode_detail_keeps_main_source_plus_line(
        client, token, seed):
    body = _detail(client, token, seed["local"])
    sources = body.get("MediaSources") or []
    assert len(sources) == 2, sources
    assert sources[0]["Id"] == seed["local"]
    assert sources[1]["Id"] == (
        f"{seed['local']}:upstream:{seed['map_local']}")
    assert body["MediaSourceCount"] == 2
