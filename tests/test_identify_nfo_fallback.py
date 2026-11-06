"""库 4 外语电影 identify 搜索未命中兜底读 .nfo、PT 标题尾巴清洗与幂等重排回归测试。"""
from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import enrich_worker as ew
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import nfo as nfo_lib
from backend.emby_server import nfo_cache
from backend.emby_server import p115_sidecar as sidecar_mod
from backend.emby_server import scanner as sc
from backend.emby_server import tmdb as tmdb_lib
from backend.emby_server import tmdb_cache
from scripts import requeue_none_items as rq


EIGHT_HALF_NFO = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>八部半</title>
  <originaltitle>8½</originaltitle>
  <year>1963</year>
  <plot>费里尼经典代表作。</plot>
  <tmdbid>422</tmdbid>
  <imdbid>tt0056801</imdbid>
</movie>
"""

IMDB_ONLY_KODI_NFO = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>八部半</title>
  <year>1963</year>
  <id>tt0056801</id>
</movie>
"""


class _Fake115SidecarProvider:
    mount_type = "115_sidecar"
    provider = "p115"
    local_root = None

    def __init__(self, entries_by_dir: dict[str, list[mount_lib.MountEntry]], texts: dict[str, str]):
        self.mount = SimpleNamespace(id=1, mount_type="115_sidecar", path="/")
        self.config = {"cookie": "UID=1_A1_1; CID=a; SEID=b"}
        self._entries_by_dir = entries_by_dir
        self._texts = texts
        self.list_calls: list[str] = []
        self.read_calls: list[str] = []

    def capabilities(self):
        return SimpleNamespace(can_read_sidecar=True, can_random_read=True)

    def list_dir(self, rel: str = "/"):
        norm = "/" + (rel or "").lstrip("/")
        norm = norm.rstrip("/") or "/"
        self.list_calls.append(norm)
        return list(self._entries_by_dir.get(norm, []))

    def read_sidecar_text(self, rel: str) -> str:
        norm = "/" + (rel or "").lstrip("/")
        self.read_calls.append(norm)
        if norm not in self._texts:
            raise mount_lib.MountError(f"not found: {norm}")
        return self._texts[norm]


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    nfo_cache.reset_for_tests()
    ew._PATH_TAG_DEAD.clear()
    monkeypatch.setenv("EMBY_NFO_CACHE_DIR", str(tmp_path / "nfo_cache"))
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(tmp_path / "tmdb_cache"))
    yield
    nfo_cache.reset_for_tests()
    ew._PATH_TAG_DEAD.clear()


def _new_ctx(lib_id: int = 4):
    snap = sc.LibrarySnapshot(
        library_id=lib_id, name="外语电影", collection_type="movies",
        paths=(), scrape_policy="smart",
    )
    return sc._ScanContext(snap=snap, lib_id=lib_id, stats={})


