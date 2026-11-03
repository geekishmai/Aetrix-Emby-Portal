"""enrich 认领降争用回归（生产数据库发热诊断建议 2）

根因：``_claim_batch`` 的认领分两步——先普通 SELECT 选候选，再一条带
``enrich_status='pending'`` 谓词的 bulk UPDATE 改状态。16 路 worker 的候选
集高度重叠（同一批 pending 头部行），UPDATE 在 PG 上要等先行认领事务提交
才肯放行（READ COMMITTED 的行锁等待 + 谓词重估），而赢家的认领事务要到
整批组处理完才提交——锁等被成倍放大：生产实测认领 UPDATE 在锁队列里
滞留 3 分钟以上、postgres 长期约 284% CPU，在线请求被堵。

修复：候选选取在 PG 上改 ``FOR UPDATE SKIP LOCKED``——先把要认领的行
锁到自己事务里，已被别人锁住的行直接跳过（它们正被认领，不是丢了）；
随后的 UPDATE 只碰自己锁住的行，不再等待。SQLite 无行锁语义，候选选取
保持原样，继续靠原子 UPDATE 谓词兜底（行为逐字不变）。

- ``test_claim_group_not_blocked_by_uncommitted_claim_pg``：真 PG 确定性
  复现——A 认领未提交时 B 认领同一批：旧代码 B 被锁堵住（本测试红），
  修复后 B 立即返回且赢走 0 行（不重复）；A 回滚后 B 能全部认领（不饿死）。
- ``test_claim_batch_concurrent_no_duplicates_pg``：真 PG 8 线程并发
  ``_claim_batch`` 抢 60 条：无重复认领、最终一条不剩（不饿死）。
- ``test_claim_lock_applies_skip_locked_only_for_postgresql``：方言门控
  单元断言——SKIP LOCKED 只出现在 PG 方言的候选 SQL 里，SQLite 不带。
- ``test_claim_batch_sqlite_semantics_unchanged``：SQLite 路径语义与
  修复前一致（分批认领互不重叠、认领完即不再被捞）。

真 PG 测试只有 ``DATABASE_URL`` 指向 postgres 时才跑（CI backend-pg
job 单列一步），否则跳过——SQLite 的写串行化复现不出行锁等待。
"""

import os
import sys
import threading
import time
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Query, sessionmaker

sys.path.insert(0, ".")

from backend.database import Base  # noqa: E402
from backend import models as app_models  # noqa: E402,F401  (system_configs 表)
from backend.emby_server import enrich_worker as ew  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def _pg_url():
    """CI backend-pg job 的 DATABASE_URL；非 PG 返回 None（调用方跳过）。"""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    if url.startswith("postgresql://"):
        # 与 backend/database.py 同一口径：没装 psycopg(v3) 就显式走 psycopg2
        try:
            import psycopg  # noqa: F401
        except ImportError:
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _create_schema(engine):
    """建本测试要用的表（与生产 init_db 同口径：先 pg_trgm 再 create_all）。"""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    tables = [em.Library.__table__, em.MediaItem.__table__,
              app_models.SystemConfig.__table__]
    Base.metadata.create_all(engine, tables=tables, checkfirst=True)


def _seed_movies(Session, n):
    """造一个唯一库 + n 部 pending 电影（各自独立组）；返回 (ids, library_id)。"""
    tag = uuid.uuid4().hex[:10]
    s = Session()
    try:
        lib = em.Library(guid=uuid.uuid4().hex, name=f"claim-race-{tag}")
        s.add(lib)
        s.flush()
        ids = []
        for i in range(n):
            item = em.MediaItem(
                guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
                name=f"claim-race-{tag}-{i}", enrich_status="pending")
            s.add(item)
            s.flush()
            ids.append(item.id)
        s.commit()
        return ids, lib.id
    finally:
        s.close()


def _cleanup(Session, ids, library_id):
    s = Session()
    try:
        s.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
            synchronize_session=False)
        s.query(em.Library).filter(em.Library.id == library_id).delete(
            synchronize_session=False)
        s.commit()
    except Exception:  # noqa: BLE001
        s.rollback()
    finally:
        s.close()


def _cand(sess, ids):
    return (sess.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(ids),
                    em.MediaItem.enrich_status == "pending"))


