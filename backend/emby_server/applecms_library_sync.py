# -*- coding: utf-8 -*-
"""苹果 CMS 媒体库同步工人（新媒体库类型一期·同步单）。

把库行（``emby_libraries.kind == "applecms"``）配置的 CMS 源目录
同步成托管行，供播放器经 Emby 兼容层直接浏览/点播（用户 2026-11-02
定案：新建媒体库类型、挂载退休为方向）。设计稿见
``~/workspace/reports/applecms-library-type-design-20261102.md``。

口径（全部 2026-11-02 定案）：
- 播放地址绝不落库（点播时由 mapped 取流口现取 302），年份/海报
  等元数据不靠 CMS 的残缺字段，由 TMDB 补全链补。电影库只拉
  provide ``ac=list`` 分页、同步期原则上不拉 detail；剧集库例外
  逐条拉 detail，但只取集数与集名（见下剧集库条）。
- 电影库地区分流（``cms_area_bucket`` 非空时，抓详情分流
  2026-11-02）：条目过分类白名单后判桶——list 带 ``vod_area``
  用 list 的；没有才查 ``applecms_detail_cache``，未命中才真打
  一次 detail（同一（源, vod）在多库/多轮间只打一次），并顺带
  把 detail 的源站资料按只填空 harvest 到行上。桶与本库不符计
  ``skipped_area`` 不建行；detail 失败/条目消失计
  ``detail_errors`` 本轮跳过、下轮重试，绝不猜桶。归一化唯一
  实现在 ``applecms_library.area_bucket``。一轮调度里配桶库
  先于总库同步（guid 先到先锚：地区库先认领本桶条目，总库只
  收剩下的其他/未知地区）。剧集库不走桶校验：其地区细拆按
  白名单口径承接（见下「库是配置层」条）。
- 身份不豁免：行建成托管行后 ``enrich_status`` 保持 ``pending``，
  现有 enrich 工人对 ``upstream_source_id`` 非空的行自动走「只走
  TMDB」补身份分支；对不上身份的行按现行货架规矩不上架，本模块
  不做任何货架过滤。
- 去重：标题+年份键（``aggregate_key_for``），一行多映射；行的
  ``upstream_source_id`` 是先到源（主来源），其余源只挂映射。
- 电影库（movies）：剧集形态（``vod_serial`` 解析出集数 > 1）
  跳过并计数，形态边界靠分类白名单（见下）。
- 剧集库（tvshows，集行同步单放行）：白名单口径与电影库一致，
  收进来的条目一律按剧处理——逐条拉 detail 现取集列表，调
  ``applecms_series.ensure_series_tree`` 建「剧行＋合成单季行＋
  集行＋逐集映射」；detail 只用来数集与取集名，地址绝不落库。
  集序号 = 直链组内 1 起序号（与取流口 resolve_play 同口径）；
  无直链组的整剧跳过计数（不建死行），detail 已消失跳过计数，
  detail 请求失败只计 detail_errors、不回滚整源。集数增减
  只增不删：新集靠增量轮重拉 detail 补行，源侧删集不删行，
  存量由月度对账兜底。mixed 等其它呈现类型仍
  blocked=unsupported_collection。
- 库是配置层：一库一白名单，本工人不写死库数量与库名——按
  地区细拆的多个剧集库（国产剧/欧美剧/日韩剧/动漫/综艺，
  2026-11-02 定版）各配一份白名单即可承接；港台并入国产的
  分类名清单由建库时逐源核定，不进代码。
- 分类：``cms_type_map`` 按**顶级分类名**判定（子类沿 provide 的
  ``class`` 分类树回溯）。映射为空 = 全收；**非空 = 白名单**：
  只有标 ``"in"`` 的顶级分类收，其余（标 ``"out"`` 或没写到）
  一律跳过计数。白名单形态是试点实测定的：3 家分类树形态差异
  大（最大资源近乎扁平、非电影顶级名有几十个），靠 out 名单
  排除法配不出电影库；且 ``vod_serial`` 在非凡全空、暴风全 0，
  剧集形态判定对这 3 家不作数——电影/剧集的边界只能靠分类
  白名单（vod_serial 判定保留，对填了它的源仍生效）。
- 映射开关关 = 整批 blocked 不建行（没映射的行播不了，不建孤儿行，
  照 ``upstream_import`` 前置门口径）。
- 增量走 provide ``h=24`` 窗口（调度日跑）；全量不带 ``h``（月度
  对账单用）。每源独立 commit，单源失败不回滚其它源。
- 进度写回库行 ``last_scan_at`` / ``scan_stats``（标准计数键 +
  ``cms_sync`` 明细），管理端列表的最近同步结果直接可见。

调度：``APPLECMS_LIBRARY_SYNC_ENABLED`` 默认关（新能力灰度，照
既有苹果 CMS 同步的双闸先例）；间隔
``APPLECMS_LIBRARY_SYNC_INTERVAL_SEC`` 默认 21600（6 小时，轮内
h=24 窗口保证增量不漏）。分页间隔复用配对工人的
``APPLECMS_SYNC_PAGE_DELAY_SEC``（默认 0.5 秒）防触发源站限速。
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_library as acl
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as us
from backend.emby_server.pinyin_sort import make_sort_name
from backend.emby_server.upstream_playback import _source_headers

logger = logging.getLogger(__name__)

#: 库同步总开关 env 键（默认关）。
SYNC_ENABLED_ENV = "APPLECMS_LIBRARY_SYNC_ENABLED"
#: 同步调度间隔 env 键（秒），默认 6 小时。
SYNC_INTERVAL_ENV = "APPLECMS_LIBRARY_SYNC_INTERVAL_SEC"
DEFAULT_SYNC_INTERVAL_SEC = 21600.0
#: 增量窗口（小时）：provide ``h=24`` 只回最近 24 小时更新的条目。
INCREMENTAL_WINDOW_HOURS = 24
#: CMS 行在 enrich 队列的优先级：低于本地补全（默认 0），防海量
#: CMS 行淹没本地库的补身份队列；仍会被轮转认领，不饿死。
CMS_ENRICH_PRIORITY = -10
#: 映射置信度/证据：与配对工人同口径（标题+年份）。
CMS_MAPPING_CONFIDENCE = 0.7
CMS_MAPPING_EVIDENCE = "title+year"

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_lock = threading.Lock()


def sync_enabled() -> bool:
    return (os.environ.get(SYNC_ENABLED_ENV) or "").strip() == "1"


def sync_interval_sec(explicit: Optional[float] = None) -> float:
    if explicit is not None:
        return max(1.0, float(explicit))
    raw = (os.environ.get(SYNC_INTERVAL_ENV) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_SYNC_INTERVAL_SEC
    except ValueError:
        value = DEFAULT_SYNC_INTERVAL_SEC
    return max(60.0, value)


def _zero_stats(library_id: int) -> Dict[str, object]:
    return {
        "library_id": library_id, "blocked": None,
        "added": 0, "updated": 0, "existing": 0, "mapped": 0,
        "pages": 0, "skipped_category": 0, "skipped_series_shape": 0,
        "skipped_invalid": 0, "skipped_other_library": 0,
        "skipped_area": 0, "detail_errors": 0, "detail_fetches": 0,
        "dedupe_hits": 0, "errors": [], "seen_by_source": {},
        # 剧集路径计数（电影库恒 0）：集行增/存量、无直链组跳过、
        # detail 已消失跳过（detail 请求失败共用上面的 detail_errors）。
        "episodes_added": 0, "episodes_existing": 0,
        "skipped_no_direct_group": 0, "skipped_detail_missing": 0,
        # 源站海报只填空计数（vod_pic，2026-11-02 图片兼容根因二）。
        "posters_filled": 0,
    }


# ---------------------------------------------------------------------------
# 分类树与条目判定（纯函数）
# ---------------------------------------------------------------------------

def class_top_names(payload: dict) -> Dict[int, str]:
    """provide ``class`` 分类树 → {type_id: 顶级分类名}。

    子类沿 ``type_pid`` 回溯到 pid 为 0 的根；树缺失/成环时退回
    条目自身的 ``type_name``（调用方兜底），不猜更深的结构。
    """
    nodes: Dict[int, tuple] = {}
    for node in (payload or {}).get("class") or []:
        if not isinstance(node, dict):
            continue
        try:
            tid = int(node.get("type_id"))
        except (TypeError, ValueError):
            continue
        try:
            pid = int(node.get("type_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        nodes[tid] = (pid, str(node.get("type_name") or "").strip())
    tops: Dict[int, str] = {}
    for tid, (_pid, name) in nodes.items():
        cur, seen = tid, set()
        while cur in nodes and cur not in seen:
            seen.add(cur)
            parent, parent_name = nodes[cur]
            if parent == 0 or parent not in nodes:
                tops[tid] = parent_name if parent == 0 else name
                break
            cur = parent
        else:
            tops[tid] = name
    return tops


def _entry_top_category(entry: dict, tops: Dict[int, str]) -> str:
    try:
        tid = int(entry.get("type_id"))
    except (TypeError, ValueError):
        tid = None
    if tid is not None and tid in tops:
        return tops[tid]
    return str(entry.get("type_name") or "").strip()


def _is_series_shaped(entry: dict) -> bool:
    """剧集形态判定：``vod_serial`` 是当前集数，>1 即多集条目。

    结构信号（计数）而非内容名单：电影条目的 vod_serial 恒为
    0/1/空。一期电影库跳过这类条目，集行由三单处理。
    """
    raw = str(entry.get("vod_serial") or "").strip()
    if not raw:
        return False
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return False
    return int(digits) > 1


# ---------------------------------------------------------------------------
# detail 缓存与 harvest（地区分流专用；总库不走这里）
# ---------------------------------------------------------------------------

#: 源站填充的出处标记（metadata_source 列的暂定值）：与 tmdb /
#: douban / nfo 等「已刮到权威数据」的标记区分——带此标记说明行
#: 上文字/图片只是 CMS 源站自带资料，TMDB 命中后可盖过（见
#: tmdb.py 对该值的填空豁免）。与多源合并填充单同一口径。
CMS_METADATA_SOURCE = "applecms"


def _detail_score(value) -> Optional[float]:
    """vod_score 转 float 或 None（与多源填充同一口径）。

    CMS 的 ``0`` / ``0.0`` 是「暂无评分」占位，落成 0.0 分会把
    没评分的片显示成 0 分，故 <=0 一律按缺失处理；非法值不猜。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        score = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return score if 0 < score <= 10 else None


