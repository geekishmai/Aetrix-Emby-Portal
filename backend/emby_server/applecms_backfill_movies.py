# -*- coding: utf-8 -*-
"""苹果 CMS 智能补源·电影版：给存量电影行按多信号打分补其它源映射。

来历（站主 2026-11-02 22:08 拍板「按计划全做，电影补源也做」）：
剧集版智能补源（``applecms_backfill``，PR #1769）只处理剧行；
2026-11-02 晚「播不了」全站排查实证库 58 有大量单源电影的线路
已死（仅剩停用源最大 8 的 281 部先天无线路、抽样判死 87 部），
而电影没有补源工具。本模块把剧集版能力扩到电影行：**同一套**
搜索钥匙、多信号打分三档、硬否决、质量门、补前验活与审计口径
（全部复用 ``applecms_backfill`` 的实现，不另造第二套），差异只
有三处形态差异，不是口径放宽：

- 行是电影行本身：映射读口按 ``local_item_id``＝电影行、集号为
  空（``episode_index IS NULL``，电影映射的既有口径）统计来源
  集合；集行那套 series_id 读口不适用于电影。
- 没有集数维度：行画像 ``episode_count`` 恒 0，打分器的集数项
  自动不奖不罚、集数否决不触发；改为形态校验——候选直链组必
  须恰好 1 个单元，多单元的是剧集形态，挂到电影行上就是错挂，
  按形态拒绝。时长信号行侧结构性缺失（CMS 电影行时长普遍为
  空、画质/时长回填另有链路），不拿它打分、如实降权：可用信
  号族是标题/年份/地区/类型/主演/导演/简介，足够中置信的双
  信号族门槛。
- 补写不建任何行：电影行已存在，只经
  ``upstream_item_mappings.record_mapping`` 给行加一条指向候
  选 vod 的映射（集号 None），与同步工人给电影行写映射同一
  落点；逐（行×源）提交、单条失败回滚不污染其它行。

取流认不认库外源映射的定论与剧集版相同（只看映射行＋源启
用，不查库的 ``upstream_source_ids``），电影行同理。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from backend.emby_server import applecms_backfill as bf
from backend.emby_server import applecms_fetcher as acf

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB 读口（电影行映射来源集合 / 参照 vod）
# ---------------------------------------------------------------------------


def movie_mapping_sources(db, movie_id: int) -> set:
    """一部电影的映射来源集合（自身行映射、集号为空的口径）。"""
    from backend.emby_server import models as em
    return {r[0] for r in db.query(em.UpstreamItemMapping.source_id).filter(
        em.UpstreamItemMapping.local_item_id == movie_id,
        em.UpstreamItemMapping.episode_index.is_(None)).distinct().all()}


def movie_mapping_vod_id(db, movie_id: int,
                         source_id: int) -> Optional[str]:
    """取某电影在某源的一条映射 vod_id（参照 detail 用）。"""
    from backend.emby_server import models as em
    row = (db.query(em.UpstreamItemMapping.upstream_item_id).filter(
        em.UpstreamItemMapping.local_item_id == movie_id,
        em.UpstreamItemMapping.source_id == source_id,
        em.UpstreamItemMapping.episode_index.is_(None)).first())
    return str(row[0]) if row and row[0] else None


def find_unplayable_movies(db, library_ids, enabled_source_ids) -> List:
    """目标行：映射来源与已启用源零交集的电影行（即不可播）。"""
    from backend.emby_server import models as em
    enabled = set(enabled_source_ids or [])
    rows = (db.query(em.MediaItem).filter(
        em.MediaItem.library_id.in_(list(library_ids)),
        em.MediaItem.item_type == "movie",
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItem.library_id, em.MediaItem.id).all())
    out = []
    for row in rows:
        if not (movie_mapping_sources(db, row.id) & enabled):
            out.append(row)
    return out


def _fetch_ref_detail_movie(db, row_id: int, have: set,
                            http_get) -> Optional[dict]:
    """行自带映射的参照 detail（只读；口径同剧集版参照读法）。

    参照源停用也只影响候选资格，参照资料照常可读——参照的是这
    部电影在旧源上的条目本身（补演员/导演/简介等行侧缺失信号）。
    """
    from backend.emby_server import upstream_sources as us
    from backend.emby_server.applecms_library_sync import _source_headers
    for source_id in sorted(have):
        vod_id = movie_mapping_vod_id(db, row_id, source_id)
        if not vod_id:
            continue
        source = us.get_source(db, source_id)
        if source is None:
            continue
        try:
            return acf.fetch_detail(
                source.base_url, vod_id, http_get=http_get,
                headers=_source_headers(source))
        except Exception:  # noqa: BLE001 - 参照拿不到就降级，不炸主流程
            continue
    return None


# ---------------------------------------------------------------------------
# 补源主流程（电影版；骨架与剧集版 run_backfill 逐项同口径）
# ---------------------------------------------------------------------------


def run_backfill_movies(db, library, rows, *, expected_area_bucket=None,
                        apply: bool = False, http_get=None, http_probe=None,
                        tmdb_details=None,
                        max_new_sources_per_row: int =
                        bf.DEFAULT_MAX_NEW_SOURCES_PER_ROW,
                        keys_per_source: int = bf.KEYS_PER_SOURCE,
                        candidates_per_source: int =
                        bf.CANDIDATES_PER_SOURCE,
                        delay_sec: float = 0.0, sleep_fn=None) -> dict:
    """对一批电影行智能补源。返回 {"stats": ..., "audit": [...]}。

    ``apply=False`` 为干跑：搜索、拉 detail、打分、验活全做，只
    不写映射。``apply=True`` 时逐（行×源）经 ``record_mapping``
    写一条电影映射（集号 None）并单独提交，单条失败回滚不污染
    其它行。映射开关关着时 apply 直接整批拒绝。
    """
    from backend.emby_server import applecms_series as acser
    from backend.emby_server import upstream_item_mappings as uim
    from backend.emby_server.applecms_library_sync import _source_headers
    sleep = sleep_fn if sleep_fn is not None else time.sleep
    stats = {"rows": 0, "filled": 0, "unresolved": 0, "mappings_added": 0,
             "searches": 0, "details": 0, "quality_skipped": 0,
             "probe_failed": 0, "errors": 0,
             "unresolved_reasons": {}}
    audit: List[dict] = []
    if apply and not uim.mapping_enabled():
        return {"stats": stats, "audit": audit,
                "blocked": "mapping_off"}
    sources = bf.enabled_applecms_sources(db, library)

    for row in rows:
        stats["rows"] += 1
        have = movie_mapping_sources(db, row.id)
        details_data = None
        if tmdb_details is not None and getattr(row, "tmdb_id", None):
            try:
                details_data = tmdb_details(str(row.tmdb_id))
            except Exception:  # noqa: BLE001 - 身份补强失败降级
                details_data = None
        ref_detail = _fetch_ref_detail_movie(db, row.id, have, http_get)
        profile = bf.build_row_profile(
            row, episode_count=0,
            expected_area_bucket=expected_area_bucket,
            tmdb_details=details_data, ref_detail=ref_detail)
        keys = bf.build_search_keys(profile)
        row_audit = {"item_id": int(row.id), "name": row.name,
                     "library_id": int(row.library_id),
                     "have_before": sorted(have), "gained": [],
                     "attempts": [], "unresolved_reason": None}
        gained = 0
        reasons: List[str] = []
        saw_shape_reject = False
        for source in sources:
            if gained >= max_new_sources_per_row:
                break
            if source.id in have:
                continue
            headers = _source_headers(source)
            attempt = {"source_id": int(source.id), "keys_tried": [],
                       "candidates": [], "error": None}
            row_audit["attempts"].append(attempt)
            best = None  # (rank, total, cand, score, detail, key_kind)
            for key_text, key_kind in keys[:keys_per_source]:
                attempt["keys_tried"].append(
                    {"key": key_text, "kind": key_kind})
                stats["searches"] += 1
                try:
                    payload = acf.fetch_page(
                        source.base_url, ac="search", wd=key_text,
                        http_get=http_get, headers=headers)
                except acf.AppleCMSError as exc:
                    attempt["error"] = exc.kind
                    break
                short_ids = bf.class_short_drama_ids(payload)
                scored = 0
                for entry in (payload.get("list") or []):
                    if scored >= candidates_per_source:
                        break
                    if not isinstance(entry, dict):
                        continue
                    gate = bf.quality_gate_reject(
                        str(entry.get("type_name") or ""),
                        str(entry.get("vod_name") or ""),
                        str(entry.get("vod_remarks") or ""),
                        vod_class=str(entry.get("vod_class") or ""),
                        short_ids=short_ids,
                        type_id=entry.get("type_id"))
                    if gate:
                        stats["quality_skipped"] += 1
                        attempt["candidates"].append({
                            "vod_id": str(entry.get("vod_id") or ""),
                            "rejected": f"quality:{gate}"})
                        continue
                    vod_id = str(entry.get("vod_id") or "")
                    stats["details"] += 1
                    try:
                        detail = acf.fetch_detail(
                            source.base_url, vod_id, http_get=http_get,
                            headers=headers)
                    except acf.AppleCMSError:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id, "rejected": "detail_error"})
                        continue
                    if detail is None:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "detail_missing"})
                        continue
                    gate = bf.quality_gate_reject(
                        str(detail.get("type_name") or ""),
                        str(detail.get("vod_name") or ""),
                        str(detail.get("vod_remarks") or ""),
                        vod_class=str(detail.get("vod_class") or ""),
                        short_ids=short_ids,
                        type_id=detail.get("type_id"))
                    if gate:
                        stats["quality_skipped"] += 1
                        attempt["candidates"].append({
                            "vod_id": vod_id, "rejected": f"quality:{gate}"})
                        continue
                    planned = acser.plan_episodes(detail)
                    if not planned:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "no_direct_group"})
                        continue
                    if len(planned) != 1:
                        # 电影行只能挂单单元候选：多单元是剧集形态。
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "multi_episode_shape"})
                        continue
                    cand = bf.candidate_from_detail(
                        detail, entry, episode_count=len(planned))
                    score = bf.score_candidate(profile, cand)
                    scored += 1
                    attempt["candidates"].append({
                        "vod_id": vod_id, "name": cand.name,
                        "key_kind": key_kind, "total": score.total,
                        "parts": score.parts, "tier": score.tier,
                        "vetoes": score.vetoes,
                        "title_via": score.title_via})
                    rank = bf._TIER_RANK[score.tier]
                    if best is None or (rank, score.total) > (best[0], best[1]):
                        best = (rank, score.total, cand, score, detail,
                                key_kind)
                if best is not None and best[3].tier in ("high", "medium"):
                    break
            if best is None:
                reasons.append(
                    "source_error" if attempt["error"] else "no_candidate")
                continue
            _rank, _total, cand, score, detail, key_kind = best
            if score.tier not in ("high", "medium"):
                reasons.append(score.vetoes[0] if score.vetoes
                               else "low_confidence")
                continue
            if not bf.probe_first_episode(detail, headers,
                                          http_probe=http_probe):
                stats["probe_failed"] += 1
                reasons.append("probe_failed")
                for cand_audit in attempt["candidates"]:
                    if cand_audit.get("vod_id") == cand.vod_id:
                        cand_audit["rejected"] = "probe_failed"
                continue
            if apply:
                try:
                    mapping = uim.record_mapping(
                        db, local_item_id=int(row.id),
                        source_id=int(source.id),
                        upstream_item_id=cand.vod_id,
                        confidence=min(1.0, max(0.0, score.total / 100.0)),
                        evidence=f"smart_backfill_movie:{score.tier}",
                        episode_index=None)
                    if mapping is None:
                        raise RuntimeError("mapping_disabled")
                    db.commit()
                except Exception:  # noqa: BLE001 - 单条失败不污染其它行
                    db.rollback()
                    stats["errors"] += 1
                    attempt["error"] = "write_failed"
                    reasons.append("write_failed")
                    continue
                stats["mappings_added"] += 1
            row_audit["gained"].append({
                "source_id": int(source.id), "vod_id": cand.vod_id,
                "tier": score.tier, "total": score.total,
                "parts": score.parts, "key_kind": key_kind,
                "title_via": score.title_via, "applied": bool(apply)})
            gained += 1
            have.add(source.id)
            if delay_sec > 0:
                sleep(delay_sec)
        if gained:
            stats["filled"] += 1
        else:
            stats["unresolved"] += 1
            reason = bf._pick_unresolved_reason(reasons, saw_shape_reject)
            row_audit["unresolved_reason"] = reason
            stats["unresolved_reasons"][reason] = \
                stats["unresolved_reasons"].get(reason, 0) + 1
        audit.append(row_audit)
    return {"stats": stats, "audit": audit}
