"""CMS 片头印制广告自动剪除（试点：非凡源 id=7，2026-11-03 站主批准）。

问题：部分片源把赌博赞助卡**烧进视频画面**的片头（如「天下会
国际娱乐集团 66109.com」整屏卡），播放列表层面删不掉——那是
cms_playlist_proxy 已上线的清单插段过滤，治的是另一种广告。

本模块的办法（源头不堵、在交付层剪）：

1. **检测**（后台有界任务，不在播放路径上同步跑）：把一条映射
   的片头约 75 秒小段拉下来抽帧（2 帧/秒、16x16 灰度），找片头
   的**静止段**（赞助卡是静止画面持续数秒），再把静止段的亮度
   签名与**已知赞助卡指纹库**（ad_skip_fingerprints.json，可追
   加）比对。双信号都中才判 ad；静止但不认识（片头 logo 等）
   记 static_unknown、**绝不剪**（宁漏勿误）；样本不足、拉不
   到（源站 403 等）各记其状态，同样不剪。
2. **记账**：结果按映射落 cms_ad_skip_detections（跳过秒数/
   置信度/证据帧路径/检测器版本），管理端有只读查询口。
3. **剪除**：播放走既有清单代理（cms_playlist_proxy）时查记
   账：只有高置信 ad 记录才把片头广告覆盖的**整段**从清单里
   摘掉；广告结束落在段中间时，多丢的正片 ≤
   ``MAX_EXTRA_LOSS_SEC``（默认 2 秒）才许扩到段尾，否则整条
   不剪。分段仍直连源站，**不代理分段字节、不转码**。

红线与回落：没检测过的线照常播未剪版、绝不让用户等检测；本
模块任何失败都只记状态/日志，播放路径查不到可用记录即与今日
行为逐字一致（清单代理不 engage、302 直发）。

开关（文件配置/env）：``CMS_AD_SKIP_ENABLED`` 总开关（默认
开）、``CMS_AD_SKIP_SOURCE_IDS`` 试点源表（默认 ``7``，逗号分
隔，按源可开可关）；检测并发由全局信号量钳制（默认 2），不抢
播放主链路。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DETECTOR_VERSION = "1.0"

#: 只有置信度 ≥ 此值且 status=ad 的记录才允许剪（宁漏勿误）。
HIGH_CONFIDENCE = 0.7
#: 片头静止段至少持续这么久才考虑是卡（短定格/logo 闪过不算）。
MIN_STATIC_SEC = 2.5
#: 静止判定：相邻帧签名汉明距离上限 + 与段首帧的亮度签名距离上限。
STATIC_HAMMING = 5
STATIC_GRID_DIST = 10.0
#: 指纹命中：静止段中位签名与指纹的 mask 后平均亮度差上限。
FINGERPRINT_MAX_DIST = 16.0
#: 黑场帧判定：最亮格也不超过此值才算纯黑（黑场引导不算内容）。
#: 不能按平均亮度判——U-NEXT 那种黑底白字整屏卡平均亮度同样很
#: 低（实测 7.8），按平均值会被当黑场吞掉；它的最亮格（文字）
#: 有约 60，纯黑帧最亮格只有个位数，靠最亮格能干净分开。
BLACK_CELL_MAX = 24.0
#: 指纹墨迹门：种子里亮度 ≥ 此值的格算「墨迹格」（卡上的字），
#: 候选帧在墨迹格上的平均亮度须 ≥ INK_MIN_MEAN 才算命中——防
#: 纯黑/近黑静止帧靠「大面积同黑」蹭中黑底卡种子（实测纯黑帧
#: 到 U-NEXT 种子的全格平均差仅 6.3，不设此门必然误剪）。
INK_CELL_MIN = 40.0
INK_MIN_MEAN = 30.0
#: 抽帧采样间隔（秒）：fps=2。
SAMPLE_INTERVAL = 0.5
#: 检测拉段的字节上限（只拉片头，防失控拖全片）。
MAX_HEAD_BYTES = 32 * 1024 * 1024

_FINGERPRINTS_PATH = Path(__file__).with_name("ad_skip_fingerprints.json")
_fingerprints_cache: list | None = None


# ---------------------------------------------------------------- 配置


def ad_skip_enabled() -> bool:
    """总开关：默认开；0/false/no/off（大小写不敏感）为关。"""
    raw = os.getenv("CMS_AD_SKIP_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def ad_skip_source_ids() -> frozenset:
    """试点源 id 表（env 逗号分隔，默认仅非凡 7）；解析失败按空表。"""
    raw = os.getenv("CMS_AD_SKIP_SOURCE_IDS", "7")
    out = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


def source_enabled(source_id) -> bool:
    if not ad_skip_enabled():
        return False
    try:
        return int(source_id) in ad_skip_source_ids()
    except (TypeError, ValueError):
        return False


def head_seconds() -> float:
    """检测窗口：片头前多少秒（默认 75）。"""
    try:
        value = float(os.getenv("CMS_AD_SKIP_HEAD_SECONDS", "75"))
    except (TypeError, ValueError):
        return 75.0
    return min(max(value, 10.0), 300.0)


def max_extra_loss() -> float:
    """段中结束时允许多丢的正片秒数上限（默认 2 秒）。"""
    try:
        value = float(os.getenv("CMS_AD_SKIP_MAX_EXTRA_LOSS_SEC", "2"))
    except (TypeError, ValueError):
        return 2.0
    return min(max(value, 0.0), 10.0)


def evidence_dir() -> Path:
    raw = os.getenv("CMS_AD_SKIP_EVIDENCE_DIR", "/tmp/ad_skip_evidence")
    return Path(raw)


# ---------------------------------------------------------------- 帧签名


def grid_from_raw16(raw: bytes) -> tuple:
    """16x16 灰度原始帧（256 字节）→ 8x8 亮度签名（2x2 块均值）。"""
    if len(raw) < 256:
        raise ValueError("raw frame too short")
    grid = []
    for by in range(8):
        for bx in range(8):
            vals = [raw[(2 * by + dy) * 16 + (2 * bx + dx)]
                    for dy in (0, 1) for dx in (0, 1)]
            grid.append(sum(vals) / 4.0)
    return tuple(grid)


def ahash_from_grid(grid) -> int:
    """8x8 签名 → 64 位平均哈希（大于均值的格记 1）。"""
    avg = sum(grid) / len(grid)
    h = 0
    for j, v in enumerate(grid):
        if v > avg:
            h |= (1 << j)
    return h


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def grid_distance(g1, g2, mask=None) -> float:
    """两签名的平均亮度差；mask 给定时只算 mask=1 的格。"""
    diffs = []
    for i, (x, y) in enumerate(zip(g1, g2)):
        if mask is not None and not mask[i]:
            continue
        diffs.append(abs(float(x) - float(y)))
    if not diffs:
        return 999.0
    return sum(diffs) / len(diffs)


def load_fingerprints() -> list:
    """加载指纹库（ad_skip_fingerprints.json）；坏文件按空库处理。"""
    global _fingerprints_cache
    if _fingerprints_cache is not None:
        return _fingerprints_cache
    try:
        data = json.loads(_FINGERPRINTS_PATH.read_text(encoding="utf-8"))
        items = data.get("fingerprints") or []
        out = []
        for it in items:
            grid = it.get("grid") or []
            mask = it.get("mask") or [1] * 64
            if len(grid) != 64 or len(mask) != 64:
                continue
            out.append({"name": str(it.get("name") or ""),
                        "family": str(it.get("family") or ""),
                        "kind": str(it.get("kind") or ""),
                        "grid": tuple(float(x) for x in grid),
                        "mask": tuple(int(x) for x in mask),
                        "note": str(it.get("note") or "")})
        _fingerprints_cache = out
    except Exception:  # noqa: BLE001 — 指纹库坏了等于没播种，不许崩
        logger.warning("ad_skip fingerprints load failed", exc_info=True)
        _fingerprints_cache = []
    return _fingerprints_cache


def match_fingerprint(grid, seeds=None):
    """签名与指纹库比对：返回 (名称, 距离) 或 None（无一命中）。

    两道门都过才算命中：①mask 后平均亮度差 ≤ FINGERPRINT_MAX_
    DIST；②墨迹门——种子墨迹格（亮度 ≥ INK_CELL_MIN）在候选帧
    上平均亮度 ≥ INK_MIN_MEAN（种子无墨迹格时跳过此门）。②专
    治黑底卡：整屏大面积同黑会让纯黑帧蹭过①，只有字真在才算。
    """
    if seeds is None:
        seeds = load_fingerprints()
    best = None
    for seed in seeds:
        dist = grid_distance(grid, seed["grid"], seed["mask"])
        if dist > FINGERPRINT_MAX_DIST:
            continue
        ink = [i for i, v in enumerate(seed["grid"])
               if seed["mask"][i] and v >= INK_CELL_MIN]
        if ink:
            ink_mean = sum(float(grid[i]) for i in ink) / len(ink)
            if ink_mean < INK_MIN_MEAN:
                continue
        if best is None or dist < best[1]:
            best = (seed["name"], dist)
    return best


# ---------------------------------------------------------------- 片头分析


def _median_grid(grids):
    cols = list(zip(*grids))
    out = []
    for col in cols:
        s = sorted(col)
        n = len(s)
        out.append(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0)
    return tuple(out)


def analyze_head(samples, *, seeds=None) -> dict:
    """片头帧序列 → 判定。samples = [(t, grid, luma), ...]（等间隔）。

    判定口径（宁漏勿误）：
    - 先跳过开头近黑帧（黑场引导）；其后第一个画面起找静止段
      （与段首帧逐帧比对，汉明距离 + 签名距离双钳制）；
    - 静止段 < MIN_STATIC_SEC → 不是卡（clean）；
    - 静止段达标但指纹不认识 → static_unknown（只记账不剪）；
    - 静止段达标且指纹命中 → ad，skip = 静止段结束时刻（黑场
      引导一并计入：它在卡之前、本就不是正片内容）。
    """
    result = {"status": "insufficient", "ad_start": None, "ad_end": None,
              "skip_seconds": None, "confidence": 0.0, "seed_name": "",
              "static_run_sec": 0.0}
    if not samples or len(samples) < 4:
        return result
    interval = SAMPLE_INTERVAL
    if len(samples) >= 2:
        interval = max(0.05, samples[1][0] - samples[0][0])
    # 跳过开头纯黑帧（按最亮格判，见 BLACK_CELL_MAX 注释：黑底
    # 白字卡平均亮度低但最亮格高，不会被误吞）
    i = 0
    while i < len(samples) and max(samples[i][1]) <= BLACK_CELL_MAX:
        i += 1
    if i >= len(samples) - 1:
        result["status"] = "clean"
        return result
    black_lead_end = samples[i][0]
    first_grid = samples[i][1]
    first_hash = ahash_from_grid(first_grid)
    j = i
    while j + 1 < len(samples):
        nxt = samples[j + 1][1]
        if hamming(ahash_from_grid(nxt), first_hash) <= STATIC_HAMMING and \
                grid_distance(nxt, first_grid) <= STATIC_GRID_DIST:
            j += 1
        else:
            break
    run_sec = samples[j][0] - samples[i][0] + interval
    result["static_run_sec"] = round(run_sec, 3)
    if run_sec < MIN_STATIC_SEC:
        result["status"] = "clean"
        return result
    median = _median_grid([s[1] for s in samples[i:j + 1]])
    hit = match_fingerprint(median, seeds)
    if hit is None:
        result["status"] = "static_unknown"
        result["confidence"] = 0.3
        result["ad_start"] = round(samples[i][0], 3)
        result["ad_end"] = round(samples[j][0] + interval, 3)
        return result
    name, dist = hit
    ad_end = samples[j][0] + interval
    result.update({
        "status": "ad",
        "ad_start": round(0.0 if black_lead_end > 0 else samples[i][0], 3),
        "ad_end": round(ad_end, 3),
        "skip_seconds": round(ad_end, 3),
        "confidence": round(max(0.0, min(1.0, 1.0 - dist / 32.0)), 3),
        "seed_name": name,
    })
    return result


# ---------------------------------------------------------------- 清单摘段


def _parse_segments(text: str):
    """把媒体清单拆成 (头行, 段列表, 尾行)。

    段 = 前置标记行（含 EXTINF）+ URI 行；解析不了（无段）返回
    None。保守口径与 cms_playlist_proxy.filter_media_playlist 的
    pending 语义一致：URI 之前的 # 行都算该段的前置行。
    """
    header: list[str] = []
    segments: list[dict] = []
    trailing: list[str] = []
    pending: list[str] = []
    seen_segment = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not seen_segment and not pending and \
                    not line.startswith("#EXTINF:"):
                header.append(line)
            else:
                pending.append(line)
            continue
        duration = 0.0
        for p in pending:
            if p.startswith("#EXTINF:"):
                try:
                    duration = float(p[len("#EXTINF:"):].split(",", 1)[0])
                except ValueError:
                    duration = 0.0
        segments.append({"tags": pending, "duration": duration, "uri": line})
        pending = []
        seen_segment = True
    trailing = pending
    if not segments:
        return None
    return header, segments, trailing


def trim_playlist(text: str, skip_seconds: float, *, max_extra_loss_sec=None):
    """把片头 skip_seconds 覆盖的整段从媒体清单里摘掉。

    段界对齐：整段结束 ≤ skip 的段全摘；广告结束落在某段中间
    时，只有该段结束点超出 skip 的量（= 多丢的正片）≤
    max_extra_loss_sec 才把该段也摘掉，否则**整条不剪**返回
    None。全部段都被覆盖、skip ≤ 0、解析失败同样返回 None。
    成功返回 (新清单文本, stats)；新清单的 EXT-X-MEDIA-SEQUENCE
    按摘掉的段数前移（有此行时）。
    """
    if skip_seconds is None or skip_seconds <= 0:
        return None
    if max_extra_loss_sec is None:
        max_extra_loss_sec = max_extra_loss()
    parsed = _parse_segments(text)
    if parsed is None:
        return None
    header, segments, trailing = parsed
    dropped = 0
    dropped_duration = 0.0
    extra_loss = 0.0
    elapsed = 0.0
    for seg in segments:
        seg_end = elapsed + seg["duration"]
        if seg_end <= skip_seconds + 1e-6:
            dropped += 1
            dropped_duration += seg["duration"]
        elif elapsed < skip_seconds < seg_end:
            if seg_end - skip_seconds <= max_extra_loss_sec + 1e-6:
                dropped += 1
                dropped_duration += seg["duration"]
                extra_loss = seg_end - skip_seconds
            else:
                return None
        else:
            break
        elapsed = seg_end
    if dropped == 0 or dropped >= len(segments):
        return None
    out: list[str] = []
    for line in header:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1].strip())
                line = f"#EXT-X-MEDIA-SEQUENCE:{seq + dropped}"
            except ValueError:
                pass
        out.append(line)
    for seg in segments[dropped:]:
        out.extend(seg["tags"])
        out.append(seg["uri"])
    out.extend(trailing)
    stats = {"trimmed_segments": dropped,
             "trimmed_duration": round(dropped_duration, 3),
             "extra_loss": round(extra_loss, 3)}
    return "\n".join(out) + "\n", stats


# ---------------------------------------------------------------- 记账


def get_detection(db, mapping_id: int):
    """按映射 id 查现行检测记录；查不到返回 None。"""
    from backend.emby_server import models as em

    return db.query(em.CmsAdSkipDetection).filter(
        em.CmsAdSkipDetection.mapping_id == int(mapping_id)).first()


def upsert_detection(db, *, mapping_id: int, local_item_id: int,
                     source_id: int, status: str, skip_seconds=None,
                     ad_start=None, ad_end=None, confidence: float = 0.0,
                     seed_name: str = "", evidence_path: str = "",
                     detail: str = "") -> None:
    """落/更新一条映射的检测结论（同映射恒一行，重复检测覆盖）。"""
    from backend.emby_server import models as em

    row = get_detection(db, mapping_id)
    if row is None:
        row = em.CmsAdSkipDetection(mapping_id=int(mapping_id))
        db.add(row)
    row.local_item_id = int(local_item_id)
    row.source_id = int(source_id)
    row.status = str(status)
    row.skip_seconds = skip_seconds
    row.ad_start = ad_start
    row.ad_end = ad_end
    row.confidence = float(confidence or 0.0)
    row.detector_version = DETECTOR_VERSION
    row.seed_name = seed_name or ""
    row.evidence_path = evidence_path or ""
    row.detail = (detail or "")[:500]
    db.flush()


def lookup_trim(db, local_item_id: int, mapping_id, source_id):
    """播放路径查剪除量：返回 (skip_seconds|None, 记录是否存在)。

    只有「试点源开启 + 记录为 ad + 置信度达标 + skip > 0」才给
    skip；其余一律 (None, exists)，调用方据此维持原行为。查的
    过程出任何错都按无记录处理（不挡播放）。
    """
    try:
        if not source_enabled(source_id):
            return None, False
        from backend.emby_server import upstream_item_mappings as uim

        mid = mapping_id
        if mid is None:
            best = uim.get_best_mapping(db, local_item_id)
            if best is None:
                return None, False
            mid = best.id
        row = get_detection(db, mid)
        if row is None:
            return None, False
        if row.status == "ad" and row.confidence >= HIGH_CONFIDENCE \
                and row.skip_seconds and row.skip_seconds > 0:
            return float(row.skip_seconds), True
        return None, True
    except Exception:  # noqa: BLE001 — 查账失败绝不挡播放
        logger.warning("ad_skip lookup failed", exc_info=True)
        return None, False


def detection_payload(row) -> dict:
    """管理只读口的响应形态（不含任何凭据类字段）。"""
    return {
        "mapping_id": row.mapping_id,
        "local_item_id": row.local_item_id,
        "source_id": row.source_id,
        "status": row.status,
        "skip_seconds": row.skip_seconds,
        "ad_start": row.ad_start,
        "ad_end": row.ad_end,
        "confidence": row.confidence,
        "detector_version": row.detector_version,
        "seed_name": row.seed_name,
        "evidence_path": row.evidence_path,
        "detail": row.detail,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------- 检测管线


class _FetchBlocked(Exception):
    """源站明确拒绝服务器拉取（403）：记 fetch_blocked，不当普通失败。"""


def _fetch_bytes(url: str, headers: dict, timeout: float,
                 max_bytes: int) -> bytes:
    """拉一段分段字节（经 SSRF 安全口径 safe_urlopen，不跟随重定向外跳）。"""
    from backend.emby_server import upstream_http

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            chunks = []
            remaining = max_bytes
            while remaining > 0:
                chunk = resp.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise _FetchBlocked(url) from exc
        raise


def _extract_samples(ts_bytes: bytes):
    """TS 字节 → [(t, grid, luma), ...]：ffmpeg 抽 2 帧/秒 16x16 灰度。

    ffmpeg 缺失/失败返回 None（调用方记 failed，不许假装检过）。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".ts", delete=False)
    try:
        tmp.write(ts_bytes)
        tmp.close()
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", tmp.name, "-vf",
             f"fps=2,scale=16:16,format=gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=120)
        if proc.returncode != 0 or not proc.stdout:
            return None
        raw = proc.stdout
        samples = []
        for k in range(len(raw) // 256):
            frame = raw[k * 256:(k + 1) * 256]
            grid = grid_from_raw16(frame)
            samples.append((round(k * SAMPLE_INTERVAL, 3), grid,
                            sum(frame) / 256.0))
        return samples
    except Exception:  # noqa: BLE001
        logger.warning("ad_skip ffmpeg extract failed", exc_info=True)
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _save_evidence_frame(ts_bytes: bytes, mapping_id: int,
                         at_sec: float) -> str:
    """把片头一帧存成证据图（best effort，失败返回空串）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".ts", delete=False)
    try:
        tmp.write(ts_bytes)
        tmp.close()
        out_dir = evidence_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"mapping_{int(mapping_id)}.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at_sec):.2f}",
             "-i", tmp.name, "-frames:v", "1", "-q:v", "3",
             str(out_path), "-y"],
            capture_output=True, timeout=60)
        if proc.returncode == 0 and out_path.exists() \
                and out_path.stat().st_size > 0:
            return str(out_path)
        return ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def detect_mapping(db, mapping_id: int, *, http_get_text=None) -> str:
    """对一条映射跑一次完整检测并落账，返回最终 status。

    全程 fail-safe：任何异常都落 failed/fetch_blocked 记录后返
    回，绝不抛给调用方（后台任务与 CLI 共用本入口）。
    """
    from backend.emby_server import models as em
    from backend.emby_server import upstream_playback as up

    mapping = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.id == int(mapping_id)).first()
    if mapping is None:
        return "missing"

    def _record(status, **kw):
        upsert_detection(db, mapping_id=mapping.id,
                         local_item_id=mapping.local_item_id,
                         source_id=mapping.source_id, status=status, **kw)
        db.commit()
        return status

    try:
        source = up.get_source_row(db, mapping.source_id)
        headers = up._source_headers(source) if source is not None else {}
        url = up.resolve_mapped_upstream_stream(
            db, mapping.local_item_id, mapping_id=mapping.id)
        fetch = http_get_text or up._default_http_get_text_followed
        status, text = fetch(url, headers, 10.0)
        if status == 403:
            return _record("fetch_blocked", detail="playlist_403")
        if status != 200 or not text:
            return _record("failed", detail=f"playlist_status={status}")
        if up._is_playlist_text(text) is False and "#EXTM3U" not in text:
            return _record("failed", detail="playlist_not_m3u8")
        media_url, media_text = url, text
        if "#EXT-X-STREAM-INF" in text:
            variant = None
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    variant = urllib.parse.urljoin(url, line)
                    break
            if not variant:
                return _record("failed", detail="master_no_variant")
            status, media_text = fetch(variant, headers, 10.0)
            if status == 403:
                return _record("fetch_blocked", detail="variant_403")
            if status != 200 or not media_text:
                return _record("failed", detail=f"variant_status={status}")
            media_url = variant
        parsed = _parse_segments(media_text)
        if parsed is None:
            return _record("failed", detail="media_no_segments")
        _header, segments, _trailing = parsed
        want = head_seconds()
        buf = bytearray()
        covered = 0.0
        for seg in segments:
            if covered >= want:
                break
            seg_url = urllib.parse.urljoin(media_url, seg["uri"])
            try:
                data = _fetch_bytes(seg_url, headers, 20.0,
                                    MAX_HEAD_BYTES - len(buf))
            except _FetchBlocked:
                return _record("fetch_blocked", detail="segment_403")
            if not data:
                return _record("failed", detail="segment_empty")
            buf.extend(data)
            covered += seg["duration"]
            if len(buf) >= MAX_HEAD_BYTES:
                break
        if not buf:
            return _record("failed", detail="no_segment_bytes")
        samples = _extract_samples(bytes(buf))
        if samples is None:
            return _record("failed", detail="ffmpeg_failed")
        res = analyze_head(samples)
        evidence = _save_evidence_frame(
            bytes(buf), mapping.id,
            res["ad_start"] if res["ad_start"] is not None else 2.0)
        return _record(
            res["status"], skip_seconds=res["skip_seconds"],
            ad_start=res["ad_start"], ad_end=res["ad_end"],
            confidence=res["confidence"], seed_name=res["seed_name"],
            evidence_path=evidence,
            detail=f"static_run={res['static_run_sec']}")
    except up.UpstreamPlaybackError as exc:
        return _record("failed", detail=f"resolve_{exc.code}"[:500])
    except Exception:  # noqa: BLE001 — 后台检测失败只记账，不许崩
        logger.warning("ad_skip detect failed mapping=%s",
                       mapping_id, exc_info=True)
        try:
            return _record("failed", detail="exception")
        except Exception:  # noqa: BLE001
            db.rollback()
            return "failed"


# ---------------------------------------------------------------- 后台调度


_detect_semaphore = threading.Semaphore(2)
_detect_inflight: set = set()
_detect_lock = threading.Lock()


def schedule_detection(local_item_id: int, mapping_id, source_id) -> bool:
    """首次使用触发的后台检测（守护线程 + 信号量有界，绝不阻塞播放）。

    已在飞、或试点未开该源时直接返回 False。线程内自开会话、
    自行落账；没检测完之前播放维持未剪版。
    """
    if not source_enabled(source_id):
        return False
    key = mapping_id if mapping_id is not None else ("local", local_item_id)
    with _detect_lock:
        if key in _detect_inflight:
            return False
        _detect_inflight.add(key)

    def _worker():
        try:
            if not _detect_semaphore.acquire(timeout=300):
                return
            try:
                from backend.database import SessionLocal
                from backend.emby_server import \
                    upstream_item_mappings as uim

                db = SessionLocal()
                try:
                    mid = mapping_id
                    if mid is None:
                        best = uim.get_best_mapping(db, local_item_id)
                        if best is None:
                            return
                        mid = best.id
                    if get_detection(db, mid) is not None:
                        return
                    detect_mapping(db, mid)
                finally:
                    db.close()
            finally:
                _detect_semaphore.release()
        except Exception:  # noqa: BLE001 — 后台线程绝不带崩主进程
            logger.warning("ad_skip scheduled detect crashed",
                           exc_info=True)
        finally:
            with _detect_lock:
                _detect_inflight.discard(key)

    threading.Thread(target=_worker, daemon=True,
                     name=f"cms-ad-skip-{key}").start()
    return True


def run_batch(db, source_id: int, limit: int = 100) -> dict:
    """批量检测一个源下尚无记录的映射（CLI/运维触发），返回计数。"""
    from backend.emby_server import models as em

    rows = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.source_id == int(source_id)).order_by(
        em.UpstreamItemMapping.id.desc()).limit(max(1, limit) * 4).all()
    stats = {"checked": 0}
    for row in rows:
        if stats["checked"] >= limit:
            break
        if get_detection(db, row.id) is not None:
            continue
        status = detect_mapping(db, row.id)
        stats["checked"] += 1
        stats[status] = stats.get(status, 0) + 1
    logger.info("cms_ad_skip_batch source=%s stats=%s", source_id, stats)
    return stats


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="CMS 片头印制广告检测（试点：后台抽帧认卡）")
    parser.add_argument("--source", type=int, default=7,
                        help="源 id（默认 7 非凡）")
    parser.add_argument("--limit", type=int, default=100,
                        help="本轮最多检测的映射数")
    parser.add_argument("--mapping", type=int, default=None,
                        help="只检测指定映射 id（验收/排障用）")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        if args.mapping is not None:
            status = detect_mapping(db, args.mapping)
            row = get_detection(db, args.mapping)
            logger.info("mapping=%s status=%s record=%s", args.mapping,
                        status,
                        detection_payload(row) if row else None)
            return 0
        from backend.emby_server import models as em

        stats = run_batch(db, args.source, args.limit)
        total = db.query(em.CmsAdSkipDetection).filter_by(
            source_id=args.source).count()
        logger.info("source=%s stats=%s total_records=%s",
                    args.source, stats, total)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
