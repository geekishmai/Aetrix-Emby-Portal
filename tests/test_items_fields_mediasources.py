# -*- coding: utf-8 -*-
"""列表 DTO 按 Fields 补齐 MediaSources 回归（2026-11-03 Reflix 实证）。

背景：Reflix 打开《丛林猎凶》详情页只发一条列表查询
``Items?AnyProviderIdEquals=Tmdb.1518076&Fields=ProviderIds,
MediaSources,MediaStreams&IncludeItemTypes=Movie&Limit=1``，我方列表
DTO 无视 Fields、MediaSources 恒为 []，Reflix 据此判「没有可用的
源」——PlaybackInfo 明明有源它也不再发 stream 请求。真实 Emby 在
Fields 明确要求 MediaSources 时会把来源带进列表 DTO。

钉死口径：
- 只有 Fields 明确含 MediaSources/MediaStreams 时才补齐，补齐内容
  与详情面（_item_dto full=True）逐字同源（托管行只放上游线路）；
- 未请求时列表行为逐字不变（MediaSources 恒 []，不给列表加成本）；
- 只处理 movie/episode，容器类（series/season）不碰。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.security import hash_password

init_db()

PW = "FieldsMs#2026"
BOUND = "fieldsms_bound"
LIB_CMS = "Fields补齐测试库"
SRC_NAME = "Fields补齐源"
CMS_MOVIE = "Fields补齐托管片"
CMS_SERIES = "Fields补齐托管剧"


def _guid():
    return uuid.uuid4().hex


def _cleanup(db):
    for lib in db.query(em.Library).filter(
            em.Library.name == LIB_CMS).all():
        ids = [r[0] for r in db.query(em.MediaItem.id).filter(
            em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.UpstreamItemMapping).filter(
                em.UpstreamItemMapping.local_item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(
            em.Library.id == lib.id).delete(synchronize_session=False)
    for src in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name == SRC_NAME).all():
        db.delete(src)
    u = db.query(models.WebUser).filter(
        models.WebUser.username == BOUND).first()
    if u is not None:
        cred_ids = [r[0] for r in db.query(
            em.UserStorageBinding.credential_id).filter(
            em.UserStorageBinding.user_id == u.id).all()]
        db.query(em.UserStorageBinding).filter(
            em.UserStorageBinding.user_id == u.id).delete(
            synchronize_session=False)
        if cred_ids:
            db.query(em.MountCredential).filter(
                em.MountCredential.id.in_(cred_ids)).delete(
                synchronize_session=False)
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == u.id).delete(
            synchronize_session=False)
        db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    user = models.WebUser(username=BOUND, password_hash=hash_password(PW),
                          is_active=True, is_staff=True,
                          emby_username=BOUND, emby_password=PW)
    db.add(user)
    db.flush()
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=user.id, sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=user.id, provider="drive",
                                 account_label="Fields补齐种子盘",
                                 credential_id=cred.id, status="active"))
    lib = em.Library(guid=_guid(), name=LIB_CMS,
                     collection_type="movies", is_enabled=True,
                     kind="applecms")
    db.add(lib)
    db.flush()
    src = em.UpstreamSource(name=SRC_NAME, source_type="applecms",
                            base_url="http://cms-fields.example",
                            enabled=True)
    db.add(src)
    db.flush()
    movie = em.MediaItem(guid=_guid(), library_id=lib.id,
                         item_type="movie", name=CMS_MOVIE,
                         production_year=2024, tmdb_id="1518076",
                         upstream_source_id=src.id, probe_status="done")
    db.add(movie)
    db.flush()
    map_a = em.UpstreamItemMapping(local_item_id=movie.id,
                                   source_id=src.id,
                                   upstream_item_id="160002",
                                   confidence=0.9, evidence="title+year")
    map_b = em.UpstreamItemMapping(local_item_id=movie.id,
                                   source_id=src.id,
                                   upstream_item_id="75072",
                                   confidence=0.8, evidence="title+year")
    db.add_all([map_a, map_b])
    series = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type="series", name=CMS_SERIES,
                          tmdb_id="999001", probe_status="done")
    db.add(series)
    db.commit()
    data = {"uid": user.id, "movie": movie.guid, "series": series.guid,
            "map_a": map_a.id, "map_b": map_b.id}
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
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    q = chr(34)
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": BOUND, "Pw": PW},
        headers={"X-Emby-Authorization":
                 f"MediaBrowser Client={q}FieldsMs{q}, Device={q}iOS{q}, "
                 f"DeviceId={q}fieldsms-dev-1{q}, Version={q}1.0{q}"})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)


def _items(client, token, seed, guid, fields=None):
    params = {"Ids": guid}
    if fields is not None:
        params["Fields"] = fields
    r = client.get(f"/emby/Users/{seed['uid']}/Items", params=params,
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text
    items = r.json()["Items"]
    assert len(items) == 1, items
    return items[0]


def test_list_fields_mediasources_filled_for_hosted_movie(
        client, token, seed):
    dto = _items(client, token, seed, seed["movie"],
                 fields="ProviderIds,MediaSources,MediaStreams")
    sources = dto["MediaSources"]
    assert len(sources) == 2
    ids = {s["Id"] for s in sources}
    assert ids == {f"{seed['movie']}:upstream:{seed['map_a']}",
                   f"{seed['movie']}:upstream:{seed['map_b']}"}
    for s in sources:
        assert s["DirectStreamUrl"]
        video = [st for st in s["MediaStreams"] if st["Type"] == "Video"]
        assert video, s


def test_list_without_fields_keeps_empty_sources(client, token, seed):
    dto = _items(client, token, seed, seed["movie"])
    assert dto["MediaSources"] == []


def test_list_other_fields_do_not_trigger_fill(client, token, seed):
    dto = _items(client, token, seed, seed["movie"],
                 fields="ProviderIds,Overview")
    assert dto["MediaSources"] == []


def test_list_fields_fill_skips_series(client, token, seed):
    dto = _items(client, token, seed, seed["series"],
                 fields="MediaSources")
    assert dto["MediaSources"] == []
