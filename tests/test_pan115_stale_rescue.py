# -*- coding: utf-8 -*-
"""副本巡检的落点口径（2026-11-02 二期删除后）。

原 #1575 的「115 stale 变体救回」随用户 115 绑盘线整体删除：
巡检不再重验/复活 pan115: 落点的存量行——`patrol_once` 对已下线
provider 的副本行（variant_drive_id 以 pan115:/quark: 开头）整行
跳过，不验活、不记 strike、不复活，行数据原样留存。本文件钉：

1. ready 的 pan115: 存量行被整行跳过（Drive 客户端绝不被拿去
   验它——那正是当年夸克行被 Drive get_file 误杀的坑）；
2. stale 的 Drive 行不在巡检复活范围（巡检只验 ready 行），
   状态与 strike 原样不动。
全部离线。
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
    row = em.MediaItem(guid=f"rescue-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="srcfid",
                       file_path="mount://9/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


class _ExplodingDrive:
    def get_file(self, *args, **kwargs):
        raise AssertionError("已下线落点行不应被巡检拿 Drive 验活")


def test_removed_provider_row_skipped_by_patrol(db):
    item = _item(db)
    row = em.TransferVariant(item_id=item.id, source_file_id="srcfid",
                             variant_file_id="newfid",
                             variant_drive_id="pan115:user-7",
                             owner_scope="user", owner_ref="7",
                             status="ready", stale_count=1,
                             pickcode="PC-NEW")
    db.add(row)
    db.commit()
    stats = tv.patrol_once(db, lambda: _ExplodingDrive())
    db.refresh(row)
    # 整行跳过：状态/strike 不动，本轮 checked/revived/stale 都不计它。
    assert row.status == "ready" and row.stale_count == 1
    assert stats["checked"] == 0 and stats["revived"] == 0
    assert stats["stale"] == 0


def test_stale_drive_row_not_revived(db):
    item = _item(db)
    row = em.TransferVariant(item_id=item.id, source_file_id="srcfid",
                             variant_file_id="drivefid",
                             variant_drive_id="userdrive:7",
                             owner_scope="user", owner_ref="7",
                             status="stale", stale_count=2,
                             pickcode="")
    db.add(row)
    db.commit()
    stats = tv.patrol_once(db, lambda: _ExplodingDrive())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["revived"] == 0
