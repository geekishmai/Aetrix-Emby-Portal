#!/usr/bin/env python3
"""补全（enrich）积压基线报告 —— **只读**，不改任何数据（v2.42.9）

先跑它，再谈优化。整套刮削优化的方向完全由积压的**构成**决定：

- 若绝大多数条目挂在 ``mount://`` 上 → 瓶颈是远程 I/O（目录列举 + NFO 读取），
  该做的是 ctx 复用 / 成组抢单（处方 1+2）；
- 若多在本机盘 → 瓶颈在 TMDB 与匹配质量，该做的是缓存落盘 / 短路 / Tier 1.5（处方 6+8+12）；
- 若 ``pending`` 里大半是「搜过、没命中」→ 该做的是终态化（处方 5），
  否则那批条目的重试会永远把积压数字顶住；
- 若 ``enriching`` 长期不为 0 且 ``done`` 不涨 → worker 没在跑或卡住了（看部署，不是看代码）。

四条都是 SELECT 能回答的，不需要先改代码再猜。

用法（容器里 / 工作区都行）：

    docker exec aetrix-api python scripts/enrich_backlog_report.py
    venv/bin/python scripts/enrich_backlog_report.py            # Freebuff 工作区
    venv/bin/python scripts/enrich_backlog_report.py --window 10  # 只算近 10 分钟

``--window N``：第 3 段会顺带报「近 N 分钟的 done/分钟」（按 ``last_scraped_at`` 粗算，
进程内计数器看 ``GET /api/admin/emby/scrape/enrich-progress`` 的 ``throughput``）。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import case, func  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402

PENDING_STATES = ("pending", "enriching")


def _pct(part: int, total: int) -> str:
    return f"{part / total * 100:5.1f}%" if total else "   —  "


def _title(text: str) -> None:
    print()
    print(f"── {text} " + "─" * max(0, 62 - len(text)))


def section_mix(db) -> None:
    """① 挂载构成 × item_type：决定 I/O 优化值不值得做"""
    _title("① 积压构成（挂载 × 类型）")
    on_mount = case((em.MediaItem.file_path.like("mount://%"), "mount://"), else_="本机盘")
    rows = (
        db.query(on_mount.label("path_kind"), em.MediaItem.item_type,
                 func.count(em.MediaItem.id))
        .filter(em.MediaItem.enrich_status.in_(PENDING_STATES))
        .group_by(on_mount, em.MediaItem.item_type)
        .all()
    )
    total = sum(r[2] for r in rows)
    if not rows:
        print("   （没有 pending/enriching 条目 —— 积压已清空）")
        return
    print(f"   {'位置':<10}{'类型':<10}{'条数':>9}   占比")
    for kind, item_type, count in sorted(rows, key=lambda r: -r[2]):
        print(f"   {kind:<10}{(item_type or '未知'):<10}{count:>9}   {_pct(count, total)}")
    print(f"   {'合计':<20}{total:>9}")
    # 软删行由 ORM 钩子排除（deleted_at IS NULL）。本合计仍含合并副本
    # （merged_into_id 非空）。健康接口与 get_progress 再排除副本，数字会更小。
    visible = (
        db.query(func.count(em.MediaItem.id))
        .filter(em.MediaItem.enrich_status.in_(PENDING_STATES),
                em.MediaItem.merged_into_id.is_(None))
        .scalar() or 0
    )
    print(f"   → 排除合并副本后（健康接口 / get_progress 口径）: {visible}")
    if total != visible:
        print(f"   → 差额 {total - visible} 是合并副本：worker 不认领，也不会被处理掉")
    mount_total = sum(r[2] for r in rows if r[0] == "mount://")
    print(f"   → 挂载占比 {_pct(mount_total, total).strip()}"
          f"{'（远程 I/O 是主因，处方 1+2+6 优先）' if mount_total * 2 > total else '（本机盘为主，处方 6+8+12 优先）'}")


def section_hopeless(db) -> None:
    """② 无望队列：决定「终态化」的收益"""
    _title("② 无望队列（搜过 / 没命中 / 还在重试）")
    hopeless = (
        db.query(func.count(em.MediaItem.id))
        .filter(em.MediaItem.enrich_status == "pending",
                em.MediaItem.metadata_source == "none",
                em.MediaItem.enrich_attempts > 0)
        .scalar() or 0
    )
    scanned = (
        db.query(func.count(em.MediaItem.id))
        .filter(em.MediaItem.metadata_source == "none")
        .scalar() or 0
    )
    pending = (
        db.query(func.count(em.MediaItem.id))
        .filter(em.MediaItem.enrich_status == "pending")
        .scalar() or 0
    )
    print(f"   已跑过但没拿到数据（metadata_source='none'）: {scanned}")
    print(f"   其中仍在 pending 且重试过（enrich_attempts>0）: {hopeless}")
    print(f"   → 占当前 pending 的 {_pct(hopeless, pending).strip()}"
          f"{'（终态化能直接让积压降这一块，处方 5 优先）' if pending and hopeless * 4 > pending else ''}")


def section_status(db, window_min: int) -> None:
    """③ 是「跑得慢」还是「根本没在跑」"""
    _title("③ 状态分布")
    rows = (db.query(em.MediaItem.enrich_status, func.count(em.MediaItem.id))
            .group_by(em.MediaItem.enrich_status).all())
    by_status = {s or "unknown": c for s, c in rows}
    total = sum(by_status.values())
    print(f"   {'状态':<12}{'条数':>9}   占比")
    for status in ("pending", "enriching", "done", "failed"):
        count = by_status.get(status, 0)
        print(f"   {status:<12}{count:>9}   {_pct(count, total)}")
    for status, count in sorted(by_status.items()):
        if status not in ("pending", "enriching", "done", "failed"):
            print(f"   {status:<12}{count:>9}   {_pct(count, total)}")
    print(f"   {'合计':<12}{total:>9}")
    print("   口径：本段是全部 enrich_status 的合计（含 done/failed），不是积压。")
    print("   ORM 自动排除软删；合并副本仍算在里面。健康接口只数 pending+enriching，")
    print("   且排除 merged_into_id 非空。")

    window = datetime.now() - timedelta(minutes=max(1, window_min))
    recent = (db.query(func.count(em.MediaItem.id))
              .filter(em.MediaItem.enrich_status == "done",
                      em.MediaItem.last_scraped_at.isnot(None),
                      em.MediaItem.last_scraped_at >= window).scalar() or 0)
    rate = recent / max(1, window_min)
    remain = by_status.get("pending", 0) + by_status.get("enriching", 0)
    print(f"   近 {window_min} 分钟 done={recent} → {rate:.1f} 条/分钟")
    if rate > 0:
        print(f"   → 按这个速率清掉剩余 {remain} 条还要约 {remain / rate / 60:.1f} 小时")
    elif remain:
        print("   → 速率为 0：多半不是「慢」，而是没在跑（查 worker 是否启动 / 是否卡死）")

    oldest, newest = (
        db.query(func.min(em.MediaItem.date_added), func.max(em.MediaItem.date_added))
        .filter(em.MediaItem.enrich_status.in_(PENDING_STATES)).first()
    )
    if oldest:
        print(f"   积压条目入库时间跨度：{oldest} → {newest}")
        print("   → 若跨度很大且新入库的在最前，老分类正在被饿死（处方 4：每库轮转）")


def section_libraries(db) -> None:
    """④ 各媒体库的 pending：验证「某个分类一直没刮削」"""
    _title("④ 各媒体库 pending 分布")
    rows = (db.query(em.MediaItem.library_id, func.count(em.MediaItem.id))
            .filter(em.MediaItem.enrich_status == "pending")
            .group_by(em.MediaItem.library_id).all())
    if not rows:
        print("   （没有 pending 条目）")
        return
    names = dict(db.query(em.Library.id, em.Library.name).all())
    total = sum(r[1] for r in rows)
    print(f"   {'库':<28}{'条数':>9}   占比")
    for library_id, count in sorted(rows, key=lambda r: -r[1]):
        name = names.get(library_id) or f"库 #{(library_id if library_id is not None else '无')}"
        print(f"   {name[:26]:<28}{count:>9}   {_pct(count, total)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="补全积压基线报告（只读）")
    parser.add_argument("--window", type=int, default=10,
                        help="第 3 段的速率窗口，单位分钟（默认 10）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = (db.query(em.MediaItem.enrich_status, func.count(em.MediaItem.id))
                .group_by(em.MediaItem.enrich_status).all())
        by_status = {s or "unknown": c for s, c in rows}
        print("=" * 66)
        print(f"Aetrix 补全积压基线报告 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"待补全 pending={by_status.get('pending', 0)} "
              f"enriching={by_status.get('enriching', 0)} "
              f"failed={by_status.get('failed', 0)} done={by_status.get('done', 0)}")
        print("=" * 66)
        section_mix(db)
        section_hopeless(db)
        section_status(db, args.window)
        section_libraries(db)
        print()
        print("提示：阶段用时分解（远程列举 / NFO / TMDB / 图片 / 条目）看管理端")
        print("      「Emby 管理 → 补全进度」，或 GET /api/admin/emby/scrape/enrich-progress。")
        return 0
    except Exception as exc:  # noqa: BLE001 — 报告脚本也不该甩栈给运维看
        print(f"❌ 报表失败（多半是库连接或表缺失）：{exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
