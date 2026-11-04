"""补资料轻量化派活三刀（P1/P2/P3）回归兜底。

背景（2026-11-04 生产诊断）：认领路径四类 SQL 占全库语句时间 72.2%，
贵在三处——候选 SELECT 用 coalesce(series_id, id) 表达式当组键走不了
索引、令牌查回列无索引全表扫、组数上限 20 让批次装不满（实测 54/200）。

钉死：
① P1 组员谓词等价：父行（series/movie，series_id 恒 NULL）＋季＋单集
   ＋series_id 为 NULL 的孤儿单集，认领集合与 coalesce 旧口径逐行一致；
   已 done / 已合并 / 已锁定 / 未到重试时间的行一概不进批次；
   与 #1834 的 parent_ready 叠加：父级未终态时单集被挡在批次外，
   父级落 done 后同组单集在下一轮被认领（谓词叠加仍等价）；
② P3 批次装满：单行组（电影）超过 20 个时，一轮认领越过旧 limit(20)
   把批次装到 limit 或组耗尽；limit 小于组数时恰好认领 limit 行；
③ P2 查回作用域：_claim_group_atomic 返回的行恰好是本批 UPDATE 真正
   改动到的行（pending → enriching 且令牌为本批令牌），别批令牌占着的
   行不被捞回、不被改动。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker
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


@pytest.fixture()
def lib(db):
    row = em.Library(guid=uuid.uuid4().hex, name="测试库",
                     collection_type="tvshows")
    db.add(row)
    db.flush()
    return row


def _item(db, lib, name, item_type="movie", **kw):
    kw.setdefault("enrich_status", "pending")
    row = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, sort_name=name,
        file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
    db.add(row)
    db.flush()
    return row


# ---------- ① P1 等价：组员集合与 coalesce 旧口径逐行一致 ----------

def test_claim_set_matches_coalesce_semantics(db, lib):
    series = _item(db, lib, "剧", item_type="series")
    season = _item(db, lib, "剧 S1", item_type="season", series_id=series.id,
                   season_number=1)
    ep1 = _item(db, lib, "剧 E1", item_type="episode", series_id=series.id,
                season_number=1, episode_number=1)
    ep2 = _item(db, lib, "剧 E2", item_type="episode", series_id=series.id,
                season_number=1, episode_number=2)
    movie = _item(db, lib, "电影")
    # 孤儿单集：series_id 为 NULL，旧口径下自成一组（coalesce = 自身 id）
    orphan = _item(db, lib, "孤儿集", item_type="episode")
    # 不该被认领的：已完成、已合并、已锁定、未到重试时间
    done = _item(db, lib, "已完成", enrich_status="done")
    merged = _item(db, lib, "副本", merged_into_id=movie.id)
    locked = _item(db, lib, "锁定", metadata_locked=True)
    from datetime import datetime, timedelta
    future = _item(db, lib, "未到期",
                   enrich_next_retry_at=datetime.now() + timedelta(hours=1))
    db.commit()

    # 第一轮：父级未终态，#1834 的 parent_ready 把单集挡在批次外；
    # 其余组员（父级、季、电影、孤儿集）与 coalesce 旧口径一致。
    claimed = enrich_worker._claim_batch(db, 100)
    claimed_ids = [row.id for row in claimed]
    assert set(claimed_ids) == {series.id, season.id, movie.id, orphan.id}
    for excluded in (ep1, ep2, done, merged, locked, future):
        assert excluded.id not in claimed_ids
    # 组内顺序：父级在子级之前（父先落 done、子集才能纯继承）
    assert claimed_ids.index(series.id) < claimed_ids.index(season.id)

    # 父级落 done 后，同一组的单集在下一轮被认领（P1 谓词与 parent_ready
    # 叠加仍等价；单集按集号顺序）。
    series_row = db.query(em.MediaItem).filter(
        em.MediaItem.id == series.id).first()
    series_row.enrich_status = "done"
    db.commit()
    claimed2 = enrich_worker._claim_batch(db, 100)
    assert [row.id for row in claimed2] == [ep1.id, ep2.id]


def test_claim_group_atomic_returns_only_own_wins(db, lib):
    # ③ P2：候选里混着一行已被别批令牌占走（enriching）的行——
    # 查回只能带回本批真正从 pending 改成 enriching 的行。
    mine = _item(db, lib, "待认领")
    others = _item(db, lib, "已被占", enrich_status="enriching",
                   enrich_claim_token="other-batch-token")
    db.commit()
    from datetime import datetime
    q = (db.query(em.MediaItem)
         .filter(em.MediaItem.id.in_([mine.id, others.id]))
         .order_by(em.MediaItem.id))
    rows = enrich_worker._claim_group_atomic(db, q, datetime.now())
    assert [row.id for row in rows] == [mine.id]
    db.expire_all()
    other_row = db.query(em.MediaItem).filter(
        em.MediaItem.id == others.id).first()
    assert other_row.enrich_status == "enriching"
    assert other_row.enrich_claim_token == "other-batch-token"
    mine_row = db.query(em.MediaItem).filter(
        em.MediaItem.id == mine.id).first()
    assert mine_row.enrich_status == "enriching"
    assert mine_row.enrich_claim_token not in (None, "other-batch-token")


# ---------- ② P3 批次装满 ----------

def test_claim_batch_fills_beyond_twenty_groups(db, lib):
    movies = [_item(db, lib, f"电影{i}") for i in range(60)]
    db.commit()
    claimed = enrich_worker._claim_batch(db, 200)
    assert {row.id for row in claimed} == {m.id for m in movies}


def test_claim_batch_respects_limit_across_group_fetches(db, lib):
    for i in range(60):
        _item(db, lib, f"电影{i}")
    db.commit()
    claimed = enrich_worker._claim_batch(db, 25)
    assert len(claimed) == 25
    # 余下的仍是 pending，下一轮能继续认领（递增取组不丢任务）
    claimed2 = enrich_worker._claim_batch(db, 200)
    assert len(claimed2) == 35
