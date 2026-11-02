# -*- coding: utf-8 -*-
"""115→Drive 跨盘与哈希包装残留回归。

本文件原为 115 秒传整套（2026-10-30）：秒传协议、开关与客户端
测试已随 2026-11-02 夸克/115 绑盘线删除（二期）一并删除——
rapid_upload/multipart_upload/simple_upload 与秒传开关均已删。
现只留：透传哈希包装（_HashingReader/_SegmentReader）与
115→Drive 跨盘源 sha1 沉淀两项通用回归。

全部离线：SA 下载一律桩替身。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
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
_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_BIG_SIZE = tv._115_SIMPLE_UPLOAD_LIMIT + 1


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"rapid-{_counter[0]}", library_id=1,
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


def _cross_job(db, item_id, user_id=7, provider="drive", dest="pan115:user-7",
               drive_id="shared1"):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id=drive_id, provider=provider,
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=dest, status="pending")
    db.add(row)
    db.commit()
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


class _FakeSA:
    """SA 桩：get_file 报大小，download_range 按段切字节并计数。"""

    def __init__(self, payload=_PAYLOAD, size=None):
        self.payload = payload
        self.size = size
        self.download_calls = 0
        self.ranges = []

    def get_file(self, drive_id, fid):
        return {"id": fid,
                "size": self.size if self.size is not None
                else len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        self.ranges.append((start, end))
        return self.payload[start:end + 1]








# ---------- ① 全量上传后 sha1 沉淀 ----------







# ---------- ② 秒传命中不消费字节读取器 ----------





# ---------- ③ 未命中回落 simple_upload ----------



# ---------- ⑤ 协议异常回落不失败 ----------





# ---------- ④ >5 GiB：命中可 done、未命中走分片 ----------





# ---------- 开关：关时不试秒传 ----------






# ---------- 透传哈希包装 ----------

def test_hashing_reader_full_and_reread():
    data = bytes(range(256)) * 3
    reader = tv._HashingReader(
        tv._SegmentReader(len(data), lambda s, e: data[s:e + 1]), len(data))
    assert reader.sha1_hexdigest() is None  # 没读全不许出值
    first = reader.read(100)
    assert reader.sha1_hexdigest() is None
    rest = reader.read()
    assert first + rest == data
    assert reader.sha1_hexdigest() == hashlib.sha1(data).hexdigest()
    reader.seek(0)  # 回读已计入区间：不许重复喂哈希
    assert reader.read(50) == data[:50]
    assert reader.sha1_hexdigest() == hashlib.sha1(data).hexdigest()


# ---------- 115→Drive 与同 provider：源 sha1 顺手沉淀 ----------

def test_cross_115_to_drive_persists_source_sha1(db, monkeypatch):
    _enable(monkeypatch)
    item = _item(db, file_path="115:/电影/a.mkv", drive_file_id="srcfid")
    _binding(db, 7, provider="drive")
    job = _cross_job(db, item.id, provider="pan115", dest="userdrive:7",
                     drive_id=None)

    class _FakeServer115:
        cookie = "server-cookie"
        ua = "ua-test"

        def list_dir(self, cid="0", limit=200):
            if str(cid) == "0":
                return [{"name": "电影", "is_dir": True, "cid": "11"}]
            return [{"name": "a.mkv", "is_dir": False, "pickcode": "srccode",
                     "size": len(_PAYLOAD), "fid": "srcfid",
                     "sha1": _PAYLOAD_SHA1}]

        def download_url(self, pickcode):
            return "https://dl.example/file"

    monkeypatch.setattr(
        "backend.emby_server.transfer115.resolve_cookie",
        lambda db_, library=None: ("server-cookie", "test"))
    monkeypatch.setattr(tv, "_make_pan115_client",
                        lambda cookie: _FakeServer115())

    def _fake_request(method, url, headers=None, timeout=None, **kw):
        start, end = (int(x) for x in
                      headers["Range"][len("bytes="):].split("-"))
        return SimpleNamespace(status_code=206,
                               content=_PAYLOAD[start:end + 1])

    monkeypatch.setattr(tv.httpx, "request", _fake_request)
    monkeypatch.setattr(tv.UserDriveApiClient, "ensure_folder",
                        lambda self, d, n: "ufolder")
    monkeypatch.setattr(
        tv.UserDriveApiClient, "resumable_upload",
        lambda self, d, name, folder, size, reader: (
            reader.read(), "newdrivefid")[1])
    monkeypatch.setattr(tv.UserDriveApiClient, "add_anyone_reader",
                        lambda self, d, f: None)
    monkeypatch.setattr(tv.UserDriveApiClient, "list_file_ids_by_name",
                        lambda self, d, f, n: ["newdrivefid"])
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1





