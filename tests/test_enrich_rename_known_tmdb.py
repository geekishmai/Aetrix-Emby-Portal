"""已知 TMDB id 的条目在覆盖模式下片名换成 TMDB 标题（目录名 {tmdb-xxx} 路径）"""
from types import SimpleNamespace

from backend.emby_server import enrich_worker, tmdb


def _item(name, item_type="movie"):
    return SimpleNamespace(name=name, sort_name="", item_type=item_type)


def test_rename_when_overwrite_mode(monkeypatch):
    monkeypatch.setattr(tmdb, "tmdb_text_fill_only", lambda db=None: False)
    it = _item("霸王花 The Inspector Wears Skirts H 265 10 bit Digital")
    enrich_worker._rename_from_known_tmdb(it, "movie", {"title": "霸王花"})
    assert it.name == "霸王花"
    assert it.sort_name


def test_series_uses_name(monkeypatch):
    monkeypatch.setattr(tmdb, "tmdb_text_fill_only", lambda db=None: False)
    it = _item("漫长的季节 The Long Season S01", "series")
    enrich_worker._rename_from_known_tmdb(it, "series", {"name": "漫长的季节"})
    assert it.name == "漫长的季节"


def test_fill_only_keeps_name(monkeypatch):
    monkeypatch.setattr(tmdb, "tmdb_text_fill_only", lambda db=None: True)
    it = _item("原名 H 265")
    enrich_worker._rename_from_known_tmdb(it, "movie", {"title": "新名"})
    assert it.name == "原名 H 265"


def test_episode_and_empty_title_untouched(monkeypatch):
    monkeypatch.setattr(tmdb, "tmdb_text_fill_only", lambda db=None: False)
    ep = _item("剧 S01E01", "episode")
    enrich_worker._rename_from_known_tmdb(ep, "episode", {"name": "集名"})
    assert ep.name == "剧 S01E01"
    mv = _item("原名")
    enrich_worker._rename_from_known_tmdb(mv, "movie", {"title": ""})
    assert mv.name == "原名"


def test_two_stage_fill_renames_only_matching_id(monkeypatch):
    """两段式补资料段：详情 id 与条目 tmdb_id 一致才改名"""
    from backend.emby_server import identify_two_stage as ts
    monkeypatch.setattr(tmdb, "tmdb_text_fill_only", lambda db=None: False)
    it = SimpleNamespace(name="风云2 The Storm Warriors H 265 10 bit HD", sort_name="",
                         item_type="movie", tmdb_id="32654")
    assert ts._rename_from_bound_details(it, {"id": 32654, "title": "风云Ⅱ"}) is True
    assert it.name == "风云Ⅱ"
    other = SimpleNamespace(name="原名 H 265", sort_name="", item_type="movie", tmdb_id="1")
    assert ts._rename_from_bound_details(other, {"id": 2, "title": "别的片"}) is False
    assert other.name == "原名 H 265"
