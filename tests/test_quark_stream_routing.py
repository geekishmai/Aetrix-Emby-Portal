# -*- coding: utf-8 -*-
"""取流 provider 分流回归（2026-11-02 夸克副本串线实锤）。

现场：user 829 播条目 696679《Running Man 第690集》，跨 provider
转存单 done、变体 ready；客户端走通用签名地址 stream.mkv，
play_sign 已下发链接消费让门禁直接 proceed（不看变体），选路
已采用夸克变体行，但 video_stream 后段只认 Drive：夸克 fid 落进
Drive 快路 302 分支被喂给 Google files.get，404 原样出门。

本文件现只留 userdrive ready 变体哨兵（正常 Drive 副本行为不变：
仍走 Drive 直链 302，防误伤）。夸克/115 ready 变体分流测试已随
2026-11-02 夸克/115 绑盘线删除（二期）一并删除——serve_115_copy /
serve_quark_copy 分支与 resolve_user115/quark_copy_url 均已删，
前提场景已不存在。

全部离线：内存 SQLite + 模块边界桩；monkeypatch 一律 patch
类/模块属性（仓库纪律）。门禁本体走真实实现（play_sign 消费
短路是本 bug 的触发点，不许桩掉）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from backend import models
from backend.emby_server import api, play_events, play_sign
from backend.emby_server import models as em
from backend.emby_server import playback_strategy
from backend.emby_server import transfer_variant as tv
from backend.emby_server import user_bindings as ub
from backend.emby_server import streaming as streaming_mod
from backend.emby_server import drive_auth

ORIG_FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"


@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture(autouse=True)
def _enable_transfer(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


@pytest.fixture()
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _request(query=b""):
    scope = {"type": "http", "method": "GET",
             "path": "/emby/Videos/x/stream.mkv",
             "raw_path": b"/emby/Videos/x/stream.mkv",
             "query_string": query,
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _item(item_id=696679):
    item = em.MediaItem()
    item.id = item_id
    item.guid = f"guid-{item_id}"
    item.name = "Running Man 第690集"
    item.file_path = "mount://3/综艺/RunningMan690.mkv"
    item.drive_file_id = ORIG_FID
    item.item_type = "episode"
    item.container = "mkv"
    item.size = 1_000_000_000
    return item


def _variant(item_id, user_id, drive_id, fid, **kw):
    row = em.TransferVariant()
    row.item_id = item_id
    row.owner_scope = "user"
    row.owner_ref = str(user_id)
    row.status = "ready"
    row.variant_drive_id = drive_id
    row.variant_file_id = fid
    row.stale_count = kw.pop("stale_count", 0)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


class _Probes:
    """Drive 侧全部出口的计数桩：任一被调用即说明串线。"""

    def __init__(self):
        self.ensure_calls = []       # _ensure_drive_file_playable 的 fid
        self.drive_relay_calls = []  # serve_drive_relay_async 入参
        self.usercontent_calls = []  # drive_usercontent_download_url 的 fid


def _stub_common(monkeypatch, item, probes, *, ensure_ok_fids=()):
    """桩通 video_stream 前置链路 + Drive 出口计数。

    门禁本体不桩（真实短路）；选路两口由各用例按场景装配。
    """
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    # 本 bug 的触发点：已下发签名链接消费（真实门禁据此 proceed）。
    monkeypatch.setattr(
        "backend.emby_server.playback_gate._request_uses_issued_play_sign",
        lambda request, db_: True)
    monkeypatch.setattr(play_sign, "check_referer",
                        lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")

    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "true"
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)

    async def fake_ensure(db_, it, fid, **kwargs):
        probes.ensure_calls.append(fid)
        if fid in ensure_ok_fids:
            return True
        # 生产形态：别家 fid 喂 Google files.get 直接 404 原样抛。
        raise HTTPException(status_code=404, detail="Not Found")

    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)

    async def fake_drive_relay(request, drive_id, file_id, **kwargs):
        probes.drive_relay_calls.append((drive_id, file_id))
        return Response(content=b"drive-relay-bytes", status_code=200)

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_drive_relay)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)

    def fake_usercontent(fid):
        probes.usercontent_calls.append(fid)
        return f"https://usercontent.test/{fid}"

    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url",
                        fake_usercontent)


def _call(db, user, item):
    return asyncio.run(api.video_stream(
        item.guid, _request(), user=user, db=db))


# ---------------------------------------------------------------------------
# 哨兵：userdrive ready 变体仍走 Drive 直链（行为不变）
# ---------------------------------------------------------------------------

def test_userdrive_ready_variant_still_goes_drive_direct(
        db, monkeypatch):
    item = _item()
    user = SimpleNamespace(id=7, is_staff=False)
    copy_fid = "drivecopyfid-7"
    row = _variant(item.id, 7, "userdrive:7", copy_fid)
    probes = _Probes()
    _stub_common(monkeypatch, item, probes, ensure_ok_fids=(copy_fid,))
    monkeypatch.setattr(
        "backend.emby_server.delivery_plan.plan_ready_leg",
        lambda db_, it, u_: (copy_fid, row))
    monkeypatch.setattr(
        tv, "resolve_play_file_id",
        lambda db_, it, u_: (ORIG_FID, None))
    # 本人副本额度探测三口桩成“无异常”（本用例不测额度链）。
    monkeypatch.setattr(tv, "usercopy_quota_probe_cached",
                        lambda user_id, fid: False)
    monkeypatch.setattr(tv, "probe_user_copy_quota_decision",
                        lambda user_id, fid: None)
    monkeypatch.setattr(tv, "usercopy_quota_probe_detail",
                        lambda user_id, fid: {})

    resp = _call(db, user, item)
    assert resp.status_code == 302
    assert resp.headers["location"] == \
        f"https://usercontent.test/{copy_fid}"
    assert probes.ensure_calls == [copy_fid]


