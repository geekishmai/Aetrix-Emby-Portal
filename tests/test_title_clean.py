"""标题清洗（问题二）：冒号副标题与脏字符不进 ``item.name``。

用户报告：刮出来「黑鸟：第一季」——冒号不该在标题里；还要求顺带排查
其他不该出现的字符。

唯一实现是 ``tmdb.clean_title``（横切一套），四个写 name 的入口全走它：

- ``TmdbClient.apply``（TMDB 搜索命中，series/movie）
- ``TmdbClient.apply_episode``（单集标题）
- ``altmeta.apply``（豆瓣兜底标题）
- ``metasources.engine.apply_to_item``（多源归并标题）

口径：
- 年份后缀 → 冒号副标题切割（全角/半角）→ 控制符/零宽/反斜杠/``"<>|``清除；
- **保留** ``/ ? *`` 等合法标题字符（Face/Off、What If?、M*A*S*H）——
  标题只用于展示与匹配、不生成文件名，砍掉会把正确标题改错；
- 清完为空返回 ``""``，调用方不写空名。
"""
from types import SimpleNamespace

import pytest

from backend.emby_server.tmdb import TmdbClient, clean_title


def _fake_item(**kw):
    base = dict(
        tmdb_id=None, last_scraped_at=None, metadata_source=None,
        overview=None, community_rating=None, poster_path=None,
        primary_image_url=None, backdrop_path=None, backdrop_image_url=None,
        name="", aliases="", genres="", production_year=None, imdb_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestCleanTitle:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # ① 冒号副标题切割（问题二的直接诉求）
            ("黑鸟：第一季", "黑鸟"),
            ("Some Show: The Return", "Some Show"),
            ("标题·：副标题", "标题"),
            # ② 年份后缀与冒号叠加（既有口径 + 新口径）
            ("马拉多纳：美好的梦想 (2021)", "马拉多纳"),
            ("Some Show (2024)", "Some Show"),
            # ③ 控制符 / 零宽 / 反斜杠 / 引号尖括号管道符
            ("迷宫\t第七话\x07", "迷宫 第七话"),
            ("换行\n标题", "换行 标题"),
            # 注：下一行字面量里是**真的**零宽/双向控制字符（U+200B U+202E U+FEFF），
            # 肉眼不可见是故意的——测试要的就是「肉眼看不见也得被清掉」
            ("​‮隐形﻿垃圾", "隐形垃圾"),
            ("A\\B\\C", "A B C"),
            ('引号"标题<标记>|竖线', "引号 标题 标记 竖线"),
            # ④ 合法标题字符必须保留
            ("Face/Off", "Face/Off"),
            ("What If?", "What If?"),
            ("M*A*S*H", "M*A*S*H"),
            ("Tom & Jerry", "Tom & Jerry"),
            # ⑤ 边界
            ("正常标题", "正常标题"),
            ("：副标题开头", "副标题开头"),
            ("：", ""),
            ("", ""),
            (None, ""),
        ],
    )
    def test_matrix(self, raw, expected):
        assert clean_title(raw) == expected


class TestApplyWritesCleanTitle:
    def test_series_colon_title_cleaned(self):
        """TMDB 中文译名带「：第一季」→ 只留主标题（用户报告的案例）"""
        item = _fake_item(name="黑鸟")
        hit = {"id": 900, "name": "黑鸟：第一季", "first_air_date": "2023-01-05"}
        TmdbClient().apply(item, hit, "series")
        assert item.name == "黑鸟"
        # 原始标题仍进 aliases：重刮/别名匹配还查得到全名
        assert "黑鸟：第一季" in (item.aliases or "")

    def test_movie_dirty_title_cleaned(self):
        item = _fake_item()
        hit = {"id": 1, "title": '迷宫\t"答案"：终局\x07', "release_date": "2019-01-01"}
        TmdbClient().apply(item, hit, "movie")
        assert item.name == "迷宫 答案"

    def test_clean_empty_keeps_current_name(self):
        """清完为空（全是脏字符）→ 不写空名"""
        item = _fake_item(name="原名")
        hit = {"id": 1, "title": "：", "release_date": None}
        TmdbClient().apply(item, hit, "movie")
        assert item.name == "原名"


class TestApplyEpisodeCleanTitle:
    def test_episode_colon_title_cleaned(self):
        ep = SimpleNamespace(name="第3集", overview="")
        result = TmdbClient.apply_episode(ep, {"name": "迷宫：终局", "overview": ""})
        assert ep.name == "迷宫"
        assert result["updated"] is True

    def test_episode_all_junk_not_written(self):
        ep = SimpleNamespace(name="第3集", overview="")
        result = TmdbClient.apply_episode(ep, {"name": "：", "overview": ""})
        assert ep.name == "第3集"        # 清完为空 → 保住现有名
        assert result["updated"] is False

    def test_episode_existing_real_name_not_overwritten(self):
        """已有真实标题（非「第X集」）→ 依旧不覆盖（既有规则不动）"""
        ep = SimpleNamespace(name="真实标题", overview="有")
        result = TmdbClient.apply_episode(ep, {"name": "别的：副标题", "overview": ""})
        assert ep.name == "真实标题"
        assert result["updated"] is False


class TestAltmetaCleanTitle:
    def test_douban_colon_title_cleaned(self):
        from backend.emby_server import altmeta

        item = _fake_item()   # 三无条目才写名（豆瓣兜底的既有规则）
        altmeta.apply(item, {"title": "某剧：特别篇 (2022)", "year": 2022}, "douban")
        assert item.name == "某剧"
        assert item.production_year == 2022


class TestMultiSourceCleanTitle:
    def test_multisource_colon_title_cleaned(self):
        from backend.emby_server.metasources import engine as ms_engine

        item = _fake_item()   # name 为空 → fill_missing_only 也写
        result = ms_engine.CollectResult(fields={"title": "黑鸟：第一季"})
        changed = ms_engine.apply_to_item(item, result, fill_missing_only=True)
        assert item.name == "黑鸟"
        assert "name" in changed

    def test_multisource_all_junk_not_written(self):
        from backend.emby_server.metasources import engine as ms_engine

        item = _fake_item()
        result = ms_engine.CollectResult(fields={"title": "："})
        changed = ms_engine.apply_to_item(item, result, fill_missing_only=True)
        assert item.name == ""
        assert "name" not in changed


@pytest.mark.parametrize("raw,expected", [
    ("黑鸟：第一季", "黑鸟"),
    ("Some Show: Season 2", "Some Show"),
    ("复仇者联盟：终局之战", "复仇者联盟：终局之战"),
    ("哆啦A梦：伴我同行", "哆啦A梦：伴我同行"),
    ("马拉多纳：美好的梦想 (2021)", "马拉多纳：美好的梦想"),
    ("Mission: Impossible", "Mission: Impossible"),
    ("：副标题开头", "副标题开头"),
    ("正常标题", "正常标题"),
])
def test_keep_subtitle_mode(monkeypatch, raw, expected):
    """TITLE_KEEP_SUBTITLE=1：只切季后缀，保留真正的副标题"""
    monkeypatch.setenv("TITLE_KEEP_SUBTITLE", "1")
    assert clean_title(raw) == expected
