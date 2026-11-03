"""CMS 片头印制广告「借帧修复」：查账口 + 管线核心（2026-11-03）。

站主 2026-11-03 20:24 亲定：口子开 60 秒、按播放热度先修一批。
方法照试点全链路实证（frame-graft-pilot-20261103）：同片干净版
与脏版按**解码顺序逐帧**对齐（TS 时间戳基线带偏移，按秒抽帧必
错位），带外相似度门控逐帧把条带像素从干净版贴回，音频整段
copy 零重编。任一关不过 → 不修、记原因、原样播（fail-closed，
选线降权已在兜底）。

分工：

- 本模块：配置读取（config_service 家族 ``cms_graft``，唯一实现
  走 integrations.store 热读）、修复表记账（models.CmsGraftRepair）、
  播放路径查账（:func:`lookup_verified`，只认 verified）、纯判定
  函数（条带定位/带尾/入选/熔断谓词）、拉流与帧管线（curl +
  ffmpeg + Pillow；镜像内无 numpy，不新增依赖）。
- 工人入口在 ``cms_graft_worker``（独立容器跑批，不进 api 进程）。
- 交付接线在 ``cms_playlist_proxy.apply_graft_segments`` 与 api
  取流口/二级端点：清单层只换修复窗的头部分段地址，其余分段仍
  直连源站；本服务只递修复段字节（数据卷文件，非代理转发）。

红线：修复标记不许只写不验——工人修完先落 ``repaired``，经
:func:`verify_repair` 按播放同函数组装清单 + 真解码复检后才置
``verified``；播放路径查不到 verified 就当无修复。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

WORKER_VERSION = "1.0"

#: 摸底实证的干净档源（借帧来源只许出自这几个源，见摸底报告
#: §兄弟口径：暴风 9 / 极速 10 / 电影天堂 12 / 如意 13）。
CLEAN_SOURCE_IDS = frozenset({9, 10, 12, 13})

#: 服务器直连拉流失败时允许回落家宽出口的源（非凡 7：摸底实测
#: 服务器侧存在拉不到的情形；其余源直连失败即记 fetch_failed）。
HOME_FALLBACK_SOURCE_IDS = frozenset({7})

#: 家宽路由默认名单：已知拒服务器出口的源（无尽 11 / 电影天堂
#: 12 / 优酷 17）。走 VPS 宿主机 SOCKS5（Tailscale exit node=
#: 飞牛 NAS），只抓片头分段、单独限速（见 cms_graft_worker）。
HOME_SOURCE_DEFAULT = frozenset({11, 12, 17})
HOME_SOCKS = "127.0.0.1:11055"

#: 试点门控阈值（0–255 标度，带外平均绝对差）：过门才许贴帧。
SIM_THRESHOLD = 12.0
#: 对齐可接受的带外中位差上限（试点主样本 0.18；超此判对齐失败）。
ALIGN_MAX_SCORE = 25.0
#: 条带占画面高度的合理区间（试点：顶部带约 12%）。
STRIP_MIN_FRAC = 0.02
STRIP_MAX_FRAC = 0.35

_UNSET = "\x00cms-graft-unset\x00"


# ---------------------------------------------------------------- 配置


def _raw(db, key):
    if db is None:
        return None
    try:
        from backend.integrations import store

        raw = store.get_value(db, key, _UNSET)
    except Exception:  # noqa: BLE001 — 配置读失败回代码默认，不挡播放
        return None
    return None if raw is None or raw == _UNSET else raw


def graft_enabled(db) -> bool:
    """总开关（工人 + 下发闸）：无行默认开（新功能默认开口径）。"""
    raw = _raw(db, "cms_graft_enabled")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def mouth_sec(db) -> float:
    """片头口子（秒）：带长 ≤ 此值才入选；无行默认 60（站主亲定）。"""
    raw = _raw(db, "cms_graft_mouth_sec")
    try:
        value = float(raw) if raw is not None else 60.0
    except (TypeError, ValueError):
        value = 60.0
    return min(max(value, 5.0), 600.0)


def _int_cfg(db, key, default, lo, hi) -> int:
    raw = _raw(db, key)
    try:
        value = int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return min(max(value, lo), hi)


def concurrency(db) -> int:
    return _int_cfg(db, "cms_graft_concurrency", 2, 1, 4)


def home_concurrency(db) -> int:
    return _int_cfg(db, "cms_graft_home_concurrency", 1, 1, 2)


def batch_limit(db) -> int:
    return _int_cfg(db, "cms_graft_batch_limit", 500, 1, 5000)


def heat_days(db) -> int:
    return _int_cfg(db, "cms_graft_heat_days", 14, 1, 90)


def home_source_ids(db) -> frozenset:
    """家宽路由源名单：无行用代码默认（11/12/17）；有行按逗号名单
    解析（显式空行 = 家宽路由关）。读口径照 demoted_source_ids。"""
    raw = _raw(db, "cms_graft_home_source_ids")
    if raw is None:
        return HOME_SOURCE_DEFAULT
    out = set()
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if n >= 1:
            out.add(n)
    return frozenset(out)


def route_for(db, source_id) -> str:
    """该源拉流走哪条出口：home（家宽 SOCKS）或 direct（服务器直连）。"""
    try:
        sid = int(source_id)
    except (TypeError, ValueError):
        return "direct"
    return "home" if sid in home_source_ids(db) else "direct"


# ---------------------------------------------------------------- 产物目录


def store_root() -> Path:
    """修复产物根目录（数据卷）：env CMS_GRAFT_DIR 覆盖，默认 /data/cms_graft。"""
    return Path(os.getenv("CMS_GRAFT_DIR", "/data/cms_graft"))


def mapping_dir(mapping_id) -> Path:
    return store_root() / str(int(mapping_id))


def segment_abspath(mapping_id, seg_file: str) -> Path:
    return mapping_dir(mapping_id) / Path(seg_file).name


# ---------------------------------------------------------------- 记账


def upsert_repair(db, *, mapping_id, local_item_id, source_id, status,
                  reason="", segments=None, band_end_sec=None,
                  band_family="", strip=None, sibling_mapping_id=None,
                  mouth=None, detail=""):
    """修复记账 upsert（一映射一行，后写覆盖状态类字段）。

    只更新调用方显式给出的字段：segments=None 表示不动段表，
    band_end_sec=None 表示不动带长——验证环节回写状态时不许把
    工人落的证据冲掉。
    """
    from backend.emby_server import models as em

    row = (db.query(em.CmsGraftRepair)
           .filter(em.CmsGraftRepair.mapping_id == int(mapping_id))
           .first())
    if row is None:
        row = em.CmsGraftRepair(
            mapping_id=int(mapping_id), local_item_id=int(local_item_id),
            source_id=int(source_id), status=status)
        db.add(row)
    row.local_item_id = int(local_item_id)
    row.source_id = int(source_id)
    row.status = status
    if reason:
        row.reason = reason
    if segments is not None:
        row.segments_json = json.dumps(segments, ensure_ascii=False)
    if band_end_sec is not None:
        row.band_end_sec = float(band_end_sec)
    if band_family:
        row.band_family = band_family
    if strip is not None:
        row.strip_json = json.dumps(strip, ensure_ascii=False)
    if sibling_mapping_id is not None:
        row.sibling_mapping_id = int(sibling_mapping_id)
    if mouth is not None:
        row.mouth_sec = float(mouth)
    if detail:
        row.detail = detail[:500]
    row.worker_version = WORKER_VERSION
    db.flush()
    return row


def get_repair(db, mapping_id):
    from backend.emby_server import models as em

    return (db.query(em.CmsGraftRepair)
            .filter(em.CmsGraftRepair.mapping_id == int(mapping_id))
            .first())


def repair_segments(row):
    """行里的段表（[{idx, dur, file}, …]）；坏数据按无段表处理。"""
    try:
        segs = json.loads(row.segments_json or "[]")
    except (TypeError, ValueError):
        return []
    return segs if isinstance(segs, list) else []


def lookup_verified(db, local_item_id, mapping_id, source_id):
    """播放路径查账：只有 verified 的修复才返回段表，其余一律 None。

    口径照 cms_ad_skip.lookup_trim：出错按无记录处理、绝不挡播放；
    总开关关 → 不查表（下发闸与工人开关同一键）。
    """
    if mapping_id is None:
        return None
    try:
        if not graft_enabled(db):
            return None
        row = get_repair(db, mapping_id)
        if row is None or row.status != "verified":
            return None
        if int(row.local_item_id) != int(local_item_id):
            return None
        if int(row.source_id) != int(source_id):
            return None
        segs = repair_segments(row)
        return segs or None
    except Exception:  # noqa: BLE001 — 查账失败按无修复，不挡播放
        logger.warning("cms_graft lookup failed mapping=%s",
                       mapping_id, exc_info=True)
        return None


def lookup_for_playback(db, local_item_id, mapping_id, source_id):
    """取流口/二级端点共用查账：返回 (mapping_id, segments) 或 None。

    mapping_id 缺省（旧形态 MediaSourceId）时按播放同口径取最佳
    映射（upstream_item_mappings.get_best_mapping）再查——展示与
    解析同源，不许另起一套挑选逻辑。
    """
    try:
        mid = mapping_id
        if mid is None:
            from backend.emby_server import upstream_item_mappings as uim

            best = uim.get_best_mapping(db, local_item_id)
            if best is None:
                return None
            mid = best.id
            source_id = best.source_id
        segs = lookup_verified(db, local_item_id, mid, source_id)
        if not segs:
            return None
        return int(mid), segs
    except Exception:  # noqa: BLE001 — 查账失败按无修复，不挡播放
        logger.warning("cms_graft playback lookup failed item=%s",
                       local_item_id, exc_info=True)
        return None


# ---------------------------------------------------------------- 纯判定


def locate_strip(profile):
    """按逐行差剖面定位条带行段（剖面坐标系内，返回 (y0, y1)）。

    ``profile`` = 每行一个差值（脏版 vs 干净版同帧行均差）。带 =
    显著高于基线（全行中位）的最强连续行段；段高须落在画面高度
    的 [STRIP_MIN_FRAC, STRIP_MAX_FRAC] 区间——整帧都不同（错位/
    异版）或全行均匀（无带）都返回 None（fail-closed）。
    """
    rows = [float(v) for v in profile]
    n = len(rows)
    if n < 8:
        return None
    ordered = sorted(rows)
    baseline = ordered[n // 2]
    threshold = max(10.0, baseline * 4.0)
    hot = [v >= threshold for v in rows]
    if not any(hot):
        return None
    best = None  # (总超出量, y0, y1)
    i = 0
    while i < n:
        if not hot[i]:
            i += 1
            continue
        j = i
        total = 0.0
        while j < n and hot[j]:
            total += rows[j] - baseline
            j += 1
        if best is None or total > best[0]:
            best = (total, i, j)
        i = j
    if best is None:
        return None
    _, y0, y1 = best
    # 边缘半强度行并入（羽化/抗锯齿让带边行只有一半差值）
    soft = max(6.0, threshold * 0.5)
    while y0 > 0 and rows[y0 - 1] >= soft:
        y0 -= 1
    while y1 < n and rows[y1] >= soft:
        y1 += 1
    frac = (y1 - y0) / n
    if frac < STRIP_MIN_FRAC or frac > STRIP_MAX_FRAC:
        return None
    return (y0, y1)


def band_end_from_series(diffs, *, fps, hi=8.0, sustain_sec=1.0):
    """条带差时间序列 → 带结束秒（解码轴）；带持续到序列尾返回 None。

    结束口径：最后一个「带在场」帧之后须有 ≥sustain_sec 的连续
    低差帧垫底，才承认带真的结束（单帧抖动不算）；序列在带还
    在场时就耗尽（探测窗不够长）→ None，由上层按「持续到上限」
    口径处置（角落水印型长带/超口子）。
    """
    vals = [float(v) for v in diffs]
    if not vals or fps <= 0:
        return None
    sustain = max(1, int(round(sustain_sec * fps)))
    last_hot = -1
    for i, v in enumerate(vals):
        if v >= hi:
            last_hot = i
    if last_hot < 0:
        return 0.0
    if len(vals) - (last_hot + 1) < sustain:
        return None
    return (last_hot + 1) / float(fps)


def decide_eligibility(*, band_end_sec, sibling_found, mouth_sec):
    """入选判定（fail-closed，顺序即优先级）：

    无兄弟 → no_clean_sibling；带没结束（持续到探测上限）→
    watermark_or_overlong；带长超口子 → band_too_long；全过 →
    eligible。
    """
    if not sibling_found:
        return "no_clean_sibling"
    if band_end_sec is None:
        return "watermark_or_overlong"
    if band_end_sec > mouth_sec:
        return "band_too_long"
    return "eligible"


def outside_similarity(img_a, img_b, strip):
    """两帧在条带之外的平均绝对差（0–255 标度，越小越像）。

    strip = (y0, y1)（本图坐标系行段）；上下两块分别用
    ImageStat 求差均值后按面积加权。试点门控阈值 SIM_THRESHOLD
    即按本口径（低清帧）标定。
    """
    from PIL import ImageChops, ImageStat

    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)
    diff = ImageChops.difference(
        img_a.convert("L"), img_b.convert("L"))
    w, h = diff.size
    y0, y1 = int(strip[0]), int(strip[1])
    total = 0.0
    area = 0
    for box in ((0, 0, w, max(0, y0)), (0, min(h, y1), w, h)):
        if box[3] <= box[1]:
            continue
        region = diff.crop(box)
        mean = ImageStat.Stat(region).mean[0]
        a = region.size[0] * region.size[1]
        total += mean * a
        area += a
    if area == 0:
        return 255.0
    return total / area


def host_ok(mem_available_kb, load1) -> bool:
    """工人熔断谓词：宿主机可用内存 ≥1.5GB 且 1 分钟负载 ≤40 才开工。"""
    try:
        return (float(mem_available_kb) >= 1_500_000
                and float(load1) <= 40.0)
    except (TypeError, ValueError):
        return False


def host_state():
    """读 /proc 的 (可用内存 KB, 1 分钟负载)；读不到按熔断处理。"""
    try:
        mem_kb = 0
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    mem_kb = int(line.split()[1])
                    break
        with open("/proc/loadavg", "r", encoding="ascii") as fh:
            load1 = float(fh.read().split()[0])
        return mem_kb, load1
    except Exception:  # noqa: BLE001 — 读不到宿主状态就不许开工
        return 0, 999.0


# ---------------------------------------------------------------- 兄弟与队列


def _clean_mappings_of(db, item_id, clean_ids, exclude_id=None):
    from backend.emby_server import models as em

    q = (db.query(em.UpstreamItemMapping)
         .filter(em.UpstreamItemMapping.local_item_id == int(item_id),
                 em.UpstreamItemMapping.source_id.in_(sorted(clean_ids)))
         .order_by(em.UpstreamItemMapping.confidence.desc(),
                   em.UpstreamItemMapping.id.desc()))
    rows = q.all()
    return [r for r in rows if exclude_id is None or r.id != exclude_id]


def find_sibling(db, mapping_id, clean_source_ids):
    """给一条脏映射找可借帧的干净兄弟映射（fail-closed，逐级找）：

    1. 同行：同一条目挂着的干净源映射（摸底口径「同行兄弟」）；
    2. 跨行：同库同身份（口径同 identity_sibling_ids：电影认
       tmdb 列；单集经所属 series 的 tmdb 身份成组、同季同集
       才算）别行的映射；脏映射带 episode_index 时，兄弟映射
       的 episode_index 须一致（集号对不上一律不借）。
    找不到返回 None（上层记 no_clean_sibling，不修）。
    """
    from backend.emby_server import models as em
    from backend.emby_server import upstream_item_mappings as uim

    dirty = (db.query(em.UpstreamItemMapping)
             .filter(em.UpstreamItemMapping.id == int(mapping_id))
             .first())
    if dirty is None:
        return None
    clean_ids = {int(s) for s in clean_source_ids}
    if not clean_ids:
        return None
    same = _clean_mappings_of(db, dirty.local_item_id, clean_ids,
                              exclude_id=dirty.id)
    if same:
        return same[0]
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == dirty.local_item_id).first())
    if item is None:
        return None
    # 跨行：identity_sibling_ids 已含集级口径（episode 经所属
    # series 的 tmdb 身份成组、同季同集才算兄弟行）；这里只再加
    # 一道集号硬校验：两边 episode_index 都有值时必须一致。
    for sib_id in uim.identity_sibling_ids(db, item):
        for cand in _clean_mappings_of(db, sib_id, clean_ids):
            if (dirty.episode_index is not None
                    and cand.episode_index is not None
                    and cand.episode_index != dirty.episode_index):
                continue
            return cand
    return None


def build_candidate_queue(db, *, demoted_source_ids, limit,
                          heat_days_n=14, now=None):
    """按播放热度建候选队列（工人入选池）。

    热度口径：近 ``heat_days_n`` 天 emby_play_start_events 中
    provider='upstream' 的起播次数（成败都算——用户点开过就是
    热度），按条目聚合降序；候选 = 这些条目挂着的、源在降权名单
    里的映射（同条目内按映射偏好序）。无起播记录的条目不进池
    （本批只修有人看的）。返回 [{mapping_id, local_item_id,
    source_id, heat}, …]，长度 ≤ limit。
    """
    from backend.emby_server import models as em
    from sqlalchemy import func

    now = now or datetime.now()
    since = now - timedelta(days=int(heat_days_n))
    demoted = {int(s) for s in demoted_source_ids}
    if not demoted or limit <= 0:
        return []
    # 停用中的源在解析口径里本就不可见（resolve 必 not_found），
    # 不进队列白跑（第一批实证：无尽停用期间 11 条全灭）。
    enabled = {int(sid) for (sid,) in db.query(em.UpstreamSource.id)
               .filter(em.UpstreamSource.enabled.is_(True)).all()}
    demoted &= enabled
    if not demoted:
        return []
    heat_rows = (db.query(em.PlayStartEvent.item_id,
                          func.count(em.PlayStartEvent.id))
                 .filter(em.PlayStartEvent.provider == "upstream",
                         em.PlayStartEvent.created_at >= since)
                 .group_by(em.PlayStartEvent.item_id)
                 .order_by(func.count(em.PlayStartEvent.id).desc(),
                           em.PlayStartEvent.item_id.asc())
                 .all())
    queue: list[dict] = []
    for item_id, heat in heat_rows:
        rows = (db.query(em.UpstreamItemMapping)
                .filter(em.UpstreamItemMapping.local_item_id == item_id,
                        em.UpstreamItemMapping.source_id.in_(
                            sorted(demoted)))
                .order_by(em.UpstreamItemMapping.confidence.desc(),
                          em.UpstreamItemMapping.id.desc())
                .all())
        for row in rows:
            queue.append({"mapping_id": row.id,
                          "local_item_id": row.local_item_id,
                          "source_id": row.source_id,
                          "heat": int(heat)})
            if len(queue) >= limit:
                return queue
    return queue


# ---------------------------------------------------------------- 拉流


class FetchError(Exception):
    """拉流失败（HTTP 非 200 / 网络错 / 空响应）；上层按路线处置。"""


class GraftError(Exception):
    """贴帧失败（门控不过/编码失败）：整条不修，message 带短码前缀。"""


class RouteStats:
    """路线流量记账（线程安全）：家宽账单独统计、写进跑批回报。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, int]] = {}

    def add(self, route: str, nbytes: int):
        with self._lock:
            slot = self._data.setdefault(
                route, {"bytes": 0, "requests": 0})
            slot["bytes"] += int(nbytes)
            slot["requests"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


def curl_bytes(url, headers=None, *, route="direct", timeout=30,
               stats: RouteStats | None = None) -> bytes:
    """curl 拉字节：direct 直连 / home 走家宽 SOCKS（只许拉片头级小文件）。

    照试点 fetcher 形状：服务器出口对部分 CDN 有 CONNECT 层拒绝，
    curl 是实测可用的通道；UA/请求头由调用方按源口径传入。
    """
    cmd = ["curl", "-sS", "-k", "-L", "--max-time", str(int(timeout)),
           "--retry", "1"]
    if route == "home":
        cmd += ["--socks5-hostname", HOME_SOCKS]
    for key, value in (headers or {}).items():
        cmd += ["-H", f"{key}: {value}"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"curl timeout {url}") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise FetchError(
            f"curl rc={proc.returncode} {url} "
            f"{proc.stderr.decode('utf-8', 'replace')[:120]}")
    if stats is not None:
        stats.add(route, len(proc.stdout))
    return proc.stdout


def fetch_with_route(url, headers, *, source_id, db, stats=None,
                     timeout=30) -> bytes:
    """按源选路拉字节：家宽名单走 home；直连失败且属回落名单
    （非凡 7）时改走家宽重试一次；再失败抛 FetchError。"""
    route = route_for(db, source_id)
    try:
        return curl_bytes(url, headers, route=route, timeout=timeout,
                          stats=stats)
    except FetchError:
        if route == "direct" and int(source_id) in HOME_FALLBACK_SOURCE_IDS:
            logger.info("cms_graft fetch fallback home source=%s url=%s",
                        source_id, url)
            return curl_bytes(url, headers, route="home",
                              timeout=timeout, stats=stats)
        raise


def parse_media_playlist(text: str, base_url: str):
    """媒体清单 → ([(dur, 绝对地址), …], init 段地址或 None, 密钥信息)。

    密钥信息 None 或 {method, key_url, iv(bytes|None), sequence}：
    一批降权源的 HLS 是 AES-128 加密的（第一批实证 32 条
    segment_not_ts 全是这类），段字节必须先解密才谈得上解码。
    """
    import urllib.parse

    segments = []
    init_url = None
    key_info = None
    sequence = 0
    pending = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:"):
            marker = 'URI="'
            if marker in line:
                uri = line.split(marker, 1)[1].split('"', 1)[0]
                init_url = urllib.parse.urljoin(base_url, uri)
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                sequence = 0
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = {}
            for part in line[len("#EXT-X-KEY:"):].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip().strip('"')
            method = attrs.get("METHOD", "NONE")
            if method != "NONE":
                iv = None
                iv_hex = attrs.get("IV", "")
                if iv_hex.lower().startswith("0x"):
                    try:
                        iv = bytes.fromhex(iv_hex[2:].rjust(32, "0")[-32:])
                    except ValueError:
                        iv = None
                key_info = {
                    "method": method,
                    "key_url": (urllib.parse.urljoin(
                        base_url, attrs.get("URI", ""))
                        if attrs.get("URI") else None),
                    "iv": iv,
                    "sequence": sequence,
                }
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                pending = None
            continue
        if not line or line.startswith("#"):
            continue
        segments.append((pending or 0.0,
                         urllib.parse.urljoin(base_url, line)))
        pending = None
    if key_info is not None and "sequence" not in key_info:
        key_info["sequence"] = sequence
    return segments, init_url, key_info


def fetch_key_bytes(key_info, headers, *, source_id, db, stats=None):
    """取 AES 密钥字节（同路线）；非 AES-128/取不到 → FetchError。"""
    if not key_info:
        return None
    if key_info.get("method") != "AES-128":
        raise FetchError(
            f"unsupported_key_method {key_info.get('method')}")
    if not key_info.get("key_url"):
        raise FetchError("key uri missing")
    key = fetch_with_route(key_info["key_url"], headers,
                           source_id=source_id, db=db, stats=stats,
                           timeout=30)
    if len(key) != 16:
        raise FetchError(f"aes key length {len(key)}")
    return key


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密一段（openssl CLI，镜像现成、无新依赖）；
    enc 默认处理 PKCS#7 填充。失败 → FetchError。"""
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc",
         "-K", key.hex(), "-iv", iv.hex()],
        input=data, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise FetchError("segment_decrypt_failed")
    return proc.stdout


