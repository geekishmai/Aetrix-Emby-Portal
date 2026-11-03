"""计数类慢查询治理（2026-11-03）：导航计数与库计数缓存 + 计数形态。

生产实测：/Items/Counts 一次请求两条带身份门子句的全表计数、每条约
13s（pg_stat_statements 近 8.75h 窗口 1,374 次）；Views 身份门开时每库
一条现算计数约 0.5s。治理口径（与列表 total 同一套基础设施，不新造
第二套缓存）：

- items_counts 结果进 items_list_cache 的计数缓存：键 = 可见范围 +
  门形态 + 各库数据版本号；版本号 bump 覆盖全部条目写路径，未 bump 的
  残余写路径由条目 TTL 兜底。命中时端点不再对 emby_items 发计数语句。
- 总数改由 GROUP BY 结果汇总（item_type 非空，Σ各类型 = 全量
  COUNT(*)，数学等价），省掉第二条全表扫描。
- _library_item_count 的门开现算同样进计数缓存（按单库版本号失效）。
- _query_items 的 total 不再用 Query.count() 子查询包装形态，改
  Core COUNT(*)（PA-035 同款，结果逐行等价）。

先红后绿：本文件的缓存命中断言（第二次调用零 emby_items 计数语句）与
SQL 形态断言（不得出现 count 包装子查询）在旧实现上必然失败。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api, identity_gate
from backend.emby_server import items_dedup_cache as idc
from backend.emby_server import items_list_cache as ilc
from backend.emby_server import models as em


class FakeRedis:
    """最小 Redis 替身：items_dedup_cache / items_list_cache 用到的子集。"""

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.calls = []

    def get(self, key):
        self.calls.append(("get", key))
        v = self.strings.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp <= time.time():
            del self.strings[key]
            return None
        return val

    def set(self, key, val, ex=None):
        self.calls.append(("set", key))
        exp = time.time() + ex if ex else None
        self.strings[key] = (val, exp)
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
        return n

    def incr(self, key):
        self.calls.append(("incr", key))
        v = self.strings.get(key)
        n = int(v[0]) + 1 if v else 1
        self.strings[key] = (str(n), v[1] if v else None)
        return n

    def zadd(self, name, mapping):
        z = self.zsets.setdefault(name, {})
        z.update(mapping)
        return len(mapping)

    def zcard(self, name):
        return len(self.zsets.get(name, {}))

    def zrange(self, name, start, stop):
        items = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        if stop == -1:
            return [k for k, _ in items[start:]]
        return [k for k, _ in items[start:stop + 1]]

    def zrem(self, name, *members):
        z = self.zsets.get(name, {})
        n = 0
        for m in members:
            if m in z:
                del z[m]
                n += 1
        return n


@pytest.fixture()
def fake_backend():
    fb = FakeRedis()
    idc._set_backend_override(fb)
    yield fb
    idc._set_backend_override(None)


class SqlRecorder:
    """记录引擎上执行过的语句，数其中对 emby_items 的计数语句。"""

    def __init__(self, engine):
        self.statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

    def item_count_statements(self):
        return [s for s in self.statements
                if "emby_items" in s.lower() and "count(" in s.lower()]

    def clear(self):
        self.statements.clear()


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/counts_cache.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True,
                     item_count=99)  # 假缓存列：门开时不许用它
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
            name=name, sort_name=name,
            file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    m = {}
    m["mb"] = mk("movie", "已绑定电影", tmdb_id="1")
    m["mu"] = mk("movie", "未绑定电影")
    m["sb"] = mk("series", "已绑定剧", tmdb_id="2")
    m["season"] = mk("season", "季", series_id=m["sb"].id, season_number=1)
    m["ep"] = mk("episode", "集", series_id=m["sb"].id,
                 season_number=1, episode_number=1)
    m["ph"] = mk("photo", "照片")  # 其它类型：只进总数、不进三类计数
    m["hm"] = mk("movie", "隐藏电影", is_hidden=True)
    user = models.WebUser(username="cnt", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    rec = SqlRecorder(engine)
    data = SimpleNamespace(db=db, m=m, user=user, lib=lib, rec=rec,
                           engine=engine)
    yield data
    db.close()
    engine.dispose()


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)


def test_nav_counts_gate_off_totals_include_other_types(scene, fake_backend):
    res = api.items_counts(user=scene.user, db=scene.db)
    assert res["MovieCount"] == 2
    assert res["SeriesCount"] == 1
    assert res["EpisodeCount"] == 1
    # 总数 = 全部类型行数（含 season/photo，不含隐藏行）：
    # 由 GROUP BY 汇总与旧 _total_q 的全量 COUNT(*) 必须一致
    assert res["ItemCount"] == 6


def test_nav_counts_gate_on_bound_only(scene, fake_backend, gate_on):
    res = api.items_counts(user=scene.user, db=scene.db)
    assert res["MovieCount"] == 1
    assert res["SeriesCount"] == 1
    assert res["EpisodeCount"] == 1
    # mb + sb + season + ep（跟随 series）+ photo：身份门判据的第四分支
    # 是「非四类不在治理内」，photo 行门开时照计（旧 _total_q 同口径 = 5）
    assert res["ItemCount"] == 5


def test_nav_counts_second_call_hits_cache(scene, fake_backend):
    first = api.items_counts(user=scene.user, db=scene.db)
    assert scene.rec.item_count_statements(), "首呼应现算（有计数语句）"
    scene.rec.clear()
    second = api.items_counts(user=scene.user, db=scene.db)
    assert second == first
    assert scene.rec.item_count_statements() == [], \
        "第二次调用必须命中计数缓存，不再对 emby_items 发计数语句"


def test_nav_counts_version_bump_invalidates(scene, fake_backend):
    api.items_counts(user=scene.user, db=scene.db)
    # 新增一条电影 + 该库版本号 bump（全部条目写路径的统一动作）
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=scene.lib.id, item_type="movie",
        name="新片", sort_name="新片",
        file_path=f"/m/{uuid.uuid4().hex}.mkv")
    scene.db.add(it)
    scene.db.flush()
    idc.bump_library_item_versions(scene.db, [scene.lib.id], force=True)
    scene.db.commit()
    scene.rec.clear()
    res = api.items_counts(user=scene.user, db=scene.db)
    assert res["MovieCount"] == 3
    assert res["ItemCount"] == 7
    assert scene.rec.item_count_statements(), "bump 后必须失效重算"


def test_nav_counts_cache_disabled_computes_live(scene, fake_backend,
                                                 monkeypatch):
    monkeypatch.setattr(ilc, "cache_enabled", lambda db=None: False)
    first = api.items_counts(user=scene.user, db=scene.db)
    scene.rec.clear()
    second = api.items_counts(user=scene.user, db=scene.db)
    assert second == first
    assert scene.rec.item_count_statements(), "缓存关时必须现算（fail-open）"


def test_library_child_count_gate_on_cached(scene, fake_backend, gate_on):
    n1 = api._library_item_count(scene.db, scene.lib)
    assert n1 == 2  # 已绑定 movie + series；不许用 item_count=99 假缓存列
    assert scene.rec.item_count_statements()
    scene.rec.clear()
    n2 = api._library_item_count(scene.db, scene.lib)
    assert n2 == 2
    assert scene.rec.item_count_statements() == [], \
        "第二次库计数必须命中计数缓存"
    # 该库版本号 bump 后失效重算（值不变、但必须真的重算）
    idc.bump_library_item_versions(scene.db, [scene.lib.id], force=True)
    scene.db.commit()
    scene.rec.clear()
    n3 = api._library_item_count(scene.db, scene.lib)
    assert n3 == 2
    assert scene.rec.item_count_statements(), "bump 后必须失效重算"


def test_items_total_uses_core_count_not_wrap(tmp_path, fake_backend):
    """_query_items 去重路径的 total：Core COUNT(*)，不得出现包装子查询。"""
    engine = create_engine(f"sqlite:///{tmp_path}/items_shape.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="电影",
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(name, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
            name=name, sort_name=name,
            file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    mk("Alpha", tmdb_id="100", production_year=2001)
    mk("Alpha 4K", tmdb_id="100", production_year=2001)  # 同 tmdb → 被去重
    mk("Beta", tmdb_id="200", production_year=1999)
    user = models.WebUser(username="shape", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    rec = SqlRecorder(engine)
    req = Request({"type": "http", "method": "GET",
                   "query_string": urlencode({}).encode(), "headers": []})
    res = api._query_items(req, user, db, "http://t")
    assert res["TotalRecordCount"] == 2  # 3 行去重掉 1 行
    counts = rec.item_count_statements()
    assert counts, "去重路径应执行了 total 计数"
    wrap = re.compile(r"count\(\*\)\s+as\s+\w+\s+from\s*\(\s*select", re.I)
    assert not any(wrap.search(s) for s in counts), \
        "total 计数不得再用 Query.count() 的子查询包装形态"
    db.close()
    engine.dispose()
