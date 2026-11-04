# -*- coding: utf-8 -*-
"""CMS 源站自带豆瓣编号收割（零搜索身份路，2026-11-03 站主亲令加速识别）。

为什么有这个模块（证据，2026-11-03 生产实查）：

- 无身份顶层条目约 8.27 万，搜索式识别（TMDB→豆瓣→多源引擎）
  净排水只有约 400–600 条/小时，且扩抓还在进水——靠搜索清不完。
- 但 CMS 源的 detail 载荷普遍自带 ``vod_douban_id``：逐源抽样
  225 条，未识别条目的映射 vod 里约 76% 带号（仅优酷源抽样 0/15）；
  list 载荷不带（实测字段清单无此键），必须走 detail（支持多 id
  批量，一次数十条）。跨源一致性抽样 27 条双号条目 26 条一致。
- 存量里 91.5% 的无身份顶层条目至少有一条上游映射，即这条路的
  理论覆盖面；预期落账约 5.7–6 万条，全程零搜索请求。

口径（不另起一套）：

- 取数走 ``applecms_fetcher.fetch_page``（provide 协议唯一实现）；
- 落账走 ``identify_two_stage`` 的 ``_merge_external_id``（只填空缺、
  已有值不覆盖）＋ ``_upsert_stage_record`` ＋ ``_douban_binding_dict``
  （与识别段豆瓣认亲同一套落账实现与证据形状），证据来源记
  ``cms_vod_douban_id``，审查面可区分、可回溯。
- 投票制置信门：同一条目全部映射源给出的豆瓣号**完全一致**才落账
  （单源给号也算一致）；任一源给出不同号 → 整条跳过，留给搜索链，
  宁可留空不许错绑。
- 候选谓词复用 ``enrich_queues.identify_filter``（与识别队列逐字
  同判据）：只碰未绑定顶层 movie/series；已是 done 终态（识别判
  none）的行落账后打回 pending，让补资料队列接手；pending 行不动
  状态（队列谓词按行状态动态分流，落账即自动转补资料队列）。
- 事务纪律：分批小步提交（默认每 200 条），绝不长事务堵部署迁移
  （2026-11-03 凌晨长事务堵 ALTER TABLE 的事故教训）。

运行方式（容器内）：``python -m backend.emby_server.applecms_identity_harvest
[--sources 7,9,...] [--limit 2000] [--dry-run]``。
"""

from __future__ import annotations

import argparse
import logging
import random
import time
from typing import Iterable, Optional

from backend.emby_server import models as em
from backend.emby_server.applecms_fetcher import fetch_page
from backend.emby_server.enrich_queues import identify_filter
from backend.emby_server.upstream_playback import _source_headers
from backend.emby_server.identify_two_stage import (
    _douban_binding_dict,
    _merge_external_id,
    _upsert_stage_record,
)

logger = logging.getLogger(__name__)

#: 段记录证据来源标记：审查面对账时按此值区分「源站自带号收割」与
#: 搜索认亲，不与其它来源混报。
BINDING_SOURCE = "cms_vod_douban_id"

_COMMIT_EVERY = 200


def parse_douban_id(vod) -> Optional[str]:
    """从一条 vod detail 记录里取合法豆瓣编号；取不到返回 None。

    合法 = 纯数字字符串且不为 "0"（CMS 的 0/空是「暂无」占位）。
    非 dict、字段缺失、带字母的值一律 None——源站数据质量参差，
    形状不对宁可不收，不做任何猜测性清洗。
    """
    if not isinstance(vod, dict):
        return None
    raw = vod.get("vod_douban_id")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "0" or not text.isdigit():
        return None
    return text


def vote_douban_id(ids: Iterable[str]) -> Optional[str]:
    """投票：全部证据号完全一致才返回该号，否则 None（跳过）。

    ids 是同一条目从各映射源 detail 里收到的豆瓣号集合（含重复）。
    空集 → None；恰好一个 distinct 值 → 它；多个 distinct 值 →
    None（源间互相矛盾，证据不成立，留给搜索链定夺）。
    """
    distinct = {i for i in ids if i}
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def _new_stats() -> dict:
    return {
        "items": 0,          # 本轮候选条目数
        "with_id": 0,        # 至少一个源给出豆瓣号的条目数
        "agreed_multi": 0,   # ≥2 个源给号且一致（已含在 written 内统计口径见下）
        "single": 0,         # 仅 1 个源给号
        "disagreed": 0,      # 源间给号矛盾、整条跳过
        "no_id": 0,          # 所有源都没给号
        "written": 0,        # 实际落账条数
        "reset_done": 0,     # 落账时由 done 终态打回 pending 的条数
        "detail_calls": 0,   # detail 请求次数
        "fetch_errors": 0,   # detail 请求失败次数（按批计）
        "libraries": [],     # 落账涉及的库 id（收尾 bump 用）
    }


