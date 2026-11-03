# -*- coding: utf-8 -*-
"""CMS 线路档位降权回归（2026-11-03 印制广告摸底处置，用户亲点定版）。

钉死口径：
- 档位只挂进既有唯一排序口（upstream_playback.get_displayable_mappings
  的 _mapping_health_sort_key），合成顺序＝健康层 → 档位 → 失败数 →
  延迟：可用性优先（死线/熔断线永不被档位顶到活线前面），同健康层
  内毒档线沉底只作兜底，同档内保持既有择优顺序；
- 名单走 SystemConfig 键 upstream_aggregate_demoted_source_ids
  （int_list），无行回代码默认（摸底毒档），有行按行解析、空行＝
  显式清空；改档经 store 写口即时生效（写时失效缓存）；
- 降权只改顺序：毒档线仍在下发列表里、仍可播；只有毒档线的条目
  顺序不变坏。

全离线：健康存储用内存实现，配置走测试内存库 SystemConfig 行。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us
from backend.integrations import store as config_store

CONFIG_KEY = "upstream_aggregate_demoted_source_ids"


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def health_store(monkeypatch):
    store = uh.InMemoryHealthStore()
    monkeypatch.setattr(up, "_get_health_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _env_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")


@pytest.fixture(autouse=True)
def _config_cache_hygiene():
    """配置热缓存是进程级的：本文件每例前后都清本键，
    不许把测试档位串给同进程其它文件（夹具隔离纪律）。"""
    config_store.invalidate(CONFIG_KEY)
    yield
    config_store.invalidate(CONFIG_KEY)


def _make_source(db, name):
    src = us.create_source(
        db, name=name, source_type="applecms",
        base_url="https://cms.example.com", auth_method="api_key")
    us.enable_source(db, src)
    db.commit()
    return src


def _seed_item_with_mappings(db, specs):
    """specs: [(source, confidence), ...]，返回 (item, [mapping, ...])。"""
    library = em.Library(guid="lib-demote", name="降权测试库")
    db.add(library)
    db.flush()
    item = em.MediaItem(
        guid="item-demote", library_id=library.id, item_type="movie",
        name="降权测试片", file_path="mount://1/demote.mp4")
    db.add(item)
    db.flush()
    mappings = []
    for source, confidence in specs:
        mappings.append(uim.record_mapping(
            db, local_item_id=item.id, source_id=source.id,
            upstream_item_id="202", confidence=confidence,
            evidence="tmdb"))
    db.commit()
    return item, mappings


def _set_demoted(db, ids):
    """经 store 正规写口落档（写时失效缓存＝改档即时生效口径）。"""
    config_store.write_values(db, {CONFIG_KEY: ",".join(str(i) for i in ids)})
    db.commit()


def _ordered_source_ids(db, item_id):
    return [s.id for _, s in up.get_displayable_mappings(db, item_id)]


# ---------------------------------------------------------------------------
# 名单读取口
# ---------------------------------------------------------------------------

def test_demoted_ids_default_is_audit_list(db):
    """无配置行 → 代码默认＝摸底毒档；干净/未测源不在名单。"""
    ids = up.demoted_source_ids(db)
    assert ids == up.DEFAULT_DEMOTED_SOURCE_IDS
    for dirty in (14, 15, 16, 18, 20, 21, 22, 24):
        assert dirty in ids
    for clean_or_unknown in (7, 9, 10, 13, 11, 12, 17, 19, 23, 25):
        assert clean_or_unknown not in ids


def test_demoted_ids_reader_tolerates_garbage(db):
    """坏项跳过、空项忽略；db=None 回默认（读失败不许关掉降权）。"""
    config_store.write_values(db, {CONFIG_KEY: "abc, 14, ,24x, 15"})
    db.commit()
    assert up.demoted_source_ids(db) == frozenset({14, 15})
    assert up.demoted_source_ids(None) == up.DEFAULT_DEMOTED_SOURCE_IDS


def test_demoted_ids_explicit_empty_row_disables(db):
    """有行但为空＝管理员显式清空（降权关），不许回退默认名单。"""
    row = base_models.SystemConfig(key=CONFIG_KEY, value="")
    db.add(row)
    db.commit()
    config_store.invalidate(CONFIG_KEY)
    assert up.demoted_source_ids(db) == frozenset()


# ---------------------------------------------------------------------------
# 排序合成
# ---------------------------------------------------------------------------

def test_demoted_line_sinks_below_clean_and_neutral(db, health_store):
    """多线（毒+干净+中性）：毒线沉底，干净/中性保持偏好序相对位置。"""
    dirty = _make_source(db, "毒档源")
    clean = _make_source(db, "干净源")
    neutral = _make_source(db, "中性源")
    # 偏好序：毒 > 干净 > 中性（无降权时毒线本应排第一）。
    item, _ = _seed_item_with_mappings(
        db, [(dirty, 0.95), (clean, 0.9), (neutral, 0.8)])
    _set_demoted(db, [dirty.id])
    assert _ordered_source_ids(db, item.id) == [
        clean.id, neutral.id, dirty.id]


def test_only_demoted_lines_all_listed_order_unchanged(db, health_store):
    """只有毒档线：全部仍下发（仍可播），顺序不变坏。"""
    first = _make_source(db, "毒档一源")
    second = _make_source(db, "毒档二源")
    item, _ = _seed_item_with_mappings(db, [(first, 0.9), (second, 0.8)])
    _set_demoted(db, [first.id, second.id])
    assert _ordered_source_ids(db, item.id) == [first.id, second.id]


def test_config_change_moves_source_in_and_out(db, health_store):
    """改配置把源移入/移出毒档 → 排序随之变（改档即时生效）。"""
    x = _make_source(db, "甲源")
    y = _make_source(db, "乙源")
    item, _ = _seed_item_with_mappings(db, [(x, 0.9), (y, 0.8)])
    # 测试库源 id 不在默认毒档名单内：无行时保持偏好序。
    assert _ordered_source_ids(db, item.id) == [x.id, y.id]
    _set_demoted(db, [x.id])
    assert _ordered_source_ids(db, item.id) == [y.id, x.id]
    _set_demoted(db, [y.id])
    assert _ordered_source_ids(db, item.id) == [x.id, y.id]


def test_same_tier_keeps_existing_health_order(db, health_store):
    """同档内保持既有择优：两条毒线按延迟排；干净线即便延迟
    更高也排在毒线之前（档位在延迟之前、同健康层内毒线沉底）。"""
    clean = _make_source(db, "干净慢源")
    dirty_slow = _make_source(db, "毒档无记录源")
    dirty_fast = _make_source(db, "毒档低延迟源")
    item, mappings = _seed_item_with_mappings(
        db, [(dirty_slow, 0.95), (dirty_fast, 0.9), (clean, 0.8)])
    by_source = {m.source_id: m for m in mappings}
    now = time.time()
    health_store.set_line(uh.LineHealthRecord(
        line_key=f"mapping:{by_source[clean.id].id}",
        last_latency_ms=50.0, last_checked_at=now))
    health_store.set_line(uh.LineHealthRecord(
        line_key=f"mapping:{by_source[dirty_fast.id].id}",
        last_latency_ms=30.0, last_checked_at=now))
    _set_demoted(db, [dirty_slow.id, dirty_fast.id])
    assert _ordered_source_ids(db, item.id) == [
        clean.id, dirty_fast.id, dirty_slow.id]


def test_demotion_never_lifts_dead_line_above_live(db, health_store):
    """可用性优先：干净源已判不可用（健康层沉底）时，活着的毒线
    排在它前面——档位不许把死线顶到活线前面。"""
    clean_dead = _make_source(db, "干净死源")
    dirty_live = _make_source(db, "毒档活源")
    item, _ = _seed_item_with_mappings(
        db, [(clean_dead, 0.9), (dirty_live, 0.8)])
    now = time.time()
    health_store.set_source(uh.SourceHealthRecord(
        source_key=str(clean_dead.id),
        availability_state=uh.STATE_UNAVAILABLE,
        consecutive_unavailable_failures=5, last_checked_at=now))
    _set_demoted(db, [dirty_live.id])
    assert _ordered_source_ids(db, item.id) == [
        dirty_live.id, clean_dead.id]
