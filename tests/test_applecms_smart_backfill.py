# -*- coding: utf-8 -*-
"""苹果 CMS 智能补源回归（站主 2026-11-02 亲令「补源要有头脑」）。

口径：
- 多信号打分三档：同名不同年份不许错挂（年份硬否决）；译名靠
  TMDB 原名钥匙命中；集数差过大拒绝；季号/国语粤语变体对不上
  拒绝；仅标题命中（旧脚本的老问题）不够中置信线，不补。
- 质量门：短剧分类跳过、枪版/低清标记跳过。
- 集成：只有最大（停用）映射的存量行，智能补源只给高/中置信
  候选补映射，剧行零新增，审计留痕可序列化。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_backfill as bf
from backend.emby_server import applecms_series as acser
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em


def _profile(**overrides):
    base = dict(item_id=1, library_id=59, name="对局", year=2019,
                titles=[], episode_count=3, expected_area_bucket="cn",
                genres=[], actors=[], directors=[], overview="",
                lang_variant=None)
    base.update(overrides)
    return bf.RowProfile(**base)


def _cand(**overrides):
    base = dict(vod_id="1", name="对局", year=2019, area="中国大陆",
                type_name="国产剧", genres=[], actors=[], directors=[],
                overview="", remarks="", episode_count=3,
                lang_variant=None)
    base.update(overrides)
    return bf.CandidateProfile(**base)


# ---------------------------------------------------------------------------
# 打分与否决（纯函数）
# ---------------------------------------------------------------------------


def test_same_title_different_year_rejected():
    score = bf.score_candidate(
        _profile(name="陷阱", year=2019, episode_count=6),
        _cand(name="陷阱", year=2024, episode_count=6))
    assert "year_mismatch" in score.vetoes
    assert score.tier == "reject"


def test_translated_name_matches_via_original_key():
    profile = _profile(name="夜曲", year=None, episode_count=3,
                       titles=[("Nocturne", "tmdb_original")])
    keys = bf.build_search_keys(profile)
    assert ("Nocturne", "tmdb_original") in keys
    score = bf.score_candidate(
        profile, _cand(name="Nocturne", year=None, area="美国"))
    assert score.parts["title"] == 40
    assert score.title_via == "tmdb_original"


def test_episode_count_far_rejected():
    score = bf.score_candidate(
        _profile(episode_count=40), _cand(episode_count=8))
    assert "episode_count_mismatch" in score.vetoes
    assert score.tier == "reject"


def test_season_mismatch_rejected():
    score = bf.score_candidate(
        _profile(name="摧花谜墓第三季", year=None, episode_count=8),
        _cand(name="摧花谜墓第二季", year=None, episode_count=8))
    assert "season_mismatch" in score.vetoes
    assert score.tier == "reject"


def test_lang_variant_mismatch_rejected():
    score = bf.score_candidate(
        _profile(name="爱·回家之三代同糖国语", year=None,
                 episode_count=13, lang_variant="guo"),
        _cand(name="爱·回家之三代同糖粤语", year=None, episode_count=13))
    assert "lang_variant_mismatch" in score.vetoes
    assert score.tier == "reject"


def test_quality_gate_tokens():
    assert bf.quality_gate_reject("短剧") == "short_drama"
    assert bf.quality_gate_reject("微短剧大全") == "short_drama"
    assert bf.quality_gate_reject("电影解说") == "non_content"
    assert bf.quality_gate_reject("国产剧", "某剧 CAM版") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "某剧", "枪版TS") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "某剧", "标清版") == "low_quality"
    assert bf.quality_gate_reject("国产剧", "某剧", "1080P高清") is None
    # 片名里正常的「枪」字不误杀（枪版才是标记）
    assert bf.quality_gate_reject("国产剧", "枪神传说") is None
    # 与扩抓 lane 同口径的扩围信号（2026-11-03 worker 质量门单）
    assert bf.quality_gate_reject("国产剧", "某片影院版") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "某片抢先") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "某片", "TELESYNC") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "某片", "HDTS") == "bad_version"
    assert bf.quality_gate_reject("国产剧", "CAMELOT行动") is None
    # 同步侧专用 helper 只查枪版/低清两层：备注带短剧词也不
    # 影响枪版判定（短剧层在同步侧走白名单，不由它拦）
    assert bf.version_quality_block("某片枪版", "短剧") == "bad_version"
    assert bf.version_quality_block("某片", "短剧") is None


def test_quality_gate_short_drama_ancestor_and_class():
    # PA-079：叶名中性、根为短剧类 → 祖先 id 命中即拒。
    payload = {"class": [
        {"type_id": 30, "type_pid": 0, "type_name": "短剧大全"},
        {"type_id": 31, "type_pid": 30, "type_name": "现代言情"},
        {"type_id": 40, "type_pid": 0, "type_name": "国产剧"}]}
    short_ids = bf.class_short_drama_ids(payload)
    assert short_ids == {30, 31}
    # 叶名本身不带标记、只靠祖先 id 命中
    assert bf.quality_gate_reject(
        "现代言情", "某长剧名", "", short_ids=short_ids,
        type_id=31) == "short_drama"
    # vod_class / remarks 带标记即拒
    assert bf.quality_gate_reject(
        "现代言情", "某长剧名", "", vod_class="短剧") == "short_drama"
    assert bf.quality_gate_reject(
        "现代言情", "某长剧名", "短剧") == "short_drama"
    # 普通国产剧不受影响
    assert bf.quality_gate_reject(
        "国产剧", "某剧", "", short_ids=short_ids,
        type_id=40) is None
    # 缺 class 树 → 空集，只认自身名降级，不反向放行
    assert bf.class_short_drama_ids({}) == set()
    assert bf.class_short_drama_ids({"class": [
        {"type_id": 31, "type_name": "现代言情"}]}) == set()


def test_is_short_drama_entry():
    short_ids = {30, 31}
    assert bf._is_short_drama_entry(
        {"type_id": 31, "type_name": "现代言情"}, short_ids)
    assert bf._is_short_drama_entry(
        {"type_name": "现代言情", "vod_class": "短剧"}, short_ids)
    assert not bf._is_short_drama_entry(
        {"type_id": 40, "type_name": "国产剧"}, short_ids)


def test_tier_high_medium_low():
    high = bf.score_candidate(_profile(), _cand())
    assert high.tier == "high" and high.total >= bf.TIER_HIGH_MIN

    medium = bf.score_candidate(
        _profile(name="薯童谣", year=None, episode_count=55,
                 expected_area_bucket="jpkr"),
        _cand(name="薯童谣", year=None, area="韩国", episode_count=50))
    assert medium.tier == "medium"

    # 仅标题命中：总分不够中置信线 → 拒绝（旧脚本的老问题）
    low = bf.score_candidate(
        _profile(year=None, episode_count=0, expected_area_bucket=None),
        _cand(year=None, area="", episode_count=0))
    assert low.tier == "reject"


def test_search_keys_cover_variants():
    profile = _profile(
        name="摧花谜墓第三季",
        titles=[("Destroy Flower Tomb", "tmdb_original")])
    keys = bf.build_search_keys(profile)
    kinds = [kind for _text, kind in keys]
    assert "name" in kinds
    assert "tmdb_original" in kinds
    assert "season_stripped" in kinds
    stripped = [text for text, kind in keys if kind == "season_stripped"]
    assert stripped == ["摧花谜墓"]


# ---------------------------------------------------------------------------
# 集成：干跑/补写全链（桩源）
# ---------------------------------------------------------------------------


def _detail(vod_id, name, episodes, *, year="2019", area="中国大陆",
            type_name="国产剧"):
    play_url = "#".join(
        f"第{i}集$https://cdn.example.com/{vod_id}/e{i}.m3u8"
        for i in range(1, episodes + 1))
    return {"vod_id": vod_id, "vod_name": name, "vod_year": year,
            "vod_area": area, "type_name": type_name,
            "vod_play_from": "直链线路", "vod_play_url": play_url}


S7_SEARCH = {
    "对局": [{"vod_id": 501, "vod_name": "对局", "vod_year": "2019",
              "type_name": "国产剧"}],
    "陷阱": [{"vod_id": 502, "vod_name": "陷阱", "vod_year": "2024",
              "type_name": "国产剧"}],
}
S7_DETAILS = {
    "501": _detail(501, "对局", 3),
    "502": _detail(502, "陷阱", 6, year="2024"),
}
S8_DETAILS = {
    "901": _detail(901, "对局", 3),
    "902": _detail(902, "陷阱", 6),
    "903": _detail(903, "现代言情剧", 60),
}


class FakeHttpGet:
    """按 host 路由 search/detail 的 provide 桩。"""

    def __call__(self, url, headers=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "search":
            wd = (query.get("wd") or [""])[0]
            entries = S7_SEARCH.get(wd, []) if host == "s7.example" else []
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": json.loads(json.dumps(
                             entries, ensure_ascii=False))}
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            table = S7_DETAILS if host == "s7.example" else S8_DETAILS
            payload = table.get(vod_id)
            if payload is None:
                return 200, {"code": 1, "page": 1, "pagecount": 0,
                             "list": []}
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed_row_with_only8(db, lib, source8, vod_id, name, episodes,
                         *, year="2019"):
    detail = S8_DETAILS[vod_id]
    record = acs.cms_entry_to_record(detail, source_id="8")
    acser.ensure_series_tree(db, lib, source8, record, detail)
    db.commit()
    row = (db.query(em.MediaItem)
           .filter(em.MediaItem.library_id == lib.id,
                   em.MediaItem.item_type == "series",
                   em.MediaItem.name == name).one())
    return row


def test_run_backfill_fills_only_confident_match(db):
    for sid, host, enabled in ((7, "s7.example", True),
                               (8, "s8.example", False)):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=enabled,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-tv", name="在线国产剧", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map="{}")
    db.add(lib)
    db.commit()
    source8 = db.get(em.UpstreamSource, 8)
    row_a = _seed_row_with_only8(db, lib, source8, "901", "对局", 3)
    row_b = _seed_row_with_only8(db, lib, source8, "902", "陷阱", 6)
    assert bf.series_mapping_sources(db, row_a.id) == {8}
    assert bf.series_mapping_sources(db, row_b.id) == {8}

    http = FakeHttpGet()
    probe = lambda url, headers, timeout: (200, None)  # noqa: E731
    rows = bf.find_unplayable_series(db, [lib.id], [7])
    assert {r.id for r in rows} == {row_a.id, row_b.id}

    dry = bf.run_backfill(db, lib, rows, expected_area_bucket="cn",
                          apply=False, http_get=http, http_probe=probe)
    assert dry["stats"]["filled"] == 1
    assert bf.series_mapping_sources(db, row_a.id) == {8}  # 干跑不写

    done = bf.run_backfill(db, lib, rows, expected_area_bucket="cn",
                           apply=True, http_get=http, http_probe=probe)
    stats = done["stats"]
    assert stats["filled"] == 1 and stats["unresolved"] == 1
    assert stats["unresolved_reasons"] == {"year_mismatch": 1}
    assert bf.series_mapping_sources(db, row_a.id) == {7, 8}
    assert bf.series_mapping_sources(db, row_b.id) == {8}
    # 剧行零新增
    assert (db.query(em.MediaItem)
            .filter(em.MediaItem.item_type == "series").count()) == 2
    # 审计留痕可序列化，且记下命中钥匙与分项
    json.dumps(done["audit"], ensure_ascii=False)
    gained = [a for a in done["audit"] if a["gained"]]
    assert gained[0]["gained"][0]["source_id"] == 7
    assert gained[0]["gained"][0]["key_kind"] == "name"
    assert gained[0]["gained"][0]["tier"] == "high"


# ---------------------------------------------------------------------------
# PA-079：补源 list 层用 class 树拦藏在中性子类下的短剧
# ---------------------------------------------------------------------------


class FakeHttpGetWithClass:
    """search payload 带 class 树的 provide 桩：叶「现代言情」
    的根是「短剧大全」，list 层就必须拒掉，不许进 detail/打分。"""

    CLASS_TREE = [
        {"type_id": 30, "type_pid": 0, "type_name": "短剧大全"},
        {"type_id": 31, "type_pid": 30, "type_name": "现代言情"},
        {"type_id": 40, "type_pid": 0, "type_name": "国产剧"},
    ]

    def __call__(self, url, headers=None, timeout=None):
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        query = urllib.parse.parse_qs(parsed.query)
        ac = (query.get("ac") or [""])[0]
        if ac == "search":
            wd = (query.get("wd") or [""])[0]
            entries = []
            if host == "s7.example" and wd == "现代言情剧":
                entries = [{"vod_id": 601, "vod_name": "现代言情剧",
                            "vod_year": "2024", "type_id": 31,
                            "type_name": "现代言情"}]
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "class": [dict(node) for node in self.CLASS_TREE],
                         "list": json.loads(json.dumps(
                             entries, ensure_ascii=False))}
        if ac == "detail":
            vod_id = (query.get("ids") or [""])[0]
            if host == "s7.example" and vod_id == "601":
                detail = _detail(601, "现代言情剧", 60, year="2024")
                detail["type_id"] = 31
                detail["type_name"] = "现代言情"
                return 200, {"code": 1, "page": 1, "pagecount": 1,
                             "list": [json.loads(json.dumps(
                                 detail, ensure_ascii=False))]}
            table = S7_DETAILS if host == "s7.example" else S8_DETAILS
            payload = table.get(vod_id)
            if payload is None:
                return 200, {"code": 1, "page": 1, "pagecount": 0,
                             "list": []}
            return 200, {"code": 1, "page": 1, "pagecount": 1,
                         "list": [json.loads(json.dumps(
                             payload, ensure_ascii=False))]}
        raise AssertionError(f"未预期的请求: {url}")


def test_run_backfill_rejects_short_drama_by_ancestor(db):
    for sid, host, enabled in ((7, "s7.example", True),
                               (8, "s8.example", False)):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=enabled,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-tv", name="在线国产剧", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map="{}")
    db.add(lib)
    db.commit()
    source8 = db.get(em.UpstreamSource, 8)
    row = _seed_row_with_only8(db, lib, source8, "903", "现代言情剧", 60)
    assert bf.series_mapping_sources(db, row.id) == {8}

    http = FakeHttpGetWithClass()
    probe = lambda url, headers, timeout: (200, None)  # noqa: E731
    rows = bf.find_unplayable_series(db, [lib.id], [7])
    assert {r.id for r in rows} == {row.id}

    dry = bf.run_backfill(db, lib, rows, expected_area_bucket="cn",
                          apply=False, http_get=http, http_probe=probe)
    assert dry["stats"]["filled"] == 0
    assert dry["stats"]["quality_skipped"] >= 1
    assert bf.series_mapping_sources(db, row.id) == {8}  # 一条映射没加
    rejected = [c.get("rejected") for a in dry["audit"]
                for attempt in a["attempts"]
                for c in attempt["candidates"]]
    assert "quality:short_drama" in rejected


# ---------------------------------------------------------------------------
# 补前探活 403 回落 GET 复验（2026-11-02：ppqrrs 类 CDN 对 Range 回
# 403、普通 GET 可拉真清单，单一 Range 口径误杀《爆笑虫子在中国》
# 5 个高置信候选；仅 403 回落一次 GET，拉到清单才算活）
# ---------------------------------------------------------------------------


def _probe_detail():
    return _detail(777, "虫子", 3)


def _range(status):
    return lambda url, headers, timeout: (status, None)


def test_probe_range_ok_no_get_fallback():
    calls = []

    def get_text(url, headers, timeout):
        calls.append(url)
        return 200, "#EXTM3U\n#EXT-X-VERSION:3\n"

    assert bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(206),
        http_get_text=get_text)
    assert calls == []  # Range 已判活，不许再发 GET


def test_probe_range_403_get_playlist_saves_candidate():
    seen = {}

    def get_text(url, headers, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return 200, "#EXTM3U\n#EXTINF:9.0,\nseg.ts\n"

    assert bf.probe_first_episode(
        _probe_detail(), {"User-Agent": "UA"}, http_probe=_range(403),
        http_get_text=get_text)
    assert seen["url"].endswith("/777/e1.m3u8")
    assert seen["timeout"] == bf._PROBE_GET_FALLBACK_TIMEOUT_SEC


def test_probe_range_403_get_also_403_still_dead():
    get_text = lambda url, headers, timeout: (403, None)  # noqa: E731
    assert not bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(403),
        http_get_text=get_text)


def test_probe_range_403_get_error_still_dead():
    def get_text(url, headers, timeout):
        raise RuntimeError("boom")

    assert not bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(403),
        http_get_text=get_text)


def test_probe_range_403_get_not_playlist_still_dead():
    get_text = lambda url, headers, timeout: (  # noqa: E731
        200, "<html>denied</html>")
    assert not bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(403),
        http_get_text=get_text)


def test_probe_range_404_no_get_fallback():
    calls = []

    def get_text(url, headers, timeout):
        calls.append(url)
        return 200, "#EXTM3U\n"

    assert not bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(404),
        http_get_text=get_text)
    assert calls == []  # 非 403 不回落，维持判死


def test_probe_range_unreachable_no_get_fallback():
    calls = []

    def get_text(url, headers, timeout):
        calls.append(url)
        return 200, "#EXTM3U\n"

    assert not bf.probe_first_episode(
        _probe_detail(), {}, http_probe=_range(None),
        http_get_text=get_text)
    assert calls == []


def test_run_backfill_probe_403_get_fallback_fills(db):
    for sid, host, enabled in ((7, "s7.example", True),
                               (8, "s8.example", False)):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=enabled,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-tv", name="在线国产剧", collection_type="tvshows",
        is_enabled=True, kind="applecms", upstream_source_ids="7",
        cms_type_map="{}")
    db.add(lib)
    db.commit()
    source8 = db.get(em.UpstreamSource, 8)
    row_a = _seed_row_with_only8(db, lib, source8, "901", "对局", 3)
    rows = bf.find_unplayable_series(db, [lib.id], [7])
    assert {r.id for r in rows} == {row_a.id}

    http = FakeHttpGet()
    probe403 = lambda url, headers, timeout: (403, None)  # noqa: E731
    get_dead = lambda url, headers, timeout: (403, None)  # noqa: E731
    get_live = lambda url, headers, timeout: (  # noqa: E731
        200, "#EXTM3U\n#EXTINF:9.0,\nseg.ts\n")

    dead = bf.run_backfill(db, lib, rows, expected_area_bucket="cn",
                           apply=False, http_get=http,
                           http_probe=probe403, http_get_text=get_dead)
    assert dead["stats"]["filled"] == 0
    assert dead["stats"]["probe_failed"] == 1

    saved = bf.run_backfill(db, lib, rows, expected_area_bucket="cn",
                            apply=False, http_get=http,
                            http_probe=probe403, http_get_text=get_live)
    assert saved["stats"]["filled"] == 1
    assert saved["stats"]["probe_failed"] == 0
