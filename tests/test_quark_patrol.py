# -*- coding: utf-8 -*-
"""夸克变体巡检分流（2026-11-02 深挖修复）。

旧逻辑：patrol_once 只给 pan115: 行分流，quark: 行落进 Drive 分支
被拿去调 Google Drive files.get——夸克 fid 在谷歌侧必然 404，
每轮巡检记一次 strike、两轮置 stale；且 stale 的夸克行从不纳入
重验。门禁又规定 stale_count>=1 的 ready 行不直接放行，于是新
转存好的夸克副本活不过一轮巡检，用户永远撞「准备中」/重建
（生产实证：17 个被巡检过的夸克变体全部 stale_count=2/stale）。

本单修法：quark: 行走 _patrol_quark_row（用用户自己的夸克绑定
Cookie 调 download_url 验活，口径与 _patrol_115_row 对齐），
stale 的夸克行与 115 行一样纳入巡检重验、可复活。

全部离线：夸克 API 与 Drive API 一律用桩替身。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer_quark
from backend.emby_server import quarkdrive
from backend.emby_server import user_bindings as ub
from backend.integrations import store
from backend.emby_server import stream_limits as sl


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


_counter = [0]


def _item(db):
    _counter[0] += 1
    row = em.MediaItem(guid=f"quark-patrol-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="srcfid",
                       file_path="mount://9/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="quark",
                                account_label="testquark",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _quark_row(db, item_id, status="ready", stale_count=0, fid="qfid-1"):
    row = em.TransferVariant(item_id=item_id, source_file_id="srcfid",
                             variant_file_id=fid,
                             variant_drive_id="quark:user-7",
                             owner_scope="user", owner_ref="7",
                             status=status, stale_count=stale_count)
    db.add(row)
    db.commit()
    return row


class _FakeQuark:
    """download_url 行为桩（ok / gone / auth / neterr）。"""

    def __init__(self, behavior="ok"):
        self.behavior = behavior
        self.cookie = "FAKECOOKIE"
        self.download_calls = []

    def download_url(self, fid):
        self.download_calls.append(fid)
        if self.behavior == "gone":
            # 上游明确应答的信封错误（文件不存在）：带 upstream_code。
            raise quarkdrive.QuarkError(
                "夸克返回 code=40401：文件不存在", step="download",
                upstream_code=40401, upstream_message="文件不存在")
        if self.behavior == "auth":
            raise quarkdrive.QuarkAuthError(
                "夸克返回 HTTP 401（登录态失效）", step="download")
        if self.behavior == "neterr":
            # 网络类失败：无上游应答，upstream_code 缺失。
            raise quarkdrive.QuarkError(
                "夸克请求失败：ConnectError", step="download")
        return "https://quark.test/dl/file.mkv"


class _Drive404:
    """Google Drive files.get 对夸克 fid 的必然结果：404。

    旧代码下夸克行会被它记 strike；新代码下根本不许调到它。
    """

    def __init__(self):
        self.calls = []

    def get_file(self, drive_id, fid):
        self.calls.append((drive_id, fid))
        raise tv._NotFound("副本不存在")


def _patch(monkeypatch, fake):
    monkeypatch.setattr(transfer_quark, "_make_quark_client",
                        lambda cookie: fake)
    monkeypatch.setattr(ub, "binding_quark_cookie",
                        lambda db_arg, user_id, binding_id: "FAKECOOKIE")
    monkeypatch.setattr(ub, "absorb_quark_cookie_rotation",
                        lambda *a, **kw: False)






def test_ready_quark_row_auth_error_no_strike(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _quark_row(db, item.id)
    _patch(monkeypatch, _FakeQuark("auth"))
    tv.patrol_once(db, lambda: _Drive404())
    tv.patrol_once(db, lambda: _Drive404())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0






def test_stale_quark_row_stays_stale_when_gone(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _quark_row(db, item.id, status="stale", stale_count=2)
    _patch(monkeypatch, _FakeQuark("gone"))
    stats = tv.patrol_once(db, lambda: _Drive404())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["stale"] == 0  # 不重复计入本轮新 stale


def test_quark_row_without_active_binding_skipped(db, monkeypatch):
    item = _item(db)
    _binding(db, 7, status="expired")
    row = _quark_row(db, item.id)
    fake = _FakeQuark("ok")
    _patch(monkeypatch, fake)
    drive = _Drive404()
    tv.patrol_once(db, lambda: drive)
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0
    assert fake.download_calls == [] and drive.calls == []
