# -*- coding: utf-8 -*-
"""CMS 源站自带豆瓣编号收割（零搜索身份路）回归。

口径钉子：
- ``vod_douban_id`` 只认纯数字非 0 值，其余形状（空/0/带字母/缺失）
  一律不收，宁可留空不许错绑；
- 投票制：同一条目全部映射源给号完全一致才落账，源间矛盾整条跳过；
- 落账只填空缺（复用识别段 ``_merge_external_id``），已有身份
  （含 external_ids 在用键）的条目根本不进候选；
- 落账写段记录（provider=douban、证据来源 cms_vod_douban_id）；
  done 终态行落账后打回 pending 交补资料队列，pending 行不改状态；
- dry_run 只取数投票、结束回滚，一行不落。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_identity_harvest as hv
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _source(db, sid, *, enabled=True):
    src = em.UpstreamSource(
        id=sid, name=f"源{sid}", source_type="applecms",
        base_url=f"http://src{sid}.test", enabled=enabled,
    )
    db.add(src)
    return src


def _item(db, iid, *, status="pending", tmdb_id=None, external_ids=None):
    it = em.MediaItem(
        id=iid, guid=f"g{iid:031d}"[-32:], library_id=58,
        item_type="movie", name=f"片{iid}", tmdb_id=tmdb_id,
        external_ids=external_ids, enrich_status=status,
    )
    db.add(it)
    return it


def _map(db, iid, sid, vod):
    db.add(em.UpstreamItemMapping(
        local_item_id=iid, source_id=sid, upstream_item_id=str(vod)))


def _fake_http(payload_by_host):
    """按 host 返回预置 detail 载荷的 http_get替身（fetch_page 口径）。"""

    def _get(url, headers, timeout):
        host = urllib.parse.urlparse(url).hostname
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        ids = (qs.get("ids") or [""])[0].split(",")
        vods = payload_by_host.get(host, {})
        lst = [vods[i] for i in ids if i in vods]
        return 200, {"code": 1, "list": lst}

    return _get


def _vod(vid, douban=None):
    v = {"vod_id": vid, "vod_name": "x"}
    if douban is not None:
        v["vod_douban_id"] = douban
    return v


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_parse_douban_id_shapes():
    assert hv.parse_douban_id({"vod_douban_id": "12345"}) == "12345"
    assert hv.parse_douban_id({"vod_douban_id": 12345}) == "12345"
    assert hv.parse_douban_id({"vod_douban_id": " 678 "}) == "678"
    assert hv.parse_douban_id({"vod_douban_id": "0"}) is None
    assert hv.parse_douban_id({"vod_douban_id": 0}) is None
    assert hv.parse_douban_id({"vod_douban_id": ""}) is None
    assert hv.parse_douban_id({"vod_douban_id": None}) is None
    assert hv.parse_douban_id({"vod_douban_id": "12a45"}) is None
    assert hv.parse_douban_id({}) is None
    assert hv.parse_douban_id(None) is None
    assert hv.parse_douban_id("12345") is None


def test_vote_douban_id():
    assert hv.vote_douban_id([]) is None
    assert hv.vote_douban_id(["111"]) == "111"
    assert hv.vote_douban_id(["111", "111"]) == "111"
    assert hv.vote_douban_id(["111", "222"]) is None
    assert hv.vote_douban_id(["", "111"]) == "111"


# ---------------------------------------------------------------------------
# 收割主流程
# ---------------------------------------------------------------------------

def _seed(db):
    s7 = _source(db, 7)
    _source(db, 10)
    _item(db, 1)                       # 双源一致 → 落账 agreed_multi
    _map(db, 1, 7, "100")
    _map(db, 1, 10, "200")
    _item(db, 2)                       # 单源给号 → 落账 single
    _map(db, 2, 7, "101")
    _item(db, 3)                       # 双源矛盾 → 跳过
    _map(db, 3, 7, "102")
    _map(db, 3, 10, "201")
    _item(db, 4)                       # 源无号 → no_id
    _map(db, 4, 7, "103")
    _item(db, 5, status="done")        # done 终态 → 落账 + 打回 pending
    _map(db, 5, 7, "104")
    _item(db, 6, tmdb_id="999")        # 已绑定 → 不进候选
    _map(db, 6, 7, "105")
    db.commit()
    return s7


_PAYLOADS = {
    "src7.test": {
        "100": _vod(100, "111"), "101": _vod(101, "222"),
        "102": _vod(102, "333"), "103": _vod(103, "0"),
        "104": _vod(104, "555"), "105": _vod(105, "666"),
    },
    "src10.test": {
        "200": _vod(200, "111"), "201": _vod(201, "444"),
    },
}


def test_harvest_source_end_to_end(db):
    s7 = _seed(db)
    stats = hv.harvest_source(db, s7, http_get=_fake_http(_PAYLOADS),
                              sleep_sec=0)
    assert stats["items"] == 5          # 条目 6 已绑定，不在候选内
    assert stats["written"] == 3        # 1、2、5
    assert stats["agreed_multi"] == 1
    assert stats["single"] == 2         # 2（单源）、5（单源）
    assert stats["disagreed"] == 1      # 3
    assert stats["no_id"] == 1          # 4
    assert stats["reset_done"] == 1     # 5
    by_id = {it.id: it for it in db.query(em.MediaItem).all()}
    assert json.loads(by_id[1].external_ids) == {"douban": "111"}
    assert json.loads(by_id[2].external_ids) == {"douban": "222"}
    assert by_id[3].external_ids is None
    assert by_id[4].external_ids is None
    assert json.loads(by_id[5].external_ids) == {"douban": "555"}
    assert by_id[5].enrich_status == "pending"
    assert by_id[5].enrich_attempts == 0
    assert by_id[1].enrich_status == "pending"  # 本就是 pending，不乱动
    assert by_id[6].external_ids is None        # 已绑定行零触碰
    rec = (db.query(em.EnrichStageRecord)
           .filter(em.EnrichStageRecord.item_id == 1).one())
    assert rec.stage == "identified"
    assert rec.provider == "douban"
    assert rec.external_id == "111"
    evidence = json.loads(rec.evidence_json)
    assert evidence["douban_id_source"] == "cms_vod_douban_id"


def test_harvest_dry_run_writes_nothing(db):
    s7 = _seed(db)
    stats = hv.harvest_source(db, s7, http_get=_fake_http(_PAYLOADS),
                              sleep_sec=0, dry_run=True)
    assert stats["written"] == 3        # 统计口径 = 本可落账数
    db.expire_all()
    for it in db.query(em.MediaItem).all():
        assert it.external_ids is None
    assert db.query(em.EnrichStageRecord).count() == 0


def test_harvest_fetch_error_isolated(db):
    s7 = _seed(db)

    def _boom(url, headers, timeout):
        raise RuntimeError("network down")

    stats = hv.harvest_source(db, s7, http_get=_boom, sleep_sec=0)
    assert stats["written"] == 0
    assert stats["fetch_errors"] >= 1
    assert db.query(em.EnrichStageRecord).count() == 0


def test_harvest_all_skips_disabled_driver(db):
    _seed(db)
    _source(db, 9, enabled=False)
    _item(db, 7)
    _map(db, 7, 9, "900")
    db.commit()
    payloads = dict(_PAYLOADS)
    payloads["src9.test"] = {"900": _vod(900, "777")}
    total = hv.harvest_all(db, http_get=_fake_http(payloads),
                           sleep_sec=0, bump_cache=False)
    # 源 9 停用不做驱动源：条目 7 不被处理；但 7/10 驱动轮照常落账
    assert total["written"] == 3
    item7 = db.query(em.MediaItem).filter(em.MediaItem.id == 7).one()
    assert item7.external_ids is None


def test_harvest_sends_source_headers(db):
    """回归（2026-11-03 试点实证）：detail 请求必须带源级请求头
    （_source_headers：UA/Referer/附加头，与同步路径同一套）——
    不带头时按 UA 拦爬虫的源直接 403，整批证据丢失。"""
    s7 = _source(db, 7)
    s7.user_agent = "UA-TEST/1.0"
    _item(db, 1)
    _map(db, 1, 7, "100")
    db.commit()
    seen = {}

    def _get(url, headers, timeout):
        host = urllib.parse.urlparse(url).hostname
        seen[host] = dict(headers or {})
        return 200, {"code": 1, "list": [_vod(100, "111")]}

    stats = hv.harvest_source(db, s7, http_get=_get, sleep_sec=0)
    assert stats["written"] == 1
    assert seen["src7.test"].get("User-Agent") == "UA-TEST/1.0"