def harvest_source(db, source, *, limit: int = 2000, ids_per_call: int = 50,
                   http_get=None, dry_run: bool = False,
                   sleep_sec: float = 0.2,
                   commit_every: int = _COMMIT_EVERY) -> dict:
    """对一个源驱动一轮收割：以「该源有映射的未识别条目」为候选集，
    投票证据跨该条目的**全部**映射源收集（不只驱动源一家）。

    返回 stats dict（口径见 _new_stats）。单源失败（接口整批抛错）
    只记 fetch_errors 并继续，绝不让一家源拖死整轮。
    """
    stats = _new_stats()
    cand_ids = [
        row[0]
        for row in (
            db.query(em.MediaItem.id)
            .join(
                em.UpstreamItemMapping,
                em.UpstreamItemMapping.local_item_id == em.MediaItem.id,
            )
            .filter(
                em.UpstreamItemMapping.source_id == source.id,
                em.UpstreamItemMapping.episode_index.is_(None),
                em.MediaItem.parent_id.is_(None),
                em.MediaItem.item_type.in_(["movie", "series"]),
                em.MediaItem.deleted_at.is_(None),
                em.MediaItem.merged_into_id.is_(None),
                identify_filter(),
            )
            .distinct()
            .order_by(em.MediaItem.id)
            .limit(limit)
            .all()
        )
    ]
    stats["items"] = len(cand_ids)
    if not cand_ids:
        return stats
    items = {
        it.id: it
        for it in db.query(em.MediaItem)
        .filter(em.MediaItem.id.in_(cand_ids))
        .all()
    }
    maps = (
        db.query(em.UpstreamItemMapping)
        .filter(
            em.UpstreamItemMapping.local_item_id.in_(cand_ids),
            em.UpstreamItemMapping.episode_index.is_(None),
        )
        .all()
    )
    per_item: dict[int, list] = {}
    vods_by_source: dict[int, set] = {}
    for m in maps:
        per_item.setdefault(m.local_item_id, []).append(
            (m.source_id, str(m.upstream_item_id))
        )
        vods_by_source.setdefault(m.source_id, set()).add(
            str(m.upstream_item_id)
        )
    src_rows = (
        db.query(em.UpstreamSource)
        .filter(em.UpstreamSource.id.in_(list(vods_by_source)))
        .all()
    )
    source_urls = {s.id: (s.base_url or "").rstrip("/") for s in src_rows}
    # 与同步路径同一套请求头（源级 UA/Referer/附加头）：不少源按 UA
    # 拦默认爬虫头，不带头 detail 直接 403（2026-11-03 试点实证）。
    source_headers = {s.id: _source_headers(s) for s in src_rows}
    found: dict = {}
    for sid, vods in vods_by_source.items():
        base_url = source_urls.get(sid)
        if not base_url:
            continue
        ordered = sorted(vods)
        for i in range(0, len(ordered), ids_per_call):
            chunk = ordered[i : i + ids_per_call]
            if sleep_sec:
                # 防封纪律：请求间隔带随机抖动，不做整齐划一的机器节奏。
                time.sleep(sleep_sec * (0.5 + random.random()))
            try:
                payload = fetch_page(
                    base_url, ac="detail", ids=",".join(chunk),
                    http_get=http_get, headers=source_headers.get(sid),
                )
            except Exception:  # noqa: BLE001 - 单批失败隔离，不拖整轮
                stats["fetch_errors"] += 1
                continue
            stats["detail_calls"] += 1
            for vod in payload.get("list") or []:
                did = parse_douban_id(vod)
                if did:
                    found[(sid, str(vod.get("vod_id")))] = did
    libraries: set = set()
    processed = 0
    for iid in cand_ids:
        item = items.get(iid)
        if item is None:
            continue
        pairs = per_item.get(iid, [])
        carrier_sources = {
            sid for (sid, vid) in pairs if (sid, vid) in found
        }
        ids = [found[(sid, vid)] for (sid, vid) in pairs
               if (sid, vid) in found]
        if not ids:
            stats["no_id"] += 1
            continue
        stats["with_id"] += 1
        chosen = vote_douban_id(ids)
        if chosen is None:
            stats["disagreed"] += 1
            continue
        if len(carrier_sources) >= 2:
            stats["agreed_multi"] += 1
        else:
            stats["single"] += 1
        before = item.external_ids
        _merge_external_id(item, "douban", chosen)
        if item.external_ids == before:
            # 谓词已保证无在用键，此处只可能是合并口径的防御性跳过；
            # 不落段记录、不计 written，如实归到 no_id 之外的静默项。
            continue
        _upsert_stage_record(
            db, item.id, "identified",
            _douban_binding_dict(chosen, BINDING_SOURCE),
        )
        if item.enrich_status == "done":
            # 曾被识别判 none 的终态行：身份已到手，打回 pending 让
            # 补资料队列接手（清退避/认领/尝试，口径同存量重排）。
            item.enrich_status = "pending"
            item.enrich_next_retry_at = None
            item.enrich_claimed_at = None
            item.enrich_claim_token = None
            item.enrich_attempts = 0
            stats["reset_done"] += 1
        stats["written"] += 1
        if item.library_id:
            libraries.add(item.library_id)
        processed += 1
        if not dry_run and commit_every and processed % commit_every == 0:
            db.commit()
    if dry_run:
        db.rollback()
    else:
        db.commit()
    stats["libraries"] = sorted(libraries)
    return stats


