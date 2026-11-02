"""上游播放接入（FMBY 学习 #25 25-4：播放接入，播放阶段 7）。

对应魂 3（调度与数据面分离）与魂 6（状态集中维护）：上游播放的
选路、探活、报错全部收进本模块，video_stream 的本地选路一字不动；
上游源的读写仍只走 25-1 的 ``upstream_sources``（本模块只读）。

## 本阶段的范围（诚实边界，先说清楚什么没做）

- 做的：一条显式的上游播放兜底口——给定「已启用的上游源 +
  上游条目 id」，服务端凭源凭据向上游取流地址，先探后 302：
  探到可播才下发 302，探到问题直接回结构化警告，不把死链、
  401、429 原样丢给播放器去猜。
- 不做的：video_stream 本地失败后自动改走上游。自动兜底需要
  「本地条目 → 上游条目」的持久映射（25-2 的聚合目前只是内存
  展示模型，落库映射尚未立项）；没有映射就自动兜底等于按标题
  猜，是虚报。本地优先的口径仍由 25-2 的
  ``select_for_presentation`` 守着（本地在前、上游只补本地没有
  的作品），本口只是那个口径下的兜底目标：客户端在本地变体
  全部不可用时才调本口，服务端不替它悄悄改主意。
- 不做的：苹果 CMS 型（用户已定首批后置，25-1 的类型校验会拒）；
  局域网上游放行（25-1 的 SSRF 守卫仍在，待裁定项不擅自改）。

## 超限结构化警告（本阶段第二件）

上游额度/配额耗尽绝不静默失败、也不笼统 500：探活结果按上游
状态码一一映射成带 code 的结构化错误体（见
:class:`UpstreamPlaybackError` 的 code 表），客户端凭 code 就能
区分「换个源」「等一会儿再试」「找管理员重录凭据」「片子没了」：

- 上游 429 → 429 ``upstream_quota_exhausted``（带 Retry-After，
  上游没给就不带，不编数字）；
- 上游 403 → 403 ``upstream_forbidden``（只报“上游拒绝”，不谎称
  是额度问题——403 也可能是权限问题，原因上游没说清就不猜）；
- 上游 401 → 409 ``upstream_credential_invalid``（凭据失效，管理
  员重录；与 BYOS 绑定失效的 409 口径一致）；
- 上游 404 → 404 ``upstream_item_not_found``（上游已无此条目）；
- 上游连不上/超时/其它 5xx → 502 ``upstream_unavailable``。
- 开关关、源不存在、源未启用同样结构化（404/409），不走 500。

## 开关与回滚

- ``UPSTREAM_PLAYBACK_ENABLED``（默认关）：关时本口返回 404，
  行为上等于该口不存在，本地播放逐字不变，是本阶段唯一回滚手段。
- 凭据纪律：探活与 302 地址里的上游令牌只出现在发往上游的请求
  与播放器的 302 地址里（Emby 直播本身就是这个形态）；日志与
  错误体里只许出现 source_id 与 code，绝不出现令牌、密封值、URL。
- 超时是运维常量（``UPSTREAM_PROBE_TIMEOUT_SEC``），不是业务档位，
  不走 env；兜底口本身就是低频路径（本地优先），一次认证 + 一次
  Range 探活是可接受的成本。

## 苹果 CMS 分支（本批新增，与 Emby/Jellyfin 并列、绝不兜底）

- 分发点：``resolve_upstream_stream`` 在源存在且启用后，按
  ``source.source_type == "applecms"`` 分发；Emby/Jellyfin 分支
  逐字不动。本分支不做「本地失败自动改走苹果 CMS」：苹果 CMS
  与 Emby/Jellyfin 是并列的上游类型，选哪条由源与映射决定，
  不在本函数里互相兜底。
- 门禁（四道，默认全关）：总闸 ``UPSTREAM_PLAYBACK_ENABLED``
  → 苹果 CMS 开关 ``APPLECMS_ENABLED``（关时与总闸关闭行为一致，
  404 upstream_playback_disabled）→ 分级门控 ``APPLECMS_TIER_GATE``
  （关时不查；开且映射未配置时 403，复用既有 upstream_forbidden
  码，见 ``_resolve_applecms_stream``）→ 解析线路开关
  ``APPLECMS_PARSE_LINES_ENABLED``（只影响解析型组的 unplayable
  原因；开时调用方可经 ``parse_resolver`` 参数注入逐站解析函数，
  未注入时仍记 parse_rule_missing，不内置任何站规则、不猜解析）。
  同步开关 ``UPSTREAM_SYNC_ENABLED``
  默认关且本批无工人消费；映射写开关
  ``UPSTREAM_ITEM_MAPPING_ENABLED`` 只管映射写路径，不 gate 播放读
  路径（它默认关，若 gate 播放则播放恒关，是错的）。
- 取数：``upstream_item_id`` 即 vod_id（映射行记的上游条目 id
  对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，调用方
  未传时默认 1（播第 1 集）。detail 现取现解析，播放地址绝不落库。
- 探活与错误码全复用既有：CMS 接口非 1 → 502
  upstream_unavailable；detail 空列表（vod 消失）→ 404
  upstream_item_not_found；429 → 429；401 → 409
  upstream_credential_invalid；CMS 接口本身的 403（取 detail 时）
  → 403 upstream_forbidden。直链探活走 ``_classify_probe_status``，
  唯一例外是探活 403 软放行（2026-11-02 定案，只限本分支）：
  播放 CDN 对数据中心 IP 的 403 不判死，记 cms_probe_soft_pass
  日志后照常下发 302——detail 现取成功已证明源与条目可用，
  403 只证明服务器 IP 被播放 CDN 拒，用户家里未必被拒；
  404/401/429/5xx 仍按统一分类判死。unplayable 原因映射：
  episode_out_of_range → 404 upstream_item_not_found，其余
  （parse_not_enabled / parse_rule_missing / no_direct_group）
  → 502 upstream_unavailable，message 区分原因，code 不新发明。
- 已知缺口（不在本批文件范围，报协调员）：源类型允许集
  （``upstream_sources.SOURCE_TYPES`` 与模型 CHECK 约束）目前
  只有 emby/jellyfin，苹果 CMS 源行尚建不出来；本分支代码与
  测试已就绪（测试用桩源绕过建源校验），等允许集放行即接通。
  播放口 HTTP 层（``api.py`` 的 upstream_stream 路由）目前未传
  episode 参数，接线时由路由把 ``?episode=`` 透传给本函数。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.emby_server import upstream_sources as us
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_http
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 上游播放总开关 env 键（默认关）。
UPSTREAM_PLAYBACK_ENV = "UPSTREAM_PLAYBACK_ENABLED"

#: 苹果 CMS 播放开关 env 键（默认关，与总闸是两道门：总闸关则全关，
#: 苹果 CMS 分支还须本开关开才放行）。
APPLECMS_ENABLED_ENV = "APPLECMS_ENABLED"

#: 苹果 CMS 解析线路开关 env 键（默认关）：关时解析型组一律
#: unplayable（parse_not_enabled），不猜解析规则。
APPLECMS_PARSE_LINES_ENV = "APPLECMS_PARSE_LINES_ENABLED"

#: 苹果 CMS 分级门控开关 env 键（默认关）：关时不查分级；
#: 开且分级映射未配置时返回 403（复用既有 upstream_forbidden 码）。
APPLECMS_TIER_GATE_ENV = "APPLECMS_TIER_GATE"

#: 上游同步开关 env 键（默认关）：本批只有读路径（现取现解析），
#: 没有同步工人；本键是未来同步工人复用的门，本模块只定义读取
#: 口，不消费它写任何东西。
APPLECMS_SYNC_ENV = "UPSTREAM_SYNC_ENABLED"

#: 苹果 CMS 坏源自动熔断阈值 env 键：真实点播路径上某 CMS 源连续
#: 失败达到该值时，自动经 upstream_sources.disable_source 停用该源
#: （只对 applecms 型源生效）。默认值与健康状态机翻 Unavailable 的
#: 阈值（UPSTREAM_HEALTH_FAILURE_THRESHOLD，默认 3）对齐——同一份
#: 连续失败计数（SourceHealthRecord）喂两处消费：先翻 Unavailable
#: 沉底排序，再达熔断阈值停用；两个阈值默认同值、各自可调。
APPLECMS_CIRCUIT_BREAKER_THRESHOLD_ENV = "APPLECMS_CIRCUIT_BREAKER_THRESHOLD"


def cms_circuit_breaker_threshold() -> int:
    """CMS 坏源自动熔断的连续失败阈值（默认与健康状态机阈值同值）。"""
    return env_int(APPLECMS_CIRCUIT_BREAKER_THRESHOLD_ENV,
                   uh.failure_threshold(), 1, 100)

#: 上游探活/认证单次超时（秒，运维常量，见模块 docstring）。
UPSTREAM_PROBE_TIMEOUT_SEC = 10.0

#: 播放探活重定向跟随上限（跳，运维常量）：上游以 302 发流是常态
#: （Emby stream → 网盘直链 → 对象存储签名链，生产实测两跳），
#: 但跟随必须有界且逐跳复验，见 ``_default_http_range_probe``。
UPSTREAM_PROBE_MAX_REDIRECTS = 3

#: 探活层视为重定向、需要跟随的状态码集合。
_PROBE_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))

#: CMS 线路画质缓存 TTL（秒，运维常量）：画质是 vod 的固有属性、
#: 极少变化，7 天足够覆盖两次点播之间的展示需求；过期后下一次
#: 点播取流时自然回填，不设主动刷新。
CMS_QUALITY_CACHE_TTL_SEC = 7 * 24 * 3600

#: m3u8 主清单抓取超时（秒，运维常量）：只为画质缓存服务，必须
#: 短——超时/失败直接放弃缓存，绝不拖住 302 下发。
M3U8_FETCH_TIMEOUT_SEC = 3.0

#: m3u8 主清单读取上限（字节）：主清单是小文本，256KB 绰绰有余，
#: 防异常大响应吃内存。
_M3U8_MAX_BYTES = 256 * 1024


def cms_quality_cache_key(mapping_id) -> str:
    """CMS 线路画质缓存键：按映射 id 一条线路一份。"""
    return f"cmsq:{int(mapping_id)}"


def read_cms_quality(mapping_id) -> Optional[dict]:
    """读 CMS 线路画质缓存（取流路径回填，展示路径消费）。

    内容形态：``{"width": int, "height": int, "bandwidth": int,
    "video_range": str|None, "label": str|None}``（字段可缺）。
    无缓存/缓存坏了返回 None——展示侧回落默认形态，不报错。
    """
    if mapping_id is None:
        return None
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(cms_quality_cache_key(mapping_id))
    except Exception:  # noqa: BLE001 — 缓存只是补强，故障不外溢
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def store_cms_quality(mapping_id, quality: dict) -> None:
    """写 CMS 线路画质缓存（与已有内容合并，新非空值优先）。

    全程 fail-safe：缓存写失败不许影响取流主路径（调用方在 302
    下发前调用，异常一律吞掉）。
    """
    if mapping_id is None or not quality:
        return
    try:
        from backend.database import CacheManager
        merged = dict(read_cms_quality(mapping_id) or {})
        for key, value in quality.items():
            if value not in (None, "", 0):
                merged[key] = value
        if not merged:
            return
        CacheManager.set(cms_quality_cache_key(mapping_id),
                         json.dumps(merged, ensure_ascii=False),
                         ttl=CMS_QUALITY_CACHE_TTL_SEC)
    except Exception:  # noqa: BLE001 — 同 read_cms_quality 口径
        pass


def _default_http_get_text(url: str, headers: dict, timeout: float):
    """默认文本 GET 层：返回 (status, text|None)，只读一小段。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            body = resp.read(_M3U8_MAX_BYTES)
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return None, None


