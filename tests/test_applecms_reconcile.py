# -*- coding: utf-8 -*-
"""苹果 CMS 月度全量对账回归（新媒体库类型一期·对账单）。

口径（2026-11-02 定案）：
- 对账 = 一轮全量同步（full，不带 h 窗口）完整跑完后，对库内
  每个源调既有 ``reconcile_hosted_lifecycle``：本轮没在源目录里
  见到的托管行走既有宽限计数（2 轮不见隐藏、4 轮软删），本模块
  不另造生命周期语义。
- 完整性红线：任一配置源本轮出错（拉取失败/源缺失/停用），
  seen 集合不可信，整轮**不对账**——抖动不许误判成下架。
- seen 口径 = 全量轮在源 list 里实际见到的全部 vod_id（分类
  过滤之前记录）：条目在不在源目录里，与本库收不收是两件事。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import applecms_reconcile as acr
from backend.emby_server import models as em


def _entry(vod_id, name, year="2021"):
    import zlib
    return {"vod_id": vod_id, "vod_name": name, "vod_year": year,
            "type_id": 2, "type_name": "动作片", "vod_serial": "0",
            "vod_time": "2024-05-01 10:00:00",
            "vod_douban_id": str(
                100000 + zlib.crc32(f"{name}|{year}".encode()) % 900000)}


CLASS_TREE = [{"type_id": 1, "type_pid": 0, "type_name": "电影"},
              {"type_id": 2, "type_pid": 1, "type_name": "动作片"}]

CATALOG_V1 = {"code": 1, "page": 1, "pagecount": 1, "total": 2,
              "class": CLASS_TREE,
              "list": [_entry(101, "沙丘"), _entry(102, "流浪地球", "2019")]}
CATALOG_V2 = {"code": 1, "page": 1, "pagecount": 1, "total": 1,
              "class": CLASS_TREE,
              "list": [_entry(101, "沙丘")]}


class CatalogHttp:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        if self.payload == "boom":
            return 500, {}
        return 200, json.loads(json.dumps(self.payload))


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db):
    db.add(em.UpstreamSource(
        id=7, name="非凡", source_type="applecms",
        base_url="http://s7.example", enabled=True))
    lib = em.Library(
        guid="lib-cms-movies", name="CMS电影",
        collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids="7")
    db.add(lib)
    db.commit()
    return lib


def _row(db, name):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.name == name).one())


def test_reconcile_counts_missing_after_catalog_shrinks(db):
    lib = _seed(db)
    out1 = acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V1))
    assert out1["reconciled"] is True
    assert out1["sync"]["added"] == 2
    assert out1["lifecycle"][7]["seen"] == 2
    # 源下架《流浪地球》：下一轮全量对账记它 missing=1（宽限内不隐藏）
    out2 = acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V2))
    assert out2["reconciled"] is True
    life = out2["lifecycle"][7]
    assert life["seen"] == 1 and life["missing"] == 1
    assert life["hidden"] == 0 and life["deleted"] == 0
    assert _row(db, "流浪地球").is_hidden is False
    # 再不见一轮 → 达到隐藏宽限（2 轮）
    out3 = acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V2))
    assert out3["lifecycle"][7]["hidden"] == 1
    assert _row(db, "流浪地球").is_hidden is True
    # 重新上架 → 恢复可见、计数清零
    out4 = acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V1))
    assert out4["lifecycle"][7]["restored"] == 1
    assert _row(db, "流浪地球").is_hidden is False


def test_reconcile_skipped_when_any_source_errors(db):
    lib = _seed(db)
    acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V1))
    out = acr.reconcile_library(db, lib, http_get=CatalogHttp("boom"))
    assert out["reconciled"] is False
    assert out["reason"] == "partial_sync"
    assert out["lifecycle"] == {}
    # 抖动没被记成缺失：生命周期表里没有任何 missing 计数
    states = db.query(em.UpstreamHostedLifecycle).all()
    assert all(s.missing_rounds == 0 for s in states)
    assert _row(db, "流浪地球").is_hidden is False


def test_reconcile_blocked_propagates(db, monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "0")
    lib = _seed(db)
    out = acr.reconcile_library(db, lib, http_get=CatalogHttp(CATALOG_V1))
    assert out["reconciled"] is False
    assert out["reason"] == "mapping_off"


# ---------------------------------------------------------------------------
# PA-068：全量同步撞页数安全阀时不许静默截断（残缺 seen 不许当完整目录对账）
# PA-069：多库共源时同一轮对账不许对同一源重复计 missing_rounds
# ---------------------------------------------------------------------------

PAGED_CATALOG = {
    1: [_entry(101, "沙丘")],
    2: [_entry(102, "流浪地球", "2019")],
    3: [_entry(103, "星际穿越", "2014")],
}


class PagedHttp:
    """按 pg 分页返回的假目录：pagecount=总页数，每页条目不同。"""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url, headers=None, timeout=None):
        from urllib.parse import parse_qs, urlparse
        self.urls.append(url)
        query = parse_qs(urlparse(url).query)
        pg = int(query.get("pg", ["1"])[0])
        payload = {"code": 1, "page": pg,
                   "pagecount": len(self.pages),
                   "total": sum(len(v) for v in self.pages.values()),
                   "list": json.loads(json.dumps(
                       self.pages.get(pg, [])))}
        if pg == 1:
            payload["class"] = CLASS_TREE
        return 200, payload



def test_sync_marks_truncation_when_page_cap_hit(db):
    lib = _seed(db)
    stats = als.sync_library(db, lib, http_get=PagedHttp(PAGED_CATALOG),
                             full=True, max_pages=2)
    assert stats["pages"] == 2
    assert stats["seen_by_source"][7] == ["101", "102"]
    # 撞上限 = 目录未翻完：必须留痕，不许静默当完整同步结束。
    assert any(e.startswith("source_truncated:7")
               for e in stats["errors"]), stats["errors"]
    # 对照：页数够用时目录完整，不许误报截断。
    stats_full = als.sync_library(db, lib, http_get=PagedHttp(PAGED_CATALOG),
                                  full=True, max_pages=3)
    assert stats_full["pages"] == 3
    assert not any(e.startswith("source_truncated")
                   for e in stats_full["errors"]), stats_full["errors"]


def test_reconcile_blocked_when_full_sync_truncated(db, monkeypatch):
    lib = _seed(db)
    monkeypatch.setattr(acf, "DEFAULT_MAX_PAGES", 10)
    out1 = acr.reconcile_library(db, lib, http_get=PagedHttp(PAGED_CATALOG))
    assert out1["reconciled"] is True
    assert out1["sync"]["added"] == 3
    # 目录涨过安全阀（等价于真实大源 pagecount 远超默认 500 页）：
    # 本轮 seen 只有前 2 页，残缺目录不许进对账。
    monkeypatch.setattr(acf, "DEFAULT_MAX_PAGES", 2)
    out2 = acr.reconcile_library(db, lib, http_get=PagedHttp(PAGED_CATALOG))
    assert out2["reconciled"] is False
    assert out2["reason"] == "partial_sync"
    assert out2["lifecycle"] == {}
    assert any(e.startswith("source_truncated:7")
               for e in out2["sync"]["errors"])
    # 第 3 页的《星际穿越》没被误判缺失：计数仍为 0、未隐藏。
    states = {s.local_item_id: s
              for s in db.query(em.UpstreamHostedLifecycle).all()}
    row3 = _row(db, "星际穿越")
    assert states[row3.id].missing_rounds == 0
    assert row3.is_hidden is False



def _seed_two_libraries_one_source(db):
    db.add(em.UpstreamSource(
        id=7, name="非凡", source_type="applecms",
        base_url="http://s7.example", enabled=True))
    for guid, name in (("lib-cms-a", "CMS电影A"), ("lib-cms-b", "CMS电影B")):
        db.add(em.Library(
            guid=guid, name=name, collection_type="movies",
            is_enabled=True, kind="applecms", upstream_source_ids="7"))
    db.commit()


def test_reconcile_round_counts_shared_source_once(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    monkeypatch.setenv(acr.RECONCILE_ENABLED_ENV, "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    seed_db = factory()
    try:
        _seed_two_libraries_one_source(seed_db)
    finally:
        seed_db.close()

    def _missing_rounds(name):
        check = factory()
        try:
            row = (check.query(em.MediaItem)
                   .filter(em.MediaItem.name == name).one())
            state = (check.query(em.UpstreamHostedLifecycle)
                     .filter(em.UpstreamHostedLifecycle.local_item_id
                             == row.id).one())
            return state.missing_rounds, row.is_hidden
        finally:
            check.close()

    out1 = acr.run_reconcile_round(
        session_factory=factory, http_get=CatalogHttp(CATALOG_V1))
    assert [r["reconciled"] for r in out1] == [True, True]
    # 源下架《流浪地球》后跑一整轮（两个库共用源 7）：只许计 1 次
    # 缺失——旧行为逐库各计一次，第 1 轮即达隐藏阈值 2。
    out2 = acr.run_reconcile_round(
        session_factory=factory, http_get=CatalogHttp(CATALOG_V2))
    assert [r["reconciled"] for r in out2] == [True, True]
    assert _missing_rounds("流浪地球") == (1, False)
    # 再跑一轮才达隐藏阈值（单库口径：2 轮隐藏）。
    acr.run_reconcile_round(
        session_factory=factory, http_get=CatalogHttp(CATALOG_V2))
    assert _missing_rounds("流浪地球") == (2, True)
