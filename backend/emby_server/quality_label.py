# -*- coding: utf-8 -*-
"""播放源画质标签（站主 2026-11-02 亲定口径）。

第三方播放器的来源卡片标题只认画质标：别家源卡显示「Dolby」、
大小、码率，并按分辨率进 4K 页签；我方此前 Name 下发的是片名、
CMS 线路名里还带着源名，卡片只剩「文件」二字落进「其他」页签。
本模块是全站唯一的画质标签构造点（横切只许一套）：

- ``Name`` 一律纯画质标签：分辨率标 + 动态范围标的组合
  （如 ``4K Dolby`` / ``1080P HDR`` / ``720P`` / ``Dolby``）；
  不带片名、不带源名、不带服务器名、不带发行来源（WEB-DL 等）。
- 分辨率标口径（Emby 惯例的短标）：高度 ≥2000 或宽度 ≥3800 →
  ``4K``；≥1000 / ≥1900 → ``1080P``；≥700 → ``720P``；
  ≥400 → ``480P``；文件名解析出的 ``2160p`` 同样归 ``4K``。
- 动态范围标：Dolby Vision → ``Dolby``；HDR10+ → ``HDR10+``；
  HDR10 → ``HDR``；HLG → ``HLG``；SDR / 未知 → 不带范围标。
- 数据来源优先级：真实探测（宽高 / video_range）> 文件名解析
  > CMS 备注文本标记。每一部分独立取舍，绝不互相编造：
  探测有宽高就不用文件名猜分辨率，文件名没有 DV 标记也不会
  因为分辨率高就虚报 Dolby。
- 实在判不出（无探测、文件名无标记）返回 None，调用方回落
  Emby 默认形态（MediaSource.Name = 条目名），不许拿别的信息凑数。
"""

from __future__ import annotations

import re
from typing import Optional

#: 探测/存储侧 video_range 原值 → 范围标。存储口径见
#: MediaStream.video_range（SDR / HDR10 / HLG / DolbyVision）。
_RANGE_TOKENS = {
    "dolbyvision": "Dolby",
    "dolby vision": "Dolby",
    "dolby": "Dolby",
    "dovi": "Dolby",
    "dv": "Dolby",
    "hdr10plus": "HDR10+",
    "hdr10+": "HDR10+",
    "hdr10": "HDR",
    "hdr": "HDR",
    "hlg": "HLG",
}

#: 文件名/备注文本里的范围标记（按优先级从高到低）。
#: ``DV`` 单独成词时才算杜比（防 DVDRip 之类误伤：``dv`` 后跟
#: 字母直接排除，``dvdrip`` 不命中）。
_TEXT_RANGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"dolby|dovi|杜比", re.IGNORECASE), "Dolby"),
    (re.compile(r"(?<![a-z])dv(?![a-z])", re.IGNORECASE), "Dolby"),
    (re.compile(r"hdr10\+|hdr10plus", re.IGNORECASE), "HDR10+"),
    (re.compile(r"hdr10", re.IGNORECASE), "HDR"),
    (re.compile(r"hlg", re.IGNORECASE), "HLG"),
    (re.compile(r"(?<![a-z])hdr(?![a-z0-9])", re.IGNORECASE), "HDR"),
]

#: 文本里的分辨率标记 → 分辨率标（只认带 p / 明确 4K 的写法，
#: 裸 ``HD`` 语义含糊，不猜）。
_TEXT_RESOLUTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"2160p|(?<![a-z0-9])4k(?![a-z0-9])|uhd", re.IGNORECASE), "4K"),
    (re.compile(r"1080p", re.IGNORECASE), "1080P"),
    (re.compile(r"720p", re.IGNORECASE), "720P"),
    (re.compile(r"480p", re.IGNORECASE), "480P"),
]

#: filename_meta 的解析分辨率（2160p 等）→ 本模块分辨率标。
_PARSED_RESOLUTION_TOKENS = {
    "2160p": "4K",
    "1080p": "1080P",
    "720p": "720P",
    "480p": "480P",
}


def resolution_token(width=None, height=None,
                     parsed_resolution: Optional[str] = None
                     ) -> Optional[str]:
    """分辨率标：真实宽高优先，其次文件名解析结果。"""
    try:
        w = int(width or 0)
    except (TypeError, ValueError):
        w = 0
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        h = 0
    if h >= 2000 or w >= 3800:
        return "4K"
    if h >= 1000 or w >= 1900:
        return "1080P"
    if h >= 700 or w >= 1200:
        return "720P"
    if h >= 400 or w >= 800:
        return "480P"
    if parsed_resolution:
        return _PARSED_RESOLUTION_TOKENS.get(
            str(parsed_resolution).strip().lower())
    return None


def range_token(video_range: Optional[str]) -> Optional[str]:
    """探测到的 video_range 原值 → 范围标；SDR/未知返回 None。"""
    if not video_range:
        return None
    key = str(video_range).strip().lower()
    if key in ("sdr", ""):
        return None
    return _RANGE_TOKENS.get(key)


def text_range_token(text: Optional[str]) -> Optional[str]:
    """从文件名/备注文本里找动态范围标记（找不到返回 None）。"""
    if not text:
        return None
    for pat, token in _TEXT_RANGE_PATTERNS:
        if pat.search(str(text)):
            return token
    return None


def text_resolution_token(text: Optional[str]) -> Optional[str]:
    """从备注文本里找分辨率标记（只认明确写法，找不到返回 None）。"""
    if not text:
        return None
    for pat, token in _TEXT_RESOLUTION_PATTERNS:
        if pat.search(str(text)):
            return token
    return None