# ---------------------------------------------------------------------------
# 真 PG：确定性锁等复现
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_pg_url() is None, reason="需要真 PG（DATABASE_URL=postgresql://...）才能复现行锁等待")
def test_claim_group_not_blocked_by_uncommitted_claim_pg():
    engine = create_engine(_pg_url(), isolation_level="READ COMMITTED")
    _create_schema(engine)
    Session = sessionmaker(bind=engine)
    ids, lib_id = _seed_movies(Session, 10)
    try:
        now = datetime.now()
        # A 先认领整批但不提交：模拟赢家认领事务未结束的窗口
        sa = Session()
        won_a = ew._claim_group_atomic(sa, _cand(sa, ids), now)
        assert len(won_a) == 10

        outcome = {}

        def claim_b():
            sb = Session()
            try:
                t0 = time.monotonic()
                outcome["won"] = ew._claim_group_atomic(sb, _cand(sb, ids), now)
                outcome["dur"] = time.monotonic() - t0
                sb.rollback()
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                sb.close()

        bt = threading.Thread(target=claim_b, daemon=True)
        bt.start()
        bt.join(timeout=10)
        blocked = bt.is_alive()
        # 放行 A，避免红灯时 B 线程永久挂住测试进程
        sa.rollback()
        sa.close()
        bt.join(timeout=30)

        assert not blocked, "第二个认领被第一个未提交的认领事务堵住（锁等）"
        assert "error" not in outcome, outcome
        # A 锁住的行一张都没被 B 抢走（不重复认领）
        assert outcome["won"] == [], outcome
        # 不饿死：A 回滚后行回到 pending，B 能全部认领
        sb2 = Session()
        try:
            won2 = ew._claim_group_atomic(sb2, _cand(sb2, ids), now)
            assert len(won2) == 10
            sb2.commit()
        finally:
            sb2.close()
    finally:
        _cleanup(Session, ids, lib_id)
        engine.dispose()


# ---------------------------------------------------------------------------
# 真 PG：多线程 _claim_batch 并发
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_pg_url() is None, reason="需要真 PG（DATABASE_URL=postgresql://...）才能复现行锁等待")
def test_claim_batch_concurrent_no_duplicates_pg():
    engine = create_engine(_pg_url(), isolation_level="READ COMMITTED")
    _create_schema(engine)
    Session = sessionmaker(bind=engine)
    ids, lib_id = _seed_movies(Session, 60)
    try:
        barrier = threading.Barrier(8)
        claimed_by_thread = {}
        errors = {}

        def worker(tag):
            sess = Session()
            mine = []
            try:
                barrier.wait(timeout=30)
                # 与 _worker_loop 同形：循环认领直到捞空（单轮上限防失控）
                for _ in range(40):
                    batch = ew._claim_batch(sess, 5)
                    if not batch:
                        break
                    mine.extend(it.id for it in batch)
                claimed_by_thread[tag] = mine
            except Exception as exc:  # noqa: BLE001
                errors[tag] = f"{type(exc).__name__}: {exc}"
            finally:
                sess.close()

        threads = [threading.Thread(target=worker, args=(f"t{i}",), daemon=True)
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "worker 卡住（疑似锁等）"
        assert not errors, errors

        all_claimed = [i for mine in claimed_by_thread.values() for i in mine]
        # 不重复：同一条目绝不被两个 worker 同时认领
        assert len(all_claimed) == len(set(all_claimed)), "出现重复认领"
        # 不饿死：60 条最终全部被认领走
        assert set(all_claimed) == set(ids), (
            f"认领 {len(all_claimed)}/60，未认领 "
            f"{sorted(set(ids) - set(all_claimed))[:10]}")
    finally:
        _cleanup(Session, ids, lib_id)
        engine.dispose()


# ---------------------------------------------------------------------------
# 方言门控（无需真 PG，处处可跑）
# ---------------------------------------------------------------------------

def test_claim_lock_applies_skip_locked_only_for_postgresql():
    base = Query(em.MediaItem.id)
    pg_sql = str(ew._apply_claim_lock(base, "postgresql").statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE SKIP LOCKED" in pg_sql
    lite_sql = str(ew._apply_claim_lock(base, "sqlite").statement.compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" not in lite_sql


# ---------------------------------------------------------------------------
# SQLite 路径语义不变
# ---------------------------------------------------------------------------

def test_claim_batch_sqlite_semantics_unchanged(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'enrich_claim_sqlite.db'}")
    _create_schema(engine)
    Session = sessionmaker(bind=engine)
    ids, lib_id = _seed_movies(Session, 12)
    try:
        s1 = Session()
        s2 = Session()
        try:
            batch1 = ew._claim_batch(s1, 5)
            batch2 = ew._claim_batch(s2, 5)
            got1 = {it.id for it in batch1}
            got2 = {it.id for it in batch2}
            assert len(got1) == 5 and len(got2) == 5
            assert got1.isdisjoint(got2), "SQLite 路径出现重复认领"
            batch3 = ew._claim_batch(s1, 5)
            assert {it.id for it in batch3} == set(ids) - got1 - got2
            assert ew._claim_batch(s1, 5) == []
        finally:
            s1.close()
            s2.close()
    finally:
        _cleanup(Session, ids, lib_id)
        engine.dispose()
