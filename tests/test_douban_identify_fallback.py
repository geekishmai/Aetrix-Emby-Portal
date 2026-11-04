"""豆瓣参与识别兜底（2026-11-02 站主亲令）。

背景定因：识别段此前只认 TMDB/NFO/别名证据（identify_two_stage 模块头
明文：Frodo 只作补资料源、不作认亲依据）；豆瓣 rich 兜底命中也只落资料
字段、豆瓣号从不写 external_ids；多源采集结果识别段根本不读——三条叠
加，TMDB 没命中的条目永远拿不到豆瓣身份（生产实查：CMS 六库无身份
12,009 条里 external_ids 带 douban 的只有 1 条）。

钉死：
① extract_binding：豆瓣 rich 命中（标题一致 + 年份门）→ 豆瓣绑定；
   年份不符 / 标题不一致 / 无 id → 不认（宁可漏也不错配）。
② extract_binding：多源 external_ids 的 tmdb/imdb 先桥回身份列口径，
   douban 次之；只有 bangumi 等其他源时识别段不越位（落库走现行
   apply_to_item 路径，与本批无关）。
③ apply_identity：豆瓣绑定把 douban 写进 external_ids（只填空缺、
   已有不同豆瓣号不覆盖），不写 tmdb/imdb 列；段记录 provider=douban。
   条目已带 external_ids.douban 时，无新证据也认（防重排空转）。
④ apply_fill：只有豆瓣身份的条目不再被「未绑定」拒绝（判据与
   identity_gate.is_bound_row 同源）；真无身份仍拒绝。
⑤ 分发（PA-081 修正）：识别队列模式（identify=True/fill=False）下
   豆瓣定身份的条目回 pending——队列谓词已与身份判据同源，豆瓣
   身份行属刮削队列，下一轮由刮削队列认领并真正跑 apply_fill；
   旧“直接终态 done”会让简介/海报/类型永远补不上，已废止。
⑥ _enrich_apply 的豆瓣 rich 落库块：命中同时把豆瓣号写进
   external_ids（此前只落资料字段、号蒸发）。
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em


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
    yield session
    session.close()


def _make_item(db, name="江河日上", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="在线国产剧",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name=name, sort_name=name, file_path="",
        production_year=2024, **kw)
    db.add(item)
    db.flush()
    return item


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


def _rich_hit(**kw):
    hit = {"id": "36742042", "title": "江河日上", "year": 2024,
           "image": "https://img.example/p.jpg", "type": "tv"}
    hit.update(kw)
    return hit


# ---------- ① rich 命中 → 豆瓣绑定（严门） ----------

def test_extract_binding_from_douban_rich_hit(db):
    item = _make_item(db)
    binding = ts.extract_binding(item, {"douban_fallback_hit": _rich_hit()})
    assert binding is not None
    assert binding["provider"] == "douban"
    assert binding["external_id"] == "36742042"
    assert binding["douban_id"] == "36742042"
    assert binding["tmdb_id"] is None and binding["imdb_id"] is None
    assert binding["evidence"]["douban_id_source"] == "douban_rich"


def test_extract_binding_douban_year_mismatch_rejected(db):
    item = _make_item(db)
    binding = ts.extract_binding(
        item, {"douban_fallback_hit": _rich_hit(year=1999)})
    assert binding is None, "年份对不上不许认豆瓣身份"


def test_extract_binding_douban_title_mismatch_rejected(db):
    item = _make_item(db)
    binding = ts.extract_binding(
        item, {"douban_fallback_hit": _rich_hit(title="完全不同的剧")})
    assert binding is None, "标题不一致不许认豆瓣身份"


def test_extract_binding_douban_hit_without_id_rejected(db):
    item = _make_item(db)
    binding = ts.extract_binding(
        item, {"douban_fallback_hit": _rich_hit(id="")})
    assert binding is None


# ---------- ② 多源桥接 ----------

def test_extract_binding_multisource_imdb_bridge(db):
    item = _make_item(db)
    fetched = {"multisource": {"external_ids": {"imdb": "tt8765432",
                                                "douban": "999"}}}
    binding = ts.extract_binding(item, fetched)
    assert binding is not None
    assert binding["provider"] == "imdb", "IMDb 桥优先于豆瓣"
    assert binding["imdb_id"] == "tt8765432"


def test_extract_binding_multisource_tmdb_bridge(db):
    item = _make_item(db)
    fetched = {"multisource": {"external_ids": {"tmdb": "208501"}}}
    binding = ts.extract_binding(item, fetched)
    assert binding is not None
    assert binding["provider"] == "tmdb"
    assert binding["tmdb_id"] == "208501"


def test_extract_binding_multisource_douban(db):
    item = _make_item(db)
    fetched = {"multisource": {"external_ids": {"douban": "1234567"}}}
    binding = ts.extract_binding(item, fetched)
    assert binding is not None
    assert binding["provider"] == "douban"
    assert binding["douban_id"] == "1234567"
    assert binding["evidence"]["douban_id_source"] == "multisource"


def test_extract_binding_multisource_other_source_only_not_identified(db):
    item = _make_item(db)
    fetched = {"multisource": {"external_ids": {"bangumi": "12345"}}}
    assert ts.extract_binding(item, fetched) is None, \
        "识别段只认豆瓣腿与其他源的 tmdb/imdb 桥，bangumi-only 不在此定身份"


def test_extract_binding_tmdb_evidence_still_wins_over_douban(db):
    item = _make_item(db)
    fetched = {"tmdb_id": "777", "douban_fallback_hit": _rich_hit()}
    binding = ts.extract_binding(item, fetched)
    assert binding["provider"] == "tmdb"
    assert binding["tmdb_id"] == "777"


# ---------- ③ apply_identity 落 external_ids ----------

def test_apply_identity_douban_writes_external_ids(db):
    item = _make_item(db)
    binding = ts.apply_identity(db, item, {"douban_fallback_hit": _rich_hit()})
    db.flush()
    assert binding is not None and binding["provider"] == "douban"
    ids = json.loads(item.external_ids)
    assert ids["douban"] == "36742042"
    assert item.tmdb_id is None and item.imdb_id is None
    record = _stage_record(db, item.id)
    assert record.provider == "douban"
    assert record.external_id == "36742042"


def test_apply_identity_douban_keeps_existing_other_keys(db):
    item = _make_item(db, external_ids='{"bangumi": "555"}')
    ts.apply_identity(db, item, {"douban_fallback_hit": _rich_hit()})
    db.flush()
    ids = json.loads(item.external_ids)
    assert ids == {"bangumi": "555", "douban": "36742042"}


def test_apply_identity_douban_does_not_overwrite_existing(db):
    item = _make_item(db, external_ids='{"douban": "111"}')
    ts.apply_identity(db, item, {"douban_fallback_hit": _rich_hit()})
    db.flush()
    ids = json.loads(item.external_ids)
    assert ids["douban"] == "111", "已有豆瓣号不许被新一轮覆盖"


def test_apply_identity_recognizes_persisted_douban(db):
    item = _make_item(db, external_ids='{"douban": "36742042"}')
    binding = ts.apply_identity(db, item, {})
    assert binding is not None
    assert binding["provider"] == "douban"
    assert binding["evidence"]["douban_id_source"] == "item_external_ids"


# ---------- ④ apply_fill 判据同源 ----------

def test_apply_fill_accepts_douban_only_item(db):
    item = _make_item(db, external_ids='{"douban": "36742042"}')
    fetched = {"tmdb_details": {"overview": "豆瓣来的简介"}}
    filled = ts.apply_fill(db, item, fetched)
    assert "overview" in filled
    assert item.overview == "豆瓣来的简介"


def test_apply_fill_still_refuses_truly_unbound(db):
    item = _make_item(db)
    fetched = {"tmdb_details": {"overview": "不该落库"}}
    assert ts.apply_fill(db, item, fetched) == []
    assert not item.overview


# ---------- ⑤ 分发：识别段后回 pending，刮削队列再跑 apply_fill ----------

def test_two_stage_identify_queue_douban_binding_returns_pending(db):
    item = _make_item(db)
    fetched = {"douban_fallback_hit": _rich_hit()}
    enrich_worker._enrich_apply_two_stage(
        db, item, fetched, identify=True, fill=False)
    db.flush()
    assert json.loads(item.external_ids)["douban"] == "36742042"
    assert item.enrich_status == "pending", \
        "PA-081：豆瓣身份行属刮削队列，识别段后回 pending 等刮削队列认领，不许直接 done"


def test_two_stage_fill_queue_douban_only_runs_apply_fill(db, monkeypatch):
    item = _make_item(db, external_ids='{"douban": "36742042"}')
    calls = []
    orig_apply_fill = ts.apply_fill

    def _recording(db_, item_, fetched_):
        calls.append(True)
        return orig_apply_fill(db_, item_, fetched_)

    monkeypatch.setattr(ts, "apply_fill", _recording)
    fetched = {"tmdb_details": {"overview": "豆瓣来的简介"}}
    enrich_worker._enrich_apply_two_stage(
        db, item, fetched, identify=False, fill=True)
    db.flush()
    assert calls, "PA-081：刮削队列必须真正跑到 apply_fill，不许回落现行写库"
    assert item.enrich_status == "done"
    assert item.overview == "豆瓣来的简介"


# ---------- ⑥ rich 落库块写豆瓣号 ----------

def test_enrich_apply_rich_hit_persists_douban_id(db):
    item = _make_item(db)
    fetched = {
        "ok": True,
        "douban_fallback_hit": _rich_hit(),
        "douban_details": {"overview": "简介", "rating": 8.1},
    }
    enrich_worker._enrich_apply(db, item, fetched)
    db.flush()
    ids = json.loads(item.external_ids or "{}")
    assert ids.get("douban") == "36742042", "rich 命中必须把豆瓣号留痕"
    assert item.metadata_source == "douban", "rich 落库块确实跑过"
    # enrich_status 走 _enrich_apply 自己的完整度阶梯（本条只有名/年份
    # 时退回 pending 可重试），与豆瓣号留痕无关，不在此断言。
