# -*- coding: utf-8 -*-
"""苹果 CMS 线路择优 + 坏源自动熔断回归（播放优化 B 件）。

钉死口径：
- CMS 取流逐次结论写既有健康记录：源级 SourceHealthRecord（键
  str(source.id)，与轻探活共用同一份连续失败计数）+ 线路级
  LineHealthRecord（键 mapping:<映射 id>，含连续失败与最近成功延迟）；
- PlaybackInfo 线路组装（get_displayable_mappings）按健康分稳定
  排序：不可用沉底、好线首位、同分保持映射偏好序；
- 连续失败达阈值（env APPLECMS_CIRCUIT_BREAKER_THRESHOLD，默认与
  UPSTREAM_HEALTH_FAILURE_THRESHOLD 同值 3）自动 disable_source，
  只对 applecms 型源生效；未达阈值不误停；条目不存在不记失败；
  手动 enable 后恢复可播；
- 留痕/熔断全程 fail-open，不改变取流成功地址与失败结构化错误。

全离线：detail 抓取与探活一律桩注入，健康存储用内存实现。
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

DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 202, "vod_play_from": "直链线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"}]}

EMPTY_DETAIL = {"code": 1, "page": 1, "pagecount": 1, "list": []}


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
def store(monkeypatch):
    health_store = uh.InMemoryHealthStore()
    monkeypatch.setattr(up, "_get_health_store", lambda: health_store)
    return health_store


@pytest.fixture(autouse=True)
def _env_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    monkeypatch.delenv("APPLECMS_PARSE_LINES_ENABLED", raising=False)
    monkeypatch.delenv("APPLECMS_CIRCUIT_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("UPSTREAM_HEALTH_FAILURE_THRESHOLD", raising=False)


def _make_source(db, name, source_type="applecms"):
    src = us.create_source(
        db, name=name, source_type=source_type,
        base_url="https://cms.example.com", auth_method="api_key")
    us.enable_source(db, src)
    db.commit()
    return src


def _detail_get(payload):
    def http_get(url, headers, timeout):
        return 200, payload
    return http_get


def _probe(status):
    def probe(url, headers, timeout):
        return status, None
    return probe


def _resolve_ok(db, source_id, **kw):
    return up.resolve_upstream_stream(
        db, source_id, "202", http_get=_detail_get(DETAIL),
        http_probe=_probe(206), **kw)


def _resolve_fail(db, source_id, **kw):
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, source_id, "202", http_get=_detail_get(DETAIL),
            http_probe=_probe(500), **kw)
    assert exc.value.code == "upstream_unavailable"
    return exc.value


def _enabled(db, source_id):
    db.expire_all()
    return us.get_source(db, source_id).enabled


# ---------------------------------------------------------------------------
# 留痕接线
# ---------------------------------------------------------------------------

def test_success_records_source_and_line_health(db, store):
    src = _make_source(db, "留痕源")
    url = _resolve_ok(db, src.id, quality_mapping_id=42)
    assert url == "https://cdn.example.com/ep1.mp4"
    record = store.get_source(str(src.id))
    assert record is not None
    assert record.availability_state == uh.STATE_ACTIVE
    assert record.consecutive_unavailable_failures == 0
    assert record.last_success_at is not None
    assert record.last_checked_at is not None
    line = store.get_line("mapping:42")
    assert line is not None
    assert line.availability_status == uh.LINE_AVAILABLE
    assert line.consecutive_failures == 0
    assert line.last_success_at is not None
    assert line.last_latency_ms is not None and line.last_latency_ms >= 0


def test_failure_updates_line_record_fields(db, store):
    src = _make_source(db, "线路留痕源")
    _resolve_fail(db, src.id, quality_mapping_id=43)
    line = store.get_line("mapping:43")
    assert line is not None
    assert line.consecutive_failures == 1
    assert line.last_failure_kind == "server_error"
    assert line.last_failure_at is not None
    record = store.get_source(str(src.id))
    assert record.consecutive_unavailable_failures == 1
    assert record.last_failure_kind == "server_error"


def test_item_not_found_does_not_count_as_source_failure(db, store):
    src = _make_source(db, "条目消失源")
    for _ in range(5):
        with pytest.raises(up.UpstreamPlaybackError) as exc:
            up.resolve_upstream_stream(
                db, src.id, "202", http_get=_detail_get(EMPTY_DETAIL),
                http_probe=_probe(206))
        assert exc.value.code == "upstream_item_not_found"
    record = store.get_source(str(src.id))
    assert record.consecutive_unavailable_failures == 0
    assert _enabled(db, src.id) is True


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def test_failures_below_threshold_keep_source_enabled(db, store):
    src = _make_source(db, "未达阈值源")
    _resolve_fail(db, src.id)
    _resolve_fail(db, src.id)
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 2
    assert _enabled(db, src.id) is True


def test_threshold_reached_auto_disables_cms_source(db, store):
    src = _make_source(db, "坏源")
    _resolve_fail(db, src.id)
    _resolve_fail(db, src.id)
    _resolve_fail(db, src.id)
    record = store.get_source(str(src.id))
    assert record.consecutive_unavailable_failures == 3
    assert record.availability_state == uh.STATE_UNAVAILABLE
    assert _enabled(db, src.id) is False


def test_rate_limited_does_not_count_toward_breaker(db, store):
    # PA-078：429 是源站对共享 IP 的短暂限流，不是源坏了的证据，
    # 与 403（upstream_forbidden）同口径：一律不记失败、不触发熔断。
    # 结构化错误本身不变（仍向调用方报 429 upstream_quota_exhausted）。
    src = _make_source(db, "被限流源")
    for _ in range(3):
        with pytest.raises(up.UpstreamPlaybackError) as exc:
            up.resolve_upstream_stream(
                db, src.id, "202", http_get=_detail_get(DETAIL),
                http_probe=_probe(429))
        assert exc.value.code == "upstream_quota_exhausted"
    record = store.get_source(str(src.id))
    assert record is None or record.consecutive_unavailable_failures == 0
    assert record is None or record.availability_state != uh.STATE_UNAVAILABLE
    assert _enabled(db, src.id) is True


def test_breaker_threshold_env_override(db, store, monkeypatch):
    monkeypatch.setenv("APPLECMS_CIRCUIT_BREAKER_THRESHOLD", "2")
    assert up.cms_circuit_breaker_threshold() == 2
    src = _make_source(db, "阈值二源")
    _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is True
    _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is False


def test_success_resets_failure_count_before_breaker(db, store):
    src = _make_source(db, "抖动源")
    _resolve_fail(db, src.id)
    _resolve_fail(db, src.id)
    _resolve_ok(db, src.id)
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 0
    _resolve_fail(db, src.id)
    _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is True


def test_breaker_never_touches_non_cms_source(db, store):
    src = _make_source(db, "Emby源", source_type="emby")
    # 直接调留痕口：非 CMS 型一律不记、不停。
    for _ in range(5):
        up._record_cms_stream_outcome(
            db, src, error_code="upstream_unavailable", latency_ms=5)
    assert store.get_source(str(src.id)) is None
    assert _enabled(db, src.id) is True
    # 走真实解析路径（Emby 分支探活失败）同样不留痕、不熔断。
    for _ in range(3):
        with pytest.raises(up.UpstreamPlaybackError):
            up.resolve_upstream_stream(
                db, src.id, "item-1", http_probe=_probe(500))
    assert store.get_source(str(src.id)) is None
    assert _enabled(db, src.id) is True


def test_manual_enable_restores_playback_after_breaker(db, store):
    src = _make_source(db, "可恢复源")
    for _ in range(3):
        _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is False
    src = us.get_source(db, src.id)
    us.enable_source(db, src)
    db.commit()
    assert _resolve_ok(db, src.id) == "https://cdn.example.com/ep1.mp4"
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 0


# ---------------------------------------------------------------------------
# 择优排序
# ---------------------------------------------------------------------------

def _seed_item_with_mappings(db, specs):
    """specs: [(source, confidence), ...]，返回 (item, [mapping, ...])。"""
    library = em.Library(guid="lib-circuit", name="熔断测试库")
    db.add(library)
    db.flush()
    item = em.MediaItem(
        guid="item-circuit", library_id=library.id, item_type="movie",
        name="排序测试片", file_path="mount://1/sort.mp4")
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


def test_displayable_mappings_sorted_by_health(db, store):
    good = _make_source(db, "低延迟源")
    plain = _make_source(db, "无记录源")
    bad = _make_source(db, "已摘线源")
    # 偏好序（置信度降序）：无记录 > 已摘线 > 低延迟。
    item, mappings = _seed_item_with_mappings(
        db, [(plain, 0.9), (bad, 0.8), (good, 0.7)])
    by_source = {m.source_id: m for m in mappings}
    now = time.time()
    store.set_source(uh.SourceHealthRecord(
        source_key=str(bad.id), availability_state=uh.STATE_UNAVAILABLE,
        consecutive_unavailable_failures=5, last_checked_at=now))
    store.set_line(uh.LineHealthRecord(
        line_key=f"mapping:{by_source[good.id].id}",
        last_latency_ms=50.0, last_checked_at=now))
    ordered = up.get_displayable_mappings(db, item.id)
    assert [s.id for _, s in ordered] == [good.id, plain.id, bad.id]


def test_displayable_mappings_stable_without_health_records(db, store):
    first = _make_source(db, "偏好一源")
    second = _make_source(db, "偏好二源")
    item, _ = _seed_item_with_mappings(db, [(first, 0.9), (second, 0.8)])
    ordered = up.get_displayable_mappings(db, item.id)
    assert [s.id for _, s in ordered] == [first.id, second.id]


def test_displayable_mappings_failing_line_sinks(db, store):
    failing = _make_source(db, "连败源")
    healthy = _make_source(db, "健康源")
    item, _ = _seed_item_with_mappings(
        db, [(failing, 0.9), (healthy, 0.8)])
    now = time.time()
    store.set_source(uh.SourceHealthRecord(
        source_key=str(failing.id),
        consecutive_unavailable_failures=2, last_checked_at=now))
    ordered = up.get_displayable_mappings(db, item.id)
    assert [s.id for _, s in ordered] == [healthy.id, failing.id]
