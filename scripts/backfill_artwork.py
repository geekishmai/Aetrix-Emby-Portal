#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_artwork.py — 存量条目透明 Logo / 无字 Backdrop / 中文横卡 Thumb / 季海报回填脚本。

口径与红线：
  - 目标条目：item_type in ('movie', 'series')、tmdb_id 非空、未合并、未软删，
    且尚无 Logo（logo_path 与 logo_image_url 均空）或名下存在无图 season 的条目。
  - 只调 TMDB（tmdb_client.details(..., require_images=True) + tmdb_client.season_poster，
    复用进程 L1 + 磁盘 L2 缓存与现有令牌桶限速），**绝不导入或调用任何 115 接口**。
  - 默认 dry-run：只查库 + 只读检查本地 TMDB 磁盘详情缓存（是否已含 images 扩展块），
    打印待补计数与预计 TMDB HTTP 请求数，零网络请求、零写库。
  - 传入 --apply 才真正拉取并写库，默认每 50 条 commit 一次；已有自定义本地图不覆盖，
    默认将旧带字 Backdrop 升级为 TMDB 无字纯净背景（可用 --no-upgrade-backdrop 关闭）。
  - 不读取或打印任何密钥/凭据原文。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, Optional, Sequence


def _bootstrap_app_path() -> None:
    candidates = ["/app", os.getcwd()]
    if "__file__" in globals():
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.extend([here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))])
    for path in candidates:
        if path and os.path.isdir(os.path.join(path, "backend")) and path not in sys.path:
            sys.path.insert(0, path)
            break


_bootstrap_app_path()

from sqlalchemy import and_, exists, func, or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import tmdb_cache  # noqa: E402
from backend.emby_server.enrich_worker import _apply_season_posters  # noqa: E402
from backend.emby_server.tmdb import (  # noqa: E402
    _has_credits,
    _has_images,
    apply_detail_artwork,
    preferred_language,
    tmdb_client,
)

DEFAULT_BATCH_SIZE = 50


def _col_empty(col):
    return or_(col.is_(None), func.trim(col) == "")


def _resolve_kinds(kind_arg: str) -> tuple[str, ...]:
    return ("movie", "series") if kind_arg == "all" else (kind_arg,)


def _unpictured_season_exists_clause():
    season_alias = em.MediaItem.__table__.alias("season_child")
    return exists().where(
        and_(
            season_alias.c.series_id == em.MediaItem.id,
            season_alias.c.item_type == "season",
            season_alias.c.season_number.isnot(None),
            season_alias.c.merged_into_id.is_(None),
            season_alias.c.deleted_at.is_(None),
            or_(season_alias.c.poster_path.is_(None), func.trim(season_alias.c.poster_path) == ""),
            or_(
                season_alias.c.primary_image_url.is_(None),
                func.trim(season_alias.c.primary_image_url) == "",
            ),
        )
    )


def _target_query(session, kinds: Sequence[str], library_id: int = 0):
    missing_logo = and_(_col_empty(em.MediaItem.logo_path), _col_empty(em.MediaItem.logo_image_url))
    need_season = and_(em.MediaItem.item_type == "series", _unpictured_season_exists_clause())
    q = (
        session.query(em.MediaItem)
        .filter(em.MediaItem.item_type.in_(tuple(kinds)))
        .filter(em.MediaItem.tmdb_id.isnot(None))
        .filter(func.trim(em.MediaItem.tmdb_id) != "")
        .filter(em.MediaItem.merged_into_id.is_(None))
        .filter(em.MediaItem.deleted_at.is_(None))
        .filter(or_(missing_logo, need_season))
    )
    if library_id:
        q = q.filter(em.MediaItem.library_id == library_id)
    return q


def _missing_seasons_for_series(session, series_id: int) -> list[tuple[int, int]]:
    rows = (
        session.query(em.MediaItem.id, em.MediaItem.season_number)
        .filter(
            em.MediaItem.series_id == series_id,
            em.MediaItem.item_type == "season",
            em.MediaItem.season_number.isnot(None),
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.deleted_at.is_(None),
            _col_empty(em.MediaItem.poster_path),
            _col_empty(em.MediaItem.primary_image_url),
        )
        .order_by(em.MediaItem.season_number.asc(), em.MediaItem.id.asc())
        .all()
    )
    return [(int(sid), int(sno)) for sid, sno in rows if sno is not None]