def _detail_people(value) -> tuple:
    """vod_actor / vod_director 人名串 → 去重保序元组（上限 10）。"""
    if not value:
        return ()
    parts = re.split(r"[,，、/;；]", str(value))
    out: List[str] = []
    for part in parts:
        name = part.strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= 10:
            break
    return tuple(out)


def _detail_blurb(detail: dict) -> str:
    """简略简介：优先 ``vod_blurb``，缺时退 ``vod_content``。

    去 HTML 标签/实体、折叠空白后截断（与多源填充同一口径）。
    """
    raw = str(detail.get("vod_blurb") or "").strip()
    if not raw:
        raw = str(detail.get("vod_content") or "").strip()
    if not raw:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:2000]


def detail_harvest(detail: dict) -> Dict[str, object]:
    """detail vod dict → 提取字段 dict（缓存与填充共用的形态）。

    键名与 ``UpstreamCatalogRecord`` 透传组一致（多源填充单的
    ``cms_entry_to_record`` 同名同义）；**只提取元数据，绝不带
    播放地址**（vod_play_url/vod_play_from 不进缓存、不落库）。
    """
    return {
        "poster_url": str(detail.get("vod_pic") or "").strip(),
        "area": str(detail.get("vod_area") or "").strip(),
        "category_name": str(detail.get("type_name") or "").strip(),
        "remarks": str(detail.get("vod_remarks") or "").strip(),
        "score": _detail_score(detail.get("vod_score")),
        "actors": list(_detail_people(detail.get("vod_actor"))),
        "directors": list(_detail_people(detail.get("vod_director"))),
        "blurb": _detail_blurb(detail),
    }


