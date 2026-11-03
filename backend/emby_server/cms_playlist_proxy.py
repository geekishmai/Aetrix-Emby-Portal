"""苹果 CMS 清单代理去广告（播放优化 C 件，保守形态）。

形态（2026-11-02 站主批准全套之③，证据见采样报告
applecms-playback-sampling-20261102）：

- 只代理 m3u8 清单正文，**绝不代理分段字节**：媒体清单里正片
  分段 URI 一律改写为原 CDN 绝对地址，由播放器直连原站取段。
- 只按有证据的规则过滤：首批唯一规则是暴风（upstream_sources
  id=9）媒体清单中路径含 ``/video/adjump/`` 的广告段——采样 3 条
  标题 7 个插入点全同构（9 段/组、同一素材跨片复用、正片为顺序
  编号段零命中）。规则按源 id 配置（``SEGMENT_PATH_FILTERS``），
  默认其它源不启用；**不许**把 DISCONTINUITY 包夹、短段时长、
  异域名、CUE/SCTE 等无证据或已被反证的形态做成规则（采样报告
  §2.4 逐项裁定）。
- 无规则的源不 engage：取流口维持今日 302 直发。
- 回落铁律：代理链任何一步失败（抓清单非 200、解析异常、规则
  处理异常、超时）→ 调用方立即回落 302 直发原地址，宁可带广告
  也不许播不了；本模块失败一律返回 None 并留结构化日志。
- 开关 ``CMS_PLAYLIST_PROXY_ENABLED`` 默认开；关时取流口行为
  与今日逐字一致（302 直发）。

主清单形态：variant 地址改写为本模块二级端点地址，带可校验的
定位参数（HMAC 绑定 uid/item/exp/目标地址，密钥复用 play_sign
同一签名密钥，不另起一套）；二级端点仍挂 get_play_user 校验链
（play_sign 验签）＋付费墙＋绑定门禁，不裸奔。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import urllib.parse

logger = logging.getLogger(__name__)

#: 清单抓取超时（秒）：与 upstream_playback 画质回填同量级，
#: 代理只是点播前的一跳，慢了就该回落直发而不是让用户等。
FETCH_TIMEOUT_SEC = 5.0

#: 按源 id 配置的分段路径过滤规则：source_id -> 路径子串元组。
#: 命中任一子串的分段（连同其专属 EXTINF 行）整段删除。
#: 首批只有暴风 id=9 的 adjump 规则（采样实证，见模块 docstring）；
#: 新增规则必须先有采样级证据，不许凭形态猜测推广到未验的源。
SEGMENT_PATH_FILTERS: dict[int, tuple[str, ...]] = {
    9: ("/video/adjump/",),
}


def playlist_proxy_enabled() -> bool:
    """总开关：默认开；0/false/no/off（大小写不敏感）为关。"""
    raw = os.getenv("CMS_PLAYLIST_PROXY_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def filter_markers_for_source(source_id) -> tuple[str, ...]:
    """该源启用的分段路径过滤标记；无规则返回空元组（不 engage）。"""
    try:
        sid = int(source_id)
    except (TypeError, ValueError):
        return ()
    return SEGMENT_PATH_FILTERS.get(sid, ())


def is_m3u8_url(url: str) -> bool:
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(".m3u8")


def engages(source_id, url: str) -> bool:
    """本源 + 本地址是否进清单代理：开关开、有规则、地址是 m3u8。"""
    return bool(
        playlist_proxy_enabled()
        and filter_markers_for_source(source_id)
        and is_m3u8_url(url)
    )


# ---------------------------------------------------------------- 定位参数


def _locator_mac(user_id, item_id: str, exp, target_url: str) -> str:
    from backend.emby_server import play_sign

    msg = f"{user_id}:{item_id}:{exp}:{target_url}".encode("utf-8")
    return hmac.new(play_sign._signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def make_locator(user_id, item_id: str, exp, target_url: str) -> str:
    """把 variant 目标地址编码成可校验定位参数（地址 + HMAC）。"""
    raw = (target_url or "").encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{body}.{_locator_mac(user_id, item_id, exp, target_url)}"


def read_locator(locator: str, user_id, item_id: str, exp):
    """校验并解出定位参数里的目标地址；对不上/过期由调用方回落。

    exp 的时效已由 get_play_user 的 play_sign 验签把关，这里只做
    绑定一致性校验（同一 uid/item/exp 下地址被换掉即 MAC 不符）。
    """
    if not locator or "." not in locator:
        return None
    body, _, mac = locator.rpartition(".")
    try:
        padded = body + "=" * (-len(body) % 4)
        target = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    expected = _locator_mac(user_id, item_id, exp, target)
    if not hmac.compare_digest(expected, mac or ""):
        return None
    return target


# ---------------------------------------------------------------- 清单处理


def _segment_path(uri: str) -> str:
    return urllib.parse.urlsplit(uri).path


def filter_media_playlist(text: str, base_url: str, markers) -> tuple[str, dict]:
    """过滤媒体清单：删广告段（EXTINF+URI），正片 URI  absolutize。

    - 命中任一 marker（按解析后的路径子串匹配）的分段整段删除，
      其专属 ``#EXTINF`` 行一并删除；其余待行（含
      ``#EXT-X-DISCONTINUITY``）保守保留——DISCONTINUITY 单独
      出现对播放器无害，误删结构标记的风险大于收益。
    - 保留分段的 URI 用 urljoin 相对 base_url 改写为绝对地址，
      播放器直连原 CDN 取段，本服务不碰分段字节。
    返回 (新清单文本, 统计 dict)。
    """
    markers = tuple(markers or ())
    out: list[str] = []
    pending: list[str] = []
    kept = 0
    dropped = 0
    kept_duration = 0.0
    dropped_duration = 0.0

    def _pending_duration(lines) -> float:
        total = 0.0
        for line in lines:
            if line.startswith("#EXTINF:"):
                try:
                    total += float(line[len("#EXTINF:"):].split(",", 1)[0])
                except ValueError:
                    pass
        return total

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        # URI 行：一个分段的收尾行
        if markers and any(m in _segment_path(line) for m in markers):
            dropped += 1
            dropped_duration += _pending_duration(pending)
            # 只丢 EXTINF（广告段专属行），其余标记保守保留
            out.extend(l for l in pending if not l.startswith("#EXTINF:"))
            pending = []
            continue
        kept += 1
        kept_duration += _pending_duration(pending)
        out.extend(pending)
        pending = []
        out.append(urllib.parse.urljoin(base_url, line))
    out.extend(pending)
    stats = {
        "kept_segments": kept,
        "dropped_segments": dropped,
        "kept_duration": round(kept_duration, 3),
        "dropped_duration": round(dropped_duration, 3),
    }
    return "\n".join(out) + "\n", stats


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in (text or "")


def rewrite_master_playlist(text: str, master_url: str, make_variant_url) -> str:
    """主清单改写：variant URI 换成本站二级端点地址，其余行原样。

    ``make_variant_url(absolute_variant_url) -> str`` 由调用方
    （api 层）按当前请求的签名轨构造。
    """
    out: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            out.append(line)
            continue
        absolute = urllib.parse.urljoin(master_url, line)
        out.append(make_variant_url(absolute))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 抓取与组装


def _fetch_text(url: str, headers: dict, http_get_text=None):
    if http_get_text is not None:
        return http_get_text(url, headers, FETCH_TIMEOUT_SEC)
    from backend.emby_server import upstream_playback as _up

    return _up._default_http_get_text(url, headers, FETCH_TIMEOUT_SEC)


def log_fallback(reason: str, *, source_id=None, item_id=None) -> None:
    """结构化回落日志：便于事后统计代理触发率与回落原因分布。"""
    logger.warning(
        "cms_playlist_proxy_fallback reason=%s source_id=%s item_id=%s",
        reason, source_id, item_id)


def build_master_response_text(url: str, headers: dict, make_variant_url, *,
                               source_id=None, item_id=None,
                               http_get_text=None):
    """取流口一级处理：抓主清单并按形态产出代理清单文本。

    - 主清单（带 variant）：改写 variant 为二级端点地址后下发；
    - 媒体清单（暴风形态，主=媒体）：直接过滤 + absolutize 下发。
    任何失败返回 None（调用方回落 302 直发），不抛异常。
    """
    try:
        status, text = _fetch_text(url, headers, http_get_text)
        if status != 200 or not text:
            log_fallback("fetch_not_200", source_id=source_id, item_id=item_id)
            return None
        if is_master_playlist(text):
            return rewrite_master_playlist(text, url, make_variant_url)
        markers = filter_markers_for_source(source_id)
        new_text, stats = filter_media_playlist(text, url, markers)
        logger.info(
            "cms_playlist_proxy_filtered source_id=%s item_id=%s "
            "kept=%s dropped=%s dropped_duration=%s",
            source_id, item_id, stats["kept_segments"],
            stats["dropped_segments"], stats["dropped_duration"])
        return new_text
    except Exception:  # noqa: BLE001 — 回落铁律：代理失败绝不挡播放
        log_fallback("process_error", source_id=source_id, item_id=item_id)
        return None


def build_media_response_text(url: str, headers: dict, *, source_id=None,
                              item_id=None, http_get_text=None):
    """二级端点处理：抓 variant 媒体清单，过滤 + absolutize 后下发。

    抓到的若仍是主清单（多层嵌套）则原样 absolutize 不过滤——
    拿不准不下刀；失败返回 None（调用方 302 回落原地址）。
    """
    try:
        status, text = _fetch_text(url, headers, http_get_text)
        if status != 200 or not text:
            log_fallback("media_fetch_not_200",
                         source_id=source_id, item_id=item_id)
            return None
        if is_master_playlist(text):
            log_fallback("media_unexpected_master",
                         source_id=source_id, item_id=item_id)
            return None
        markers = filter_markers_for_source(source_id)
        new_text, stats = filter_media_playlist(text, url, markers)
        logger.info(
            "cms_playlist_proxy_filtered source_id=%s item_id=%s "
            "kept=%s dropped=%s dropped_duration=%s",
            source_id, item_id, stats["kept_segments"],
            stats["dropped_segments"], stats["dropped_duration"])
        return new_text
    except Exception:  # noqa: BLE001 — 回落铁律
        log_fallback("media_process_error",
                     source_id=source_id, item_id=item_id)
        return None


def source_id_for_mapping(db, local_item_id: int, mapping_id=None):
    """按取流口同口径现查映射，给出源 id（代理 engage 判定用）。

    与 resolve_mapped_upstream_stream 的映射挑选口径一致：
    定向 mapping_id 时只认那一条；否则最佳映射。查不到返回
    None（调用方不 engage、维持原 302——此时 resolve 本身也会
    给出同样的无映射结论，两边口径不打架）。
    """
    try:
        from backend.emby_server import upstream_item_mappings as uim

        if mapping_id is not None:
            for row in uim.get_all_mappings(db, local_item_id):
                if row.id == mapping_id:
                    return row.source_id
            return None
        mapping = uim.get_best_mapping(db, local_item_id)
        return mapping.source_id if mapping is not None else None
    except Exception:  # noqa: BLE001 — 查不到就不 engage，不挡原取流
        return None