def _segment_iv(key_info, index: int) -> bytes:
    if key_info.get("iv"):
        return key_info["iv"]
    seq = int(key_info.get("sequence") or 0) + int(index)
    return seq.to_bytes(16, "big")


def fetch_media_playlist_text(url, headers, *, source_id, db,
                              stats=None):
    """拉清单原文（主清单自动下钻一层）；返回 (媒体清单文本, 媒体清单地址)。"""
    import urllib.parse

    def _require_m3u8(payload: bytes) -> str:
        decoded = payload.decode("utf-8", "replace")
        if not decoded.lstrip("﻿ \t\r\n").startswith("#EXTM3U"):
            # 源站把 HTML 验证/错误页当清单回（第一批实证：清单是
            # 整页 HTML 时段 URL 直接解出乱码、curl rc=3）。
            raise FetchError(f"playlist_not_m3u8 {url}")
        return decoded

    raw = fetch_with_route(url, headers, source_id=source_id, db=db,
                           stats=stats)
    text = _require_m3u8(raw)
    if "#EXT-X-STREAM-INF" in text:
        variant = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                variant = urllib.parse.urljoin(url, line)
                break
        if not variant:
            raise FetchError(f"master without variant {url}")
        raw = fetch_with_route(variant, headers, source_id=source_id,
                               db=db, stats=stats)
        text = _require_m3u8(raw)
        url = variant
    return text, url