def _make_item(**kw):
    defaults = dict(
        id=6753,
        library_id=4,
        item_type="movie",
        name="Eight and a Half 10bit MiniHD",
        scan_title="Eight and a Half 10bit MiniHD",
        file_path="mount://1/媒体库/外语电影/八部半[中英字幕].Eight.and.a.Half.1963.BluRay.1080p.x265.10bit-MiniHD/Eight.and.a.Half.1963.BluRay.1080p.x265.10bit-MiniHD.mkv",
        size=8_919_883_207,
        container="mkv",
        production_year=1963,
        poster_path=None,
        primary_image_url=None,
        backdrop_path=None,
        backdrop_image_url=None,
        imdb_id=None,
        aliases=None,
        tmdb_id=None,
        external_ids=None,
        upstream_source_id=None,
        repair_requested_at=None,
        overview=None,
        genres=None,
        community_rating=None,
        series_id=None,
        parent_id=None,
        season_number=None,
        episode_number=None,
        metadata_locked=False,
        metadata_source=None,
        enrich_status="enriching",
        enrich_attempts=0,
        enrich_claim_token="tok-1",
        drive_file_id=None,
        content_hash=None,
        content_hash_algo=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_four_ops_raw_samples_and_beitai_title_cleanup():
    """覆盖运维给出的全部四个库 4 终态 none 原文样例 + S5 BeiTai / DDP5.1 / 双语 @beAst，
    并验证反例（Beauty and the Beast / Beast / Ma / 梅根2.0）绝不误伤。
    """
    ops_samples = [
        # (存量已落库 name, 原始文件路径, 年份, 期望干净英文查询, TMDB 官方标题)
        (
            "Eight and a Half 10bit MiniHD",
            "/外语电影/Eight.and.a.Half.1963.BluRay.1080p.x265.10bit-MiniHD.mkv",
            1963,
            "Eight and a Half",
            "Eight and a Half",
        ),
        (
            "Pride and Prejudice and Zombies 10bit 2Audio MiniHD",
            "/外语电影/Pride.and.Prejudice.and.Zombies.2016.BluRay.1080p.x265.10bit.2Audio-MiniHD.mkv",
            2016,
            "Pride and Prejudice and Zombies",
            "Pride and Prejudice and Zombies",
        ),
        (
            "Sherlock Holmes A Game of Shadows HK HD MA beAst",
            "/外语电影/Sherlock.Holmes.A.Game.of.Shadows.2011.HK.BluRay.Repack.1080p.DTS-HD.MA.5.1.x264-beAst.mkv",
            2011,
            "Sherlock Holmes A Game of Shadows",
            "Sherlock Holmes: A Game of Shadows",
        ),
        (
            "Anywhere Somewhere Nowhere GL CHDWEB",
            "/外语电影/Anywhere.Somewhere.Nowhere.2014.1080p.GL.WEB-DL.H264.AAC-CHDWEB.mkv",
            2014,
            "Anywhere Somewhere Nowhere",
            "Anywhere Somewhere Nowhere",
        ),
        # 扩展实证：S5 备胎 BeiTai + DDP5.1-CHDWEB + 双语 uploader@beAst
        (
            "Joker 10bit BeiTai",
            "/外语电影/Joker.2019.BluRay.1080p.x265.10bit-BeiTai.mkv",
            2019,
            "Joker",
            "Joker",
        ),
        (
            "An Unremarkable Christmas DDP5.1 CHDWEB",
            "/外语电影/An.Unremarkable.Christmas.2020.1080p.NF.WEB-DL.H264.DDP5.1-CHDWEB.mkv",
            2020,
            "An Unremarkable Christmas",
            "An Unremarkable Christmas",
        ),
        (
            "谍影重重1 The Bourne Identity 2Audio HD MA beAst",
            "/外语电影/谍影重重1.The.Bourne.Identity.2002.Blu-ray.1080p.2Audio.DTS-HD.MA.5.1.x264-beAst.mkv",
            2002,
            "The Bourne Identity",
            "The Bourne Identity",
        ),
    ]

    for stored_name, raw_path, year, expected_cand, tmdb_title in ops_samples:
        # 1) 存量已入库 name 经 _search_candidates 必须产出干净查询并通过 _hit_score Tier 2
        cands = [q for q, _ in tmdb_lib._search_candidates(stored_name, year)]
        assert expected_cand in cands, f"{stored_name!r} 未产出 {expected_cand!r}: {cands}"
        fake_hit = {
            "id": 1,
            "title": tmdb_title,
            "original_title": tmdb_title,
            "release_date": f"{year}-06-01",
        }
        scores = [
            tmdb_lib._hit_score(stored_name, q, fake_hit, fuzzy_ok=f, year=year)
            for q, f in tmdb_lib._search_candidates(stored_name, year)
        ]
        assert any(s is not None and s[0] == 2 for s in scores), (
            f"{stored_name!r} 未通过 Tier 2 精确匹配: cands={cands} scores={scores}"
        )

        # 2) 新入库文件经 parse_media_filename 解析后同样剥净尾巴
        parsed = sc.parse_media_filename(raw_path, "movies")
        assert parsed["year"] == year
        parsed_cands = [q for q, _ in tmdb_lib._search_candidates(parsed["name"], year)]
        assert expected_cand in parsed_cands, (
            f"新扫描 {raw_path!r} -> {parsed['name']!r} 未产出 {expected_cand!r}: {parsed_cands}"
        )

    # 反例守卫：真片名含 Beast / Ma / 版本号 2.0 绝不能被误剥
    parsed_batb = sc.parse_media_filename(
        "/外语电影/Beauty.and.the.Beast.2017.BluRay.1080p.DTS-HD.MA.7.1.x264-beAst.mkv",
        "movies",
    )
    assert parsed_batb["name"] == "Beauty and the Beast"
    assert tmdb_lib._clean_query("Beast") == "Beast"
    assert "Ma" in [q for q, _ in tmdb_lib._search_candidates("Ma", 2019)]
    assert sc.clean_name("梅根2.0 (2025) 1080p.WEB-DL.DDP5.1-CHDWEB") == "梅根2.0 (2025)"


def test_identify_search_hit_zero_nfo_vs_miss_falls_back_to_nfo_with_pickcode_cache(monkeypatch):
    """两段式 identify + ENRICH_NFO_SMART_SKIP=1：
    - 搜索直接命中时 0 次读 .nfo；
    - 搜索未命中（如 Eight and a Half vs TMDB 原名 8½）时自动兜底读同目录 .nfo 取 tmdbid/imdbid，
      且走 pickcode 永久缓存（即使清空内存重跑也最多 1 次取链），fill 段完成后将 metadata_source 置为 tmdb。
    """
    monkeypatch.setenv("ENRICH_NFO_SMART_SKIP", "1")
    monkeypatch.setenv("EMBY_NFO_PICKCODE_CACHE", "1")
    monkeypatch.setattr(tmdb_lib.TmdbClient, "configured", property(lambda self: True))
    monkeypatch.setattr("backend.emby_server.tmdb.prewarm_images", lambda *a, **kw: 0)
    monkeypatch.setattr(ew, "_should_multisource_fill_missing", lambda *a, **kw: False)
    monkeypatch.setattr(ew, "_douban_fallback_on_tmdb_miss", lambda *a, **kw: False)
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client.images_with_language", lambda *a, **kw: None)

    # Case A: 搜索命中 -> 0 次读 .nfo
    dir_hit = "/媒体库/外语电影/傲慢与偏见与僵尸.Pride.and.Prejudice.and.Zombies.2016.1080p.x265.10bit.2Audio-MiniHD"
    prov_hit = _Fake115SidecarProvider(
        entries_by_dir={
            dir_hit: [
                mount_lib.MountEntry("p.mkv", f"{dir_hit}/p.mkv", False, 5000, file_id="pcmkv_p"),
                mount_lib.MountEntry("p.nfo", f"{dir_hit}/p.nfo", False, 500, file_id="pcnfo_p"),
            ]
        },
        texts={f"{dir_hit}/p.nfo": EIGHT_HALF_NFO},
    )
    sf_hit = SimpleNamespace(
        local_dir=None, mount_id=1, provider=prov_hit,
        dir_rel=dir_hit, name="p.mkv", stored_path=f"mount://1{dir_hit}/p.mkv",
    )
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf_hit)
    monkeypatch.setattr(ew, "_shared_ctx", lambda item, holder: _new_ctx())
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_search",
        lambda queries, return_transient=False: (
            [{"id": 58431, "title": "傲慢与偏见与僵尸", "original_title": "Pride and Prejudice and Zombies", "release_date": "2016-02-05"}],
            set(),
        ),
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_details",
        lambda pairs, return_transient=False: (
            [{"id": 58431, "title": "傲慢与偏见与僵尸", "release_date": "2016-02-05", "poster_path": "/ppz.jpg", "overview": "简介"}],
            set(),
        ),
    )

    item_hit = _make_item(
        id=6766,
        name="Pride and Prejudice and Zombies 10bit 2Audio MiniHD",
        production_year=2016,
        file_path=f"mount://1{dir_hit}/p.mkv",
    )
    fetched_hit = ew._enrich_fetch(item_hit, holder={})
    assert prov_hit.read_calls == []
    assert fetched_hit["tmdb_hit"]["id"] == 58431

    # Case B: 搜索零命中 -> 兜底读同目录 .nfo 救回 tmdb_id=422 / imdb_id=tt0056801
    dir_miss = "/媒体库/外语电影/八部半[中英字幕].Eight.and.a.Half.1963.BluRay.1080p.x265.10bit-MiniHD"
    fname = "Eight.and.a.Half.1963.BluRay.1080p.x265.10bit-MiniHD"
    prov_miss = _Fake115SidecarProvider(
        entries_by_dir={
            dir_miss: [
                mount_lib.MountEntry(f"{fname}.mkv", f"{dir_miss}/{fname}.mkv", False, 8_919_883_207, file_id="pcmkv_85"),
                mount_lib.MountEntry(f"{fname}.nfo", f"{dir_miss}/{fname}.nfo", False, len(EIGHT_HALF_NFO.encode("utf-8")), file_id="pcnfo_85"),
            ]
        },
        texts={f"{dir_miss}/{fname}.nfo": EIGHT_HALF_NFO},
    )
    sf_miss = SimpleNamespace(
        local_dir=None, mount_id=1, provider=prov_miss,
        dir_rel=dir_miss, name=f"{fname}.mkv", stored_path=f"mount://1{dir_miss}/{fname}.mkv",
    )
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf_miss)
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_search",
        lambda queries, return_transient=False: ([None for _ in queries], set()),
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.details",
        lambda tid, kind: {
            "id": int(tid),
            "title": "八部半",
            "original_title": "8½",
            "release_date": "1963-02-14",
            "poster_path": "/8half.jpg",
            "overview": "费里尼经典。",
            "vote_average": 8.1,
            "genres": [{"id": 18, "name": "剧情"}],
        },
    )

    item_miss = _make_item()
    fetched_miss = ew._enrich_fetch(item_miss, holder={})
    assert len(prov_miss.read_calls) == 1
    # pre 与 fallback 复用同一 _ScanContext，目录列举也只发生 1 次
    assert len(prov_miss.list_calls) == 1
    assert fetched_miss["tmdb_id"] == "422"
    assert fetched_miss["imdb_id"] == "tt0056801"
    assert fetched_miss["tmdb_details"]["id"] == 422
    binding = ts.extract_binding(item_miss, fetched_miss)
    assert binding is not None
    assert binding["tmdb_id"] == "422"
    assert binding["imdb_id"] == "tt0056801"

    # 清空 L1 模拟重启后再次触发兜底 -> 命中磁盘 pickcode 永久缓存，read_calls 仍为 1
    with nfo_cache._mem_lock:
        nfo_cache._mem_cache.clear()
    fetched_miss_2 = ew._enrich_fetch(_make_item(), holder={})
    assert fetched_miss_2["tmdb_id"] == "422"
    assert len(prov_miss.read_calls) == 1


