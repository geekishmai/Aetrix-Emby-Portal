# -*- coding: utf-8 -*-
"""苹果 CMS 地区归一化口径（抓详情分流单，2026-11-02 定案）。

``area_bucket`` 是全仓唯一的地区归并实现：同步工人判桶、存量
迁移判桶共用一份（横切只许一套）。口径：
- 港台 → 华语；日本＋韩国 → 日韩；欧美国家表 → 欧美；
- 合拍（单条 area 横跨两桶）与已知非三桶地区 → ``other``；
- 空值/未识别/纯脏值 → ``unknown``；脏值（如 ``美国},{``）须
  容错切分后查表，查不到不许 substring 猜。
库级配置 ``cms_area_bucket`` 的解析/格式化也在本文件回归：
空 = 不分地区（总库行为），非法值读侧宽容回空、写侧报错。
"""

from __future__ import annotations

import pytest

from backend.emby_server import applecms_library as acl


@pytest.mark.parametrize("raw,expected", [
    # 华语：大陆/中国/香港/台湾/澳门及组合写法
    ("大陆", "huayu"),
    ("中国大陆", "huayu"),
    ("中国", "huayu"),
    ("内地", "huayu"),
    ("香港", "huayu"),
    ("中国香港", "huayu"),
    ("台湾", "huayu"),
    ("澳门", "huayu"),
    ("华语", "huayu"),
    # 欧美国家表
    ("美国", "oumei"),
    ("英国", "oumei"),
    ("法国", "oumei"),
    ("德国", "oumei"),
    ("加拿大", "oumei"),
    ("澳大利亚", "oumei"),
    ("西班牙", "oumei"),
    ("俄罗斯", "oumei"),
    ("欧美", "oumei"),
    # 日韩
    ("日本", "rihan"),
    ("韩国", "rihan"),
    ("日韩", "rihan"),
    # 已知但非三桶地区 → other
    ("印度", "other"),
    ("泰国", "other"),
    ("印尼", "other"),
    ("印度尼西亚", "other"),
    ("土耳其", "other"),
    ("其它", "other"),
    ("其他", "other"),
    # 合拍横跨两桶 → other（不入任何地区库，留总库）
    ("中国大陆,美国", "other"),
    ("美国,中国大陆", "other"),
    ("日本,韩国,美国", "other"),
    ("香港,台湾", "huayu"),          # 同桶多值仍是该桶
    ("日本，韩国", "rihan"),          # 全角分隔
    # 脏值容错：切分后查表
    ("美国},{", "oumei"),
    (" 美国 ", "oumei"),
    # 未识别/空 → unknown
    ("", "unknown"),
    ("   ", "unknown"),
    (None, "unknown"),
    ("乌兹别克斯坦", "unknown"),
    ("格鲁吉亚", "unknown"),
    ("},[{", "unknown"),
    (123, "unknown"),
])
def test_area_bucket(raw, expected):
    assert acl.area_bucket(raw) == expected


def test_area_bucket_separators():
    assert acl.area_bucket("中国大陆、美国") == "other"
    assert acl.area_bucket("日本/韩国") == "rihan"
    assert acl.area_bucket("法国;德国") == "oumei"


def test_parse_cms_area_bucket_lenient_read():
    assert acl.parse_cms_area_bucket(None) == ""
    assert acl.parse_cms_area_bucket("") == ""
    assert acl.parse_cms_area_bucket("huayu") == "huayu"
    assert acl.parse_cms_area_bucket(" OUMEI ") == "oumei"
    assert acl.parse_cms_area_bucket("bogus") == ""

    class _Lib:
        cms_area_bucket = "rihan"
    assert acl.parse_cms_area_bucket(_Lib()) == "rihan"

    class _LibEmpty:
        pass
    assert acl.parse_cms_area_bucket(_LibEmpty()) == ""


def test_format_cms_area_bucket_strict_write():
    assert acl.format_cms_area_bucket(None) is None
    assert acl.format_cms_area_bucket("") is None
    assert acl.format_cms_area_bucket("huayu") == "huayu"
    assert acl.format_cms_area_bucket(" rihan ") == "rihan"
    with pytest.raises(ValueError):
        acl.format_cms_area_bucket("bogus")


def test_area_bucket_labels_are_chinese():
    labels = acl.AREA_BUCKET_LABELS
    assert labels[""] == "不分地区"
    assert labels["huayu"] == "华语"
    assert labels["oumei"] == "欧美"
    assert labels["rihan"] == "日韩"
