#!/usr/bin/env python3
"""幂等重排脚本：将 enrich_status='done' 且 metadata_source='none' 的条目翻回 pending。

设计约定（2026-11-06 库 4 外语电影修复配套）：
- **默认 dry-run（只读零副作用）**：不传 ``--apply`` 时绝不写库、不清缓存、不联网，
  仅输出命中总数、按库与条目类型分桶计数及抽样明细；
- **严格过滤边界**：只选未软删（``deleted_at IS NULL``）、非合并副本
  （``merged_into_id IS NULL``）、未人工锁定（``metadata_locked IS NOT TRUE``）、
  未绑定 ``tmdb_id``，且同时满足 ``enrich_status == 'done'`` 与
  ``metadata_source == 'none'`` 的行；支持 ``--library-id`` 按库过滤；
- **幂等性**：``--apply`` 将命中行原子翻为 ``enrich_status='pending'``、
  ``metadata_source=None``、``enrich_attempts=0``、``enrich_next_retry_at=None``、
  ``enrich_claimed_at=None``、``enrich_claim_token=None``、``enrich_priority=50``，
  并同步清理旧查询名与新清洗名的 TMDB 搜索阴性磁盘缓存（防 24h 旧无结果缓存拦路）；
  重复执行 ``--apply`` 命中数为 0，严格幂等；全程 **0 次 115 调用**。

用法（容器内）：
  # 1) 默认 dry-run，看库 4 命中计数与样例：
  python scripts/requeue_none_items.py --library-id 4

  # 2) 确认无误后执行重排：
  python scripts/requeue_none_items.py --library-id 4 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import Iterable, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import and_, func, or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.enrich_worker import ENRICH_PRIORITY_RETRY_UNMATCHED  # noqa: E402


def _parse_library_ids(raw_values: Optional[Sequence[str]]) -> list[int]:
    out: list[int] = []
    for raw in raw_values or ():
        for part in str(raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            val = int(part)
            if val not in out:
                out.append(val)
    return out


def _parse_item_types(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def build_none_done_query(
    db,
    *,
    library_ids: Optional[Iterable[int]] = None,
    item_types: Optional[Iterable[str]] = None,
):
    """构造严格匹配 ``enrich_status='done' AND metadata_source='none'`` 的查询。"""
    ws = " \t\n\r\f\v"
    conds = [
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
        em.MediaItem.metadata_locked.isnot(True),
        em.MediaItem.enrich_status == "done",
        em.MediaItem.metadata_source == "none",
        or_(
            em.MediaItem.tmdb_id.is_(None),
            func.trim(em.MediaItem.tmdb_id, ws) == "",
        ),
    ]
    lib_list = [int(x) for x in (library_ids or ()) if x is not None]
    if lib_list:
        conds.append(em.MediaItem.library_id.in_(lib_list))
    type_list = [str(t).strip() for t in (item_types or ()) if str(t).strip()]
    if type_list:
        conds.append(em.MediaItem.item_type.in_(type_list))
    return db.query(em.MediaItem).filter(and_(*conds))


def requeue_none_items(
    db,
    *,
    library_ids: Optional[Sequence[int]] = None,
    item_types: Optional[Sequence[str]] = None,
    apply: bool = False,
    batch_size: int = 500,
    sample_limit: int = 10,
) -> dict:
    """统计或执行 ``done + metadata_source='none'`` 条目重排。"""
    lib_names = {
        row.id: (row.name or f"lib#{row.id}")
        for row in db.query(em.Library.id, em.Library.name).all()
    }
    q = build_none_done_query(db, library_ids=library_ids, item_types=item_types)
    rows = (
        q.with_entities(
            em.MediaItem.id,
            em.MediaItem.library_id,
            em.MediaItem.item_type,
            em.MediaItem.name,
            em.MediaItem.scan_title,
            em.MediaItem.production_year,
        )
        .order_by(em.MediaItem.id.asc())
        .all()
    )

    by_library_type: Counter[tuple[int, str, str]] = Counter()
    for _id, lib_id, itype, _name, _stitle, _year in rows:
        by_library_type[(int(lib_id), lib_names.get(lib_id, f"lib#{lib_id}"), itype or "")] += 1

    samples = [
        {
            "id": int(r[0]),
            "library_id": int(r[1]),
            "item_type": r[2] or "",
            "name": r[3] or "",
            "year": r[5],
        }
        for r in rows[: max(0, int(sample_limit))]
    ]

    report = {
        "mode": "apply" if apply else "dry-run",
        "matched": len(rows),
        "requeued": 0,
        "purged_tmdb_cache": 0,
        "by_library": [
            {
                "library_id": lib_id,
                "library_name": lib_name,
                "item_type": itype,
                "count": cnt,
            }
            for (lib_id, lib_name, itype), cnt in sorted(by_library_type.items())
        ],
        "samples": samples,
    }
    if not apply or not rows:
        db.rollback()
        return report

    ids = [int(r[0]) for r in rows]
    step = max(1, int(batch_size or 500))
    total_updated = 0
    ws = " \t\n\r\f\v"
    for idx in range(0, len(ids), step):
        chunk = ids[idx : idx + step]
        updated = (
            db.query(em.MediaItem)
            .filter(
                em.MediaItem.id.in_(chunk),
                em.MediaItem.deleted_at.is_(None),
                em.MediaItem.merged_into_id.is_(None),
                em.MediaItem.metadata_locked.isnot(True),
                em.MediaItem.enrich_status == "done",
                em.MediaItem.metadata_source == "none",
                or_(
                    em.MediaItem.tmdb_id.is_(None),
                    func.trim(em.MediaItem.tmdb_id, ws) == "",
                ),
            )
            .update(
                {
                    "enrich_status": "pending",
                    "metadata_source": None,
                    "enrich_attempts": 0,
                    "enrich_next_retry_at": None,
                    "enrich_claimed_at": None,
                    "enrich_claim_token": None,
                    "enrich_priority": ENRICH_PRIORITY_RETRY_UNMATCHED,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        total_updated += int(updated or 0)
    report["requeued"] = total_updated

    # 同步清理旧名称与新清洗后的 TMDB 搜索阴性缓存，确保重排后真发请求。
    try:
        from backend.emby_server import tmdb as tmdb_lib
        from backend.emby_server import tmdb_cache

        lang = tmdb_lib.preferred_language()
        keys_to_purge: set[tuple[str, Optional[int], str]] = set()
        for _id, _lib_id, itype, name, stitle, year in rows:
            kind = "series" if itype == "series" else ("movie" if itype == "movie" else "")
            if not kind:
                continue
            for raw_t in (name, stitle):
                if not raw_t:
                    continue
                keys_to_purge.add((raw_t, year, kind))
                cleaned_t = tmdb_lib._clean_query(raw_t)
                if cleaned_t:
                    keys_to_purge.add((cleaned_t, year, kind))
        purged = 0
        for qname, qyear, qkind in keys_to_purge:
            purged += int(tmdb_cache.invalidate_search(qname, qyear, qkind, lang) or 0)
        report["purged_tmdb_cache"] = purged
    except Exception:  # noqa: BLE001 — 缓存清理失败不回滚已提交的重排状态
        pass

    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="幂等重排 enrich_status='done' 且 metadata_source='none' 的条目（默认 dry-run）"
    )
    parser.add_argument(
        "--library-id",
        action="append",
        default=[],
        help="按媒体库 ID 过滤（可多次指定或逗号分隔，如 --library-id 4）",
    )
    parser.add_argument(
        "--item-type",
        default="",
        help="可选条目类型过滤（逗号分隔，如 movie,series；默认不限）",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="只读统计不写库（默认行为）",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="真正把匹配行翻回 pending 并清理旧搜索阴性缓存",
    )
    parser.add_argument("--batch-size", type=int, default=500, help="批量提交大小（默认 500）")
    parser.add_argument("--sample", type=int, default=10, help="输出前 N 条样例（默认 10）")
    args = parser.parse_args(argv)

    library_ids = _parse_library_ids(args.library_id)
    item_types = _parse_item_types(args.item_type)

    db = SessionLocal()
    try:
        report = requeue_none_items(
            db,
            library_ids=library_ids,
            item_types=item_types,
            apply=bool(args.apply),
            batch_size=args.batch_size,
            sample_limit=args.sample,
        )
    finally:
        db.close()

    sys.stdout.write(
        f"[{report['mode']}] matched={report['matched']} requeued={report['requeued']} "
        f"purged_tmdb_cache={report['purged_tmdb_cache']} "
        f"library_filter={library_ids or 'ALL'}\n"
    )
    for bucket in report["by_library"]:
        sys.stdout.write(
            f"  library_id={bucket['library_id']} ({bucket['library_name']}) "
            f"type={bucket['item_type']}: {bucket['count']}\n"
        )
    for s in report["samples"]:
        sys.stdout.write(
            f"  sample id={s['id']} lib={s['library_id']} type={s['item_type']} "
            f"year={s['year']} name={s['name']!r}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