def load_media_playlist(url, headers, *, source_id, db, stats=None):
    """拉清单并解析分段表。返回 (segments, init_url, media_url)；
    变体选择取第一条可用 variant；修复产物由 verify 环节用播放
    函数对原文再核一遍（见 verify_repair）。"""
    text, url = fetch_media_playlist_text(
        url, headers, source_id=source_id, db=db, stats=stats)
    segments, init_url, key_info = parse_media_playlist(text, url)
    if not segments:
        raise FetchError(f"empty media playlist {url}")
    return segments, init_url, url, key_info


def resolve_stream(db, mapping):
    """映射 → (取流地址, 请求头)：与播放路径同一个解析口径。"""
    from backend.emby_server import upstream_playback as up

    url = up.resolve_mapped_upstream_stream(
        db, mapping.local_item_id, mapping_id=mapping.id)
    source = up.get_source_row(db, mapping.source_id)
    headers = up._source_headers(source) if source is not None else {}
    return url, headers


# ---------------------------------------------------------------- 帧管线


def ffprobe(path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise FetchError(f"ffprobe failed {path}")
    return json.loads(proc.stdout.decode("utf-8", "replace"))


def video_info(path) -> dict:
    """首个视频流的 codec/宽高/fps + 是否有音频 + 时长。"""
    data = ffprobe(path)
    info = {"codec": "", "width": 0, "height": 0, "fps": 25.0,
            "has_audio": False, "duration": 0.0}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info["codec"]:
            info["codec"] = stream.get("codec_name", "")
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            rate = stream.get("avg_frame_rate") or "25/1"
            info["fps_rate"] = rate
            try:
                num, _, den = rate.partition("/")
                info["fps"] = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                info["fps"] = 25.0
        elif stream.get("codec_type") == "audio":
            info["has_audio"] = True
    try:
        info["duration"] = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    return info


def _frame_iter(cmd, frame_bytes):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            yield buf
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        proc.kill()
        proc.wait()


def decode_frames(paths, *, width, height, fps, gray=True):
    """按解码顺序逐帧产出 PIL 帧（多文件 concat，fps/尺寸归一）。

    对齐铁律（试点实证）：两版 TS 时间戳基线带偏移，一切按解码
    顺序数帧，禁止按秒 -ss 抽帧。
    """
    from PIL import Image

    if not paths:
        return
    listfile = None
    if len(paths) > 1:
        import tempfile

        fd, listfile = tempfile.mkstemp(
            prefix="graft_concat_", suffix=".txt", dir="/tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for p in paths:
                fh.write(f"file '{p}'\n")
        src = ["-f", "concat", "-safe", "0", "-i", listfile]
    else:
        src = ["-i", str(paths[0])]
    pix = "gray" if gray else "rgb24"
    vf = f"scale={width}:{height},fps={fps}"
    cmd = (["ffmpeg", "-v", "error"] + src +
           ["-vf", vf, "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"])
    mode = "L" if gray else "RGB"
    frame_bytes = width * height * (1 if gray else 3)
    try:
        for buf in _frame_iter(cmd, frame_bytes):
            yield Image.frombytes(mode, (width, height), buf)
    finally:
        if listfile:
            try:
                os.unlink(listfile)
            except OSError:
                pass


# ---------------------------------------------------------------- 对齐与贴帧


def _mid_score(img_a, img_b):
    """对齐评分：画面中段（去顶/底各 22%）的平均绝对差。"""
    from PIL import ImageChops, ImageStat

    w, h = img_a.size
    box = (0, int(h * 0.22), w, int(h * 0.78))
    diff = ImageChops.difference(
        img_a.convert("L"), img_b.convert("L")).crop(box)
    return ImageStat.Stat(diff).mean[0]


def analyze_pair(dirty_paths, clean_paths, info, *, horizon_sec):
    """脏/净一对片头的解码顺序对齐 + 条带定位 + 带尾判定。

    返回 dict：ok=False 时带 reason（align_failed / no_band_found）；
    ok=True 时带 offset_frames（净版解码帧相对脏版的偏移，正 =
    净版要多跳过这么多帧才与脏版第 0 帧对齐）、strip_low /
    strip_full（(y0, y1) 行段）、band_end_sec（脏版解码轴秒数，
    带持续到探测窗尾时为 None）、outside_median、family。
    """
    from PIL import ImageChops

    fps = float(info["fps"])
    width, height = int(info["width"]), int(info["height"])
    aw = 320
    ah = max(2, int(round(aw * height / max(1, width))))
    # ---- 粗对齐：5fps 采样、整数采样帧偏移搜索（试点同款）
    sw = 256
    sh = max(2, int(round(sw * height / max(1, width))))
    dirty_samples = []
    for i, frame in enumerate(
            decode_frames(dirty_paths, width=sw, height=sh, fps=5)):
        dirty_samples.append(frame)
        if i >= 299:
            break
    clean_samples = []
    for i, frame in enumerate(
            decode_frames(clean_paths, width=sw, height=sh, fps=5)):
        clean_samples.append(frame)
        if i >= 499:
            break
    if len(dirty_samples) < 10 or len(clean_samples) < 10:
        return {"ok": False, "reason": "align_failed",
                "detail": "samples too few"}
    best = None  # (score, offset_samples)
    for off in range(-25, 151):
        pairs = 0
        total = 0.0
        for k, dframe in enumerate(dirty_samples[:120]):
            j = k + off
            if 0 <= j < len(clean_samples):
                total += _mid_score(dframe, clean_samples[j])
                pairs += 1
        if pairs >= 20:
            score = total / pairs
            if best is None or score < best[0]:
                best = (score, off)
    if best is None or best[0] > ALIGN_MAX_SCORE:
        return {"ok": False, "reason": "align_failed",
                "detail": f"best_score={best[0] if best else None}"}
    offset_frames = int(round(best[1] / 5.0 * fps))
    # ---- 全帧率低清配对：行差剖面（前 12 秒）→ 条带 → 带尾序列
    dirty_gen = decode_frames(dirty_paths, width=aw, height=ah, fps=fps)
    clean_gen = decode_frames(clean_paths, width=aw, height=ah, fps=fps)
    if offset_frames >= 0:
        for _ in range(offset_frames):
            if next(clean_gen, None) is None:
                break
        dirty_base = 0
    else:
        for _ in range(-offset_frames):
            if next(dirty_gen, None) is None:
                break
        dirty_base = -offset_frames
    horizon_frames = int(horizon_sec * fps)
    profile = [0.0] * ah
    profile_n = 0
    buffered: list[tuple[int, object, object]] = []
    strip = None
    strip_diffs: list[float] = []
    outside_samples: list[float] = []
    band_end = None
    phase_a_frames = int(12 * fps)
    idx = dirty_base - 1
    for dframe in dirty_gen:
        idx += 1
        cframe = next(clean_gen, None)
        if cframe is None:
            break
        rel = idx - dirty_base
        if rel >= horizon_frames:
            break
        if strip is None:
            diff = ImageChops.difference(dframe, cframe)
            data = diff.tobytes()
            for y in range(ah):
                row = data[y * aw:(y + 1) * aw]
                profile[y] += sum(row) / aw
            profile_n += 1
            buffered.append((idx, dframe, cframe))
            if rel + 1 < phase_a_frames:
                continue
            # 剖面攒够：定位条带，并把缓冲帧的带差补算进序列
            avg = [v / max(1, profile_n) for v in profile]
            strip = locate_strip(avg)
            if strip is None:
                full_med = sorted(
                    outside_similarity(d, c, (0, 0))
                    for _, d, c in buffered[::10])
                med = full_med[len(full_med) // 2] if full_med else 255.0
                reason = ("align_failed" if med > ALIGN_MAX_SCORE
                          else "no_band_found")
                return {"ok": False, "reason": reason,
                        "detail": f"full_med={med:.1f}"}
            for bidx, bd, bc in buffered:
                region = ImageChops.difference(bd, bc).crop(
                    (0, strip[0], aw, strip[1]))
                from PIL import ImageStat

                strip_diffs.append(ImageStat.Stat(region).mean[0])
                if bidx % 5 == 0:
                    outside_samples.append(
                        outside_similarity(bd, bc, strip))
            buffered = []
            band_end = band_end_from_series(strip_diffs, fps=fps)
            if band_end is not None:
                break
            continue
        from PIL import ImageStat

        region = ImageChops.difference(dframe, cframe).crop(
            (0, strip[0], aw, strip[1]))
        strip_diffs.append(ImageStat.Stat(region).mean[0])
        if rel % 5 == 0:
            outside_samples.append(
                outside_similarity(dframe, cframe, strip))
        band_end = band_end_from_series(strip_diffs, fps=fps)
        if band_end is not None:
            break
    if strip is None:
        # 流在剖面窗口攒满前就结束（短片头）：用已攒剖面兜底定位，
        # 缓冲帧补算带差序列；攒得太少（<15 帧）才判无带。
        if profile_n < 15:
            return {"ok": False, "reason": "no_band_found",
                    "detail": "stream shorter than profile window"}
        avg = [v / max(1, profile_n) for v in profile]
        strip = locate_strip(avg)
        if strip is None:
            full_med = sorted(
                outside_similarity(d, c, (0, 0))
                for _, d, c in buffered[::10])
            med = full_med[len(full_med) // 2] if full_med else 255.0
            reason = ("align_failed" if med > ALIGN_MAX_SCORE
                      else "no_band_found")
            return {"ok": False, "reason": reason,
                    "detail": f"short stream full_med={med:.1f}"}
        from PIL import ImageStat

        for bidx, bd, bc in buffered:
            region = ImageChops.difference(bd, bc).crop(
                (0, strip[0], aw, strip[1]))
            strip_diffs.append(ImageStat.Stat(region).mean[0])
            if bidx % 5 == 0:
                outside_samples.append(
                    outside_similarity(bd, bc, strip))
        band_end = band_end_from_series(strip_diffs, fps=fps)
    outside_samples.sort()
    outside_med = (outside_samples[len(outside_samples) // 2]
                   if outside_samples else 255.0)
    if outside_med > ALIGN_MAX_SCORE:
        return {"ok": False, "reason": "align_failed",
                "detail": f"outside_med={outside_med:.1f}"}
    if band_end is not None and dirty_base:
        # 净版领先时脏版解码轴被跳过 dirty_base 帧，带尾换算回脏轴
        band_end = band_end + dirty_base / fps
    y0 = int(strip[0] * height / ah)
    y1 = int(strip[1] * height / ah)
    if y1 <= height * 0.18:
        family = "D"
    elif y0 >= height * 0.8:
        family = "A"
    else:
        family = "B"
    return {"ok": True, "offset_frames": offset_frames,
            "strip_low": strip, "strip_full": (y0, y1),
            "band_end_sec": band_end, "outside_median": outside_med,
            "family": family}


def _strip_uniform_dark(frame, strip) -> bool:
    """脏帧条带区是否近均匀暗色（许中值填充的唯一情形，试点口径）。"""
    from PIL import ImageStat

    region = frame.convert("L").crop(
        (0, int(strip[0]), frame.size[0], int(strip[1])))
    stat = ImageStat.Stat(region)
    return stat.mean[0] < 60.0 and stat.stddev[0] < 12.0


def _feather_mask(width, y0e, y1e, height, feather=6):
    """条带（含羽化边）的 alpha 掩膜（整帧尺寸，带外为 0）。"""
    from PIL import Image, ImageDraw

    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([0, y0e, width, y1e], fill=255)
    for i in range(feather):
        alpha = int(255 * (i + 1) / (feather + 1))
        if y0e - 1 - i >= 0:
            draw.line([0, y0e - 1 - i, width, y0e - 1 - i], fill=alpha)
        if y1e + 1 + i < height:
            draw.line([0, y1e + 1 + i, width, y1e + 1 + i], fill=alpha)
    return mask


def _start_encoder(out_path, seg_path, info):
    """逐段编码器：rawvideo 帧进、TS 出；音频从原段整段 copy（零重编）。"""
    width, height = int(info["width"]), int(info["height"])
    rate = str(info.get("fps_rate") or f"{float(info['fps']):.4f}")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", rate, "-i", "pipe:0"]
    if info.get("has_audio"):
        cmd += ["-i", str(seg_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-f", "mpegts", str(out_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def graft_window(dirty_seg_paths, clean_paths, info, *, strip_full,
                 offset_frames, band_end_sec, out_dir: Path):
    """贴帧主pass：逐段解码脏版、全清配对、门控贴回、逐段出 TS。

    返回 (segments, counters)：segments=[{idx, dur(EXTINF 由上层
    填), file, frames, bytes}]；任一「带在场帧」三路皆不过 →
    抛 GraftError('gate_failed')（整条不修，fail-closed）。
    """
    fps = float(info["fps"])
    width, height = int(info["width"]), int(info["height"])
    aw = 320
    ah = max(2, int(round(aw * height / max(1, width))))
    strip_low = (int(strip_full[0] * ah / height),
                 int(strip_full[1] * ah / height))
    y0e = max(0, int(strip_full[0]) - 6)
    y1e = min(height, int(strip_full[1]) + 6)
    mask = _feather_mask(width, y0e, y1e, height)
    clean_gen = decode_frames(clean_paths, width=width, height=height,
                              fps=fps, gray=False)
    if offset_frames >= 0:
        for _ in range(offset_frames):
            if next(clean_gen, None) is None:
                break
        dirty_skip = 0
    else:
        dirty_skip = -offset_frames
    counters = {"pasted": 0, "filled": 0, "kept": 0}
    segments = []
    global_idx = -1
    out_dir.mkdir(parents=True, exist_ok=True)
    for seg_idx, seg_path in enumerate(dirty_seg_paths):
        tmp_out = out_dir / f"seg_{seg_idx:03d}.ts.tmp"
        encoder = _start_encoder(tmp_out, seg_path, info)
        frames_here = 0
        failed = None
        gen = decode_frames([seg_path], width=width, height=height,
                            fps=fps, gray=False)
        for dframe in gen:
            global_idx += 1
            cframe = next(clean_gen, None)
            if global_idx < dirty_skip:
                # 净版领先段：这些脏帧无配对（对齐基点之前），
                # 只可能出现在带窗外（带从 0 起），原样编码。
                encoder.stdin.write(dframe.tobytes())
                frames_here += 1
                counters["kept"] += 1
                continue
            t = global_idx / fps
            out_frame = dframe
            if t <= band_end_sec + 0.5:
                gate = 999.0
                if cframe is not None:
                    gate = outside_similarity(
                        dframe.resize((aw, ah)).convert("L"),
                        cframe.resize((aw, ah)).convert("L"), strip_low)
                if gate <= SIM_THRESHOLD and cframe is not None:
                    out_frame = dframe.copy()
                    out_frame.paste(cframe, (0, 0), mask)
                    counters["pasted"] += 1
                elif _strip_uniform_dark(dframe, strip_full):
                    from PIL import ImageDraw

                    fill = (dframe.crop(
                        (0, int(strip_full[0]), width, int(strip_full[1])))
                        .resize((1, 1)).getpixel((0, 0)))
                    out_frame = dframe.copy()
                    ImageDraw.Draw(out_frame).rectangle(
                        [0, y0e, width, y1e], fill=fill)
                    counters["filled"] += 1
                elif t < band_end_sec:
                    failed = (f"gate_failed frame={global_idx} "
                              f"t={t:.2f} gate={gate:.1f}")
                    break
                else:
                    counters["kept"] += 1
            else:
                counters["kept"] += 1
            encoder.stdin.write(out_frame.tobytes())
            frames_here += 1
        try:
            encoder.stdin.close()
        except BrokenPipeError:
            pass
        rc = encoder.wait()
        if failed is not None:
            tmp_out.unlink(missing_ok=True)
            raise GraftError(failed)
        if rc != 0 or frames_here == 0:
            tmp_out.unlink(missing_ok=True)
            raise GraftError(f"encode_failed seg={seg_idx} rc={rc}")
        final = out_dir / f"seg_{seg_idx:03d}.ts"
        os.replace(tmp_out, final)
        segments.append({"idx": seg_idx, "file": final.name,
                         "frames": frames_here,
                         "bytes": final.stat().st_size})
    if counters["pasted"] + counters["filled"] == 0:
        raise GraftError("gate_failed no frame grafted")
    return segments, counters


def _download_segments(segments, headers, *, source_id, db, stats,
                       work_dir: Path, prefix, cover_sec,
                       key_info=None, key_bytes=None):
    """按清单序下载分段直到累计时长 ≥ cover_sec；返回 (路径表, 累计时长)。

    清单带 EXT-X-KEY（AES-128）时先解密再验真/落盘：调用方用
    fetch_key_bytes 取好密钥传入；没传密钥直接 FetchError，
    不把密文当坏段误记 segment_not_ts。
    """
    if key_info is not None and key_bytes is None:
        raise FetchError("key_bytes_missing")
    paths = []
    total = 0.0
    for i, (dur, seg_url) in enumerate(segments):
        if total >= cover_sec:
            break
        data = fetch_with_route(seg_url, headers, source_id=source_id,
                                db=db, stats=stats, timeout=60)
        if key_info is not None:
            data = decrypt_segment(data, key_bytes,
                                   _segment_iv(key_info, i))
        if not data or (data[0] != 0x47 and b"ftyp" not in data[:12]):
            # 既不是 TS（同步字节 0x47）也不是 fMP4（ftyp 盒）=
            # 下回来的不是视频段（HTML 错误页等，第一批实证 5 条
            # ffprobe failed 的共同形态）；门口拦下记清原因，
            # 不进解码白烧 CPU。
            raise FetchError(f"segment_not_ts {seg_url}")
        path = work_dir / f"{prefix}_{i:03d}.ts"
        path.write_bytes(data)
        paths.append(path)
        total += float(dur or 0.0)
    return paths, total


def _strip_diff(img_a, img_b, strip) -> float:
    """两帧条带区的平均绝对差（同尺寸假设，不同时先对齐到 a）。"""
    from PIL import ImageChops, ImageStat

    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)
    region = ImageChops.difference(
        img_a.convert("L"), img_b.convert("L")).crop(
        (0, int(strip[0]), img_a.size[0], int(strip[1])))
    return ImageStat.Stat(region).mean[0]


def process_pair(db, dirty, sibling, *, mouth, stats=None):
    """一对映射的完整修复管线（探测→判定→贴帧→段级自检）。

    返回 outcome dict：status ∈ repaired / skipped / failed，
    reason 短码，repaired 时带 segments/strip/band_end_sec。
    只写产物文件，不写库（记账由 process_candidate 统一落）。
    """
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix=f"graft_{dirty.id}_", dir="/tmp"))
    try:
        durl, dhdr = resolve_stream(db, dirty)
        curl_, chdr = resolve_stream(db, sibling)
        dsegs, dinit, _, dkey = load_media_playlist(
            durl, dhdr, source_id=dirty.source_id, db=db, stats=stats)
        csegs, cinit, _, ckey = load_media_playlist(
            curl_, chdr, source_id=sibling.source_id, db=db, stats=stats)
        if dinit or cinit:
            return {"status": "skipped",
                    "reason": "unsupported_init_segment"}
        dkey_bytes = fetch_key_bytes(
            dkey, dhdr, source_id=dirty.source_id, db=db, stats=stats)
        ckey_bytes = fetch_key_bytes(
            ckey, chdr, source_id=sibling.source_id, db=db, stats=stats)
        horizon = float(mouth) + 30.0
        dpaths, _ = _download_segments(
            dsegs, dhdr, source_id=dirty.source_id, db=db, stats=stats,
            work_dir=work, prefix="dirty", cover_sec=horizon,
            key_info=dkey, key_bytes=dkey_bytes)
        cpaths, _ = _download_segments(
            csegs, chdr, source_id=sibling.source_id, db=db, stats=stats,
            work_dir=work, prefix="clean", cover_sec=horizon + 45.0,
            key_info=ckey, key_bytes=ckey_bytes)
        if not dpaths or not cpaths:
            return {"status": "failed", "reason": "fetch_failed",
                    "detail": "no segments downloaded"}
        info = video_info(dpaths[0])
        if info["codec"] != "h264":
            return {"status": "skipped", "reason": "codec_unsupported",
                    "detail": f"codec={info['codec']}"}
        analysis = analyze_pair(dpaths, cpaths, info, horizon_sec=horizon)
        if not analysis["ok"]:
            status = ("skipped" if analysis["reason"] == "no_band_found"
                      else "failed")
            return {"status": status, "reason": analysis["reason"],
                    "detail": analysis.get("detail", "")}
        band_end = analysis["band_end_sec"]
        decision = decide_eligibility(
            band_end_sec=band_end, sibling_found=True, mouth_sec=mouth)
        if decision != "eligible":
            return {"status": "skipped", "reason": decision,
                    "band_end_sec": band_end,
                    "band_family": analysis["family"],
                    "strip": {"y0": analysis["strip_full"][0],
                              "y1": analysis["strip_full"][1],
                              "h": info["height"],
                              "offset": analysis["offset_frames"]}}
        # 修复窗 = 覆盖带尾的那一段为止的整段集合
        window = 0
        cum = 0.0
        for i, (dur, _u) in enumerate(dsegs):
            cum += float(dur or 0.0)
            window = i + 1
            if cum >= band_end:
                break
        if cum < band_end or window > len(dpaths):
            return {"status": "failed", "reason": "probe_failed",
                    "detail": f"window={window} cum={cum:.1f}"}
        out_dir = mapping_dir(dirty.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("seg_*.ts"):
            old.unlink(missing_ok=True)
        segments, counters = graft_window(
            dpaths[:window], cpaths, info,
            strip_full=analysis["strip_full"],
            offset_frames=analysis["offset_frames"],
            band_end_sec=band_end, out_dir=out_dir)
        for i, seg in enumerate(segments):
            seg["dur"] = float(dsegs[i][0] or 0.0)
            # 段级自检：时长与原段帧数口径对齐、音频在否与原段一致
            vinfo = video_info(out_dir / seg["file"])
            expect = seg["frames"] / float(info["fps"])
            oinfo = video_info(dpaths[i])
            if (abs(vinfo["duration"] - expect) > 0.75
                    or vinfo["has_audio"] != oinfo["has_audio"]):
                for seg2 in segments:
                    (out_dir / seg2["file"]).unlink(missing_ok=True)
                return {"status": "failed", "reason": "post_check_failed",
                        "detail": f"seg={i} dur={vinfo['duration']:.2f} "
                                  f"expect={expect:.2f}"}
        return {"status": "repaired", "reason": "",
                "segments": segments, "band_end_sec": band_end,
                "band_family": analysis["family"],
                "strip": {"y0": analysis["strip_full"][0],
                          "y1": analysis["strip_full"][1],
                          "h": info["height"],
                          "offset": analysis["offset_frames"]},
                "detail": json.dumps(
                    {"counters": counters,
                     "outside_median": round(
                         analysis["outside_median"], 2),
                     "window_sec": round(cum, 2)},
                    ensure_ascii=False)}
    except FetchError as exc:
        return {"status": "failed", "reason": "fetch_failed",
                "detail": str(exc)[:200]}
    except GraftError as exc:
        reason = str(exc).split(" ", 1)[0] or "gate_failed"
        return {"status": "failed", "reason": reason,
                "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — 单条失败不许带崩整批
        from backend.emby_server.upstream_playback import (
            UpstreamPlaybackError)

        if isinstance(exc, UpstreamPlaybackError):
            return {"status": "failed", "reason": "fetch_failed",
                    "detail": f"resolve {exc.code}"}
        logger.warning("cms_graft process_pair error mapping=%s",
                       dirty.id, exc_info=True)
        return {"status": "failed", "reason": "process_error",
                "detail": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def _frame_at(paths, index, *, width, height, fps):
    """解码顺序取第 index 帧（RGB 原尺寸）；越界返回 None。"""
    for i, frame in enumerate(
            decode_frames(paths, width=width, height=height, fps=fps,
                          gray=False)):
        if i == index:
            return frame
        if i > index:
            break
    return None


def verify_repair(db, mapping_id, *, stats=None):
    """修后验证（标记不许只写不验）：按播放同函数组装清单 + 真解码复检。

    1. 现拉脏版清单原文，走 cms_playlist_proxy.apply_graft_segments
       （播放路径同一个函数）——替换不成立（清单已变）即 verify_failed；
    2. 修复段文件逐段可解码、时长与落库段表对齐；
    3. 带中点帧复检：修复帧条带须与脏帧明显不同、且（兄弟可达时）
       与干净帧条带接近——证下发的就是修复版而不是原版字节。
    网络类失败（源站临时不可达）不判死：保持 repaired、记
    verify_deferred，下轮再验。返回 outcome dict。
    """
    from backend.emby_server import cms_playlist_proxy as proxy
    from backend.emby_server import models as em

    row = get_repair(db, mapping_id)
    if row is None or row.status != "repaired":
        return {"status": row.status if row else "missing"}
    segments = repair_segments(row)
    dirty = (db.query(em.UpstreamItemMapping)
             .filter(em.UpstreamItemMapping.id == int(mapping_id))
             .first())
    if dirty is None or not segments:
        upsert_repair(db, mapping_id=mapping_id,
                      local_item_id=row.local_item_id,
                      source_id=row.source_id, status="verify_failed",
                      reason="mapping_gone")
        db.commit()
        return {"status": "verify_failed", "reason": "mapping_gone"}
    try:
        url, headers = resolve_stream(db, dirty)
        text, media_url = fetch_media_playlist_text(
            url, headers, source_id=dirty.source_id, db=db, stats=stats)
    except Exception as exc:  # noqa: BLE001 — 验时网络失败先缓判
        upsert_repair(db, mapping_id=mapping_id,
                      local_item_id=row.local_item_id,
                      source_id=row.source_id, status="repaired",
                      reason="verify_deferred",
                      detail=f"{type(exc).__name__}"[:200])
        db.commit()
        return {"status": "repaired", "reason": "verify_deferred"}
    public = [{"idx": s["idx"], "dur": s["dur"]} for s in segments]
    _new_text, astats = proxy.apply_graft_segments(
        text, public, lambda i: f"graft://{mapping_id}/{i}")
    if not astats["graft_applied"]:
        upsert_repair(db, mapping_id=mapping_id,
                      local_item_id=row.local_item_id,
                      source_id=row.source_id, status="verify_failed",
                      reason="playlist_changed")
        db.commit()
        return {"status": "verify_failed", "reason": "playlist_changed"}
    # 段级解码与时长复检
    for seg in segments:
        path = segment_abspath(mapping_id, seg["file"])
        if not path.exists():
            upsert_repair(db, mapping_id=mapping_id,
                          local_item_id=row.local_item_id,
                          source_id=row.source_id,
                          status="verify_failed",
                          reason="segment_missing",
                          detail=seg["file"])
            db.commit()
            return {"status": "verify_failed",
                    "reason": "segment_missing"}
        vinfo = video_info(path)
        expect = seg["frames"] / max(1.0, _seg_fps(vinfo))
        if abs(vinfo["duration"] - expect) > 0.9:
            upsert_repair(db, mapping_id=mapping_id,
                          local_item_id=row.local_item_id,
                          source_id=row.source_id,
                          status="verify_failed",
                          reason="segment_duration",
                          detail=f"seg={seg['idx']} "
                                 f"dur={vinfo['duration']:.2f}")
            db.commit()
            return {"status": "verify_failed",
                    "reason": "segment_duration"}
    # 带中点帧复检（有带信息时）
    strip = {}
    try:
        strip = json.loads(row.strip_json or "{}")
    except ValueError:
        strip = {}
    if strip.get("y0") is not None and row.band_end_sec:
        verdict = _verify_frame(db, row, segments, strip, text,
                                media_url, stats=stats)
        if verdict is not None:
            upsert_repair(db, mapping_id=mapping_id,
                          local_item_id=row.local_item_id,
                          source_id=row.source_id,
                          status="verify_failed", reason=verdict[0],
                          detail=verdict[1])
            db.commit()
            return {"status": "verify_failed", "reason": verdict[0]}
    upsert_repair(db, mapping_id=mapping_id,
                  local_item_id=row.local_item_id,
                  source_id=row.source_id, status="verified", reason="")
    db.commit()
    return {"status": "verified", "reason": ""}


def _seg_fps(vinfo) -> float:
    return float(vinfo.get("fps") or 25.0)


def _verify_frame(db, row, segments, strip, playlist_text, media_url,
                  *, stats=None):
    """带中点帧三方比对；返回 None=过，否则 (reason, detail)。

    网络失败返回 None（本帧复检缓判，不因验方网络问题判死修复；
    清单组装与段解码两关已在上层验过）。
    """
    import tempfile

    from backend.emby_server import models as em

    try:
        band_mid = float(row.band_end_sec) / 2.0
        # 目标段与段内帧号：段表 frames 是脏版解码帧数（贴帧时逐段
        # 计数），带中点全局帧号减段起始帧号即段内帧号（解码轴）。
        rep_path0 = segment_abspath(row.mapping_id, segments[0]["file"])
        fps = _seg_fps(video_info(rep_path0))
        global_idx = int(band_mid * fps)
        acc_frames = 0
        seg = segments[-1]
        seg_start_frames = 0
        for cand in segments:
            if acc_frames + cand["frames"] > global_idx:
                seg, seg_start_frames = cand, acc_frames
                break
            acc_frames += cand["frames"]
        else:
            seg_start_frames = acc_frames
        rep_path = segment_abspath(row.mapping_id, seg["file"])
        rinfo = video_info(rep_path)
        fps = _seg_fps(rinfo)
        local_idx = max(0, global_idx - seg_start_frames)
        rep = _frame_at([rep_path], local_idx, width=rinfo["width"],
                        height=rinfo["height"], fps=fps)
        if rep is None:
            return ("frame_check_failed", "repaired frame missing")
        # 脏版同段同帧
        dsegs, _init, dkey = parse_media_playlist(playlist_text, media_url)
        durl, dhdr = None, None
        dirty = (db.query(em.UpstreamItemMapping)
                 .filter(em.UpstreamItemMapping.id == row.mapping_id)
                 .first())
        durl, dhdr = resolve_stream(db, dirty)
        with tempfile.TemporaryDirectory(prefix="graft_verify_") as td:
            dpath = Path(td) / "dirty.ts"
            data = fetch_with_route(
                dsegs[seg["idx"]][1], dhdr, source_id=dirty.source_id,
                db=db, stats=stats, timeout=60)
            if dkey is not None:
                dkey_bytes = fetch_key_bytes(
                    dkey, dhdr, source_id=dirty.source_id, db=db,
                    stats=stats)
                data = decrypt_segment(
                    data, dkey_bytes, _segment_iv(dkey, seg["idx"]))
            dpath.write_bytes(data)
            dinfo = video_info(dpath)
            dframe = _frame_at([dpath], local_idx,
                               width=dinfo["width"],
                               height=dinfo["height"],
                               fps=_seg_fps(dinfo))
            if dframe is None:
                return None
            strip_t = (int(strip["y0"]), int(strip["y1"]))
            diff_rep_dirty = _strip_diff(rep, dframe, strip_t)
            # 兄弟帧（可达时）：修复帧应明显比脏帧更接近干净帧
            if row.sibling_mapping_id:
                sibling = (db.query(em.UpstreamItemMapping)
                           .filter(em.UpstreamItemMapping.id
                                   == row.sibling_mapping_id).first())
                if sibling is not None:
                    curl_, chdr = resolve_stream(db, sibling)
                    csegs, _ci, _cu, ckey = load_media_playlist(
                        curl_, chdr, source_id=sibling.source_id, db=db,
                        stats=stats)
                    ckey_bytes = fetch_key_bytes(
                        ckey, chdr, source_id=sibling.source_id, db=db,
                        stats=stats)
                    cpaths, _tot = _download_segments(
                        csegs, chdr, source_id=sibling.source_id, db=db,
                        stats=stats, work_dir=Path(td), prefix="clean",
                        cover_sec=band_mid + 15.0,
                        key_info=ckey, key_bytes=ckey_bytes)
                    if cpaths:
                        cinfo = video_info(cpaths[0])
                        gidx = (int(band_mid * _seg_fps(cinfo))
                                + int(strip.get("offset") or 0))
                        cframe = _frame_at(
                            cpaths, gidx, width=cinfo["width"],
                            height=cinfo["height"], fps=_seg_fps(cinfo))
                        if cframe is not None:
                            diff_dirty_clean = _strip_diff(
                                dframe, cframe, strip_t)
                            diff_rep_clean = _strip_diff(
                                rep, cframe, strip_t)
                            if (diff_dirty_clean >= 8.0
                                    and diff_rep_clean > 18.0):
                                return ("frame_check_failed",
                                        f"rep_clean={diff_rep_clean:.1f}")
                            if (diff_dirty_clean >= 8.0
                                    and diff_rep_clean
                                    >= diff_dirty_clean - 3.0):
                                return ("frame_check_failed",
                                        f"rep_clean={diff_rep_clean:.1f} "
                                        f"dirty_clean="
                                        f"{diff_dirty_clean:.1f}")
                            return None
            if diff_rep_dirty < 5.0:
                return ("frame_check_failed",
                        f"rep_dirty={diff_rep_dirty:.1f} (band signal "
                        f"weak or unchanged)")
            return None
    except Exception:  # noqa: BLE001 — 帧复检自身异常缓判（见 docstring）
        logger.warning("cms_graft frame verify deferred mapping=%s",
                       row.mapping_id, exc_info=True)
        return None


def process_candidate(db, candidate, *, mouth, stats=None, verdict=None):
    """工人单条全流程：入选判定 → 修复 → 验证，终局状态落库。

    返回 outcome dict（供跑批统计）：{mapping_id, status, reason,
    band_end_sec, heat}。终局状态：verified / repaired（验证缓判）/
    verify_failed / skipped / failed。
    """
    from backend.emby_server import models as em

    mapping_id = int(candidate["mapping_id"])
    existing = get_repair(db, mapping_id)
    if existing is not None and existing.status == "verified":
        return {"mapping_id": mapping_id, "status": "verified",
                "reason": "already", "heat": candidate.get("heat")}
    dirty = (db.query(em.UpstreamItemMapping)
             .filter(em.UpstreamItemMapping.id == mapping_id).first())
    if dirty is None:
        return {"mapping_id": mapping_id, "status": "failed",
                "reason": "mapping_gone", "heat": candidate.get("heat")}
    upsert_repair(db, mapping_id=mapping_id,
                  local_item_id=dirty.local_item_id,
                  source_id=dirty.source_id, status="running",
                  mouth=mouth)
    db.commit()
    sibling = find_sibling(db, mapping_id, CLEAN_SOURCE_IDS)
    if sibling is None:
        upsert_repair(db, mapping_id=mapping_id,
                      local_item_id=dirty.local_item_id,
                      source_id=dirty.source_id, status="skipped",
                      reason="no_clean_sibling", mouth=mouth)
        db.commit()
        return {"mapping_id": mapping_id, "status": "skipped",
                "reason": "no_clean_sibling",
                "heat": candidate.get("heat")}
    # 摸底判定复用（现成数据不重测）：带长已判超口子/长带持续的
    # 直接跳过，省家宽与 CPU；判定含糊的一律走真实管线复核。
    if verdict:
        end_sec = verdict.get("end_sec")
        horizon = float(verdict.get("horizon") or 0.0)
        if end_sec is not None and float(end_sec) > mouth:
            upsert_repair(db, mapping_id=mapping_id,
                          local_item_id=dirty.local_item_id,
                          source_id=dirty.source_id, status="skipped",
                          reason="band_too_long",
                          band_end_sec=float(end_sec),
                          sibling_mapping_id=sibling.id, mouth=mouth,
                          detail="verdict_seed")
            db.commit()
            return {"mapping_id": mapping_id, "status": "skipped",
                    "reason": "band_too_long",
                    "band_end_sec": float(end_sec),
                    "heat": candidate.get("heat")}
        if (end_sec is None and verdict.get("band")
                and horizon >= mouth + 30.0):
            upsert_repair(db, mapping_id=mapping_id,
                          local_item_id=dirty.local_item_id,
                          source_id=dirty.source_id, status="skipped",
                          reason="watermark_or_overlong",
                          sibling_mapping_id=sibling.id, mouth=mouth,
                          detail="verdict_seed")
            db.commit()
            return {"mapping_id": mapping_id, "status": "skipped",
                    "reason": "watermark_or_overlong",
                    "heat": candidate.get("heat")}
    outcome = process_pair(db, dirty, sibling, mouth=mouth, stats=stats)
    status = outcome["status"]
    upsert_repair(
        db, mapping_id=mapping_id, local_item_id=dirty.local_item_id,
        source_id=dirty.source_id, status=status,
        reason=outcome.get("reason", ""),
        segments=outcome.get("segments"),
        band_end_sec=outcome.get("band_end_sec"),
        band_family=outcome.get("band_family", ""),
        strip=outcome.get("strip"),
        sibling_mapping_id=sibling.id, mouth=mouth,
        detail=outcome.get("detail", ""))
    db.commit()
    result = {"mapping_id": mapping_id, "status": status,
              "reason": outcome.get("reason", ""),
              "band_end_sec": outcome.get("band_end_sec"),
              "heat": candidate.get("heat")}
    if status == "repaired":
        verified = verify_repair(db, mapping_id, stats=stats)
        result["status"] = verified["status"]
        result["reason"] = verified.get("reason", "")
    return result
