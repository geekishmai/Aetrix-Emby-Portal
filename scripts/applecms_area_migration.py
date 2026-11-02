# -*- coding: utf-8 -*-
"""苹果 CMS 存量地区迁移 CLI（抓详情分流配套，2026-11-02）。

把总库（kind=applecms、没配地区桶的电影库）的存量托管行按
detail 的 vod_area 判桶，迁进配了对应桶的地区库。核心逻辑在
``backend/emby_server/applecms_area_migration.py``，与同步工人
共用同一份 detail 缓存与同一处地区归一化。

用法（仓库根目录，生产在后端容器/宿主机按部署惯例执行）：

    # 先干跑：只打 detail 进缓存 + 输出逐桶计数，一行不动
    python3 scripts/applecms_area_migration.py \
        --from-library 58 --to huayu=59 --to oumei=60 --to rihan=61

    # 数字核对无误后真跑：只改 library_id，guid/映射不动
    python3 scripts/applecms_area_migration.py \
        --from-library 58 --to huayu=59 --to oumei=60 --to rihan=61 \
        --apply

时序红线（与工人同批上线）：先部署带地区分流的工人 → 再建
地区库配桶 → 最后跑本脚本。工人没上线就迁行，行会被总库同步
按 guid 锚定跳过、又没人判桶，陈旧化。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _parse_targets(pairs) -> dict:
    targets = {}
    for pair in pairs or []:
        bucket, sep, raw_id = str(pair).partition("=")
        if not sep or not raw_id.strip().isdigit():
            raise SystemExit(f"--to 格式应为 桶=库id，收到: {pair!r}")
        targets[bucket.strip()] = int(raw_id.strip())
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-library", type=int, required=True,
                        help="总库（无地区桶的 CMS 电影库）id")
    parser.add_argument("--to", action="append", default=[],
                        metavar="桶=库id",
                        help="目标：如 huayu=59，可重复给多个桶")
    parser.add_argument("--apply", action="store_true",
                        help="真跑（默认干跑：只计数不动行）")
    args = parser.parse_args()
    targets = _parse_targets(args.to)

    from backend.database import SessionLocal
    from backend.emby_server import applecms_area_migration as aam

    db = SessionLocal()
    try:
        result = aam.run_area_migration(
            db, from_library_id=args.from_library, targets=targets,
            apply=args.apply)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