def _detail_cache_get(db: Session, source_id: int,
                      vod_id: str) -> Optional[dict]:
    row = (
        db.query(em.AppleCMSDetailCache)
        .filter(em.AppleCMSDetailCache.source_id == source_id,
                em.AppleCMSDetailCache.upstream_item_id == vod_id)
        .first())
    if row is None:
        return None
    try:
        harvest = json.loads(row.harvest or "")
    except (ValueError, TypeError):
        harvest = None
    if not isinstance(harvest, dict):
        return None
    harvest.setdefault("area", row.area or "")
    return harvest


def _detail_cache_put(db: Session, source_id: int, vod_id: str,
                      harvest: dict) -> None:
    payload = json.dumps(harvest, ensure_ascii=False)
    try:
        with db.begin_nested():
            existing = (
                db.query(em.AppleCMSDetailCache)
                .filter(em.AppleCMSDetailCache.source_id == source_id,
                        em.AppleCMSDetailCache.upstream_item_id == vod_id)
                .first())
            if existing is not None:
                existing.area = str(harvest.get("area") or "")[:200]
                existing.harvest = payload
                existing.fetched_at = datetime.now()
            else:
                db.add(em.AppleCMSDetailCache(
                    source_id=source_id, upstream_item_id=vod_id,
                    area=str(harvest.get("area") or "")[:200],
                    harvest=payload))
    except Exception:
        # 缓存写失败只是下轮多打一次 detail，不许拖垮整源同步。
        logger.warning(
            "applecms detail 缓存写入失败 source_id=%s vod_id=%s",
            source_id, vod_id, exc_info=True)


def get_or_fetch_detail_harvest(db: Session, source, vod_id: str, *,
                                http_get=None, headers=None):
    """（源, vod）的 detail 提取结果，三元组 ``(harvest, kind, fetched)``。

    - 命中缓存 → ``(harvest, None, False)``，不发请求；
    - 未命中真打一次 detail 成功 → 写缓存后 ``(harvest, None, True)``；
    - 失败（网络/上游错误）或条目已消失 → ``(None, kind, True)``，
      **不写缓存**，调用方按「本轮跳过、下轮重试」处理。

    工人与存量迁移例程共用本函数——「同一（源, vod）只打一次
    detail」只有这一处实现，不许各写一份。
    """
    cached = _detail_cache_get(db, int(source.id), vod_id)
    if cached is not None:
        return cached, None, False
    try:
        detail = acf.fetch_detail(
            source.base_url, vod_id, http_get=http_get,
            headers=headers if headers is not None
            else _source_headers(source))
    except acf.AppleCMSError as exc:
        return None, exc.kind, True
    if not isinstance(detail, dict):
        return None, "missing", True
    harvest = detail_harvest(detail)
    _detail_cache_put(db, int(source.id), vod_id, harvest)
    return harvest, None, True


