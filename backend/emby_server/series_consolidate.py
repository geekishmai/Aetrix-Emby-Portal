# -*- coding: utf-8 -*-
"""存量剧集库伪 series 收敛合并原型（2026-11-07，零 115 调用，默认 dry-run）。

处理两类历史首扫已落库的伪 series（新扫描已由 scanner.py 直接从源头归一）：
1. 日期型单集被当成独立 series：如 `快乐大本营2019年合集 .../快乐大本营.Happy.Camp.20190105...mp4`
   因首扫未识别 8 位播出日期 `YYYYMMDD`，落成 52 行 `item_type='series'` 且自带单文件 `file_path`；
   合并规则：同父目录内 ≥2 个命中 `_TV_DATE_EP_RE` 的伪 series 归一到按该父目录算出的唯一真实
   `series` 行，原伪 series 行就地转为 `item_type='episode'`（`season_number=YYYY`、`episode_number=MMDD`），
   保留既有 guid 与播放记录，零删行、零 115 IO。
2. 长剧按 `part1..part6` 分卷目录拆出的重复 series：如 `剑侠.../A.Chinese.Immortal.Story.part1..6`
   或平铺 `Negotiations.Sweetheart.part1..5`；
   合并规则：按 `scanner._canonical_tv_series_dir` 计算每个 series（据其下属 episode 路径）的规范剧目录键，
   同库同规范键下选定一个主 `series`（已绑 tmdb_id 优先，否则 part1 / 最小 id），其余分卷 `series` 下的
   `episode` 迁入主 `series`（同季号复用主剧 Season 行），副本 `series` 标记 `merged_into_id = primary.id`
   并软隐藏，幂等可重入。
"""
from __future__ import annotations

import posixpath
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import scanner as sc


def _parse_mount_rel(file_path: str) -> tuple[int | None, str]:
    raw = (file_path or "").strip()
    if raw.startswith("mount://"):
        rest = raw[len("mount://"):]
        slash = rest.find("/")
        if slash > 0 and rest[:slash].isdigit():
            return int(rest[:slash]), rest[slash:]
    return None, raw.replace("\\", "/")