def _capture_cms_quality(source, mapping_id, detail, address, *,
                         http_get_text=None) -> None:
    """取流成功顺手回填 CMS 线路画质（备注标记 + m3u8 主清单）。

    两路信号：detail 的 vod_remarks 里带明确画质标记（4K/1080P
    等）时拼标签存 ``label``；播放地址是 m3u8 时现抓主清单解析
    分辨率/带宽/范围信号（真实数值，优先于备注标签）。全程
    fail-safe：任何失败只留空，绝不阻塞、绝不改写 302 结果。

    新鲜度门禁（PA-073）：缓存里已有 label 且宽、高非空即视为
    完整，直接返回、不再发任何 HTTP——画质是 vod 的固有属性，
    7 天 TTL 内重复点播不该每次都在 302 下发前同步抓主清单；
    仅缺失/不完整（缺 label 或宽高任一为空）时才抓取回填，
    与 CMS_QUALITY_CACHE_TTL_SEC 注释的「过期后下一次点播取流
    时自然回填」口径一致。
    """
    if mapping_id is None:
        return
    try:
        existing = read_cms_quality(mapping_id) or {}
        if (existing.get("label") and existing.get("width")
                and existing.get("height")):
            return
        from backend.emby_server import quality_label as ql
        payload: dict = {}
        if isinstance(detail, dict):
            label = ql.label_from_text(detail.get("vod_remarks"))
            if label:
                payload["label"] = label
        path = (address or "").split("?", 1)[0].lower()
        if path.endswith(".m3u8"):
            get_text = _default_http_get_text if http_get_text is None \
                else http_get_text
            try:
                status, text = get_text(
                    address, _source_headers(source), M3U8_FETCH_TIMEOUT_SEC)
            except Exception:  # noqa: BLE001 — 抓清单失败留空
                status, text = None, None
            if status == 200 and text:
                master = ql.parse_m3u8_master(text)
                if master:
                    payload.update({
                        "width": master.get("width") or 0,
                        "height": master.get("height") or 0,
                        "bandwidth": master.get("bandwidth") or 0,
                        "video_range": master.get("video_range"),
                    })
                    master_label = ql.label_for_master(master)
                    if master_label:
                        payload["label"] = master_label
        if payload:
            store_cms_quality(mapping_id, payload)
    except Exception:  # noqa: BLE001 — 画质回填绝不影响取流
        pass