def _fill_people(db: Session, row: em.MediaItem, actors) -> bool:
    """演员填空：行上还没有任何人物行时，按名单落行。

    与 enrich 侧 ``_apply_cast`` 同口径：人物行「先写者赢」——
    已有行（哪怕是另一源写的）就不合并、不重写，TMDB 命中后
    按名字补头像/id。导演只进缓存不落行：现有人物模型只有演员
    一种类型，把导演写成演员是错数据。
    """
    if not actors:
        return False
    exists = (db.query(em.EmbyPerson.id)
              .filter(em.EmbyPerson.item_id == row.id)
              .first())
    if exists:
        return False
    for idx, name in enumerate(actors):
        db.add(em.EmbyPerson(
            item_id=row.id, name=str(name)[:200], role="", image="",
            sort_order=idx, person_tmdb_id=""))
    return True


def _fill_row_from_harvest(db: Session, row: em.MediaItem,
                           harvest: dict) -> bool:
    """harvest 合行填空：只填行上为空的字段，绝不覆盖。

    落点与多源合并填充单逐项一致（沿用现有列，不新建列）：海报
    →primary_image_url、简介→overview、评分→community_rating、
    地区→countries、分类名→genres、备注→tagline、演员→
    emby_people。任一字段被填且行上还没有出处标记时，标
    ``metadata_source="applecms"``（暂定填充，TMDB 可盖过）；
    身份字段（tmdb_id 等）绝不碰。
    """
    changed = False
    poster = str(harvest.get("poster_url") or "").strip()
    if poster and not (row.primary_image_url or "").strip():
        row.primary_image_url = poster[:1024]
        changed = True
    blurb = str(harvest.get("blurb") or "").strip()
    if blurb and not (row.overview or "").strip():
        row.overview = blurb
        changed = True
    score = harvest.get("score")
    if score is not None and row.community_rating is None:
        row.community_rating = score
        changed = True
    area = str(harvest.get("area") or "").strip()
    if area and not (row.countries or "").strip():
        row.countries = area[:500]
        changed = True
    category = str(harvest.get("category_name") or "").strip()
    if category and not (row.genres or "").strip():
        row.genres = category
        changed = True
    remarks = str(harvest.get("remarks") or "").strip()
    if remarks and not (row.tagline or "").strip():
        row.tagline = remarks[:300]
        changed = True
    if _fill_people(db, row, harvest.get("actors") or ()):
        changed = True
    if changed and not (row.metadata_source or "").strip():
        row.metadata_source = CMS_METADATA_SOURCE
    return changed


def _entry_bucket(db: Session, source, entry: dict, vod_id: str,
                  stats: dict, *, http_get, headers):
    """判一条条目属哪个地区桶，返回 ``(桶键, harvest|None)``。

    list 自带 ``vod_area`` 时直接判桶（harvest 为 None：list
    字段的填充归多源填充口径，本函数只判桶）；没有才走
    detail 缓存/抓取。detail 取不到时返回 ``(None, None)``
    且已计 ``detail_errors``——绝不猜桶。
    """
    area_raw = entry.get("vod_area")
    if isinstance(area_raw, str) and area_raw.strip():
        return acl.area_bucket(area_raw), None
    harvest, _kind, fetched = get_or_fetch_detail_harvest(
        db, source, vod_id, http_get=http_get, headers=headers)
    if fetched:
        stats["detail_fetches"] += 1
    if harvest is None:
        stats["detail_errors"] += 1
        return None, None
    return acl.area_bucket(harvest.get("area")), harvest


# ---------------------------------------------------------------------------
# 单库同步
# ---------------------------------------------------------------------------

