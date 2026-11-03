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
    """无 path_tag 时 NFO id 照旧胜（plan 走 NFO details）。"""
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    it = _item("mount://1/x/a.mkv")
    plan = enrich_worker._tmdb_plan_for_item(it, {"tmdb_id": "999"}, "movie", True)
    assert plan["op"] == "details"
    assert plan["tmdb_id"] == "999"
    assert plan["why"] == "nfo"


def test_path_tag_wins_over_nfo_when_both_present(monkeypatch):
    """§14.16.1b（Claude 批准）：目录 {tmdb-N} 与 NFO tmdb_id 不一致时，
    path_tag 优先——NFO 是按旧库生成的，可能带旧错 id（194/208/408 实证：
    NFO imdb 换算出上集/前作）。"""
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    it = _item("mount://1/x/赤壁（下） (2009) {tmdb-15384}/赤壁.mkv")
    plan = enrich_worker._tmdb_plan_for_item(it, {"tmdb_id": "12289"}, "movie", True)
    assert plan["op"] == "details"
    assert plan["tmdb_id"] == "15384"
    assert plan["why"] == "path_tag"


def test_nfo_id_not_written_when_path_tag_wins():
    """result["tmdb_id"] 不被 NFO 旧 id 覆盖（path_tag 存在时）。"""
    it = _item("mount://1/x/赤壁（下） (2009) {tmdb-15384}/赤壁.mkv")
    result = {}
    nfo_data = {"tmdb_id": "12289"}
    # 与 _enrich_fetch_pre 的落段同口径：path_tag 存在 → 不落 NFO id
    _path_tag_wins = bool(enrich_worker._path_tmdb_tag(it))
    if nfo_data.get("tmdb_id") and not _path_tag_wins:
        result["tmdb_id"] = str(nfo_data["tmdb_id"])
    assert result.get("tmdb_id") is None, "NFO 旧错 id 不得覆盖 path_tag 场景"