def inspect_plan(
    session,
    *,
    kinds: Sequence[str] = ("movie", "series"),
    library_id: int = 0,
    limit: int = 0,
) -> dict:
    q = (
        _target_query(session, kinds, library_id)
        .with_entities(
            em.MediaItem.id,
            em.MediaItem.item_type,
            em.MediaItem.tmdb_id,
            em.MediaItem.logo_path,
            em.MediaItem.logo_image_url,
        )
        .order_by(em.MediaItem.id.asc())
    )
    if limit > 0:
        q = q.limit(limit)
    rows = q.all()

    by_kind = {"movie": 0, "series": 0}
    missing_logo_count = 0
    unique_detail_pairs: set[tuple[str, str]] = set()
    unique_season_keys: set[tuple[str, int]] = set()
    missing_season_rows = 0

    for item_id, item_type, tmdb_id, logo_path, logo_url in rows:
        k = str(item_type or "")
        tid = str(tmdb_id or "").strip()
        if not tid:
            continue
        by_kind[k] = by_kind.get(k, 0) + 1
        no_logo = not ((logo_path or "").strip() or (logo_url or "").strip())
        if no_logo:
            missing_logo_count += 1
            unique_detail_pairs.add((k, tid))
        if k == "series":
            s_list = _missing_seasons_for_series(session, int(item_id))
            missing_season_rows += len(s_list)
            for _sid, sno in s_list:
                unique_season_keys.add((tid, sno))

    lang = preferred_language(session)
    detail_disk_hits = 0
    for k, tid in unique_detail_pairs:
        endpoint = "tv" if k == "series" else "movie"
        try:
            hit, data = tmdb_cache.load_details(endpoint, tid, lang)
            if hit and _has_credits(data) and _has_images(data):
                detail_disk_hits += 1
        except Exception:  # noqa: BLE001
            pass

    season_disk_hits = 0
    for tid, sno in unique_season_keys:
        try:
            hit, data = tmdb_cache.load_details("tv_season", f"{tid}:{sno}", "")
            if hit and isinstance(data, dict) and data.get("id") is not None:
                season_disk_hits += 1
        except Exception:  # noqa: BLE001
            pass

    est_detail_http = max(0, len(unique_detail_pairs) - detail_disk_hits)
    est_season_http = max(0, len(unique_season_keys) - season_disk_hits)
    return {
        "total": len(rows),
        "movies": by_kind.get("movie", 0),
        "series": by_kind.get("series", 0),
        "missing_logo": missing_logo_count,
        "missing_season_rows": missing_season_rows,
        "unique_detail_pairs": len(unique_detail_pairs),
        "detail_disk_hits": detail_disk_hits,
        "unique_season_keys": len(unique_season_keys),
        "season_disk_hits": season_disk_hits,
        "estimated_http_requests": est_detail_http + est_season_http,
    }