def sync_library(db: Session, library, *, http_get=None, full: bool = False,
                 max_pages: Optional[int] = None,
                 sleep_fn: Optional[Callable[[float], None]] = None
                 ) -> Dict[str, object]:
    """同步一个 CMS 库的全部配置源（电影库/剧集库）。返回统计 dict。

    ``full=False`` 为增量轮（h=24 窗口）；``full=True`` 全量（对账
    单用，seen_by_source 才是完整的源目录清单）。本函数不查总开关
    （开关在调度层，照配对工人口径）；映射开关关时整批 blocked。
    """
    stats = _zero_stats(int(library.id))
    if not acl.is_applecms_library(library):
        stats["blocked"] = "not_applecms_library"
        return stats
    collection = getattr(library, "collection_type", "") or ""
    if collection not in ("movies", "tvshows"):
        # 呈现类型只放行电影/剧集：mixed 等无行形口径，继续 blocked。
        stats["blocked"] = "unsupported_collection"
        return stats
    series_mode = collection == "tvshows"
    if not uim.mapping_enabled():
        stats["blocked"] = "mapping_off"
        return stats
    source_ids = acl.parse_upstream_source_ids(
        getattr(library, "upstream_source_ids", ""))
    if not source_ids:
        stats["blocked"] = "no_sources"
        return stats
    type_map = acl.parse_cms_type_map(
        getattr(library, "cms_type_map", "")) or {}
    area_bucket_cfg = acl.parse_cms_area_bucket(library)
    started = time.monotonic()
    run_cache: Dict[str, em.MediaItem] = {}
    for source_id in source_ids:
        _sync_source(db, library, source_id, stats, run_cache,
                     type_map=type_map, area_bucket_cfg=area_bucket_cfg,
                     http_get=http_get, full=full,
                     max_pages=max_pages, sleep_fn=sleep_fn,
                     series_mode=series_mode)
    _write_progress(db, library, stats, started)
    return stats


def _sync_source(db: Session, library, source_id: int, stats: dict,
                 run_cache: dict, *, type_map: dict, area_bucket_cfg: str = "",
                 http_get, full: bool, max_pages, sleep_fn,
                 series_mode: bool = False) -> None:
    source = us.get_source(db, source_id)
    if source is None:
        stats["errors"].append(f"source_missing:{source_id}")
        return
    if getattr(source, "source_type", None) != "applecms":
        stats["errors"].append(f"source_not_applecms:{source_id}")
        return
    if not getattr(source, "enabled", False):
        stats["errors"].append(f"source_disabled:{source_id}")
        return
    headers = _source_headers(source)
    seen: List[str] = []
    stats["seen_by_source"][source_id] = seen
    tops: Dict[int, str] = {}
    delay = acs._page_delay_sec()
    do_sleep = sleep_fn if sleep_fn is not None else time.sleep
    page_limit = max_pages or acf.DEFAULT_MAX_PAGES
    pg = 1
    try:
        while pg <= page_limit:
            payload = acf.fetch_page(
                source.base_url, ac="list", pg=pg,
                h=None if full else INCREMENTAL_WINDOW_HOURS,
                http_get=http_get, headers=headers)
            stats["pages"] += 1
            if pg == 1:
                tops = class_top_names(payload)
            entries = [e for e in (payload.get("list") or [])
                       if isinstance(e, dict)]
            for entry in entries:
                _process_entry(db, library, source, entry, stats,
                               run_cache, type_map=type_map, tops=tops,
                               seen=seen, series_mode=series_mode,
                               area_bucket_cfg=area_bucket_cfg,
                               http_get=http_get, headers=headers,
                               do_sleep=do_sleep, delay=delay)
            try:
                pagecount = int(payload.get("pagecount") or 0)
            except (TypeError, ValueError):
                pagecount = 0
            if not entries or (pagecount and pg >= pagecount):
                break
            pg += 1
            if delay > 0:
                do_sleep(delay)
        db.commit()
    except acf.AppleCMSError as exc:
        db.rollback()
        stats["errors"].append(
            f"source_error:{source_id}:{exc.kind}")
        logger.warning(
            "applecms 库同步单源失败 library_id=%s source_id=%s kind=%s",
            library.id, source_id, exc.kind)
    except Exception:
        db.rollback()
        stats["errors"].append(f"source_error:{source_id}:unexpected")
        logger.exception(
            "applecms 库同步单源异常 library_id=%s source_id=%s",
            library.id, source_id)