def harvest_all(db, *, source_ids: Optional[Iterable[int]] = None,
                per_source_limit: int = 2000, dry_run: bool = False,
                sleep_sec: float = 0.2, http_get=None,
                bump_cache: bool = True) -> dict:
    """按启用源逐家驱动收割并汇总；收尾对落账涉及的库 bump 数据版本
    （判据/可见性切换纪律：身份落账必须配套清列表/去重缓存）。

    驱动源只取 enabled 的源（停用源如最大 8 不主动驱动）；但投票
    证据收集不限于启用源——候选条目在停用源上的映射 detail 若能
    取到，同样是该条目自身来源记录的证据。
    """
    q = db.query(em.UpstreamSource).filter(em.UpstreamSource.enabled.is_(True))
    if source_ids:
        q = q.filter(em.UpstreamSource.id.in_(list(source_ids)))
    sources = q.order_by(em.UpstreamSource.id).all()
    total = _new_stats()
    total["per_source"] = {}
    libraries: set = set()
    for source in sources:
        stats = harvest_source(
            db, source, limit=per_source_limit, http_get=http_get,
            dry_run=dry_run, sleep_sec=sleep_sec,
        )
        total["per_source"][source.id] = stats
        libraries.update(stats.get("libraries") or [])
        for key in ("items", "with_id", "agreed_multi", "single",
                    "disagreed", "no_id", "written", "reset_done",
                    "detail_calls", "fetch_errors"):
            total[key] += stats[key]
    total["libraries"] = sorted(libraries)
    if bump_cache and not dry_run and libraries:
        from backend.emby_server.items_dedup_cache import (
            bump_library_item_versions,
        )

        bump_library_item_versions(db, sorted(libraries), force=True)
        db.commit()
    logger.info("CMS 豆瓣号收割一轮完成: %s", total)
    return total


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CMS 源站自带豆瓣编号收割（零搜索身份路）")
    parser.add_argument(
        "--sources", default="",
        help="逗号分隔的驱动源 id；缺省 = 全部启用源")
    parser.add_argument(
        "--limit", type=int, default=2000,
        help="每个驱动源本轮最多处理的候选条数")
    parser.add_argument(
        "--sleep", type=float, default=0.2,
        help="detail 请求间隔基数（秒，实际带 0.5–1.5 倍随机抖动）")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只取数投票、不落账（结束回滚）")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from backend.database import SessionLocal

    source_ids = None
    if args.sources:
        source_ids = [int(x) for x in args.sources.split(",") if x.strip()]
    db = SessionLocal()
    try:
        total = harvest_all(
            db, source_ids=source_ids, per_source_limit=args.limit,
            dry_run=args.dry_run, sleep_sec=args.sleep,
        )
    finally:
        db.close()
    logger.info("收割结果: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