def test_identify_nfo_fallback_imdb_only_kodi_id_tag_bridges_to_tmdb(monkeypatch):
    """NFO 仅含 Kodi <id>tt0056801</id>（无 <tmdbid>）时：
    1) parse_nfo 识别出 imdb_id；
    2) identify 兜底经 find_by_imdb 桥接升级出 tmdb_id=422 并拉齐 details；
    3) 若 TMDB /find 无对应词条，仍保底产出 imdb 绑定而非落 metadata_source='none'。
    """
    parsed = nfo_lib.parse_nfo(IMDB_ONLY_KODI_NFO)
    assert parsed is not None
    assert parsed["imdb_id"] == "tt0056801"
    assert not parsed["tmdb_id"]

    monkeypatch.setenv("ENRICH_NFO_SMART_SKIP", "1")
    monkeypatch.setattr(tmdb_lib.TmdbClient, "configured", property(lambda self: True))
    monkeypatch.setattr("backend.emby_server.tmdb.prewarm_images", lambda *a, **kw: 0)
    monkeypatch.setattr(ew, "_should_multisource_fill_missing", lambda *a, **kw: False)
    monkeypatch.setattr(ew, "_douban_fallback_on_tmdb_miss", lambda *a, **kw: False)
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client.images_with_language", lambda *a, **kw: None)

    dir_rel = "/媒体库/外语电影/Eight.and.a.Half.1963"
    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_rel: [
                mount_lib.MountEntry("m.mkv", f"{dir_rel}/m.mkv", False, 1000, file_id="pcm1"),
                mount_lib.MountEntry("m.nfo", f"{dir_rel}/m.nfo", False, len(IMDB_ONLY_KODI_NFO), file_id="pcn_imdb1"),
            ]
        },
        texts={f"{dir_rel}/m.nfo": IMDB_ONLY_KODI_NFO},
    )
    sf = SimpleNamespace(
        local_dir=None, mount_id=1, provider=prov,
        dir_rel=dir_rel, name="m.mkv", stored_path=f"mount://1{dir_rel}/m.mkv",
    )
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf)
    monkeypatch.setattr(ew, "_shared_ctx", lambda item, holder: _new_ctx())
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_search",
        lambda queries, return_transient=False: ([None for _ in queries], set()),
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.find_by_imdb",
        lambda iid: {"movie_results": [{"id": 422, "title": "8½"}]} if iid == "tt0056801" else {},
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.details",
        lambda tid, kind: {"id": int(tid), "title": "八部半", "release_date": "1963-02-14"},
    )

    fetched = ew._enrich_fetch(_make_item(file_path=f"mount://1{dir_rel}/m.mkv"), holder={})
    assert fetched["imdb_id"] == "tt0056801"
    assert fetched["tmdb_id"] == "422"
    assert fetched["tmdb_details"]["id"] == 422

    # 即使 TMDB /find 返回空，仍保底抽出 imdb 绑定
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client.find_by_imdb", lambda iid: {"movie_results": []})
    fetched_imdb_only = ew._enrich_fetch(_make_item(file_path=f"mount://1{dir_rel}/m.mkv"), holder={})
    binding = ts.extract_binding(_make_item(), fetched_imdb_only)
    assert binding is not None
    assert binding["provider"] == "imdb"
    assert binding["imdb_id"] == "tt0056801"