def _process_entry(db: Session, library, source, entry: dict,
                   stats: dict, run_cache: dict, *, type_map: dict,
                   tops: dict, seen: List[str],
                   series_mode: bool = False, area_bucket_cfg: str = "",
                   http_get=None, headers: Optional[dict] = None,
                   do_sleep=None, delay: float = 0.0) -> None:
    if series_mode:
        # 剧集路径独立成段：电影路径逐字不动（回归口径）。地区
        # 细拆走白名单口径，本路径不接 cms_area_bucket 校验。
        _process_series_entry(
            db, library, source, entry, stats, run_cache,
            type_map=type_map, tops=tops, seen=seen, http_get=http_get,
            headers=headers, do_sleep=do_sleep, delay=delay)
        return
    vod_id = str(entry.get("vod_id") or "").strip()
    if not vod_id:
        stats["skipped_invalid"] += 1
        return
    seen.append(vod_id)
    top = _entry_top_category(entry, tops)
    if type_map and type_map.get(top) != "in":
        # 非空映射 = 白名单：没标 in 的顶级分类一律不收。
        stats["skipped_category"] += 1
        return
    if _is_series_shaped(entry):
        stats["skipped_series_shape"] += 1
        return
    harvest = None
    if area_bucket_cfg:
        # 地区分流只对配了桶的库生效；没配桶的库（总库）本段
        # 整体不执行，detail 请求数为零、行为与升级前一致。
        bucket, harvest = _entry_bucket(
            db, source, entry, vod_id, stats,
            http_get=http_get, headers=headers)
        if bucket is None:
            return  # detail 取不到：detail_errors 已计，下轮重试
        if bucket != area_bucket_cfg:
            stats["skipped_area"] += 1
            return
    record = acs.cms_entry_to_record(entry, source_id=str(source.id))
    if record is None:
        stats["skipped_invalid"] += 1
        return
    key = agg.aggregate_key_for(record)
    row = run_cache.get(key)
    if row is None:
        row = _find_row(db, library, source, vod_id, record, key)
        if row is not None:
            run_cache[key] = row
    if row is _OTHER_LIBRARY:
        stats["skipped_other_library"] += 1
        return
    if row is None:
        row = _create_row(db, library, source, vod_id, record)
        run_cache[key] = row
        stats["added"] += 1
        if harvest and _fill_row_from_harvest(db, row, harvest):
            db.flush()
    else:
        changed = _refresh_row(row, record)
        if harvest and _fill_row_from_harvest(db, row, harvest):
            changed = True
        if changed:
            stats["updated"] += 1
        else:
            stats["existing"] += 1
        if row.upstream_source_id != source.id:
            stats["dedupe_hits"] += 1
    if _fill_row_poster(row, entry):
        stats["posters_filled"] += 1
    uim.record_mapping(
        db, local_item_id=row.id, source_id=source.id,
        upstream_item_id=vod_id,
        confidence=CMS_MAPPING_CONFIDENCE,
        evidence=CMS_MAPPING_EVIDENCE)
    stats["mapped"] += 1


def _process_series_entry(db: Session, library, source, entry: dict,
                          stats: dict, run_cache: dict, *,
                          type_map: dict, tops: dict, seen: List[str],
                          http_get, headers, do_sleep,
                          delay: float) -> None:
    """剧集库单条目：分类白名单 → 去重找剧行 → 拉 detail 建/补树。

    集序号解析口径：detail 的首个全直链组内 1 起序号
    （``applecms_series.plan_episodes``，与取流口同一选组逻辑）。
    解析不了（无直链组）的整剧跳过并计数留痕——建了也是播不了
    的死行；detail 已消失（源侧删条目）跳过计数；detail 请求
    失败只计 detail_errors 继续下一条，不回滚整源（拉 detail
    发生在任何写库之前，单条失败不污染事务）。源侧删集不删行：
    已有集行只增不删，存量由月度对账兜底。
    """
    # 延迟导入：applecms_series 反向引用本模块的映射常量，顶层
    # 互引会成环；接线只在剧集路径发生，电影路径不付这个成本。
    from backend.emby_server import applecms_series as acser

    vod_id = str(entry.get("vod_id") or "").strip()
    if not vod_id:
        stats["skipped_invalid"] += 1
        return
    seen.append(vod_id)
    top = _entry_top_category(entry, tops)
    if type_map and type_map.get(top) != "in":
        # 与电影库同一白名单：一库一映射，只有标 in 的顶级分类收。
        stats["skipped_category"] += 1
        return
    try:
        record = acs.cms_entry_to_record(entry, source_id=str(source.id))
    except ValueError:
        stats["skipped_invalid"] += 1
        return
    key = agg.aggregate_key_for(record)
    row = run_cache.get(key)
    if row is None:
        row = _find_series_row(db, library, source, vod_id, record, key)
        if row is not None:
            run_cache[key] = row
    if row is _OTHER_LIBRARY:
        stats["skipped_other_library"] += 1
        return
    if delay > 0 and do_sleep is not None:
        do_sleep(delay)
    try:
        detail = acf.fetch_detail(source.base_url, vod_id,
                                  http_get=http_get, headers=headers)
    except acf.AppleCMSError as exc:
        stats["detail_errors"] += 1
        logger.warning(
            "applecms 剧集 detail 失败 library_id=%s source_id=%s "
            "vod_id=%s kind=%s",
            library.id, source.id, vod_id, exc.kind)
        return
    if detail is None:
        stats["skipped_detail_missing"] += 1
        return
    if not acser.plan_episodes(detail):
        stats["skipped_no_direct_group"] += 1
        logger.info(
            "applecms 剧集无直链组跳过 library_id=%s source_id=%s "
            "vod_id=%s", library.id, source.id, vod_id)
        return
    if row is None:
        tree = acser.ensure_series_tree(db, library, source, record,
                                        detail)
        stats["added"] += 1
        row = _find_series_row(db, library, source, vod_id, record, key)
        if row is not None and row is not _OTHER_LIBRARY:
            run_cache[key] = row
    else:
        if _refresh_row(row, record):
            stats["updated"] += 1
        else:
            stats["existing"] += 1
        if row.upstream_source_id != source.id:
            stats["dedupe_hits"] += 1
        tree = acser.ensure_series_tree(db, library, source, record,
                                        detail, existing_series=row)
    # 剧行海报只填空：detail 的 vod_pic 优先（非凡等源 list 不带图、
    # detail 才带），detail 没有再看 list 条目；集行不单独填，
    # 取图链自会回退剧图。
    if row is not None and row is not _OTHER_LIBRARY:
        if _fill_row_poster(row, detail) or _fill_row_poster(row, entry):
            stats["posters_filled"] += 1
    stats["episodes_added"] += tree["episodes_added"]
    stats["episodes_existing"] += tree["episodes_existing"]
    stats["mapped"] += tree["mapped"]


