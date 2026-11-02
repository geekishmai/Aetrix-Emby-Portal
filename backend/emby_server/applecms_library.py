# -*- coding: utf-8 -*-
"""苹果 CMS 媒体库类型的共享小工具（库行字段解析，零依赖）。

库类型本身长在 ``emby_libraries`` 行上（``kind`` /
``upstream_source_ids`` / ``cms_type_map`` 三列，见 models.Library），
消费方有三类：管理端建库/改库（portal）、扫描隔离（scan_queue /
scanner）、CMS 同步工人（applecms_library_sync）。解析口径必须只有
一份，所以收在这里；本模块只做纯文本解析，不碰 DB、不碰网络。
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional

#: 库类型取值：local = 现有全部库；applecms = 本案新类型。
KIND_LOCAL = "local"
KIND_APPLECMS = "applecms"
LIBRARY_KINDS = (KIND_LOCAL, KIND_APPLECMS)


def library_kind(library) -> str:
    """库类型：老行/老对象没有 kind 列时按 local（升级前行为）。"""
    kind = getattr(library, "kind", None)
    return kind if kind in LIBRARY_KINDS else KIND_LOCAL


def is_applecms_library(library) -> bool:
    return library_kind(library) == KIND_APPLECMS


def parse_upstream_source_ids(raw) -> List[int]:
    """逗号分隔的源 id 文本 → int 列表（去空、去重、保序）。

    入参可为库行对象（取其 upstream_source_ids 列）或原始文本。
    非法片段直接跳过：这一列只由管理端校验后写入，读侧宽容不炸。
    """
    if raw is not None and not isinstance(raw, str):
        raw = getattr(raw, "upstream_source_ids", "")
    text = (raw or "").strip()
    if not text:
        return []
    out: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


def format_upstream_source_ids(ids: Iterable[int]) -> str:
    out: List[int] = []
    for value in ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return ",".join(str(v) for v in out)


def parse_cms_type_map(raw) -> dict:
    """分类映射 JSON 文本 → {分类名: "in"/"out"}（非法一律回空 dict）。

    入参可为库行对象（取其 cms_type_map 列）、原始文本或已解析 dict。
    空 = 全收（由消费方按此口径处理）；值只认 in/out，其余条目丢弃。
    """
    if raw is not None and not isinstance(raw, (str, dict)):
        raw = getattr(raw, "cms_type_map", None)
    if isinstance(raw, dict):
        data = raw
    else:
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        name = str(key).strip()
        verdict = str(value).strip().lower()
        if name and verdict in ("in", "out"):
            out[name] = verdict
    return out


def format_cms_type_map(mapping) -> Optional[str]:
    """分类映射 dict → 入库 JSON 文本；空映射存 None（= 全收）。"""
    parsed = parse_cms_type_map(mapping)
    if not parsed:
        return None
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 地区分流（抓详情分流，2026-11-02 定案）：库级桶配置 + vod_area 归一化
# ---------------------------------------------------------------------------

#: 地区桶键（库行 ``cms_area_bucket`` 的合法值；空 = 不分地区）。
AREA_BUCKET_HUAYU = "huayu"
AREA_BUCKET_OUMEI = "oumei"
AREA_BUCKET_RIHAN = "rihan"
AREA_BUCKETS = (AREA_BUCKET_HUAYU, AREA_BUCKET_OUMEI, AREA_BUCKET_RIHAN)

#: ``area_bucket`` 的非库桶返回值：已知地区但不属三桶（含横跨两桶
#: 的合拍）/ 完全判不出。两者都不入地区库、留总库，区别只在计数
#: 与后续补表空间（unknown 补上国名表后可再判，other 不会变）。
AREA_OTHER = "other"
AREA_UNKNOWN = "unknown"

#: 桶 → 管理端中文名（选项文案一处定义，前端不另写一份）。
AREA_BUCKET_LABELS = {
    "": "不分地区",
    AREA_BUCKET_HUAYU: "华语",
    AREA_BUCKET_OUMEI: "欧美",
    AREA_BUCKET_RIHAN: "日韩",
}

# 地区字面值 → 桶：源站自带的大区写法直接归桶。
_AREA_LITERAL_BUCKETS = {
    "华语": AREA_BUCKET_HUAYU,
    "欧美": AREA_BUCKET_OUMEI,
    "日韩": AREA_BUCKET_RIHAN,
}

# 国名/地区名 → 桶（归并口径 2026-11-02 定案）：港台澳归华语；
# 日本＋韩国归日韩；欧洲＋美洲＋大洋洲国家归欧美。表里没有的名
# 字一律判不出——不许 substring 猜（脏值「美国},{」切分后只剩
# 「美国」能查表，靠的是切分＋精确查表，不是模糊匹配）。
_AREA_NAME_BUCKETS = {
    # 华语
    "中国": AREA_BUCKET_HUAYU, "大陆": AREA_BUCKET_HUAYU,
    "中国大陆": AREA_BUCKET_HUAYU, "内地": AREA_BUCKET_HUAYU,
    "中國": AREA_BUCKET_HUAYU, "中國大陸": AREA_BUCKET_HUAYU,
    "香港": AREA_BUCKET_HUAYU, "中国香港": AREA_BUCKET_HUAYU,
    "台湾": AREA_BUCKET_HUAYU, "中国台湾": AREA_BUCKET_HUAYU,
    "台灣": AREA_BUCKET_HUAYU, "澳门": AREA_BUCKET_HUAYU,
    "中国澳门": AREA_BUCKET_HUAYU,
    # 日韩
    "日本": AREA_BUCKET_RIHAN, "韩国": AREA_BUCKET_RIHAN,
    "韓國": AREA_BUCKET_RIHAN,
    # 欧美（欧洲）
    "英国": AREA_BUCKET_OUMEI, "英國": AREA_BUCKET_OUMEI,
    "法国": AREA_BUCKET_OUMEI, "法國": AREA_BUCKET_OUMEI,
    "德国": AREA_BUCKET_OUMEI, "德國": AREA_BUCKET_OUMEI,
    "意大利": AREA_BUCKET_OUMEI, "西班牙": AREA_BUCKET_OUMEI,
    "葡萄牙": AREA_BUCKET_OUMEI, "荷兰": AREA_BUCKET_OUMEI,
    "比利时": AREA_BUCKET_OUMEI, "瑞士": AREA_BUCKET_OUMEI,
    "奥地利": AREA_BUCKET_OUMEI, "爱尔兰": AREA_BUCKET_OUMEI,
    "丹麦": AREA_BUCKET_OUMEI, "瑞典": AREA_BUCKET_OUMEI,
    "挪威": AREA_BUCKET_OUMEI, "芬兰": AREA_BUCKET_OUMEI,
    "冰岛": AREA_BUCKET_OUMEI, "波兰": AREA_BUCKET_OUMEI,
    "捷克": AREA_BUCKET_OUMEI, "匈牙利": AREA_BUCKET_OUMEI,
    "希腊": AREA_BUCKET_OUMEI, "罗马尼亚": AREA_BUCKET_OUMEI,
    "保加利亚": AREA_BUCKET_OUMEI, "克罗地亚": AREA_BUCKET_OUMEI,
    "塞尔维亚": AREA_BUCKET_OUMEI, "乌克兰": AREA_BUCKET_OUMEI,
    "俄罗斯": AREA_BUCKET_OUMEI,
    # 欧美（美洲＋大洋洲）
    "美国": AREA_BUCKET_OUMEI, "美國": AREA_BUCKET_OUMEI,
    "加拿大": AREA_BUCKET_OUMEI, "墨西哥": AREA_BUCKET_OUMEI,
    "巴西": AREA_BUCKET_OUMEI, "阿根廷": AREA_BUCKET_OUMEI,
    "智利": AREA_BUCKET_OUMEI, "哥伦比亚": AREA_BUCKET_OUMEI,
    "秘鲁": AREA_BUCKET_OUMEI, "委内瑞拉": AREA_BUCKET_OUMEI,
    "澳大利亚": AREA_BUCKET_OUMEI, "新西兰": AREA_BUCKET_OUMEI,
}

# 已知但不属三桶的地区名 → other（与「表里没有」分开：这些判得
# 出是别处，只是 不入地区库；补国名表也不会改变它们的归属）。
_AREA_OTHER_NAMES = {
    "印度", "泰国", "印尼", "印度尼西亚", "土耳其", "越南",
    "菲律宾", "马来西亚", "新加坡", "缅甸", "柬埔寨", "老挝",
    "巴基斯坦", "孟加拉国", "斯里兰卡", "尼泊尔", "哈萨克斯坦",
    "伊朗", "伊拉克", "以色列", "沙特阿拉伯", "阿联酋", "埃及",
    "南非", "尼日利亚", "肯尼亚", "埃塞俄比亚",
    "其它", "其他",
}

#: vod_area 切分符与脏字符：合拍写成「中国大陆,美国」之类多值串，
#: 脏值带 ``},{`` 这类残损符号——一律先当分隔符切开再逐片查表。
_AREA_SPLIT_CHARS = ",，、/;；|{}[]\"' "


def _area_tokens(raw) -> List[str]:
    if raw is None or not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    for ch in _AREA_SPLIT_CHARS:
        text = text.replace(ch, "\x00")
    return [part.strip() for part in text.split("\x00") if part.strip()]


def area_bucket(vod_area) -> str:
    """``vod_area`` 原文 → 地区桶键（全仓唯一实现）。

    返回 ``huayu`` / ``oumei`` / ``rihan`` / ``other`` /
    ``unknown`` 之一。消费方：CMS 同步工人判桶、CMS 存量迁移
    判桶——共用本函数，不许各写一份。规则：逐片精确查表；所有
    片段同属一桶才是该桶；横跨两桶（含与 other 混）= 合拍 →
    ``other``；只有 other 片段 → ``other``；一片都查不到（含
    空值/非字符串）→ ``unknown``。
    """
    buckets = set()
    saw_other = False
    for token in _area_tokens(vod_area):
        if token in _AREA_LITERAL_BUCKETS:
            buckets.add(_AREA_LITERAL_BUCKETS[token])
        elif token in _AREA_NAME_BUCKETS:
            buckets.add(_AREA_NAME_BUCKETS[token])
        elif token in _AREA_OTHER_NAMES:
            saw_other = True
    if len(buckets) == 1 and not saw_other:
        return next(iter(buckets))
    if buckets or saw_other:
        return AREA_OTHER
    return AREA_UNKNOWN


def parse_cms_area_bucket(raw) -> str:
    """库行 ``cms_area_bucket`` → 桶键（读侧宽容：非法一律回 ""）。

    入参可为库行对象（取其 cms_area_bucket 列）或原始文本；
    "" = 不分地区（总库行为）。
    """
    if raw is not None and not isinstance(raw, str):
        raw = getattr(raw, "cms_area_bucket", None)
    text = (raw or "").strip().lower()
    return text if text in AREA_BUCKETS else ""


def format_cms_area_bucket(value) -> Optional[str]:
    """桶键 → 入库值（写侧严格：非法直接 ValueError）。

    空/None 存 None（= 不分地区）；合法桶原样；其余报错，逼
    调用方（管理端 API）在入库前把非法值挡成 400。
    """
    text = (value or "").strip().lower() if isinstance(value, str) \
        else value
    if text is None or text == "":
        return None
    if text in AREA_BUCKETS:
        return text
    raise ValueError(f"未知地区桶: {value!r}")