class UpstreamPlaybackError(Exception):
    """上游播放结构化错误：HTTP 状态 + 机器码 + 人话 + 源 id。

    code 表（见模块 docstring 的超限一节）；message 是固定文案，
    绝不拼接上游原始体（原始体可能含内部信息）与任何凭据。
    """

    def __init__(self, status: int, code: str, message: str,
                 source_id: Optional[int] = None,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.source_id = source_id
        self.retry_after = retry_after

    def to_body(self) -> dict:
        """错误体：只含 code / message / source_id，retry_after 有才带。"""
        body = {
            "code": self.code,
            "message": self.message,
            "source_id": self.source_id,
        }
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


def playback_enabled() -> bool:
    """上游播放总开关（env，默认关）。每次调用现读，供测试 monkeypatch。"""
    return (str(os.getenv(UPSTREAM_PLAYBACK_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def _env_on(env_key: str) -> bool:
    """env 开关通用读法（默认关；0/false/no/off/空 = 关）。"""
    return (str(os.getenv(env_key, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def applecms_enabled() -> bool:
    """苹果 CMS 播放开关（env，默认关）。"""
    return _env_on(APPLECMS_ENABLED_ENV)


def applecms_parse_lines_enabled() -> bool:
    """苹果 CMS 解析线路开关（env，默认关）。"""
    return _env_on(APPLECMS_PARSE_LINES_ENV)


def applecms_tier_gate_enabled() -> bool:
    """苹果 CMS 分级门控开关（env，默认关）。"""
    return _env_on(APPLECMS_TIER_GATE_ENV)


def applecms_sync_enabled() -> bool:
    """上游同步开关（env，默认关）。

    本批没有同步工人：本函数只是把门留给未来同步工作复用，
    播放分支绝不因它写库、写映射、起后台任务。
    """
    return _env_on(APPLECMS_SYNC_ENV)


def applecms_tier_configured() -> bool:
    """苹果 CMS 分级映射是否已配置。

    本批无映射（TODO：分级→可播范围的映射由后续批次定义并落
    配置后，本函数才返回 True）。现阶段恒为 False，因此分级门
    控一旦开启，苹果 CMS 播放一律 403——这是 fail-closed，不是 bug。
    """
    # TODO(苹果 CMS 分级映射)：映射定义落地后在此接配置读取。
    return False


def build_upstream_stream_url(base_url: str, upstream_item_id: str, *,
                              api_key: Optional[str] = None,
                              media_source_id: Optional[str] = None) -> str:
    """拼上游 Emby/Jellyfin 直播地址。

    形态与 Emby 直播一致：``{base}/Videos/{id}/stream?static=true``，
    mediaSourceId 原样透传（上游多版本时客户端指定用哪个），api_key
    有才带（匿名源不带）。base_url 在 25-1 建源时已归一化（无尾斜
    杠），这里仍 defensive 地去一次尾斜杠；条目 id 进 path 前 quote，
    media_source_id 与 api_key 走 urlencode，不许原文拼进 URL。
    """
    base = (base_url or "").rstrip("/")
    item = urllib.parse.quote((upstream_item_id or "").strip(), safe="")
    if not item:
        raise ValueError("上游条目 id 不能为空")
    params = [("static", "true")]
    if (media_source_id or "").strip():
        params.append(("mediaSourceId", media_source_id.strip()))
    if api_key:
        params.append(("api_key", api_key))
    return f"{base}/Videos/{item}/stream?{urllib.parse.urlencode(params)}"


def _source_headers(source) -> dict:
    """发往上游的请求头：源上配的 UA / Referer / 附加头（25-1 字段）。

    附加头在 25-1 已校验为「键值均为字符串」的对象；脏数据按空处理，
    不炸探活。凭据绝不走这里——凭据只走 api_key 参数或认证口。
    """
    headers: dict = {"Accept": "*/*"}
    if getattr(source, "user_agent", None):
        headers["User-Agent"] = source.user_agent
    if getattr(source, "referer", None):
        headers["Referer"] = source.referer
    try:
        extra = json.loads(source.extra_headers_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
    return headers


def _default_http_post(url: str, payload: dict, headers: dict,
                       timeout: float):
    """默认认证 POST 层（urllib，与 25-3 的 _default_http_get 同款）。

    返回 (status:int|None, body:dict|None)：HTTPError 带状态码，
    超时/断连返回 (None, None)。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            body = None
        return exc.code, body
    except Exception:
        return None, None


def _probe_single_hop(url: str, headers: dict, timeout: float):
    """探活单跳：返回 (status, retry_after, location)。

    单跳口径与旧探活逐字一致：成功回状态码；HTTPError 带状态码与
    Retry-After（能转 int 才带，转不了按没有计，不编数字）；
    超时/断连/出站校验拦截回 (None, None, None)。location 只取
    Location 头，任何一跳都不读响应体（探活不是拉流）。
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            location = resp.headers.get("Location") if resp.headers else None
            return resp.status, None, location
    except urllib.error.HTTPError as exc:
        retry_after = None
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if raw is not None:
            try:
                retry_after = int(str(raw).strip())
            except (TypeError, ValueError):
                retry_after = None
        location = exc.headers.get("Location") if exc.headers else None
        return exc.code, retry_after, location
    except Exception:
        return None, None, None


def _default_http_range_probe(url: str, headers: dict, timeout: float):
    """默认 Range 探活层（urllib）：只读 1 字节，不拉整条流。

    有界跟随重定向（最多 ``UPSTREAM_PROBE_MAX_REDIRECTS`` 跳）：
    上游以 302 发流是 Emby 常见形态（stream → 网盘直链 → 签名链），
    而 safe_urlopen 按 SSRF 纪律刻意不跟随；若探活也不跟，302 会
    落进 ``_classify_probe_status`` 的兜底 502，把完全可播的内容
    误判不可用（2026-10-31 生产实证：两跳 302 后 206 可播）。
    跟随只在本层手工逐跳做：每一跳的新 URL 先过
    ``upstream_http.verify_outbound_url`` 复验才发请求（safe_urlopen
    发请求前还会再验一次，opener 本身仍不装重定向处理器）；
    验不过、没有 Location、或跳数超限，一律按连不上口径处理。

    返回 (status:int|None, retry_after:int|None)：均为**最终跳**
    的状态与 Retry-After，调用方的分类表只看最终跳，与直达同
    口径；超时/断连返回 (None, None)。
    """
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"
    current_url = url
    for hop in range(UPSTREAM_PROBE_MAX_REDIRECTS + 1):
        status, retry_after, location = _probe_single_hop(
            current_url, probe_headers, timeout)
        if status not in _PROBE_REDIRECT_STATUSES:
            return status, retry_after
        if hop >= UPSTREAM_PROBE_MAX_REDIRECTS or not location:
            return None, None
        next_url = urllib.parse.urljoin(current_url, location)
        try:
            upstream_http.verify_outbound_url(next_url)
        except Exception:
            return None, None
        current_url = next_url
    return None, None


def _auth_token_for_source(source, *,
                           http_post: Optional[Callable] = None) -> Optional[str]:
    """取上游播放凭据（25-1 凭据模型的唯一消费点之一）。

    - 未配凭据 → None（匿名访问，探活与 302 都不带 api_key）；
    - api_key 形态 → 密封值本身就是 key，直接返回；
    - password 形态 → 向上游认证口换 AccessToken（Emby/Jellyfin
      标准 ``/Users/AuthenticateByName``），换不到按凭据失效/上游
      不可用映射（见内联注释），绝不降级成匿名。
    密封解不开抛 SealedSecretError，调用方按凭据失效处理。
    """
    cred = us.load_credential(source)
    if cred is None:
        return None
    if cred["auth_method"] == "api_key":
        return cred["secret"]
    if cred["auth_method"] == "password":
        post = _default_http_post if http_post is None else http_post
        base = (source.base_url or "").rstrip("/")
        headers = dict(_source_headers(source))
        headers["Content-Type"] = "application/json"
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="Aetrix", Device="upstream-playback", '
            'DeviceId="upstream-playback", Version="1.0"'
        )
        status, body = post(
            f"{base}/Users/AuthenticateByName",
            {"Username": cred["username"], "Pw": cred["secret"]},
            headers, UPSTREAM_PROBE_TIMEOUT_SEC)
        if status == 200 and isinstance(body, dict) and body.get("AccessToken"):
            return str(body["AccessToken"])
        if status in (401, 403):
            raise UpstreamPlaybackError(
                409, "upstream_credential_invalid",
                "上游凭据失效，请管理员重录该源凭据",
                source_id=source.id)
        raise UpstreamPlaybackError(
            502, "upstream_unavailable",
            "上游服务当前不可用，请稍后再试",
            source_id=source.id)
    raise UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source.id)


def _classify_probe_status(status: Optional[int], source_id: int,
                           retry_after: Optional[int]) -> None:
    """把探活状态码映射成结构化错误；可播（200/206）返回 None。

    映射口径见模块 docstring 的超限一节；其它 5xx 与 None（连不上/
    超时）一律 upstream_unavailable，不把上游内部状态原样透传。
    """
    if status in (200, 206):
        return None
    if status == 429:
        raise UpstreamPlaybackError(
            429, "upstream_quota_exhausted",
            "上游额度已耗尽，请稍后再试或换个源",
            source_id=source_id, retry_after=retry_after)
    if status == 403:
        raise UpstreamPlaybackError(
            403, "upstream_forbidden",
            "上游拒绝了本次播放，请检查该源权限",
            source_id=source_id)
    if status == 401:
        raise UpstreamPlaybackError(
            409, "upstream_credential_invalid",
            "上游凭据失效，请管理员重录该源凭据",
            source_id=source_id)
    if status == 404:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目",
            source_id=source_id)
    raise UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source_id)


def _map_applecms_error(exc, source_id: int) -> UpstreamPlaybackError:
    """把 ``AppleCMSError`` 按种类映射成既有结构化错误（不发明新码）。

    - rate_limited → 429 upstream_quota_exhausted（CMS 侧未透传
      Retry-After，本批不带该字段，不编数字）；
    - unauthorized：401 → 409 upstream_credential_invalid，
      403 → 403 upstream_forbidden（与既有探活同义映射一致）；
    - not_found → 404 upstream_item_not_found；
    - network / http_error / bad_response / bad_code → 502
      upstream_unavailable（CMS 接口非 1 即属此类）。
    """
    kind = getattr(exc, "kind", None)
    status = getattr(exc, "http_status", None)
    if kind == "rate_limited":
        return UpstreamPlaybackError(
            429, "upstream_quota_exhausted",
            "上游额度已耗尽，请稍后再试或换个源",
            source_id=source_id)
    if kind == "unauthorized":
        if status == 401:
            return UpstreamPlaybackError(
                409, "upstream_credential_invalid",
                "上游凭据失效，请管理员重录该源凭据",
                source_id=source_id)
        return UpstreamPlaybackError(
            403, "upstream_forbidden",
            "上游拒绝了本次播放，请检查该源权限",
            source_id=source_id)
    if kind == "not_found":
        return UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目",
            source_id=source_id)
    return UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source_id)


def _resolve_applecms_stream(source, vod_id: str, *,
                             episode_index: int = 1,
                             http_get: Optional[Callable] = None,
                             http_probe: Optional[Callable] = None,
                             http_get_text: Optional[Callable] = None,
                             quality_mapping_id=None,
                             parse_resolver: Optional[Callable] = None) -> str:
    """苹果 CMS 播放分支：现取现解析、先探后 302，绝不落库。

    门禁顺序：APPLECMS_ENABLED 关 → 404 upstream_playback_disabled
    （与总闸关闭行为一致）；分级门控开且映射未配置 → 403
    upstream_forbidden（文件里唯一的 403 码，复用；其标准语义是
    「上游拒绝」，此处是本地分级未配置的拒绝，message 已区分，
    若协调员要为分级单独立码，换这里一处即可）。

    vod_id 即调用方传入的 ``upstream_item_id``（映射行里记的上游
    条目 id 对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，
    默认 1（调用方未传集序号时播第 1 集）。detail 现取现解析，
    播放地址只出现在返回的 302 地址与探活请求里，不写库、不进日志
    （日志只含 source_id）。CMS 密封凭据不走 Emby 认证口：
    本分支只用源上配的 UA / Referer / 附加头（``_source_headers``）
    发请求；CMS 若需密钥，走附加头配置，不走密封凭据。
    """
    if not applecms_enabled():
        raise UpstreamPlaybackError(
            404, "upstream_playback_disabled",
            "上游播放未开启", source_id=source.id)
    if applecms_tier_gate_enabled() and not applecms_tier_configured():
        raise UpstreamPlaybackError(
            403, "upstream_forbidden",
            "该源分级映射未配置，苹果 CMS 播放暂不可用",
            source_id=source.id)
    from backend.emby_server import applecms_fetcher as acf
    headers = _source_headers(source)
    try:
        detail = acf.fetch_detail(
            source.base_url, vod_id, http_get=http_get, headers=headers)
    except acf.AppleCMSError as exc:
        raise _map_applecms_error(exc, source.id) from exc
    if detail is None:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目", source_id=source.id)
    try:
        episode = int(episode_index)
    except (TypeError, ValueError):
        episode = -1
    result = acf.resolve_play(
        detail, episode,
        parse_lines_enabled=applecms_parse_lines_enabled(),
        parse_resolver=parse_resolver)
    if result[0] != "ok":
        reason = result[1]
        if reason == "episode_out_of_range":
            raise UpstreamPlaybackError(
                404, "upstream_item_not_found",
                "上游无此集", source_id=source.id)
        messages = {
            "parse_not_enabled": "解析型线路未开启，该条目暂无直链可播",
            "parse_rule_missing": "本站无解析规则，该条目暂无直链可播",
            "no_direct_group": "该条目暂无直链可播",
        }
        raise UpstreamPlaybackError(
            502, "upstream_unavailable",
            messages.get(reason, "该条目暂无直链可播"),
            source_id=source.id)
    _, _group_name, address = result
    probe = _default_http_range_probe if http_probe is None else http_probe
    probe_status, retry_after = probe(
        address, _source_headers(source), UPSTREAM_PROBE_TIMEOUT_SEC)
    if probe_status == 403:
        # 探活软放行（CMS 分支专属，2026-11-02 定案）：多家 CMS 播放
        # CDN 对数据中心 IP 一律 403——403 只证明「服务器这个 IP 被
        # 拒」，不证明链死；detail 刚刚现取成功（源活着、条目在、
        # 直链组在、集序号不越界）才是可用性信号。照常下发 302，
        # 播放器在用户家里自拉。日志只记 id 不记地址。其余状态仍走
        # 统一分类（404 判死 / 401 凭据 / 429 额度 / 5xx 不可用）；
        # Emby/Jellyfin 分支不经这里，403 仍判 upstream_forbidden。
        logger.info("cms_probe_soft_pass source_id=%s", source.id)
        _capture_cms_quality(source, quality_mapping_id, detail, address,
                             http_get_text=http_get_text)
        return address
    _classify_probe_status(probe_status, source.id, retry_after)
    logger.info("上游播放解析成功 source_id=%s", source.id)
    _capture_cms_quality(source, quality_mapping_id, detail, address,
                         http_get_text=http_get_text)
    return address


# ---------------------------------------------------------------------------
# CMS 取流留痕、线路择优与坏源熔断（播放优化 B 件，2026-11-02）
#
# 数据来源钉死在真实点播路径上：生产探活工人三守护默认关，健康分
# 若只等探活喂数就是空中楼阁。取流收口（resolve_upstream_stream 的
# CMS 分支）逐次把成败/延迟写进既有健康记录（源级 SourceHealthRecord
# + 线路级 LineHealthRecord，同一份 Redis 存储、同一套状态机），
# PlaybackInfo 组装（get_displayable_mappings）按这份记录择优排序，
# 连续失败达阈值时经唯一启停写口 disable_source 自动停用坏源。
# ---------------------------------------------------------------------------

def _get_health_store():
    """健康记录存储口（生产 Redis 实现；测试替换本函数注入内存实现）。"""
    return uh.RedisHealthStore()


def _cms_line_key(source_id, quality_mapping_id) -> str:
    """线路健康键：有映射按映射 id（一条映射 = 一条下发线路），
    显式源口无映射时按源 id 兜底。"""
    if quality_mapping_id is not None:
        return f"mapping:{quality_mapping_id}"
    return f"source:{source_id}"


#: 取流失败的结构化错误码 → 健康状态机的 failure_kind。未列出的码
#: 不写健康结论——那是本地状态，不是源的可用性证据：
#: upstream_playback_disabled 是本地总闸；upstream_forbidden 在 CMS
#: 分支有两个来源（本地分级门控未配置、源站接口 403），按码无法
#: 区分，且 403 与探活软放行同哲学（只证明服务器 IP 被拒或本地
#: 未配，不证明源坏了），一律不记失败，免得把好源误熔断。
_CMS_FAILURE_KINDS = {
    "upstream_quota_exhausted": "rate_limited",
    "upstream_credential_invalid": "unauthorized",
    "upstream_unavailable": "server_error",
}


def _record_cms_stream_outcome(db, source, *, error_code=None,
                               latency_ms=None,
                               quality_mapping_id=None) -> None:
    """把一次 CMS 取流结论写进既有健康记录（源级 + 线路级）。

    - 源级：复用 upstream_health.apply_source_result 与
      SourceHealthRecord（键 = str(source.id)，与 CMS 轻探活同
      口径）——点播结论与探活结论共用同一份连续失败计数，健康分
      不另起第二套统计。
    - 线路级：复用 LineHealthRecord / apply_line_result（状态翻转
      仍守「源级已确认才摘线」口径），并补记线路连续失败数、失败
      原因与最近一次成功延迟，供择优排序消费。
    - 熔断：失败累计达到 cms_circuit_breaker_threshold() 且源仍
      启用时，经唯一启停写口 us.disable_source 停用并落一条审计
      日志；只对 applecms 型源生效。恢复只走手动 enable_source
      （管理端现成启停，「最大」源人工停用即先例）——生产探活
      工人默认关，不存在可靠的自动复活信号，不许假装能自动恢复。
    - 条目不存在（upstream_item_not_found）按 unknown 记：只推进
      last_checked_at。源站明明应答了、是条目没了，不许拿它给源
      记失败、更不许触发熔断。
    全程 fail-open：留痕/熔断的任何异常只记 warning，绝不打断
    取流主路径（成功照常下发、失败照常抛原结构化错误）。
    """
    try:
        if getattr(source, "source_type", None) != "applecms":
            return
        if error_code is None:
            outcome = uh.ProbeOutcome(uh.OUTCOME_SUCCESS)
        elif error_code == "upstream_item_not_found":
            outcome = uh.ProbeOutcome(uh.OUTCOME_UNKNOWN)
        elif error_code in _CMS_FAILURE_KINDS:
            outcome = uh.ProbeOutcome(
                uh.OUTCOME_FAILURE, _CMS_FAILURE_KINDS[error_code])
        else:
            return
        store = _get_health_store()
        now = time.time()
        source_key = str(source.id)
        record = store.get_source(source_key) or uh.SourceHealthRecord(
            source_key=source_key)
        uh.apply_source_result(record, outcome,
                               threshold=uh.failure_threshold(), now=now)
        store.set_source(record)
        line_key = _cms_line_key(source.id, quality_mapping_id)
        line = store.get_line(line_key) or uh.LineHealthRecord(
            line_key=line_key)
        uh.apply_line_result(
            line, outcome,
            confirmed=record.availability_state == uh.STATE_UNAVAILABLE,
            now=now)
        if outcome.kind == uh.OUTCOME_SUCCESS:
            line.consecutive_failures = 0
            line.last_success_at = now
            if latency_ms is not None:
                line.last_latency_ms = float(latency_ms)
        elif outcome.kind == uh.OUTCOME_FAILURE:
            line.consecutive_failures += 1
            line.last_failure_kind = outcome.failure_kind
            line.last_failure_at = now
        store.set_line(line)
        if (outcome.kind == uh.OUTCOME_FAILURE and db is not None
                and getattr(source, "enabled", False)
                and record.consecutive_unavailable_failures
                >= cms_circuit_breaker_threshold()):
            us.disable_source(db, source)
            db.commit()
            logger.error(
                "cms_circuit_breaker_open source_id=%s failures=%s "
                "threshold=%s trigger=resolve_upstream_stream",
                source.id, record.consecutive_unavailable_failures,
                cms_circuit_breaker_threshold())
    except Exception:  # noqa: BLE001 — 留痕/熔断不许打断取流，见 docstring
        logger.warning("CMS 取流留痕/熔断异常，已忽略 source_id=%s",
                       getattr(source, "id", None), exc_info=True)


def _mapping_health_sort_key(store, mapping, source):
    """线路择优排序键（越小越靠前；同键靠稳定排序保持映射偏好序）。

    分层：源健康已判不可用（含过期等效不可用）沉底；源有连续失败
    或本线路已翻 unavailable 的次之；健康线路在前。层内先按连续
    失败数升序、再按最近成功延迟升序。无记录 = 没有坏过的证据，
    按健康计（与 is_source_available 的无记录当 Active 同口径）。
    """
    record = store.get_source(str(source.id))
    line = store.get_line(_cms_line_key(source.id, mapping.id))
    if record is not None and not uh.is_source_available(record):
        tier = 2
    elif ((record is not None
           and record.consecutive_unavailable_failures > 0)
          or (line is not None and not uh.is_line_available(line))):
        tier = 1
    else:
        tier = 0
    failures = record.consecutive_unavailable_failures if record else 0
    if line is not None and line.consecutive_failures > failures:
        failures = line.consecutive_failures
    latency = line.last_latency_ms if line is not None else None
    return (tier, failures,
            latency if latency is not None else float("inf"))


def resolve_upstream_stream(db: Session, source_id: int,
                            upstream_item_id: str, *,
                            media_source_id: Optional[str] = None,
                            http_probe: Optional[Callable] = None,
                            http_post: Optional[Callable] = None,
                            episode_index: int = 1,
                            http_get: Optional[Callable] = None,
                            http_get_text: Optional[Callable] = None,
                            quality_mapping_id=None,
                            parse_resolver: Optional[Callable] = None) -> str:
    """上游播放解析：查源 → 取凭据 → 拼地址 → Range 探活 → 返回 302 地址。

    每一步失败都以 UpstreamPlaybackError 结构化返回，绝不返回死链、
    绝不静默回落其它源（换源是客户端拿着 code 自己决定的事，全站
    一个规矩：本地优先、上游兜底，不在本函数里替客户端挑第二个源）。
    关着的源、不存在的源、开关关，一律先失败，不发任何上游请求。
    """
    if not playback_enabled():
        raise UpstreamPlaybackError(
            404, "upstream_playback_disabled",
            "上游播放未开启", source_id=source_id)
    item_id = (upstream_item_id or "").strip()
    if not item_id:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目", source_id=source_id)
    source = us.get_source(db, source_id)
    if source is None:
        raise UpstreamPlaybackError(
            404, "upstream_source_not_found",
            "上游源不存在", source_id=source_id)
    if not source.enabled:
        raise UpstreamPlaybackError(
            409, "upstream_source_disabled",
            "该上游源未启用", source_id=source_id)
    if getattr(source, "source_type", None) == "applecms":
        # CMS 取流留痕/熔断接线（播放优化 B 件）：逐次结论写既有
        # 健康记录，失败达阈值自动停用坏源；留痕全程 fail-open，
        # 原结构化错误与成功地址逐字不变（见 _record_cms_stream_outcome）。
        _cms_t0 = time.perf_counter()
        try:
            _cms_url = _resolve_applecms_stream(
                source, item_id, episode_index=episode_index,
                http_get=http_get, http_probe=http_probe,
                http_get_text=http_get_text,
                quality_mapping_id=quality_mapping_id,
                parse_resolver=parse_resolver)
        except UpstreamPlaybackError as exc:
            _record_cms_stream_outcome(
                db, source, error_code=exc.code,
                latency_ms=int((time.perf_counter() - _cms_t0) * 1000),
                quality_mapping_id=quality_mapping_id)
            raise
        _record_cms_stream_outcome(
            db, source,
            latency_ms=int((time.perf_counter() - _cms_t0) * 1000),
            quality_mapping_id=quality_mapping_id)
        return _cms_url
    try:
        token = _auth_token_for_source(source, http_post=http_post)
    except us.SealedSecretError as exc:
        raise UpstreamPlaybackError(
            409, "upstream_credential_invalid",
            "上游凭据失效，请管理员重录该源凭据",
            source_id=source_id) from exc
    url = build_upstream_stream_url(
        source.base_url, item_id, api_key=token,
        media_source_id=media_source_id)
    probe = _default_http_range_probe if http_probe is None else http_probe
    status, retry_after = probe(url, _source_headers(source),
                                UPSTREAM_PROBE_TIMEOUT_SEC)
    _classify_probe_status(status, source.id, retry_after)
    logger.info("上游播放解析成功 source_id=%s", source.id)
    return url


# ---------------------------------------------------------------------------
# U-02 详情页上游线路（客户端入口）
#
# 详情/PlaybackInfo 的来源列表在本地条目有上游映射时多一条「上游」线路，
# 点它走本站取流口：口子按本地条目 id 现查映射（get_best_mapping）、
# 复验后才调 resolve_upstream_stream——(source_id, upstream_item_id)
# 永不下发给前端长期持有。线路开关是 SystemConfig（upstream_aggregate
# 家族），默认开；关时列表不出现、取流口 404 明示。
# ---------------------------------------------------------------------------

#: 详情页上游线路开关 SystemConfig 键（upstream_aggregate 家族）。
KEY_DETAIL_LINE_ENABLED = "upstream_aggregate_detail_line_enabled"


def _system_config_bool(db, key, default):
    """SystemConfig 布尔热读：无行/非法/异常一律回默认。

    与 transfer_variant._system_config_bool 同口径（读失败不许把
    展示链路打挂，一律回落代码默认）。
    """
    if db is None:
        return default
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return default
    text = str(raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def detail_line_enabled(db) -> bool:
    """详情页上游线路开关：默认开（已部署功能开关默认开）。"""
    return _system_config_bool(db, KEY_DETAIL_LINE_ENABLED, True)


def get_displayable_mapping(db, local_item_id):
    """列表展示口径的映射读取：返回 ``(mapping, source)`` 或 None。

    命中 get_best_mapping 且源存在、已启用才算可展示；映射指向的源
    已删/已停用时线路不出现（取流口复验同口径，见
    resolve_mapped_upstream_stream）。
    """
    mapping = uim.get_best_mapping(db, local_item_id)
    if mapping is None:
        return None
    source = us.get_source(db, mapping.source_id)
    if source is None or not source.enabled:
        return None
    return mapping, source


def get_displayable_mappings(db, local_item_id):
    """列表展示口径的全部映射读取：返回 ``[(mapping, source), ...]``。

    播放源按映射逐条下发（站主 2026-11-02 亲定：每条上游线路一个
    MediaSource、点选哪条定向解析哪条）需要的全量读口。有效性口径
    与 get_displayable_mapping 逐字一致（映射层 O-10 校验先行，
    再逐条验源存在且启用），偏好序即列表展示序（最佳在前）。

    择优排序（播放优化 B 件）：返回前按既有健康记录稳定排序——
    不可用/连续失败的线路沉底、延迟低且成功率高的排前面、最优线
    成为默认首位，其余线保留可手选；同分保持映射偏好序。健康
    记录读不到（Redis 故障等）时 fail-open 回偏好序原样，绝不
    因为择优把线路弄丢或把 PlaybackInfo 组装打挂。
    """
    out = []
    for mapping in uim.get_all_mappings(db, local_item_id):
        source = us.get_source(db, mapping.source_id)
        if source is None or not source.enabled:
            continue
        out.append((mapping, source))
    if len(out) > 1:
        try:
            store = _get_health_store()
            out.sort(key=lambda pair: _mapping_health_sort_key(
                store, pair[0], pair[1]))
        except Exception:  # noqa: BLE001 — 择优是补强，故障回偏好序
            logger.warning("上游线路择优排序异常，按映射偏好序下发",
                           exc_info=True)
    return out


def _default_http_get_json(url: str, headers: dict, timeout: float):
    """默认 GET 层（与 _default_http_post 同款）：返回 (status, body)。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            body = None
        return exc.code, body
    except Exception:
        return None, None


#: 现查媒体参数时轨道只许带这些字段：上游 MediaStream 里的
#: DeliveryUrl / Path 等可能嵌着对方地址与凭据，一律不许过界。
_MEDIA_PARAM_STREAM_KEYS = (
    "Index", "Type", "Codec", "Language", "DisplayTitle", "IsDefault",
    "IsForced", "IsExternal", "Width", "Height", "Channels", "BitRate",
    "AspectRatio", "VideoRange", "SampleRate",
)


def fetch_upstream_media_params(source, upstream_item_id: str, *,
                                http_get: Optional[Callable] = None,
                                http_post: Optional[Callable] = None):
    """现查上游条目的媒体参数（无本地源时的合成源取数，进库一期）。

    GET ``{base}/Items/{id}`` 取首个 MediaSource 的 Size /
    RunTimeTicks / Container / Bitrate 与轨道摘要（白名单字段，见
    ``_MEDIA_PARAM_STREAM_KEYS``——绝不带上游 URL、路径与凭据）。
    凭据走本模块唯一消费点 ``_auth_token_for_source``。任何失败
    （凭据失效、上游不可用、响应异常）一律返回 None：合成源回落
    本地行数据，现查只是参数补强，绝不许把 PlaybackInfo 打挂。
    """
    try:
        token = _auth_token_for_source(source, http_post=http_post)
    except Exception:
        return None
    base = (source.base_url or "").rstrip("/")
    item_part = urllib.parse.quote((upstream_item_id or "").strip(), safe="")
    if not base or not item_part:
        return None
    url = f"{base}/Items/{item_part}"
    if token:
        url += "?" + urllib.parse.urlencode({"api_key": token})
    get = _default_http_get_json if http_get is None else http_get
    try:
        status, body = get(url, _source_headers(source),
                           UPSTREAM_PROBE_TIMEOUT_SEC)
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    media_sources = body.get("MediaSources")
    if not isinstance(media_sources, list) or not media_sources:
        return None
    first = media_sources[0]
    if not isinstance(first, dict):
        return None
    streams = []
    raw_streams = first.get("MediaStreams")
    if isinstance(raw_streams, list):
        for raw in raw_streams:
            if isinstance(raw, dict):
                streams.append({k: raw[k] for k in _MEDIA_PARAM_STREAM_KEYS
                                if k in raw})
    return {
        "size": first.get("Size"),
        "run_time_ticks": first.get("RunTimeTicks"),
        "container": first.get("Container"),
        "bitrate": first.get("Bitrate"),
        "media_streams": streams,
    }


def resolve_mapped_upstream_stream(db: Session, local_item_id: int, *,
                                   mapping_id=None,
                                   http_probe=None, http_post=None,
                                   http_get=None, http_get_text=None,
                                   parse_resolver=None) -> str:
    """按本地条目 id 解析上游取流地址（详情页上游线路取流口专用）。

    现查映射 + 复验：开关关 → 404 upstream_line_disabled；无映射 →
    404 upstream_mapping_not_found；源停用按 O-10 读契约
    （get_best_mapping 默认 validate=True）在现查时即不可见、同
    无映射口径 404（查与解析之间源被停用的竞态仍由
    resolve_upstream_stream 抛 409 upstream_source_disabled）；
    凭据失效等其余结构化错误原样上抛——明示失败，
    绝不静默回落到任何其它线路。

    ``mapping_id`` 定向口径（站主 2026-11-02 亲定，FMBY 形态）：
    播放器点选了具体某条线路（MediaSourceId 带映射 id）时，只解
    析那一条映射——映射不存在、已不属于本条目、或按读契约已不可
    见（源停用/条目已死），一律 404 upstream_mapping_not_found
    硬报错，**绝不偷偷换到最佳映射**。不带 mapping_id 的旧形态
    （MediaSourceId 只到 ``:upstream``）保持原最佳映射口径。
    """
    if not detail_line_enabled(db):
        raise UpstreamPlaybackError(
            404, "upstream_line_disabled", "上游线路未开启")
    if mapping_id is not None:
        mapping = None
        for row in uim.get_all_mappings(db, local_item_id):
            if row.id == mapping_id:
                mapping = row
                break
        if mapping is None:
            raise UpstreamPlaybackError(
                404, "upstream_mapping_not_found", "所选线路已不可用")
    else:
        mapping = uim.get_best_mapping(db, local_item_id)
        if mapping is None:
            raise UpstreamPlaybackError(
                404, "upstream_mapping_not_found", "该条目暂无上游线路")
    return resolve_upstream_stream(
        db, mapping.source_id, mapping.upstream_item_id,
        episode_index=mapping.episode_index or 1,
        http_probe=http_probe, http_post=http_post, http_get=http_get,
        http_get_text=http_get_text, quality_mapping_id=mapping.id,
        parse_resolver=parse_resolver)