def _find_series_row(db: Session, library, source, vod_id: str,
                     record, key: str):
    """剧集版找行：先按本源集映射 → 再按 guid → 再按去重键。

    与电影版同序：映射命中说明本源喂过这部剧（集映射挂在集行上，
    顺 series_id 回到剧行）；guid 命中别库同样回 _OTHER_LIBRARY
    哨兵，不抢行；去重键只在本库 series 行里比（标题+年份）。
    """
    mapping = (
        db.query(em.UpstreamItemMapping)
        .filter(em.UpstreamItemMapping.source_id == source.id,
                em.UpstreamItemMapping.upstream_item_id == vod_id,
                em.UpstreamItemMapping.episode_index.isnot(None))
        .order_by(em.UpstreamItemMapping.id)
        .first())
    if mapping is not None:
        episode_row = db.get(em.MediaItem, mapping.local_item_id)
        if episode_row is not None and episode_row.series_id:
            row = db.get(em.MediaItem, episode_row.series_id)
            if row is not None and row.library_id == library.id:
                return row
    row = uimp._usable_row_by_guid(db, uimp.hosted_guid(source.id, vod_id))
    if row is not None:
        return row if row.library_id == library.id else _OTHER_LIBRARY
    candidates = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.library_id == library.id,
                em.MediaItem.item_type == "series",
                em.MediaItem.name == record.name)
        .all())
    for cand in candidates:
        # PA-070：候选比对必须剥身份——入同步的记录恒无身份
        # （cms_entry_to_record 置 tmdb_id/imdb_id=None），去重键恒为
        # 标题+年份；已补身份的候选若带身份建键会得 tmdb:/imdb: 键，
        # 与记录键失配致建第二棵树。本函数 docstring 原本就写
        # 「去重键只在本库 series 行里比（标题+年份）」，此处使代码
        # 与口径一致。电影版 _find_row 的同构问题属 PA-062，另单处理。
        cand_record = agg.UpstreamCatalogRecord(
            source_id=str(source.id), upstream_item_id=vod_id,
            name=cand.name or "", item_type="Series",
            production_year=cand.production_year)
        if agg.aggregate_key_for(cand_record) == key:
            return cand
    return None


_OTHER_LIBRARY = object()


def _find_row(db: Session, library, source, vod_id: str, record,
              key: str):
    """找已有行：先按本源映射（同源重跑）→ 再按 guid → 再按去重键。

    guid 命中但行属别的库时返回 ``_OTHER_LIBRARY`` 哨兵：一个源
    一期只喂一个库，跨库共用同一 guid 行会把行在两库间扯动，
    宁可跳过计数，不抢行。
    """
    mapping = (
        db.query(em.UpstreamItemMapping)
        .filter(em.UpstreamItemMapping.source_id == source.id,
                em.UpstreamItemMapping.upstream_item_id == vod_id,
                em.UpstreamItemMapping.episode_index.is_(None))
        .first())
    if mapping is not None:
        row = db.get(em.MediaItem, mapping.local_item_id)
        if row is not None and row.library_id == library.id:
            return row
    row = uimp._usable_row_by_guid(db, uimp.hosted_guid(source.id, vod_id))
    if row is not None:
        return row if row.library_id == library.id else _OTHER_LIBRARY
    candidates = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.library_id == library.id,
                em.MediaItem.item_type == "movie",
                em.MediaItem.name == record.name)
        .all())
    for cand in candidates:
        cand_record = agg.UpstreamCatalogRecord(
            source_id=str(source.id), upstream_item_id=vod_id,
            name=cand.name or "", item_type="Movie",
            production_year=cand.production_year,
            tmdb_id=cand.tmdb_id or None,
            imdb_id=cand.imdb_id or None)
        if agg.aggregate_key_for(cand_record) == key:
            return cand
    return None


def _create_row(db: Session, library, source, vod_id: str,
                record) -> em.MediaItem:
    row = em.MediaItem(
        guid=uimp.hosted_guid(source.id, vod_id),
        library_id=library.id,
        item_type="movie",
        name=record.name,
        sort_name=make_sort_name(record.name),
        production_year=record.production_year,
        upstream_source_id=source.id,
        probe_status="done",
        enrich_priority=CMS_ENRICH_PRIORITY,
    )
    db.add(row)
    db.flush()
    return row