def test_p115_sidecar_fetch_range_uses_app_first_cookie_and_singleflight():
    """修复线上 p115_sidecar._fetch_range 读小文件 .nfo 返 HTTP 403 的根因：
    1) download_url 传 app_first=True（避开 webapi f=3 动态 Set-Cookie 403）；
    2) CDN 请求头携带会话 Cookie；
    3) 同 pickcode 并发 open_range 单飞合并为 1 次 download_url。
    """
    dl_calls: list[tuple[str, bool]] = []
    http_headers_seen: list[dict] = []
    dl_entered = threading.Event()
    dl_release = threading.Event()

    class _FakePan115Client:
        def list_dir(self, cid: str = "0", deadline=None):
            return [{"name": "a.nfo", "is_dir": False, "size": len(EIGHT_HALF_NFO.encode("utf-8")), "cid": "2", "pickcode": "pc_nfo_sidecar"}]

        def download_url(self, pickcode: str, *, app_first: bool = False, deadline=None, file_size=None):
            dl_calls.append((pickcode, app_first))
            dl_entered.set()
            dl_release.wait(timeout=3)
            return "https://cdn.115.example/a.nfo"

    class _FakeHttpClient:
        def get(self, url: str, headers: dict | None = None):
            http_headers_seen.append(dict(headers or {}))
            return SimpleNamespace(status_code=206, content=EIGHT_HALF_NFO.encode("utf-8"))

    handler = sidecar_mod.P115SidecarHandler(
        client_factory=lambda cookie, ua: _FakePan115Client(),
        http_client_factory=lambda: _FakeHttpClient(),
    )
    handler.session_upsert("s1", {"cookie": "UID=42_A1_1; CID=sec; SEID=tok", "ua": "TestUA", "root_cid": "0"})
    sess = SimpleNamespace(session_id="s1")

    outs: list[dict] = []

    def _call():
        outs.append(handler.open_range(sess, "/a.nfo", 0, None))

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    assert dl_entered.wait(timeout=2)
    t2.start()
    dl_release.set()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert len(outs) == 2
    assert dl_calls == [("pc_nfo_sidecar", True)]
    assert all(h.get("Cookie") == "UID=42_A1_1; CID=sec; SEID=tok" for h in http_headers_seen)


