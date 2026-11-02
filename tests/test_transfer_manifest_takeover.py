# -*- coding: utf-8 -*-
"""换代线 step 4：115 执行面接管＋调度合并（PR-B）。

- 接管开（默认）时，Drive→115 的续传会话恢复以 transfer_deliveries
  落盘行为准（校验过才用），sidecar 退为第二落点；两者不一致以
  落盘行为准并记日志。接管/记账关 → 恢复顺序逐字回旧（sidecar 先）。
- 清单 complete 且整文件 sha1 在册时，执行器直接采信该 sha1
  （秒传首查即带哈希），省掉大文件整遍哈希重读。
- 调度合并：同 content_key 已有在跑的领跑单时，后到单不烧
  attempts、不碰源，defer 回 pending 等领跑单把暂存/清单备好；
  领跑租约过期、单子太老、接管关时都不合并、照常执行。

全部离线，桩口径同 test_transfer_cross_merge_resume.py。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_manifest as tman
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
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
_PAYLOAD = bytes((i * 7) % 256 for i in range(2000))
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"tk-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/a.mkv", size=len(_PAYLOAD))
    for key, value in kw.items():
        setattr(row, key, value)
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
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _cross_job(db, item_id, user_id=7, dest=None, drive_id="shared1"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id=drive_id, provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest or f"pan115:user-{user_id}",
                         status="pending")
    db.add(row)
    db.commit()
    return row





# ---------- 清单可信 sha1 ----------

def _seed_manifest(db, state="complete", sha1=_PAYLOAD_SHA1, size=len(_PAYLOAD)):
    """用 #1648 的记账正道（recorder＋tracker）把清单喂到目标状态。"""
    key = tv._stage_key("drive", "shared1", "origfid", size)
    recorder = tman.begin_recorder(db, key, size)
    assert recorder is not None
    recorder.tracker.feed(0, _PAYLOAD[:size])
    recorder.flush(db, whole_sha1=sha1 if state == "complete" else None)
    row = db.query(em.TransferContentManifest).filter_by(
        content_key=key).one()
    assert row.manifest_state == state
    return key


def test_manifest_trusted_sha1_helper(db):
    key = _seed_manifest(db)
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD)) == _PAYLOAD_SHA1
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD) + 1) is None
    assert tman.manifest_trusted_sha1(db, "nonexistent", len(_PAYLOAD)) is None


def test_manifest_trusted_sha1_partial_manifest_rejected(db):
    key = _seed_manifest(db, state="partial")
    assert tman.manifest_trusted_sha1(db, key, len(_PAYLOAD)) is None