def _fill_row_poster(row: em.MediaItem, entry) -> bool:
    """源站海报只填空：行无任何图（poster_path 与 primary_image_url
    皆空）且源条目带合法 ``vod_pic`` 时才写 primary_image_url。

    TMDB / 本地图永远优先：已有任何一份图就不动（与全站「只填空」
    口径一致，enrich 侧同理只填空，两边谁先到谁生效、互不覆盖）。
    写的是远程 URL——取图端点会按需本地化/代理（media_routes），
    本工人写库时不发图片请求。填了返回 True。
    """
    if getattr(row, "poster_path", None) or getattr(row, "primary_image_url", None):
        return False
    pic = acs.cms_entry_poster(entry)
    if not pic:
        return False
    row.primary_image_url = pic
    return True


def _refresh_row(row: em.MediaItem, record) -> bool:
    """重见时刷新可变字段（名字/年份）；有变化返回 True。

    只填新值非空且不同的字段，绝不清空已有值；身份字段
    （tmdb_id 等）归 enrich 链管，本工人不碰。
    """
    changed = False
    if record.name and row.name != record.name:
        row.name = record.name
        row.sort_name = make_sort_name(record.name)
        changed = True
    if (record.production_year is not None
            and row.production_year != record.production_year):
        row.production_year = record.production_year
        changed = True
    return changed


def _write_progress(db: Session, library, stats: dict,
                    started: float) -> None:
    detail = {
        "mapped": stats["mapped"], "pages": stats["pages"],
        "skipped_category": stats["skipped_category"],
        "skipped_series_shape": stats["skipped_series_shape"],
        "skipped_invalid": stats["skipped_invalid"],
        "skipped_other_library": stats["skipped_other_library"],
        "skipped_area": stats["skipped_area"],
        "detail_errors": stats["detail_errors"],
        "detail_fetches": stats["detail_fetches"],
        "dedupe_hits": stats["dedupe_hits"],
        "episodes_added": stats["episodes_added"],
        "episodes_existing": stats["episodes_existing"],
        "skipped_no_direct_group": stats["skipped_no_direct_group"],
        "skipped_detail_missing": stats["skipped_detail_missing"],
        "posters_filled": stats["posters_filled"],
        "errors": list(stats["errors"]),
        "sources": list(stats["seen_by_source"].keys()),
    }
    payload = {
        "added": stats["added"], "updated": stats["updated"],
        "unchanged": stats["existing"], "removed": 0,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cms_sync": detail,
    }
    try:
        library.last_scan_at = datetime.now()
        library.scan_status = "failed" if (
            stats["errors"] and not stats["seen_by_source"]
        ) else ("partial" if stats["errors"] else "success")
        library.scan_stats = json.dumps(payload, ensure_ascii=False)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("applecms 库同步进度写回失败 library_id=%s",
                         library.id)


# ---------------------------------------------------------------------------
# 调度（开关默认关；照 applecms_scheduler 的循环/线程形态）
# ---------------------------------------------------------------------------

def run_library_sync_round(*, session_factory=None, http_get=None,
                           full: bool = False) -> List[dict]:
    """一轮调度：同步全部已启用的 CMS 库。总开关关时直接返回 []。"""
    if not sync_enabled():
        return []
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    results: List[dict] = []
    db = session_factory()
    try:
        libraries = (
            db.query(em.Library)
            .filter(em.Library.kind == acl.KIND_APPLECMS,
                    em.Library.is_enabled.is_(True))
            .all())
        # 地区库先跑：guid 是跨库唯一锚（一对（源, vod）只许住
        # 一个库），地区库先认领本桶条目，总库随后只收剩下的
        # 其他/未知地区；同类内按 id 稳定排序。
        libraries.sort(key=lambda lib: (
            0 if acl.parse_cms_area_bucket(lib) else 1, lib.id or 0))
    finally:
        db.close()
    for library in libraries:
        lib_db = session_factory()
        try:
            row = lib_db.get(em.Library, library.id)
            if row is None:
                continue
            results.append(sync_library(
                lib_db, row, http_get=http_get, full=full))
        except Exception:
            logger.exception("applecms 库同步整库失败 library_id=%s",
                             library.id)
            try:
                lib_db.rollback()
            except Exception:
                pass
        finally:
            lib_db.close()
    return results


def run_scheduler_loop(stop_event=None, *, run_once=None,
                       interval_sec: Optional[float] = None) -> None:
    do_run = run_once if run_once is not None else run_library_sync_round
    interval = sync_interval_sec(interval_sec)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            do_run()
        except Exception:
            logger.exception("applecms 库同步整轮失败（下一轮继续）")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_library_sync_scheduler(stop_event=None) -> bool:
    """启动库同步调度线程；开关关或已启动时返回 False。"""
    global _scheduler_thread
    if not sync_enabled():
        return False
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        thread = threading.Thread(
            target=run_scheduler_loop, args=(stop_event,),
            name="applecms-library-sync", daemon=True)
        thread.start()
        _scheduler_thread = thread
    return True
