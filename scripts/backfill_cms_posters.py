#!/usr/bin/env python3
"""CMS 托管行海报存量回填（一次性例程）——源站 vod_pic 只填空。

背景（2026-11-02 图片兼容根因二）：CMS 同步链此前不解析 ``vod_pic``，
托管行海报全靠 TMDB enrich 回填，没对上身份的行永远无图（库 58 带图率
47.6%、动漫/综艺库仅 22%/11%）。同步侧已修复（list/detail 带出
``vod_pic``、落行只填空），存量靠本脚本补：

- 目标行：CMS 库（``kind='applecms'``）的 movie / series 行，
  ``poster_path`` 与 ``primary_image_url`` 皆空、未合并、未软删。
  集行不回填（取图链自会回退剧图）。
- 数据来源：按 ``upstream_item_mappings`` 回源站取 detail 的
  ``vod_pic``（剧行无自有映射时经其集行映射回溯同一 vod）。
  detail 载荷三家实测都带图（非凡 list 不带、detail 带；暴风
  list 即带），故统一走 detail，一行一请求。
- 只填空：写前再查一次两字段仍空才写；已有 TMDB / 本地图的行
  绝不覆盖（与同步侧同一口径）。
- 限速可续跑：``--delay`` 控制请求间隔（默认 0.2 秒）；幂等，
  中断后重跑自动跳过已填行；单源 429 时停该源本轮、其余源继续。

用法（容器里跑）：

    docker exec aetrix-api python scripts/backfill_cms_posters.py            # dry-run 只点数（不联网）
    docker exec aetrix-api python scripts/backfill_cms_posters.py --sample 20  # dry-run + 抽样试取估命中率
    docker exec aetrix-api python scripts/backfill_cms_posters.py --apply     # 真正回填
    docker exec aetrix-api python scripts/backfill_cms_posters.py --apply --limit 500 --library-id 58
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import applecms_fetcher as acf  # noqa: E402
from backend.emby_server import applecms_sync as acs  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.upstream_playback import _source_headers  # noqa: E402


def _missing_poster():
    """双图皆空：poster_path 与 primary_image_url 任一为空不算缺，
    两者都为空（NULL / 空串 / 纯空白）才算待回填。"""
    return (
        or_(em.MediaItem.poster_path.is_(None),
            func.trim(em.MediaItem.poster_path) == "")
        & or_(em.MediaItem.primary_image_url.is_(None),
              func.trim(em.MediaItem.primary_image_url) == ""))


def target_rows(session, *, library_id: int = 0, limit: int = 0):
    """待回填行（id 升序，续跑口径稳定）：CMS 库 movie/series、双图皆空。"""
    cms_lib_ids = [row[0] for row in session.query(em.Library.id)
                   .filter(em.Library.kind == "applecms").all()]
    if library_id:
        cms_lib_ids = [i for i in cms_lib_ids if i == library_id]
    if not cms_lib_ids:
        return []
    q = (session.query(em.MediaItem)
         .filter(em.MediaItem.library_id.in_(cms_lib_ids))
         .filter(em.MediaItem.item_type.in_(("movie", "series")))
         .filter(_missing_poster())
         .filter(em.MediaItem.merged_into_id.is_(None))
         .filter(em.MediaItem.deleted_at.is_(None))
         .order_by(em.MediaItem.id.asc()))
    if limit:
        q = q.limit(limit)
    return q.all()


def resolve_mapping(session, row):
    """行 → (source, vod_id)：自有映射优先（按行 upstream_source_id
    对齐的先取）；剧行无自有映射时经集行映射回溯。找不到返回 None。"""
    maps = (session.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id == row.id)
            .all())
    if not maps and row.item_type == "series":
        ep_ids = [r[0] for r in session.query(em.MediaItem.id)
                  .filter(em.MediaItem.series_id == row.id,
                          em.MediaItem.item_type == "episode").all()]
        if ep_ids:
            maps = (session.query(em.UpstreamItemMapping)
                    .filter(em.UpstreamItemMapping.local_item_id.in_(ep_ids))
                    .all())
    if not maps:
        return None
    own = [m for m in maps
           if int(m.source_id) == int(row.upstream_source_id or 0)]
    chosen = (own or maps)[0]
    source = (session.query(em.UpstreamSource)
              .filter(em.UpstreamSource.id == chosen.source_id).first())
    if source is None or not getattr(source, "base_url", None):
        return None
    return source, str(chosen.upstream_item_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少行（0=不限）")
    parser.add_argument("--library-id", type=int, default=0, help="只回填某个 CMS 库")
    parser.add_argument("--delay", type=float, default=0.2, help="每请求间隔秒（默认 0.2）")
    parser.add_argument("--sample", type=int, default=0,
                        help="dry-run 时抽样试取多少行估命中率（会发请求、不写库）")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = target_rows(session, library_id=args.library_id,
                           limit=args.limit)
        print(f"目标行（双图皆空的 CMS movie/series）: {len(rows)}")
        resolvable = {}
        for row in rows:
            found = resolve_mapping(session, row)
            if found is not None:
                resolvable[row.id] = found
        print(f"可回溯映射的行: {len(resolvable)}；无映射跳过: "
              f"{len(rows) - len(resolvable)}")

        todo = [(row, resolvable[row.id]) for row in rows
                if row.id in resolvable]
        if not args.apply:
            sample = todo[:args.sample] if args.sample else []
            hits = 0
            for row, (source, vod_id) in sample:
                try:
                    detail = acf.fetch_detail(
                        source.base_url, vod_id,
                        headers=_source_headers(source))
                except acf.AppleCMSError:
                    detail = None
                if detail and acs.cms_entry_poster(detail):
                    hits += 1
                if args.delay > 0:
                    time.sleep(args.delay)
            if sample:
                print(f"抽样 {len(sample)} 行：detail 带 vod_pic {hits} 行")
            print("dry-run 结束（未写库）。加 --apply 真正回填。")
            return 0

        stats = {"filled": 0, "no_pic": 0, "errors": 0, "gone": 0}
        dead_sources: set[int] = set()
        for row, (source, vod_id) in todo:
            if source.id in dead_sources:
                continue
            try:
                detail = acf.fetch_detail(
                    source.base_url, vod_id,
                    headers=_source_headers(source))
            except acf.AppleCMSError as exc:
                stats["errors"] += 1
                if exc.kind == "rate_limited":
                    dead_sources.add(source.id)
                    print(f"源 {source.id} 限流，本轮停该源")
                continue
            if detail is None:
                stats["gone"] += 1
                continue
            pic = acs.cms_entry_poster(detail)
            if not pic:
                stats["no_pic"] += 1
                continue
            # 写前复查只填空（同步工人/ enrich 可能刚补上）
            fresh = (session.query(em.MediaItem)
                     .filter(em.MediaItem.id == row.id).first())
            if fresh is None or fresh.poster_path or fresh.primary_image_url:
                continue
            fresh.primary_image_url = pic
            session.commit()
            stats["filled"] += 1
            if args.delay > 0:
                time.sleep(args.delay)
        print(f"回填完成: {stats}")
        remaining = target_rows(session, library_id=args.library_id)
        print(f"仍缺图行: {len(remaining)}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
