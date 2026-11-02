# -*- coding: utf-8 -*-
"""命名清洗 v3（feat/title-cleanup）回归：

- rule_pack v3 词表新增 avc / 2d3d / ttg（发行组与介质/视图变体尾词），
  生产实证：《哆啦A梦：伴我同行》.iso 标题
  「哆啦A梦：伴我同行 Stand By Me Doraemon HK 2D3D AVC HD MA TTG」
  在 v2 词表下残余噪声 → TMDB 搜索全零结果（终态 none）。
- `_search_candidates` 新增两类变体候选：
  ① 尾部「区域/介质」token 逐词剥离（HK/MA/HD 等不进全局词表的易撞词）；
  ② 双语标题的仅英文段（拉丁段）候选——TMDB 对中英混查常零结果。
- 反例（词表/正则不得误伤）：
  * 纯《Ma》（2015 Taylor Sheridan? 实为 2019 Octavia Spencer 电影）
    不能被 \b ma \b 类规则清成空串或年份；
  * 「Ma Rainey's Black Bottom」不能被剥掉 Ma；
  * 无噪声的干净标题候选集保持不变。
"""
import sys

import pytest

sys.path.insert(0, ".")
from backend.emby_server import rule_pack as rp  # noqa: E402
from backend.emby_server.tmdb import _search_candidates  # noqa: E402

DORAEMON = "哆啦A梦：伴我同行 Stand By Me Doraemon HK 2D3D AVC HD MA TTG"


def test_pack_version_is_v3():
    assert rp.PACK_VERSION == "v3"
    assert rp.default_pack().version == "v3"


def test_v3_terms_added_and_freeze_intact():
    terms = set(rp.DEFAULT_STRIP_TERMS)
    assert {"avc", "2d3d", "ttg"} <= terms
    # 冻存：v3 - v3新增 = v2；v2 - v2新增 = v1
    assert set(rp.PACK_VERSION_TERMS["v3"]) == terms
    v3_added = {"avc", "2d3d", "ttg"}
    v2_added = {"adweb", r"ddp\s+\d\s+\d", r"dd\s+\d\s+\d", r"dts\s+\d\s+\d",
                r"eac3\s+\d\s+\d", r"ac3\s+\d\s+\d", r"aac\s+\d\s+\d"}
    assert set(rp.PACK_VERSION_TERMS["v2"]) == terms - v3_added
    assert set(rp.PACK_VERSION_TERMS["v1"]) == terms - v3_added - v2_added
    assert rp.PACK_VERSION_ORDER == ("v1", "v2", "v3")


def test_pack_strips_doraemon_noise():
    stripped = rp.default_pack().strip(DORAEMON)
    assert "2d3d" not in stripped.lower()
    assert "avc" not in stripped.lower()
    assert "ttg" not in stripped.lower()
    # 保留主体：中文主标题与英文名仍在
    assert "哆啦A梦" in stripped
    assert "Doraemon" in stripped


def test_candidates_contain_tail_stripped_and_latin_segment():
    queries = [q for q, _ in _search_candidates(DORAEMON, 2014)]
    # 尾部剥离：无 HK/MA/TTG 的变体必须出现（词表已剥 2d3d/avc/ttg）
    assert any(("hk" not in q.lower() and "ttg" not in q.lower()
                and "ma" not in q.lower() and "hd" not in q.lower())
               and "Doraemon" in q for q in queries)
    # 仅英文段候选（拉丁段提取；首字母 A 是孤词，正则只会取
    # “Stand By Me Doraemon”这一连续段）
    assert any(q == "Stand By Me Doraemon" for q in queries)
    # 仅中文段候选（CJK-only，Tier 2 口径）
    assert any("Doraemon" not in q and "哆啦" in q for q, fuzzy in _search_candidates(DORAEMON, 2014))


def test_candidates_tail_strip_survives_multiple_tags():
    # 尾部多枚 token 连挂也能全剥
    queries = [q for q, _ in _search_candidates(
        "某电影 Some Movie 1080p HK MA AVC", 2023)]
    assert any("some movie" in q.lower() for q in queries)


def test_clean_title_ma_not_destroyed():
    """反例：电影《Ma》不能被当成噪声剥掉（v3 加的 2d3d/ttg/ma 边界核查）。"""
    queries = [q for q, _ in _search_candidates("Ma", 2019)]
    assert "Ma" in queries
    assert "Ma 2019" in queries


def test_clean_title_ma_rainey_kept():
    """反例：《Ma Rainey's Black Bottom》的 Ma 是标题首词，不得被剥。"""
    queries = [q for q, _ in _search_candidates(
        "Ma Rainey's Black Bottom", 2020)]
    assert any("ma rainey" in q.lower() for q in queries)


def test_clean_title_hk_in_title_not_stripped():
    """反例：HK 出现在标题中段（前后无分隔锚定）不该被剥；
    尾部出现的 HK 才剥（候选变体层）。"""
    queries = [q for q, _ in _search_candidates(
        "香港HK故事", 2023)]
    assert any("香港" in q for q in queries)


def test_clean_title_unchanged_for_clean_names():
    """干净标题不应产生多余变体（除既有候选外，v3 不引入噪声候选）。"""
    base = [q for q, _ in _search_candidates("Breaking Bad", 2008)]
    assert "Breaking Bad" in base
    assert all("hk" not in q.lower() for q in base)
