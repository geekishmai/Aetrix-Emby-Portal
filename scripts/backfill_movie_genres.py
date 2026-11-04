#!/usr/bin/env python3
"""类型（genres）存量回填（一次性例程）——P0-3 + CMS 托管行批

历史背景（P0-3）：``apply_details()`` 曾完全忽略详情接口的 ``genres``
字段，走「tmdb_id → 详情」分支的电影永远拿不到类型，代码修复后用
本脚本补存量。

2026-11-02 扩展背景：CMS 托管行 enrich 不写 genres 定案——托管行无
本地文件、不走扫描，只走 enrich 两段式写库，而补资料段白名单此前
不含 genres（代码已在 identify_two_stage 修复）。存量（库 58 有
tmdb_id 的 1,614 行全缺类型）靠本脚本回填，并顺带支持剧集与按库
定向。缺 genres 的目标行 = 有 tmdb_id 且 genres 为空、未合并、
未软删的行（合并/软删行不可见，不浪费回填）。

数据源两级，都不做搜索、不猜身份：
1. **donor（优先，纯本地零请求）**：同 tmdb_id、同类型、已有
   genres 的已落库行——本地库与托管库同片同源，类型直接抄已落库
   值（那些值本就是 apply/apply_details 按同一口径写的）。
2. **TMDB 详情缓存**：donor 覆盖不到的才走 ``tmdb_client.details()``，
   它自带两级缓存（进程 300 秒 + 磁盘 30 天）与令牌桶限速：enrich/
   扫描阶段拉过的详情绝大多数还在磁盘缓存里、命中不产生新请求；
   没命中缓存的才走令牌桶慢慢补。
没 tmdb_id 的行不在本脚本范围（那是 enrich 搜索命中的事）；
CMS 行没对上 TMDB 的，类型由源站分类名填充（多源填充单）接管。

用法（容器里 / 工作区都行）：

    docker exec aetrix-api python scripts/backfill_movie_genres.py          # 先 dry-run 看数
    docker exec aetrix-api python scripts/backfill_movie_genres.py --apply  # 真正写库
    docker exec aetrix-api python scripts/backfill_movie_genres.py --apply --limit 500
    docker exec aetrix-api python scripts/backfill_movie_genres.py --library-id 58 --apply
    docker exec aetrix-api python scripts/backfill_movie_genres.py --kind series --apply

幂等：只碰 genres 为空的条目，可反复跑。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.tmdb import _genres_from_details, tmdb_client  # noqa: E402


def _missing_genres():
    """genres 为空的过滤条件：NULL / 空串 / 纯空白都算缺"""
    return or_(em.MediaItem.genres.is_(None),
               func.trim(em.MediaItem.genres) == "")


def _target_query(session, kind: str, library_id: int = 0):
    """待回填目标：有 tmdb_id、缺类型、未合并、未软删（可按库定向）。"""
    q = (session.query(em.MediaItem)
         .filter(em.MediaItem.item_type == kind)
         .filter(em.MediaItem.tmdb_id.isnot(None))
         .filter(_missing_genres())
         .filter(em.MediaItem.merged_into_id.is_(None))
         .filter(em.MediaItem.deleted_at.is_(None)))
    if library_id:
        q = q.filter(em.MediaItem.library_id == library_id)
    return q


def donor_genres(session, *, kind: str = "movie") -> dict:
    """donor 映射：{tmdb_id(str): genres}，同类型已有类型的行。

    纯本地数据源、零网络请求。同 tmdb_id 多行类型不一致时取 id
    最小的一行（确定性口径）；空白类型、合并行、软删行、无身份行
    都不作 donor。
    """
    rows = (session.query(em.MediaItem.tmdb_id, em.MediaItem.genres)
            .filter(em.MediaItem.item_type == kind)
            .filter(em.MediaItem.tmdb_id.isnot(None))
            .filter(em.MediaItem.genres.isnot(None))
            .filter(func.trim(em.MediaItem.genres) != "")
            .filter(em.MediaItem.merged_into_id.is_(None))
            .filter(em.MediaItem.deleted_at.is_(None))
            .order_by(em.MediaItem.id)
            .all())
    out: dict = {}
    for tmdb_id, genres in rows:
        key = str(tmdb_id)
        if key not in out and (genres or "").strip():
            out[key] = genres
    return out


def backfill_genres(session, *, kind: str = "movie", library_id: int = 0,
                    dry_run: bool = True, limit: int = 0,
                    batch_size: int = 200, details_fetcher=None) -> dict:
    """回填缺类型行，返回统计 dict。

    stats 键：total（目标数）/ donor_filled / details_filled /
    still_missing（两级数据源都没有类型）/ errors（details 拉取异常）。
    dry_run 时只计数不写库（donor 计数为「可补」，details 仍会真调
    取数口径计数，与旧版 dry-run 行为一致）。apply 时只填空：
    写前复查仍为空才写，永不覆盖。
    details_fetcher 签名 (tmdb_id: str, kind: str) -> dict | None，
    默认 tmdb_client.details（两级缓存 + 令牌桶）。
    """
    if details_fetcher is None:
        details_fetcher = tmdb_client.details
    donors = donor_genres(session, kind=kind)
    stats = {"total": 0, "donor_filled": 0, "details_filled": 0,
             "still_missing": 0, "errors": 0}
    total = _target_query(session, kind, library_id).count()
    if limit:
        total = min(total, limit)
    stats["total"] = total
    if not total:
        return stats

    done = 0
    last_id = 0
    while done < total:
        batch = (_target_query(session, kind, library_id)
                 .filter(em.MediaItem.id > last_id)
                 .order_by(em.MediaItem.id)
                 .limit(batch_size)
                 .all())
        if not batch:
            break
        for row in batch:
            last_id = row.id
            done += 1
            text = donors.get(str(row.tmdb_id))
            if text:
                stats["donor_filled"] += 1
            else:
                try:
                    data = details_fetcher(str(row.tmdb_id), kind)
                except Exception as exc:  # noqa: BLE001 — 单条失败不拦整批
                    stats["errors"] += 1
                    print(f"  ! id={row.id} tmdb_id={row.tmdb_id} "
                          f"详情拉取失败：{exc}")
                    continue
                gnames = _genres_from_details(data or {})
                if gnames:
                    text = ",".join(gnames)
                    stats["details_filled"] += 1
                else:
                    stats["still_missing"] += 1
            if text and not dry_run and not (row.genres or "").strip():
                row.genres = text
        if not dry_run:
            session.commit()
    return stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="回填有 tmdb_id 但缺类型的条目")
    p.add_argument("--apply", action="store_true",
                   help="真正写库；不加则只 dry-run 统计")
    p.add_argument("--kind", choices=("movie", "series"), default="movie",
                   help="条目类型（默认 movie；剧集传 series，类型口径按类型隔离）")
    p.add_argument("--library-id", type=int, default=0,
                   help="只回填指定库（0=全库，如 CMS 试点库传 58）")
    p.add_argument("--limit", type=int, default=0,
                   help="最多处理多少条（0=不限），dry-run 时同样生效")
    p.add_argument("--batch-size", type=int, default=200,
                   help="每多少条提交一次（默认 200）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    t0 = time.time()
    try:
        total = _target_query(db, args.kind, args.library_id).count()
        if args.limit:
            total = min(total, args.limit)
        donors = donor_genres(db, kind=args.kind)
        target_ids = [tid for (tid,) in _target_query(
            db, args.kind, args.library_id)
            .with_entities(em.MediaItem.tmdb_id).all()]
        covered = sum(1 for tid in target_ids if str(tid) in donors)
        uncovered = total - covered
        if uncovered > 0 and not tmdb_client.configured:
            print("TMDB 未配置（无可用 key），donor 覆盖不到的 "
                  f"{uncovered} 条无法拉详情，退出。")
            return 2
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] 待补类型（kind={args.kind}，"
              f"library_id={args.library_id or '全库'}）：{total} 条"
              f"（donor 可补 {covered} 条）")
        if not total:
            return 0
        stats = backfill_genres(
            db, kind=args.kind, library_id=args.library_id,
            dry_run=not args.apply, limit=args.limit,
            batch_size=args.batch_size)
    finally:
        db.close()

    print(f"[{mode}] 完成：共 {stats['total']} 条，donor 补 "
          f"{stats['donor_filled']} 条，详情补 {stats['details_filled']} 条，"
          f"仍缺 {stats['still_missing']} 条，失败 {stats['errors']} 条，"
          f"用时 {time.time() - t0:.0f}s")
    if not args.apply:
        print("dry-run 未写库；确认数字后加 --apply 真正执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
