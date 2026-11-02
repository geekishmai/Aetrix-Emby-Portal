#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""苹果 CMS 智能补源例程·电影版（库 58 存量电影行补其它源映射）。

背景与口径见 ``backend/emby_server/applecms_backfill_movies.py``
模块头：与剧集版同一套多钥匙搜索 + 多信号打分三档裁决 + 补前
验活 + 质量门 + 审计留痕，差异只有电影形态三处（行自身映射读
口、无集数维度改单单元形态校验、补写只加映射不建行）。

用法（容器里 / 工作区都行，默认干跑、不写库）：

    python3 scripts/applecms_backfill_movies.py                  # 干跑库 58 全部不可播电影
    python3 scripts/applecms_backfill_movies.py --apply          # 正式补写
    python3 scripts/applecms_backfill_movies.py --row-ids 982040,982041 --apply

审计结果（条目级 JSON）用 ``--json-out 路径`` 落盘：每行补了哪
个源、总分与分项、命中钥匙；被拒候选及原因；未命中原因分类。
错挂事后按 item_id 查这份 JSON 即可定位、按行删映射回滚。

安全口径（与剧集版一致）：同镜像一次性容器跑、不重启任何常驻
容器；逐（行×源）提交；只写映射，不动库配置、不删存量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("UPSTREAM_ITEM_MAPPING_ENABLED", "1")

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import applecms_backfill as bf  # noqa: E402
from backend.emby_server import applecms_backfill_movies as bfm  # noqa: E402
from backend.emby_server import models as em  # noqa: E402

#: 库 → 期望地区桶。库 58「在线电影」是地区混合库（地区细拆三
#: 库未落地），不设期望桶——地区信号如实降权，不拿混合库猜桶。
#: 细拆落地后在新库 id 上按 cn/west/jpkr 补登即可。
LIB_AREA_BUCKETS: dict = {}


def _tmdb_details(tmdb_id: str):
    """enrich 同口径的 TMDB 详情（带两级缓存）；失败降级为 None。"""
    try:
        from backend.emby_server.tmdb import tmdb_client
        return tmdb_client.details(str(tmdb_id), "movie")
    except Exception:  # noqa: BLE001 - 身份补强失败不许拖垮补源
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="苹果 CMS 智能补源·电影版")
    parser.add_argument("--library-ids", default="58",
                        help="逗号分隔的库 id（默认 58）")
    parser.add_argument("--row-ids", default="",
                        help="逗号分隔的电影行 id；给了就只补这些行")
    parser.add_argument("--apply", action="store_true",
                        help="正式补写（默认干跑）")
    parser.add_argument("--max-new-sources-per-row", type=int,
                        default=bf.DEFAULT_MAX_NEW_SOURCES_PER_ROW)
    parser.add_argument("--delay", type=float, default=0.2,
                        help="补上一个源后的间隔秒数（默认 0.2）")
    parser.add_argument("--json-out", default="",
                        help="审计 JSON 落盘路径")
    args = parser.parse_args()

    library_ids = [int(x) for x in args.library_ids.split(",") if x.strip()]
    db = SessionLocal()
    try:
        sources = bf.enabled_applecms_sources(db)
        enabled_ids = [s.id for s in sources]
        print(f"ENABLED_SOURCES {enabled_ids}", flush=True)
        grand = {"stats": None, "audit": []}
        for lib_id in library_ids:
            library = db.get(em.Library, lib_id)
            if library is None:
                print(f"LIB {lib_id} MISSING", flush=True)
                continue
            if args.row_ids:
                wanted = [int(x) for x in args.row_ids.split(",")
                          if x.strip()]
                rows = (db.query(em.MediaItem).filter(
                    em.MediaItem.id.in_(wanted),
                    em.MediaItem.library_id == lib_id,
                    em.MediaItem.item_type == "movie",
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None)).all())
            else:
                rows = bfm.find_unplayable_movies(db, [lib_id], enabled_ids)
            print(f"LIB {lib_id} {library.name}: targets={len(rows)}",
                  flush=True)
            result = bfm.run_backfill_movies(
                db, library, rows,
                expected_area_bucket=LIB_AREA_BUCKETS.get(lib_id),
                apply=args.apply, tmdb_details=_tmdb_details,
                max_new_sources_per_row=args.max_new_sources_per_row,
                delay_sec=args.delay)
            if grand["stats"] is None:
                grand["stats"] = result["stats"]
            else:
                for key, value in result["stats"].items():
                    if isinstance(value, dict):
                        for sub_key, sub_value in value.items():
                            grand["stats"][key][sub_key] = \
                                grand["stats"][key].get(sub_key, 0) + sub_value
                    else:
                        grand["stats"][key] = \
                            grand["stats"].get(key, 0) + value
            grand["audit"].extend(result["audit"])
            print(f"LIB {lib_id} STATS "
                  + json.dumps(result["stats"], ensure_ascii=False),
                  flush=True)
            if result.get("blocked"):
                print(f"BLOCKED {result['blocked']}", flush=True)
                return 2
        print("DONE " + json.dumps(grand["stats"], ensure_ascii=False),
              flush=True)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(grand, fh, ensure_ascii=False, indent=1)
            print(f"AUDIT_JSON {args.json_out}", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
