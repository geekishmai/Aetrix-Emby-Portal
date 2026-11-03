"""仪表盘补口径（2026-11-03）：今日新增入库 + 24 小时趋势序列。

背景：仪表盘换新 PR #1805 两处因后端无口径临时替换——
① KPI「今日新增入库」无接口口径，临时换成「今日新增用户」；
② 播放趋势无 24 小时粒度序列，分段临时改 7/14/30 天。
本文件钉住补上的真口径：

- 口径定义（与按日 trend 同源同定义，只换分桶粒度）：
  - 新增入库 = MediaItem.date_added（入库时间，UTC-naive 存储）落在
    Asia/Shanghai 自然日内；排除软删除（deleted_at 非空）与合并副本
    （merged_into_id 非空）。
  - 播放 = PlaybackSession.start_time 计数（起播），与按日 plays 同源。
- overview.emby.today_new_items == trend series 末日 new_items ==
  当日小时桶 new_items 求和，三处必须对得上（对账断言）。
- 小时桶：最近 24 个上海整点桶（含当前未走完的整点），缺桶补零。

时钟冻结在上海 2026-10-18 14:30，与 r43 趋势测试同手法
（monkeypatch 目标模块的 datetime 类）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import admin as admin_mod
from backend.api import admin_economy, panel
from backend.emby_server import models as em_models

SH = ZoneInfo("Asia/Shanghai")
FROZEN_SH = datetime(2026, 10, 18, 14, 30, tzinfo=SH)
FROZEN_UTC_NAIVE = FROZEN_SH.astimezone(timezone.utc).replace(tzinfo=None)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_SH if tz is not None else FROZEN_UTC_NAIVE


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(admin_economy, "datetime", _FrozenDateTime)
    monkeypatch.setattr(panel, "datetime", _FrozenDateTime)


def _utc(sh_dt: datetime) -> datetime:
    """上海 aware 时间 → 入库帧 UTC-naive。"""
    return sh_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _sh(day: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=SH)


def _mk_item(db, added_sh: datetime, **kw):
    it = em_models.MediaItem(
        guid=uuid.uuid4().hex, library_id=1, item_type="movie",
        name="片", file_path="/tmp/x.mp4", enrich_status="done",
        date_added=_utc(added_sh), **kw)
    db.add(it)
    db.commit()
    return it


def _mk_play(db, start_sh: datetime, user_id: int = 1):
    p = em_models.PlaybackSession(user_id=user_id, start_time=_utc(start_sh))
    db.add(p)
    db.commit()
    return p


def _seed(db):
    # ---- 条目（date_added 按上海时间标注） ----
    _mk_item(db, _sh(18, 0, 10))          # 今日 00:10（旧 UTC 日口径会漏）
    _mk_item(db, _sh(18, 0, 0))           # 今日 00:00 整（左闭边界）
    _mk_item(db, _sh(17, 23, 59, 59))     # 昨日 23:59:59（不算今日）
    _mk_item(db, _sh(18, 9, 15))          # 今日
    _mk_item(db, _sh(18, 12, 0), deleted_at=datetime.now())   # 软删：不计
    _mk_item(db, _sh(18, 13, 0), merged_into_id=999)          # 副本：不计
    _mk_item(db, _sh(17, 10, 0))          # 昨日
    _mk_item(db, _sh(18, 14, 5))          # 今日（当前小时桶）
    # ---- 播放（start_time 按上海时间标注） ----
    _mk_play(db, _sh(18, 0, 30))          # 今日 00:00 桶
    _mk_play(db, _sh(18, 13, 5))          # 今日 13:00 桶
    _mk_play(db, _sh(18, 13, 50))         # 今日 13:00 桶
    _mk_play(db, _sh(18, 14, 10))         # 今日 14:00 桶（当前桶）
    _mk_play(db, _sh(17, 16, 20))         # 昨日 16:00 桶（窗口内）
    _mk_play(db, _sh(17, 14, 59))         # 昨日 14:59（窗口外，差 1 分钟）


def test_trend_new_items_daily_caliber(db, frozen):
    _seed(db)
    resp = admin_economy.get_stats_trend(days=3, current_admin=None, db=db)
    labels = [p["date"] for p in resp["series"]]
    assert labels == ["2026-10-16", "2026-10-17", "2026-10-18"]
    got = {p["date"]: p["new_items"] for p in resp["series"]}
    assert got == {"2026-10-16": 0, "2026-10-17": 2, "2026-10-18": 4}, got
    assert resp["totals"]["new_items"] == 6
    # 播放按日值不被本次改动带偏
    plays = {p["date"]: p["plays"] for p in resp["series"]}
    assert plays == {"2026-10-16": 0, "2026-10-17": 2, "2026-10-18": 4}


def test_overview_today_new_items(db, frozen):
    _seed(db)
    admin = models.WebUser(username="a", password_hash="", is_staff=True,
                           is_active=True)
    out = admin_mod.get_stats_overview(admin, db)
    assert out["emby"]["today_new_items"] == 4, out["emby"]


def test_hourly_series_shape_and_buckets(db, frozen):
    _seed(db)
    resp = admin_economy.get_stats_trend(days=3, current_admin=None, db=db)
    hourly = resp["hourly"]
    assert hourly["hours"] == 24
    series = hourly["series"]
    assert len(series) == 24
    assert series[0]["hour"] == "2026-10-17 15:00"
    assert series[-1]["hour"] == "2026-10-18 14:00"
    by_hour = {p["hour"]: p for p in series}
    # 播放分桶
    assert by_hour["2026-10-18 00:00"]["plays"] == 1
    assert by_hour["2026-10-18 13:00"]["plays"] == 2
    assert by_hour["2026-10-18 14:00"]["plays"] == 1
    assert by_hour["2026-10-17 16:00"]["plays"] == 1
    # 入库分桶
    assert by_hour["2026-10-18 00:00"]["new_items"] == 2
    assert by_hour["2026-10-18 09:00"]["new_items"] == 1
    assert by_hour["2026-10-18 14:00"]["new_items"] == 1
    assert by_hour["2026-10-17 23:00"]["new_items"] == 1
    # 补零：无事件的桶必须在序列里且为 0
    assert by_hour["2026-10-18 03:00"] == {
        "hour": "2026-10-18 03:00", "plays": 0, "new_items": 0}
    # 窗口外（昨日 14:59 的播放）不进任何小时桶
    assert sum(p["plays"] for p in series) == 5
    assert sum(p["new_items"] for p in series) == 5


def test_hourly_reconciles_with_daily_and_overview(db, frozen):
    """对账：当日小时桶求和 == 按日值 == overview 今日值。"""
    _seed(db)
    resp = admin_economy.get_stats_trend(days=3, current_admin=None, db=db)
    today = "2026-10-18"
    hourly_today_plays = sum(
        p["plays"] for p in resp["hourly"]["series"]
        if p["hour"].startswith(today))
    hourly_today_items = sum(
        p["new_items"] for p in resp["hourly"]["series"]
        if p["hour"].startswith(today))
    daily_today = resp["series"][-1]
    assert daily_today["date"] == today
    assert hourly_today_plays == daily_today["plays"] == 4
    assert hourly_today_items == daily_today["new_items"] == 4
    admin = models.WebUser(username="a", password_hash="", is_staff=True,
                           is_active=True)
    out = admin_mod.get_stats_overview(admin, db)
    assert out["emby"]["today_new_items"] == hourly_today_items