def combine(resolution: Optional[str], range_: Optional[str]) -> Optional[str]:
    """分辨率标 + 范围标拼成最终标签；两者皆无返回 None。"""
    parts = [p for p in (resolution, range_) if p]
    return " ".join(parts) if parts else None


def label_for(width=None, height=None, video_range: Optional[str] = None,
              filename: Optional[str] = None) -> Optional[str]:
    """本地条目画质标签：探测优先、文件名补缺（逐分量独立取舍）。

    - 分辨率：item 宽高（探测）→ 文件名解析；
    - 范围：探测 video_range → 文件名标记。
    两分量都判不出返回 None（调用方回落 Emby 默认形态）。
    """
    parsed_resolution = None
    filename_range = None
    if filename:
        from backend.emby_server import filename_meta
        meta = filename_meta.parse_filename(filename)
        parsed_resolution = meta.get("resolution")
        filename_range = text_range_token(filename)
    resolution = resolution_token(width, height, parsed_resolution)
    range_ = range_token(video_range) or filename_range
    return combine(resolution, range_)


def label_from_streams(media_streams) -> Optional[str]:
    """从上游现查的 MediaStreams 摘要（白名单 dict 列表）拼标签。

    取第一条 Video 轨的 Width/Height/VideoRange；无视频轨或全空
    返回 None。供上游线路（Emby 型现查参数）命名用。
    """
    for st in media_streams or []:
        if not isinstance(st, dict):
            continue
        if (st.get("Type") or "").lower() != "video":
            continue
        return combine(
            resolution_token(st.get("Width"), st.get("Height")),
            range_token(st.get("VideoRange")))
    return None


def label_from_text(text: Optional[str]) -> Optional[str]:
    """从 CMS 备注等源数据文本拼标签（只认明确标记，不猜）。"""
    if not text:
        return None
    return combine(text_resolution_token(text), text_range_token(text))


_STREAM_INF_PREFIX = "#EXT-X-STREAM-INF:"


def _parse_attr_list(text: str) -> dict:
    """解析 HLS 属性列表（值可带引号、引号内可含逗号）。"""
    attrs: dict = {}
    parts: list[str] = []
    buf = ""
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            buf += ch
        elif ch == "," and not in_quotes:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        attrs[key.strip().upper()] = value.strip().strip('"')
    return attrs


def parse_m3u8_master(text: Optional[str]) -> Optional[dict]:
    """解析 m3u8 主清单：取 BANDWIDTH 最高的一档变体参数。

    返回 ``{"width": int, "height": int, "bandwidth": int,
    "video_range": str|None}``（缺的字段为 0/None）；没有变体行
    （单码率清单/非主清单）返回 None——判不出就不报，不猜。
    video_range 只取清单里有据可查的信号：CODECS 含 dvhe/dvh1 →
    DolbyVision；VIDEO-RANGE=HLG → HLG；VIDEO-RANGE=PQ → HDR10。
    """
    if not text:
        return None
    best: Optional[dict] = None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line.startswith(_STREAM_INF_PREFIX):
            continue
        attrs = _parse_attr_list(line[len(_STREAM_INF_PREFIX):])
        try:
            bandwidth = int(attrs.get("BANDWIDTH") or 0)
        except ValueError:
            bandwidth = 0
        width = height = 0
        resolution = attrs.get("RESOLUTION") or ""
        if "x" in resolution:
            w_text, h_text = resolution.split("x", 1)
            try:
                width, height = int(w_text), int(h_text)
            except ValueError:
                width = height = 0
        codecs = (attrs.get("CODECS") or "").lower()
        video_range = None
        if "dvhe" in codecs or "dvh1" in codecs:
            video_range = "DolbyVision"
        elif (attrs.get("VIDEO-RANGE") or "").upper() == "HLG":
            video_range = "HLG"
        elif (attrs.get("VIDEO-RANGE") or "").upper() == "PQ":
            video_range = "HDR10"
        candidate = {"width": width, "height": height,
                     "bandwidth": bandwidth, "video_range": video_range}
        if best is None or bandwidth > best["bandwidth"]:
            best = candidate
    return best


def media_playlist_duration_sec(text: Optional[str]) -> Optional[float]:
    """media playlist 总时长（秒）：全部分段 #EXTINF 时长求和。

    只有分段、没有主清单变体的清单（部分源直链即此形态）拿不到
    宽高/带宽，但时长是真信号——逐行累加 ``#EXTINF:<秒>,``。
    主清单（含 #EXT-X-STREAM-INF）不是计量对象（时长在变体清单
    里），返回 None；没有 EXTINF 或合计非正同样返回 None：
    判不出就不报，不猜。坏行跳过不整单作废。
    """
    if not text:
        return None
    total = 0.0
    seen = False
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith(_STREAM_INF_PREFIX):
            return None
        if not line.startswith("#EXTINF:"):
            continue
        num = line[len("#EXTINF:"):].split(",", 1)[0].strip()
        try:
            total += float(num)
        except ValueError:
            continue
        seen = True
    if not seen or total <= 0:
        return None
    return round(total, 3)


def label_for_master(master: Optional[dict]) -> Optional[str]:
    """主清单解析结果 → 画质标签（判不出返回 None）。"""
    if not master:
        return None
    return combine(
        resolution_token(master.get("width"), master.get("height")),
        range_token(master.get("video_range")))
