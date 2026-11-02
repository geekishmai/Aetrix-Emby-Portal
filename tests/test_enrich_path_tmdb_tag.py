"""路径 {tmdb-N} 标签：直接按 id 拉详情，不按带发布尾巴的文件名搜"""
from types import SimpleNamespace

from backend.emby_server import enrich_worker


class _Client:
    configured = True


def _item(path, name="霸王花 The Inspector Wears Skirts H 265 10 bit", tmdb_id=None):
    return SimpleNamespace(file_path=path, name=name, production_year=1988,
                           tmdb_id=tmdb_id, poster_path="/p.jpg", primary_image_url=None)


def test_path_tag_parsed():
    it = _item("mount://1/媒体库/华语电影/霸王花 (1988) {tmdb-56271}/霸王花.mkv")
    assert enrich_worker._path_tmdb_tag(it) == "56271"
    assert enrich_worker._path_tmdb_tag(_item("mount://1/x/{tmdbid=42}/a.mkv")) == "42"
    assert enrich_worker._path_tmdb_tag(_item("mount://1/x/a.mkv")) is None
    assert enrich_worker._path_tmdb_tag(_item(None)) is None


def test_plan_uses_details_for_path_tag(monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    it = _item("mount://1/媒体库/华语电影/霸王花 (1988) {tmdb-56271}/霸王花.mkv")
    plan = enrich_worker._tmdb_plan_for_item(it, None, "movie", False)
    assert plan["op"] == "details"
    assert plan["tmdb_id"] == "56271"
    assert plan["why"] == "path_tag"
    assert plan["name"] and plan["year"] == 1988


def test_plan_without_tag_still_searches(monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    monkeypatch.setattr(enrich_worker, "_alias_tmdb_id", lambda name: None)
    it = _item("mount://1/媒体库/华语电影/霸王花/霸王花.mkv")
    assert enrich_worker._tmdb_plan_for_item(it, None, "movie", False)["op"] == "search"


def test_nfo_id_still_wins(monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    it = _item("mount://1/x/{tmdb-1}/a.mkv")
    plan = enrich_worker._tmdb_plan_for_item(it, {"tmdb_id": "999"}, "movie", True)
    assert plan["tmdb_id"] == "999"
