# -*- coding: utf-8 -*-
"""PA-078 治本：整源自动停用双重保险 + 手动启用清零计数。

事故背景：整源自动熔断四度误伤——源站详情接口出现分钟级坏窗口时，
真实点播的 counted 失败在几分钟内攒满阈值 3，整源被 disable_source
停用（源 11 一次误停连带 5,174 部已验活单源片无线路可播）。旧口径
「计数达阈值即停」没有任何时间维度与第二意见。

本文件钉死的新口径：
- 保险一（时间跨度门）：连续失败串必须持续至少
  APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC（默认 600 秒）才许进入停用
  判定；失败串起点由健康记录 first_failure_at 记（成功清零、旧记录
  无此字段时以首次见到失败的时刻起算）。爆发式攒满（坏窗口内几分钟
  攒满）一律先拦下，健康状态机本身（翻 Unavailable 沉底、线路计数）
  不受影响、照常工作。
- 保险二（复核门）：跨度达标后，停用前再换轻探活口径问一次源站
  API 首页（ac=list&pg=1，与 applecms_probe 同形态、只读不写状态）；
  只有拿到明确失败证据（AppleCMSError）才算「复核确认已死」；
  API 正常应答、或复核本身无法定性（异常/参数错误）一律不许停。
- 手动启用清零：enable_source 时把该源健康记录的失败计数清零
  （旧口径不清——源带着遗留计数复活，真实流量再喂一次 counted
  失败就又被停，乐子 19 遗留计数 2 即活例子）。
- 线路级行为不动：线路连续失败计数、源级翻 Unavailable 沉底排序
  均按旧口径；保险只拦「整源停用」这一个动作。

全离线：detail 抓取、探活、复核一律桩注入，健康存储用内存实现。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

DETAIL = {
    "code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": 202, "vod_play_from": "直链线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"}]}


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
    monkeypatch.setenv("APPLECMS_DETAIL_CACHE_ENABLED", "0")
    monkeypatch.delenv("APPLECMS_CIRCUIT_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC",
                       raising=False)
    monkeypatch.delenv("UPSTREAM_HEALTH_FAILURE_THRESHOLD", raising=False)


def _make_source(db, name):
    src = us.create_source(
        db, name=name, source_type="applecms",
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


def _resolve_ok(db, source_id):
    return up.resolve_upstream_stream(
        db, source_id, "202", http_get=_detail_get(DETAIL),
        http_probe=_probe(206))


def _resolve_fail(db, source_id):
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, source_id, "202", http_get=_detail_get(DETAIL),
            http_probe=_probe(500))
    assert exc.value.code == "upstream_unavailable"


def _enabled(db, source_id):
    db.expire_all()
    return us.get_source(db, source_id).enabled


def _recheck_dead(monkeypatch):
    monkeypatch.setattr(
        up, "_cms_breaker_recheck_confirms_dead", lambda source: True)


def _recheck_alive(monkeypatch):
    monkeypatch.setattr(
        up, "_cms_breaker_recheck_confirms_dead", lambda source: False)


def _seed_streak(store, source_id, failures, age_sec):
    now = time.time()
    store.set_source(uh.SourceHealthRecord(
        source_key=str(source_id),
        consecutive_unavailable_failures=failures,
        first_failure_at=now - age_sec,
        last_failure_at=now - 1, last_checked_at=now - 1))


# ---------------------------------------------------------------------------
# 失败串起点（first_failure_at）状态机口径
# ---------------------------------------------------------------------------

def test_first_failure_at_tracks_streak_start():
    record = uh.SourceHealthRecord(source_key="7")
    t0 = 1_000_000.0
    uh.apply_source_result(
        record, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"),
        threshold=3, now=t0)
    assert record.first_failure_at == t0
    uh.apply_source_result(
        record, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"),
        threshold=3, now=t0 + 30)
    # 串内后续失败不刷新起点。
    assert record.first_failure_at == t0
    uh.apply_source_result(
        record, uh.ProbeOutcome(uh.OUTCOME_SUCCESS),
        threshold=3, now=t0 + 60)
    assert record.first_failure_at is None
    assert record.consecutive_unavailable_failures == 0
    # 成功后新串重新起算。
    uh.apply_source_result(
        record, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"),
        threshold=3, now=t0 + 90)
    assert record.first_failure_at == t0 + 90


def test_first_failure_at_legacy_record_stamped_on_next_failure():
    # 旧记录（上线前已攒了计数、没有起点字段）：下一次失败时补盖
    # 起点，跨度从那一刻起算——保险口径从严，不许拿缺失字段当
    # 「已持续很久」放行停用。
    record = uh.SourceHealthRecord(
        source_key="7", consecutive_unavailable_failures=2)
    assert record.first_failure_at is None
    uh.apply_source_result(
        record, uh.ProbeOutcome(uh.OUTCOME_FAILURE, "server_error"),
        threshold=3, now=5_000.0)
    assert record.first_failure_at == 5_000.0


def test_first_failure_at_survives_json_roundtrip():
    record = uh.SourceHealthRecord(
        source_key="7", consecutive_unavailable_failures=2,
        first_failure_at=1234.5)
    loaded = uh._record_from_json("7", uh._record_to_json(record))
    assert loaded is not None
    assert loaded.first_failure_at == 1234.5
    # 旧版 JSON（无该键）照常读出、起点为 None。
    legacy = ('{"availability_state": "Active",'
              ' "consecutive_unavailable_failures": 2}')
    loaded = uh._record_from_json("7", legacy)
    assert loaded is not None
    assert loaded.first_failure_at is None


# ---------------------------------------------------------------------------
# 保险一：时间跨度门
# ---------------------------------------------------------------------------

def test_burst_failures_within_bad_window_do_not_disable(db, store,
                                                         monkeypatch):
    # 复现事故形态：坏窗口里几分钟内攒满阈值——复核口径就算确认
    # 已死，跨度没熬满也绝不许停整源。
    _recheck_dead(monkeypatch)
    src = _make_source(db, "坏窗口源")
    for _ in range(3):
        _resolve_fail(db, src.id)
    record = store.get_source(str(src.id))
    assert record.consecutive_unavailable_failures == 3
    # 健康状态机照常翻 Unavailable（沉底排序不受保险影响）。
    assert record.availability_state == uh.STATE_UNAVAILABLE
    assert _enabled(db, src.id) is True


def test_span_satisfied_and_recheck_dead_disables(db, store, monkeypatch):
    # 真持续死源：失败串已持续超过最小跨度、复核也确认已死 → 照停。
    _recheck_dead(monkeypatch)
    src = _make_source(db, "真死源")
    _seed_streak(store, src.id, failures=2, age_sec=601)
    _resolve_fail(db, src.id)
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 3
    assert _enabled(db, src.id) is False


def test_min_span_default_and_env_override(monkeypatch):
    assert up.cms_circuit_breaker_min_span_sec() == 600.0
    monkeypatch.setenv("APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC", "0")
    assert up.cms_circuit_breaker_min_span_sec() == 0.0


def test_span_zero_restores_count_only_gate(db, store, monkeypatch):
    # 跨度配 0 = 退回计数单门（运维显式选择，非默认）：攒满即停。
    monkeypatch.setenv("APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC", "0")
    _recheck_dead(monkeypatch)
    src = _make_source(db, "跨度零源")
    for _ in range(3):
        _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is False


def test_success_resets_span_so_burst_after_recovery_is_safe(
        db, store, monkeypatch):
    _recheck_dead(monkeypatch)
    src = _make_source(db, "恢复后爆发源")
    _seed_streak(store, src.id, failures=2, age_sec=3600)
    _resolve_ok(db, src.id)
    record = store.get_source(str(src.id))
    assert record.consecutive_unavailable_failures == 0
    assert record.first_failure_at is None
    for _ in range(3):
        _resolve_fail(db, src.id)
    # 新串刚起（跨度≈0）：不许拿恢复前的老串时长凑跨度。
    assert _enabled(db, src.id) is True


# ---------------------------------------------------------------------------
# 保险二：停用前复核门
# ---------------------------------------------------------------------------

def test_recheck_alive_blocks_disable_even_with_span(db, store,
                                                     monkeypatch):
    # 跨度熬满、计数达标，但停用前换轻探活口径问源站 API——API
    # 还活着（坏的只是点播/detail 窗口）→ 不许停整源。
    _recheck_alive(monkeypatch)
    src = _make_source(db, "API还活着源")
    _seed_streak(store, src.id, failures=2, age_sec=3600)
    _resolve_fail(db, src.id)
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 3
    assert _enabled(db, src.id) is True
    # 复核转死后，下一次 counted 失败即放行停用。
    _recheck_dead(monkeypatch)
    _resolve_fail(db, src.id)
    assert _enabled(db, src.id) is False


def _bare_source():
    return SimpleNamespace(
        base_url="https://cms.example.com", user_agent=None,
        referer=None, extra_headers_json=None)


def test_recheck_api_success_means_not_dead():
    def http_get(url, headers, timeout):
        assert "ac=list" in url
        return 200, {"code": 1, "list": []}
    assert up._cms_breaker_recheck_confirms_dead(
        _bare_source(), http_get=http_get) is False


def test_recheck_api_failure_means_dead():
    def http_get_none(url, headers, timeout):
        return None, None
    assert up._cms_breaker_recheck_confirms_dead(
        _bare_source(), http_get=http_get_none) is True

    def http_get_401(url, headers, timeout):
        return 401, None
    assert up._cms_breaker_recheck_confirms_dead(
        _bare_source(), http_get=http_get_401) is True

    def http_get_bad_code(url, headers, timeout):
        return 200, {"code": 0}
    assert up._cms_breaker_recheck_confirms_dead(
        _bare_source(), http_get=http_get_bad_code) is True


def test_recheck_undetermined_means_not_dead():
    # 复核无法定性 = 绝不当作「确认已死」放行停用。真实形态：
    # 源地址为空、拼不出 provide 地址时 fetch_page 在发请求前抛
    # ValueError——这是配置/我方问题，不是源死了的证据。（注：
    # http_get 本身抛出的异常会被抓取层按 network 定性，与默认
    # 实现连不上同口径，不走本分支。）
    def http_get_boom(url, headers, timeout):
        raise AssertionError("地址都拼不出来，不该发请求")
    source = SimpleNamespace(
        base_url="", user_agent=None, referer=None,
        extra_headers_json=None)
    assert up._cms_breaker_recheck_confirms_dead(
        source, http_get=http_get_boom) is False


# ---------------------------------------------------------------------------
# 手动启用清零熔断计数
# ---------------------------------------------------------------------------

def test_manual_enable_clears_breaker_count(db, store, monkeypatch):
    monkeypatch.setattr(uh, "RedisHealthStore", lambda *a, **k: store)
    src = _make_source(db, "遗留计数源")
    # 乐子 19 形态：停用前攒下的计数遗留在健康记录里。
    _seed_streak(store, src.id, failures=2, age_sec=3600)
    store.get_source(str(src.id)).availability_state = uh.STATE_UNAVAILABLE
    us.enable_source(db, src)
    db.commit()
    record = store.get_source(str(src.id))
    assert record is not None
    assert record.consecutive_unavailable_failures == 0
    assert record.availability_state == uh.STATE_ACTIVE
    assert record.first_failure_at is None


def test_manual_enable_without_record_is_noop(db, store, monkeypatch):
    monkeypatch.setattr(uh, "RedisHealthStore", lambda *a, **k: store)
    src = _make_source(db, "无记录源")
    us.enable_source(db, src)
    db.commit()
    assert store.get_source(str(src.id)) is None
    assert _enabled(db, src.id) is True


def test_enable_then_single_failure_does_not_disable(db, store,
                                                     monkeypatch):
    # 清零的实际意义：启用后真实流量再喂一次 counted 失败，
    # 计数从 1 重新起算，不许被遗留计数顶到阈值直接再停。
    monkeypatch.setattr(uh, "RedisHealthStore", lambda *a, **k: store)
    _recheck_dead(monkeypatch)
    monkeypatch.setenv("APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC", "0")
    src = _make_source(db, "复活源")
    _seed_streak(store, src.id, failures=2, age_sec=3600)
    us.enable_source(db, src)
    db.commit()
    _resolve_fail(db, src.id)
    assert store.get_source(
        str(src.id)).consecutive_unavailable_failures == 1
    assert _enabled(db, src.id) is True