def test_requeue_none_items_dry_run_library_filter_and_idempotency(monkeypatch, tmp_path):
    """验证 scripts/requeue_none_items.py：
    - 默认 dry-run 只统计不改库；
    - 严格按 library_id 过滤，且排除已绑 tmdb_id、metadata_locked、merged 副本与软删行；
    - --apply 将目标行翻为 pending + metadata_source=None + priority=50 并清理 TMDB 搜索阴性缓存；
    - 连续第二次 --apply 命中 0 行，严格幂等；两段式 fill 完成后 metadata_source 自动转正为 tmdb。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'requeue.db'}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(rq, "SessionLocal", TestSession)

    # 预埋两条阴性搜索磁盘缓存，验证 --apply 会清掉它们
    tmdb_cache.save_search("movie", tmdb_lib._norm_text("Eight and a Half"), 1963, [], lang="zh-CN")
    monkeypatch.setattr(tmdb_lib, "preferred_language", lambda: "zh-CN")

    db = TestSession()
    try:
        db.add_all([
            em.Library(id=3, guid="lib-3", name="华语电影", collection_type="movies", paths="/c"),
            em.Library(id=4, guid="lib-4", name="外语电影", collection_type="movies", paths="/f"),
        ])
        db.add_all([
            # 应被重排的 2 条库 4 done+none
            em.MediaItem(
                id=1, library_id=4, guid="g1", item_type="movie",
                name="Eight and a Half 10bit MiniHD", scan_title="Eight and a Half 10bit MiniHD",
                production_year=1963, enrich_status="done", metadata_source="none",
            ),
            em.MediaItem(
                id=2, library_id=4, guid="g2", item_type="movie",
                name="Anywhere Somewhere Nowhere GL CHDWEB", production_year=2014,
                enrich_status="done", metadata_source="none",
            ),
            # 库 4 已绑 tmdb_id -> 不动
            em.MediaItem(
                id=3, library_id=4, guid="g3", item_type="movie",
                name="已命中片", tmdb_id="100", enrich_status="done", metadata_source="tmdb",
            ),
            # 库 4 人工锁定 -> 不动
            em.MediaItem(
                id=4, library_id=4, guid="g4", item_type="movie",
                name="人工锁定片", enrich_status="done", metadata_source="none", metadata_locked=True,
            ),
            # 库 4 合并副本 -> 不动
            em.MediaItem(
                id=5, library_id=4, guid="g5", item_type="movie",
                name="合并副本", enrich_status="done", metadata_source="none", merged_into_id=3,
            ),
            # 库 4 软删 -> 不动
            em.MediaItem(
                id=6, library_id=4, guid="g6", item_type="movie",
                name="已删片", enrich_status="done", metadata_source="none", deleted_at=datetime.now(),
            ),
            # 库 3 done+none -> 指定 --library-id 4 时不动
            em.MediaItem(
                id=7, library_id=3, guid="g7", item_type="movie",
                name="库3未命中", enrich_status="done", metadata_source="none",
            ),
        ])
        db.commit()
    finally:
        db.close()

    # 1) 默认 dry-run：matched=2, requeued=0，数据库零改动
    db = TestSession()
    try:
        dry_rep = rq.requeue_none_items(db, library_ids=[4], apply=False)
        assert dry_rep["mode"] == "dry-run"
        assert dry_rep["matched"] == 2
        assert dry_rep["requeued"] == 0
        row1 = db.get(em.MediaItem, 1)
        assert row1.enrich_status == "done"
        assert row1.metadata_source == "none"
    finally:
        db.close()

    # 2) CLI --apply：库 4 仅 id=1,2 翻回 pending，库 3 id=7 与锁定/合并/软删保持原状
    assert rq.main(["--library-id", "4", "--apply"]) == 0
    db = TestSession()
    try:
        r1 = db.get(em.MediaItem, 1)
        r2 = db.get(em.MediaItem, 2)
        r4 = db.get(em.MediaItem, 4)
        r5 = db.get(em.MediaItem, 5)
        r7 = db.get(em.MediaItem, 7)
        assert r1.enrich_status == "pending" and r1.metadata_source is None and r1.enrich_priority == 50
        assert r2.enrich_status == "pending" and r2.metadata_source is None
        assert r4.enrich_status == "done" and r4.metadata_source == "none"
        assert r5.enrich_status == "done"
        assert r7.enrich_status == "done" and r7.metadata_source == "none"
    finally:
        db.close()

    # 阴性缓存已被清除
    hit_clean, _ = tmdb_cache.load_search("movie", tmdb_lib._norm_text("Eight and a Half"), 1963, lang="zh-CN")
    assert not hit_clean

    # 3) 立即二次执行 --apply：matched=0, requeued=0（严格幂等）
    db = TestSession()
    try:
        again_rep = rq.requeue_none_items(db, library_ids=[4], apply=True)
        assert again_rep["matched"] == 0
        assert again_rep["requeued"] == 0

        # 模拟两段式 identify+fill 收尾：哪怕历史残留 metadata_source='none'，fill 完成后也转正为 'tmdb'
        r1 = db.get(em.MediaItem, 1)
        r1.tmdb_id = "422"
        r1.metadata_source = "none"
        monkeypatch.setattr(ew, "_maybe_queue_probe", lambda d, it: None)
        ew._enrich_apply_two_stage(
            db, r1,
            {"tmdb_details": {"id": 422, "overview": "费里尼经典。", "poster_path": "/8half.jpg"}},
            identify=False, fill=True,
        )
        db.commit()
        assert r1.enrich_status == "done"
        assert r1.metadata_source == "tmdb"

        # fill 完成后再次跑重排脚本，依然为 0（不会把已修好的片再次翻回 pending）
        after_fill_rep = rq.requeue_none_items(db, library_ids=[4], apply=True)
        assert after_fill_rep["matched"] == 0
    finally:
        db.close()
        engine.dispose()