def backfill_artwork(
    session,
    *,
    kinds: Sequence[str] = ("movie", "series"),
    library_id: int = 0,
    dry_run: bool = True,
    limit: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    upgrade_backdrop: bool = True,
    details_fetcher: Optional[Callable[[str, str], Optional[dict]]] = None,
    season_poster_fetcher: Optional[Callable[[str, int], Optional[str]]] = None,
) -> dict:
    if batch_size <= 0:
        raise ValueError("batch_size must be >= 1")

    plan = inspect_plan(session, kinds=kinds, library_id=library_id, limit=limit)
    stats = {
        **plan,
        "processed": 0,
        "logos_filled": 0,
        "thumbs_filled": 0,
        "backdrops_updated": 0,
        "seasons_filled": 0,
        "no_logo_in_tmdb": 0,
        "errors": 0,
        "commits": 0,
    }
    if dry_run or plan["total"] == 0:
        return stats

    if details_fetcher is None:
        details_fetcher = lambda tid, kind: tmdb_client.details(tid, kind, require_images=True)
    if season_poster_fetcher is None:
        season_poster_fetcher = tmdb_client.season_poster

    id_q = (
        _target_query(session, kinds, library_id)
        .with_entities(em.MediaItem.id)
        .order_by(em.MediaItem.id.asc())
    )
    if limit > 0:
        id_q = id_q.limit(limit)
    target_ids = [int(i) for (i,) in id_q.all()]

    uncommitted = 0
    for idx in range(0, len(target_ids), batch_size):
        chunk_ids = target_ids[idx : idx + batch_size]
        rows = (
            session.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(chunk_ids))
            .order_by(em.MediaItem.id.asc())
            .all()
        )
        for row in rows:
            stats["processed"] += 1
            tid = str(row.tmdb_id or "").strip()
            kind = str(row.item_type or "movie")
            need_logo = not ((row.logo_path or "").strip() or (row.logo_image_url or "").strip())
            if need_logo:
                try:
                    details = details_fetcher(tid, kind)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    print(
                        f"  ! id={row.id} kind={kind} tmdb_id={tid} details 失败: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    details = None
                if details:
                    changed = apply_detail_artwork(
                        row, details, upgrade_backdrop=upgrade_backdrop
                    )
                    if "logo_image_url" in changed:
                        stats["logos_filled"] += 1
                    else:
                        stats["no_logo_in_tmdb"] += 1
                    if "thumb_image_url" in changed:
                        stats["thumbs_filled"] += 1
                    if "backdrop_image_url" in changed:
                        stats["backdrops_updated"] += 1

            if kind == "series":
                s_missing = _missing_seasons_for_series(session, row.id)
                got_posters: dict[int, str] = {}
                for sid, sno in s_missing:
                    try:
                        s_url = season_poster_fetcher(tid, sno)
                    except Exception as exc:  # noqa: BLE001
                        stats["errors"] += 1
                        print(
                            f"  ! series_id={row.id} tmdb_id={tid} S{sno} 季海报失败: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        continue
                    if s_url:
                        got_posters[sid] = s_url
                if got_posters:
                    stats["seasons_filled"] += _apply_season_posters(session, got_posters)

            uncommitted += 1
            if uncommitted >= batch_size:
                session.commit()
                stats["commits"] += 1
                uncommitted = 0
                print(
                    f"[APPLY] 进度 {stats['processed']}/{len(target_ids)} "
                    f"(logo={stats['logos_filled']}, backdrop={stats['backdrops_updated']}, "
                    f"thumb={stats['thumbs_filled']}, season={stats['seasons_filled']}, "
                    f"commits={stats['commits']})",
                    flush=True,
                )

    if uncommitted > 0:
        session.commit()
        stats["commits"] += 1

    return stats


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="回填存量条目透明 Logo / 无字 Backdrop / 中文 Thumb / 季海报（默认 dry-run，只调 TMDB）"
    )
    p.add_argument("--apply", action="store_true", help="真正请求 TMDB 并写库（每 50 条 commit）")
    p.add_argument("--kind", choices=("all", "movie", "series"), default="all")
    p.add_argument("--library-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--no-upgrade-backdrop",
        action="store_true",
        help="保留已有带字 Backdrop，只填空缺",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    kinds = _resolve_kinds(args.kind)
    mode = "APPLY" if args.apply else "DRY-RUN"
    t0 = time.monotonic()

    db = SessionLocal()
    try:
        key_info = tmdb_client.refresh_keys(db)
        plan = inspect_plan(db, kinds=kinds, library_id=args.library_id, limit=args.limit)
        rate_info = tmdb_client.stats()
        print(
            f"[{mode}] 目标范围 kind={args.kind} library_id={args.library_id or 'all'}"
            f"{f' limit={args.limit}' if args.limit else ''}:\n"
            f"  待处理条目: {plan['total']} 条 (movie={plan['movies']}, series={plan['series']}; "
            f"缺 Logo={plan['missing_logo']} 条, 缺自有季海报={plan['missing_season_rows']} 季)\n"
            f"  详情去重对: {plan['unique_detail_pairs']} 个 (含 images 磁盘命中={plan['detail_disk_hits']}) | "
            f"季海报去重: {plan['unique_season_keys']} 个 (磁盘命中={plan['season_disk_hits']})\n"
            f"  预计发往 TMDB 的 HTTP 请求数: {plan['estimated_http_requests']} 次 "
            f"(key 池 count={key_info.get('count', 0)} source={key_info.get('source', 'none')}, "
            f"令牌桶 rate={rate_info.get('rate')}/s, 115 调用=0)",
            flush=True,
        )

        if not args.apply:
            print("[DRY-RUN] 未发网络请求、未写库；确认后加 --apply 执行。", flush=True)
            return 0

        if plan["total"] == 0:
            print("[APPLY] 无需回填条目，退出。", flush=True)
            return 0

        if plan["estimated_http_requests"] > 0 and not tmdb_client.configured:
            print(
                f"[APPLY] 错误：需发起 {plan['estimated_http_requests']} 次 TMDB 请求，"
                "但当前无可用 TMDB API Key，拒绝执行。",
                file=sys.stderr,
                flush=True,
            )
            return 2

        stats = backfill_artwork(
            db,
            kinds=kinds,
            library_id=args.library_id,
            dry_run=False,
            limit=args.limit,
            batch_size=args.batch_size,
            upgrade_backdrop=not args.no_upgrade_backdrop,
        )
    finally:
        db.close()

    elapsed = time.monotonic() - t0
    print(
        f"[APPLY] 完成：处理 {stats['processed']}/{stats['total']} 条 | "
        f"补 Logo {stats['logos_filled']} 条 (TMDB 无 Logo {stats['no_logo_in_tmdb']} 条) | "
        f"升级/补无字 Backdrop {stats['backdrops_updated']} 条 | "
        f"补中文横卡 Thumb {stats['thumbs_filled']} 条 | "
        f"补季海报 {stats['seasons_filled']} 季 | "
        f"错误 {stats['errors']} | commit {stats['commits']} 批 | 耗时 {elapsed:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
