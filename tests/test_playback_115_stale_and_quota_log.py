"""播放修复：quota 结构化日志（2026-10-29 晚两起故障其一）。

故障 A：变体 ready 后 stream 409——最可能分支是 #1479 的用户副本
额度探测（按 user+fid 缓存 300 秒）；quota 409 此前只有
_note_play_start_safe 打点，应用日志里没有带 item/user/detail 的
结构化行。本文件钉住：quota 409 分支旁的 log_byos 行带 item、user、
fid 与缓存命中标记；另留记 strike（mark_variant_stale_once）与
quota 探测缓存 peek 两项通用回归。

故障 B（115 serve_115_copy 死链/探测/重建）测试已随 2026-11-02
夸克/115 绑盘线删除（二期）一并删除——serve_115_copy 分支、
check_115_download_alive、rebuild_dead_user115_copy 均已删，
前提场景已不存在。

全部离线：门禁用桩或 sqlite 内存库；不碰真实谷歌与真实 DB。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


_counter = [0]


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"pb-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="115:/电影/a.mkv", size=1000)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id, mount_type, path):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type=mount_type, path=path)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id,
                                status=status)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, scope="user", ref="7", status="ready",
             drive_id="pan115:user-7", stale=0):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id="copyfid",
                             variant_drive_id=drive_id,
                             owner_scope=scope, owner_ref=ref,
                             status=status, size_bytes=1000,
                             stale_count=stale, pickcode="PC123")
    db.add(row)
    db.commit()
    return row


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")






# ---------- 门禁：stale 115 行不再直接放行 ----------

















# ---------- 记 strike：与巡检同款语义 ----------

def test_mark_variant_stale_once(db):
    row = _variant(db, 1, stale=0)
    assert tv.mark_variant_stale_once(db, row.id) == 1
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.status == "ready"
    assert tv.mark_variant_stale_once(db, row.id) == 2
    row = db.query(em.TransferVariant).filter(
        em.TransferVariant.id == row.id).first()
    assert row.status == "stale"
    assert tv.mark_variant_stale_once(db, 999999) is None
    assert tv.mark_variant_stale_once(db, None) is None


# ---------- quota 探测缓存 peek ----------

def test_usercopy_quota_probe_cached_peek(monkeypatch):
    # 缓存里没有 → 未命中；写进去 → 命中；顺序语义由调用方保证。
    from backend.database import CacheManager

    monkeypatch.setattr(CacheManager, "get", lambda key, domain=None: None)
    assert tv.usercopy_quota_probe_cached(7, "FID1") is False
    monkeypatch.setattr(CacheManager, "get",
                        lambda key, domain=None: "quota")
    assert tv.usercopy_quota_probe_cached(7, "FID1") is True
    assert tv.usercopy_quota_probe_cached(7, "") is False



def _run(coro):
    return asyncio.run(coro)


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def one_or_none(self):
        return None

    def all(self):
        # 无授权行：路径授权门禁 layers 为空即放行（与真实空表一致）。
        return []


class _FakeDB:
    def query(self, *args, **kwargs):
        return _FakeQuery()














def test_video_stream_quota_409_logs_structured(monkeypatch, caplog):
    """quota 409 分支旁的 log_byos 行带 item/user/fid/缓存命中标记。"""
    from backend.emby_server import api
    from backend.emby_server import playback_strategy as _strategy

    variant = SimpleNamespace(owner_scope="user",
                              variant_drive_id="userdrive:7",
                              variant_file_id="copyfid")
    item = SimpleNamespace(id=321, drive_file_id="origfid",
                           container="mkv", file_path="mount://3/电影/a.mkv")
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db: False)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)
    monkeypatch.setattr(api, "_note_play_start_safe",
                        lambda item, **kwargs: None)

    def fake_get(db, key, default="", **kwargs):
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    monkeypatch.setattr(tv, "resolve_play_file_id",
                        lambda db, item, user: ("origfid", variant))
    monkeypatch.setattr(tv, "ensure_user_copy_gate",
                        lambda db, item, user, **kwargs: {
                            "verdict": "proceed",
                            "reason": "has_ready_variant",
                            "variant": variant})
    monkeypatch.setattr(tv, "probe_user_copy_quota", lambda u, f: "quota")
    monkeypatch.setattr(tv, "usercopy_quota_probe_cached", lambda u, f: True)

    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream",
             "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    with caplog.at_level(logging.INFO):
        with pytest.raises(HTTPException) as excinfo:
            _run(api.video_stream("item", Request(scope),
                                  SimpleNamespace(id=7), _FakeDB()))
    assert excinfo.value.status_code == 409
    assert "byos_usercopy_quota" in caplog.text
    assert "item=321" in caplog.text
    assert "fid=copyfid" in caplog.text
    assert "cached=True" in caplog.text









