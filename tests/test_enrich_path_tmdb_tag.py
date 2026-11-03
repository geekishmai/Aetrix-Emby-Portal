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


# ---------------- QA AW P2：死号回退与降级标记 ----------------

class _ClientWithSearch:
    configured = True


def test_path_tag_stale_falls_back_to_search(monkeypatch):
    """path_tag details 404 → Round 2 用 plan 的 name/year 回退 search。

    _apply_tmdb_plan_result 层验证：out 已带 search 的 hit（r2 命中）时，
    result["tmdb_id"] 应写成 search 命中的 id，而非死号。
    """
    result = {}
    item = _item("mount://1/x/赤壁（下） (2009) {tmdb-15384}/赤壁.mkv")
    plan = {"op": "details", "tmdb_id": "893590", "why": "path_tag",
            "kind": "movie", "name": "盖世英雄演唱会影音全记录", "year": 2006}
    out = {"hit": {"id": 183670, "title": "王力宏 盖世英雄 Live Concert 演唱会",
                   "release_date": "2006-08-21"},
           "details": None, "transient": False}
    enrich_worker._apply_tmdb_plan_result(result, item, plan, out, "movie")
    assert result["tmdb_id"] == "183670"
    assert result["tmdb_hit"]["id"] == 183670
    # 死号进降级标记
    assert "893590" in enrich_worker._PATH_TAG_DEAD
    enrich_worker._PATH_TAG_DEAD.discard("893590")


def test_path_tag_dead_id_goes_search(monkeypatch):
    """死号降级：_PATH_TAG_DEAD 命中的 id 不再发 details，直接 search 计划。"""
    enrich_worker._PATH_TAG_DEAD.add("893590")
    try:
        from backend.emby_server import tmdb
        monkeypatch.setattr(tmdb, "tmdb_client", _Client())
        it = _item("mount://1/x/盖世英雄演唱会影音全记录 (2006) {tmdb-893590}/a.mkv",
                   name="盖世英雄演唱会影音全记录 H 265")
        plan = enrich_worker._tmdb_plan_for_item(it, None, "movie", False)
        assert plan["op"] == "search"
        assert plan["kind"] == "movie"
        assert plan["name"].startswith("盖世英雄演唱会")
    finally:
        enrich_worker._PATH_TAG_DEAD.discard("893590")


def test_path_tag_alive_stays_details(monkeypatch):
    """活号不受降级影响：不在 _PATH_TAG_DEAD 时照旧 details。"""
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _Client())
    it = _item("mount://1/x/霸王花 (1988) {tmdb-56271}/霸王花.mkv")
    plan = enrich_worker._tmdb_plan_for_item(it, None, "movie", False)
    assert plan["op"] == "details"
    assert plan["why"] == "path_tag"


def test_path_tag_round2_included_when_details_none(monkeypatch):
    """_run_tmdb_batch 的 Round 2 清单：path_tag plan（details=None）也要进
    stale 回退（此前只认 alias——AW P2-1 的根）。"""
    plans = [("k1", {"op": "details", "tmdb_id": "893590", "why": "path_tag",
                     "kind": "movie", "name": "盖世英雄演唱会影音全记录",
                     "year": 2006})]

    class _BatchClient:
        configured = True

        def batch_details(self, ids, return_transient=False):
            # 死号 → None（非 transient）
            return ([None], set()) if return_transient else ([None],)

        def batch_search(self, queries, return_transient=False):
            hits = [{"id": 183670, "title": "王力宏 盖世英雄 Live Concert 演唱会"}]
            return (hits, set()) if return_transient else (hits,)

    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", _BatchClient())
    out = enrich_worker._run_tmdb_batch(plans)
    assert out["k1"]["hit"]["id"] == 183670, "死号应回退 search 并命中"
    enrich_worker._apply_tmdb_plan_result({}, _item("mount://1/x/a.mkv"),
                                          plans[0][1], out["k1"], "movie")
    enrich_worker._PATH_TAG_DEAD.discard("893590")