def consolidate_tv_pseudo_series(
    db: Session,
    library_id: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """扫描并合并指定库（或全部 tvshows 库）中的日期伪 series 与 part1..N 分卷 series。

    默认 ``apply=False`` 只读预演并在返回前 rollback，绝不改动数据库。
    """
    lib_q = db.query(em.Library).filter(em.Library.collection_type == "tvshows")
    if library_id is not None:
        lib_q = lib_q.filter(em.Library.id == library_id)
    tv_libs = {lib.id: lib for lib in lib_q.all()}
    if not tv_libs:
        return {"date_groups": [], "part_groups": [], "converted_episodes": 0, "merged_series": 0, "applied": False}

    series_rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.library_id.in_(list(tv_libs.keys())),
            em.MediaItem.item_type == "series",
            em.MediaItem.deleted_at.is_(None),
            em.MediaItem.merged_into_id.is_(None),
        )
        .order_by(em.MediaItem.id.asc())
        .all()
    )

    # ---------- 1) 日期型单文件伪 series 归一 ----------
    date_buckets: dict[tuple[int, str], list[tuple[em.MediaItem, dict]]] = defaultdict(list)
    remaining_series: list[em.MediaItem] = []
    for s in series_rows:
        fp = (s.file_path or "").strip()
        if fp:
            parsed = sc.parse_media_filename(fp, "tvshows")
            if parsed.get("season") is not None and parsed.get("episode") is not None:
                mid, rel = _parse_mount_rel(fp)
                parent_rel = posixpath.dirname(rel.rstrip("/")) or "/"
                canon_dir, _ = sc._canonical_tv_series_dir(parent_rel, is_posix=True)
                key_path = mount_lib.mount_path(mid, canon_dir) if mid is not None else canon_dir
                date_buckets[(s.library_id, key_path)].append((s, parsed))
                continue
        remaining_series.append(s)

    date_report = []
    converted_eps = 0
    for (lib_id, key_path), members in date_buckets.items():
        if len(members) < 2:
            remaining_series.extend(m[0] for m in members)
            continue
        first_item, first_parsed = members[0]
        mid, rel = _parse_mount_rel(first_item.file_path or "")
        parent_rel = posixpath.dirname(rel.rstrip("/")) or "/"
        _, dir_name = sc._canonical_tv_series_dir(parent_rel, is_posix=True)
        clean_dir_name = sc._SERIES_DIR_META_NOISE_RE.sub(" ", dir_name)
        series_title = sc._tidy_name(clean_dir_name) or first_parsed.get("name") or first_item.name
        series_year = first_parsed.get("year")
        if series_year and str(series_year) in series_title:
            series_title = sc._strip_year(series_title, series_year)
        target_guid = sc.item_guid(key_path)

        date_report.append({
            "library_id": lib_id,
            "series_title": series_title,
            "series_guid": target_guid,
            "episode_count": len(members),
            "sample_Episode": f"S{first_parsed['season']}E{first_parsed['episode']}",
        })
        converted_eps += len(members)

        if apply:
            parent_series = db.query(em.MediaItem).filter_by(guid=target_guid).first()
            if parent_series is None:
                parent_series = em.MediaItem(
                    guid=target_guid,
                    library_id=lib_id,
                    item_type="series",
                    name=series_title,
                    scan_title=series_title,
                    production_year=series_year,
                    scan_year=series_year,
                    enrich_status="pending",
                )
                db.add(parent_series)
                db.flush()

            season_cache: dict[int, em.MediaItem] = {}
            for pseudo_s, parsed in members:
                sno = int(parsed["season"])
                ep_no = int(parsed["episode"])
                if sno not in season_cache:
                    s_guid = sc.item_guid(f"{key_path}#S{sno:02d}")
                    s_row = db.query(em.MediaItem).filter_by(guid=s_guid).first()
                    if s_row is None:
                        s_row = em.MediaItem(
                            guid=s_guid,
                            library_id=lib_id,
                            item_type="season",
                            name=f"Season {sno}",
                            parent_id=parent_series.id,
                            series_id=parent_series.id,
                            season_number=sno,
                            enrich_status="done",
                        )
                        db.add(s_row)
                        db.flush()
                    season_cache[sno] = s_row
                season_row = season_cache[sno]
                pseudo_s.item_type = "episode"
                pseudo_s.series_id = parent_series.id
                pseudo_s.parent_id = season_row.id
                pseudo_s.season_number = sno
                pseudo_s.episode_number = ep_no
                pseudo_s.name = f"{series_title} S{sno}E{ep_no:04d}"
                pseudo_s.enrich_status = "pending"
                pseudo_s.metadata_source = None

    # ---------- 2) part1..part6 分卷重复 series 合并 ----------
    part_buckets: dict[tuple[int, str], list[tuple[em.MediaItem, str]]] = defaultdict(list)
    for s in remaining_series:
        ep = (
            db.query(em.MediaItem)
            .filter(
                em.MediaItem.series_id == s.id,
                em.MediaItem.item_type == "episode",
                em.MediaItem.deleted_at.is_(None),
            )
            .order_by(em.MediaItem.id.asc())
            .first()
        )
        if ep is None or not (ep.file_path or "").strip():
            continue
        mid, rel = _parse_mount_rel(ep.file_path)
        ep_dir = posixpath.dirname(rel.rstrip("/")) or "/"
        canon_dir, canon_name = sc._canonical_tv_series_dir(ep_dir, is_posix=True)
        if canon_dir == ep_dir:
            # 普通单目录剧（但也可能是与 part1..N 同规范路径的父目录剧，如 满仓进城）
            pass
        key_path = mount_lib.mount_path(mid, canon_dir) if mid is not None else canon_dir
        part_buckets[(s.library_id, key_path)].append((s, canon_name))

    part_report = []
    merged_series_cnt = 0
    for (lib_id, key_path), group in part_buckets.items():
        if len(group) < 2:
            continue
        # 选主 series：已绑 tmdb_id 优先，其次名字不含 part 的主剧，最后 id 最小
        group_sorted = sorted(
            group,
            key=lambda pair: (
                0 if (pair[0].tmdb_id or "").strip() else 1,
                0 if not sc._TV_PART_TOKEN_RE.search(f" {pair[0].name} ") else 1,
                pair[0].id,
            ),
        )
        primary, canon_dir_name = group_sorted[0]
        duplicates = [p[0] for p in group_sorted[1:]]
        clean_title = sc._tidy_name(sc._SERIES_DIR_META_NOISE_RE.sub(" ", canon_dir_name)) or primary.name
        if primary.production_year and str(primary.production_year) in clean_title:
            clean_title = sc._strip_year(clean_title, primary.production_year)

        part_report.append({
            "library_id": lib_id,
            "canonical_key": key_path,
            "primary_id": primary.id,
            "resolved_title": clean_title,
            "merged_ids": [d.id for d in duplicates],
        })
        merged_series_cnt += len(duplicates)

        if apply:
            if not (primary.tmdb_id or "").strip():
                primary.name = clean_title
                primary.scan_title = clean_title
                primary.enrich_status = "pending"
                primary.metadata_source = None

            primary_seasons = {
                sr.season_number: sr
                for sr in db.query(em.MediaItem).filter(
                    em.MediaItem.series_id == primary.id,
                    em.MediaItem.item_type == "season",
                    em.MediaItem.deleted_at.is_(None),
                ).all()
                if sr.season_number is not None
            }

            for dup in duplicates:
                dup_eps = (
                    db.query(em.MediaItem)
                    .filter(
                        em.MediaItem.series_id == dup.id,
                        em.MediaItem.item_type == "episode",
                        em.MediaItem.deleted_at.is_(None),
                    )
                    .all()
                )
                for ep in dup_eps:
                    sno = ep.season_number if ep.season_number is not None else 1
                    target_season = primary_seasons.get(sno)
                    if target_season is None:
                        s_guid = sc.item_guid(f"{primary.guid}#S{sno:02d}")
                        target_season = db.query(em.MediaItem).filter_by(guid=s_guid).first()
                        if target_season is None:
                            target_season = em.MediaItem(
                                guid=s_guid,
                                library_id=lib_id,
                                item_type="season",
                                name=f"Season {sno}",
                                parent_id=primary.id,
                                series_id=primary.id,
                                season_number=sno,
                                enrich_status="done",
                            )
                            db.add(target_season)
                            db.flush()
                        primary_seasons[sno] = target_season
                    ep.series_id = primary.id
                    ep.parent_id = target_season.id
                dup.merged_into_id = primary.id
                dup.is_hidden = True

    if apply:
        db.commit()
    else:
        db.rollback()

    return {
        "date_groups": date_report,
        "part_groups": part_report,
        "converted_episodes": converted_eps,
        "merged_series": merged_series_cnt,
        "applied": apply,
    }
