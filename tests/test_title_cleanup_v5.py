# -*- coding: utf-8 -*-
"""残留 none 三大分桶治理回归套件（fix/title-cleanup-v5，2026-11-07）。

覆盖：
1. rule_pack v5 词表冻存与尾巴剥离：Hero 10bit 2Audio MNHD FRDS、-ALT、-CHD、
   -CTRLWEB、-MOMOWEB、-BATHD、Complete OurTV、kadaj@beAst / -@beAst、HD MA5 1 / DAudio / TriAudio；
   且 Hero (2002) 通过英文别名 + 精确年份命中张艺谋《英雄》(tmdb_id=79)，正经片名（Beast / 梅根2.0 / 哥斯拉-1.0）零误伤。
2. beAst 多部曲合集目录 year_mismatch 根治：
   - 电影位于合集目录（年份跨度 1984-2009 / 1999-2003 / 合集 / 三部曲）时只按单文件解析年份，无年份子文件不回退取目录年份区间；
   - 同目录含 >=2 个视频文件或命中合集目录名时，_find_nfos 跳过共享 `movie.nfo`，仅读同名 `<stem>.nfo`。
3. 剧集库日期型单集（快乐大本营 20190105..20191228）与 part1..part6 分卷目录合并：
   - 扫描侧：日期文件解析为 episode 聚合到同一部剧；嵌套分卷（剑侠/part1..6）、平铺分卷（Negotiations.Sweetheart.part1..5）
     与误套异剧分卷（传奇大掌柜/New.Legend.of.Chu.Liu.Xiang.part1..2）精确归一且不串剧；
   - 存量收敛原型 series_consolidate：dry-run 零写库，apply 幂等合并。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import rule_pack as rp
from backend.emby_server import scanner as sc
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.series_consolidate import consolidate_tv_pseudo_series


# ==================== 1. v5 词表与 Hero 10bit 2Audio MNHD FRDS ====================

def test_rule_pack_v5_freeze_and_new_tail_tokens():
    assert rp.PACK_VERSION == "v5"
    assert rp.PACK_VERSION_ORDER == ("v1", "v2", "v3", "v4", "v5")
    assert set(rp.PACK_VERSION_TERMS["v5"]) - set(rp._V5_ADDED_TERMS) == set(rp.PACK_VERSION_TERMS["v4"])

    cases = [
        ("Hero.2002.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv", "movies", "Hero", 2002),
        ("Hero 10bit 2Audio MNHD FRDS.mkv", "movies", "Hero", None),
        ("The.Magic.Hour.2008.BluRay.1080p.DTS-HD.MA5.1.x265.10bit-ALT.mkv", "movies", "The Magic Hour", 2008),
        ("Dark.Spell.2021.1080p.BluRay.DTS.x265-10bit-CHD.mkv", "movies", "Dark Spell", 2021),
        ("Song.of.the.Phoenix.2013.2160p.WEB-DL.H265.AAC-CTRLWEB.mp4", "movies", "Song of the Phoenix", 2013),
        ("Best.of.Times.2009.2160p.WEB-DL.H265.AAC-MOMOWEB.mp4", "movies", "Best of Times", 2009),
        ("Super.Typhoon.2008.1080p.BluRay.DTS.x265-10bit-BATHD.mkv", "movies", "Super Typhoon", 2008),
        ("Family.Harmony.2015.Complete.4K.WEB-DL.H.264.AAC-OurTV", "movies", "Family Harmony", 2015),
        ("Argo.2012.Theatrical.BluRay.2160p.x265.10bit.HDR.3Audio-MiniHD.mkv", "movies", "Argo", 2012),
        ("Once.Upon.A.Time.In.China.1991.Remastered.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv", "movies", "Once Upon A Time In China", 1991),
        ("The.Hunger.Games.2012.BluRay.1080p.x264.DTS-HD.MA.7.1-kadaj@beAst.mkv", "movies", "The Hunger Games", 2012),
        ("Some.Film.2020.1080p.BluRay-@beAst.mkv", "movies", "Some Film", 2020),
        ("TLOTR.The.Two.Towers.2002.Extended.REPACK.1080p.Blu-ray.DAudio.DTS-HD.MA6.1.PGS.x264-beAst.mkv", "movies", "TLOTR The Two Towers", 2002),
        # QA CS2-P1-1 样例 1 & 2：《原钻 Uncut Gems》《完全未知 A Complete Unknown》真片名绝不误删 Uncut / Complete
        ("/外语电影/Uncut.Gems.2019.1080p.BluRay.x264-SPARKS.mkv", "movies", "Uncut Gems", 2019),
        ("/外语电影/A.Complete.Unknown.2024.1080p.WEB-DL.DDP5.1.H264.mkv", "movies", "A Complete Unknown", 2024),
        # 运维活体抽样 1：BeiTai 无空格声道残留 MA5 1 / MA7 1 整体剥净
        ("Hot Fuzz MA5 1.mkv", "movies", "Hot Fuzz", None),
        ("Inglourious Basterds MA5 1.mkv", "movies", "Inglourious Basterds", None),
        ("Black Panther Wakanda Forever MA7 1.mkv", "movies", "Black Panther Wakanda Forever", None),
        ("Hot.Fuzz.2007.BluRay.1080p.DTS-HD.MA5.1.x265-BeiTai.mkv", "movies", "Hot Fuzz", 2007),
    ]
    for path, lib_type, expected_name, expected_year in cases:
        parsed = sc.parse_media_filename(path, lib_type)
        assert parsed["name"] == expected_name, (path, parsed)
        assert parsed["year"] == expected_year, (path, parsed)

    # 反例守卫：单字版本形容词作片名（Uncut Gems / A Complete Unknown / Unrated / Theatrical）与 Beast / 梅根2.0 绝不误删
    assert sc.clean_name("Uncut Gems") == "Uncut Gems"
    assert sc.clean_name("A Complete Unknown") == "A Complete Unknown"
    assert sc.clean_name("Unrated") == "Unrated"
    assert sc.clean_name("Theatrical") == "Theatrical"
    assert sc.clean_name("Beast") == "Beast"
    assert sc.clean_name("Beauty and the Beast") == "Beauty and the Beast"
    assert sc.clean_name("梅根2.0") == "梅根2.0"
    assert sc.clean_name("哥斯拉-1.0") == "哥斯拉-1.0"

    # 尾部候选剥离验证：无年份存量串 `Gorgeous UNCUT` 首候选保留原词，尾部次选候选生成 `Gorgeous`
    g_cands = [q for q, _ in tmdb_mod._search_candidates("Gorgeous UNCUT", 1999)]
    assert g_cands[0] == "Gorgeous UNCUT"
    assert "Gorgeous" in g_cands
    # 而《Uncut Gems》《A Complete Unknown》的首候选与全部候选均保留完整片名
    assert tmdb_mod._clean_query("Uncut Gems") == "Uncut Gems"
    assert tmdb_mod._clean_query("A Complete Unknown") == "A Complete Unknown"
    # Unrated 仅在 tmdb._REGION_TAIL_RE 尾部候选里剥离：首候选保留原词，次选候选给出剥除 Unrated 的片名
    u_cands = [q for q, _ in tmdb_mod._search_candidates("Hot Fuzz Unrated", 2007)]
    assert u_cands[0] == "Hot Fuzz Unrated"
    assert "Hot Fuzz" in u_cands
    assert tmdb_mod._clean_query("Black Panther Wakanda Forever MA7 1") == "Black Panther Wakanda Forever"


def test_cs2_four_cases_alternative_titles_gate_and_nfo_fallback(monkeypatch, tmp_path):
    """QA CS2 四大回归样例端到端验证：
    1) Uncut.Gems.2019.1080p.BluRay.x264-SPARKS.mkv → 精确命中英文真片《Uncut Gems》(id=473033)，绝不误删 Uncut；
    2) A.Complete.Unknown.2024.1080p.WEB-DL.DDP5.1.H264.mkv → 精确命中英文真片《A Complete Unknown》(id=661539)；
    3) Hero 10bit 2Audio MNHD FRDS (2002) → TmdbClient.search 用缓存 details.alternative_titles 核对含 "Hero" 放行《英雄》(id=79)，0 额外请求；
    4) 偶然同年撞词（如带未知组尾巴 `Uncut Gems UNKNOWNGRP` 2019 仅返回同年华语片《火花》，alternative_titles 无精确别名）
       → TmdbClient.search 严格拦截返回 None，enrich_worker 安全落回 _rescue_nfo_on_identify_miss 读 .nfo 救回真 tmdb_id=473033！
    """
    # Case 1 & 2：真英文片直中 Tier 2
    p_uncut = sc.parse_media_filename("/外语电影/Uncut.Gems.2019.1080p.BluRay.x264-SPARKS.mkv", "movies")
    s_uncut = tmdb_mod._hit_score(p_uncut["name"], p_uncut["name"], {
        "id": 473033, "title": "原钻", "original_title": "Uncut Gems",
        "original_language": "en", "release_date": "2019-12-13",
    }, fuzzy_ok=True, year=2019)
    assert s_uncut == (2, len("uncutgems"))

    p_complete = sc.parse_media_filename("/外语电影/A.Complete.Unknown.2024.1080p.WEB-DL.DDP5.1.H264.mkv", "movies")
    s_complete = tmdb_mod._hit_score(p_complete["name"], p_complete["name"], {
        "id": 661539, "title": "完全未知", "original_title": "A Complete Unknown",
        "original_language": "en", "release_date": "2024-12-25",
    }, fuzzy_ok=True, year=2024)
    assert s_complete == (2, len("acompleteunknown"))

    # Case 3 & 4：TmdbClient.search + details.alternative_titles 精确对账 + 0 额外请求 + NFO 兜底联动
    monkeypatch.setenv("TMDB_API_KEY", "fake-key-for-cs2")
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(tmp_path / "tmdb_disk_cache"))
    client = tmdb_mod.TmdbClient()
    http_detail_calls = []

    fake_details_db = {
        "/movie/79": {
            "id": 79,
            "title": "英雄",
            "original_title": "英雄",
            "original_language": "zh",
            "release_date": "2002-12-19",
            "credits": {"cast": [], "crew": []},
            "images": {"logos": [], "backdrops": []},
            "alternative_titles": {
                "titles": [
                    {"iso_3166_1": "US", "title": "Hero"},
                    {"iso_3166_1": "HK", "title": "Ying xiong"},
                ]
            },
        },
        "/movie/9991": {
            "id": 9991,
            "title": "火花",
            "original_title": "火花",
            "original_language": "zh",
            "release_date": "2019-06-01",
            "credits": {"cast": [], "crew": []},
            "images": {"logos": [], "backdrops": []},
            "alternative_titles": {
                "titles": [{"iso_3166_1": "US", "title": "Sparks"}]
            },
        },
        "/movie/473033": {
            "id": 473033,
            "title": "原钻",
            "original_title": "Uncut Gems",
            "release_date": "2019-12-13",
            "credits": {"cast": [], "crew": []},
            "images": {"logos": [], "backdrops": []},
            "alternative_titles": {"titles": []},
        },
    }

    def _fake_search_raw(query, year, kind, lang="zh-CN"):
        if query == "Hero" and year == 2002:
            return [{
                "id": 79, "title": "英雄", "original_title": "英雄",
                "original_language": "zh", "release_date": "2002-12-19", "popularity": 35.8,
            }]
        if "Uncut Gems UNKNOWNGRP" in query and year == 2019:
            return [{
                "id": 9991, "title": "火花", "original_title": "火花",
                "original_language": "zh", "release_date": "2019-06-01", "popularity": 12.0,
            }]
        return []

    def _fake_http_get(path, params=None):
        http_detail_calls.append((path, (params or {}).get("append_to_response", "")))
        return fake_details_db.get(path)

    monkeypatch.setattr(client, "_search_raw", _fake_search_raw)
    monkeypatch.setattr(client, "_get", _fake_http_get)
    monkeypatch.setattr(tmdb_mod, "language_fallback_chain", lambda: ["zh-CN"])

    # Case 3：Hero (2002) 通过真 details() 拉回的 alternative_titles["titles"] ("Hero") 放行，
    # 且 enrich_worker 紧接着再调 client.details("79", "movie") 直接命中 L1 缓存（总计仅 1 次 HTTP，0 额外请求）
    hero_hit = client.search("Hero 10bit 2Audio MNHD FRDS", 2002, "movie")
    assert hero_hit is not None and hero_hit["id"] == 79
    det_cached = client.details("79", "movie")
    assert det_cached is not None and det_cached["id"] == 79
    assert len(http_detail_calls) == 1
    assert http_detail_calls[0][0] == "/movie/79"
    assert "alternative_titles" in http_detail_calls[0][1]
    # Case 4：偶然同年华语片撞词因 alternative_titles 无精确匹配被拒（返回 None），继而走 _rescue_nfo_on_identify_miss 读 .nfo 救回 473033
    collision_hit = client.search("Uncut Gems UNKNOWNGRP", 2019, "movie")
    assert collision_hit is None

    from backend.emby_server import enrich_worker as ew
    monkeypatch.setattr(tmdb_mod, "tmdb_client", client)
    # 473033 已在上方 fake_details_db 中就绪
    movie_dir = tmp_path / "Uncut.Gems.2019"
    movie_dir.mkdir()
    vid = movie_dir / "Uncut.Gems.2019.UNKNOWNGRP.mkv"
    vid.write_bytes(b"0")
    (movie_dir / "movie.nfo").write_text("<movie><tmdbid>473033</tmdbid></movie>", encoding="utf-8")
    item_obj = SimpleNamespace(id=501, name="Uncut Gems UNKNOWNGRP", production_year=2019, file_path=str(vid), tmdb_id=None, imdb_id=None)
    sf = SimpleNamespace(local_dir=str(movie_dir), name=vid.name, stored_path=str(vid), mount_id=None, provider=None)
    res_dict = {"ok": True}
    ew._rescue_nfo_on_identify_miss(item_obj, "movie", res_dict, tmdb_ctx={"_scan_file": sf, "_ctx": SimpleNamespace(nfo_cache={}, remote_dir_cache={})})
    assert res_dict.get("tmdb_id") == "473033"

    # CS2-P2-1 顺手验证：单部电影目录含 `1 正片 + 1 trailer + movie.nfo` 时，trailer 不计入多片，正常读取 movie.nfo
    single_with_trailer = tmp_path / "Single.Movie.2020"
    single_with_trailer.mkdir()
    main_v = single_with_trailer / "Single.Movie.2020.1080p.mkv"
    trl_v = single_with_trailer / "Single.Movie.2020-trailer.mp4"
    main_v.write_bytes(b"0")
    trl_v.write_bytes(b"0")
    (single_with_trailer / "movie.nfo").write_text("<movie><tmdbid>12345</tmdbid></movie>", encoding="utf-8")
    sf_single = SimpleNamespace(local_dir=str(single_with_trailer), name=main_v.name, stored_path=str(main_v), mount_id=None, provider=None)
    nfo_single, _, _ = sc._find_nfos(SimpleNamespace(nfo_cache={}, remote_dir_cache={}), sf_single, "movie")
    assert nfo_single is not None and nfo_single["tmdb_id"] == "12345"
# ==================== 2. 多部曲合集目录按文件解析年份 & 隔离共享 movie.nfo ====================

def test_multi_movie_collection_dir_uses_file_year_only_and_skips_shared_movie_nfo(tmp_path):
    col_dir = tmp_path / "[终结者四部曲].Terminator.Quadrilogy.1984-2009.BluRay.1080p.3Audio.DTS-HD.MA.5.1.x264-beAst"
    col_dir.mkdir()

    f1 = col_dir / "Terminator.1.1984.CEE.BluRay.1080p.x264-beAst.mkv"
    f2 = col_dir / "Terminator.2.1991.Special.Edition.BluRay.1080p.x264-beAst.mkv"
    f_noyear = col_dir / "Terminator.Bonus.Doc.BluRay.1080p.x264-beAst.mkv"
    for f in (f1, f2, f_noyear):
        f.write_bytes(b"0")

    # 1) 各续集取各自文件名年份；无年份子文件绝不回退拿合集目录的 1984 或 2009
    p1 = sc.parse_media_filename(str(f1), "movies")
    p2 = sc.parse_media_filename(str(f2), "movies")
    p_ny = sc.parse_media_filename(str(f_noyear), "movies")
    assert p1["year"] == 1984
    assert p2["year"] == 1991
    assert p_ny["year"] is None

    # 2) 同目录存在共享 `movie.nfo`（写着第 1 部 1984 的 tmdbid=218）与第 2 部专属 `.nfo`（tmdbid=280）：
    #    第 1 部无专属 nfo 时不得误读共享 movie.nfo；第 2 部正常读取专属同名 nfo
    (col_dir / "movie.nfo").write_text("<movie><tmdbid>218</tmdbid><year>1984</year></movie>", encoding="utf-8")
    (col_dir / "Terminator.2.1991.Special.Edition.BluRay.1080p.x264-beAst.nfo").write_text(
        "<movie><tmdbid>280</tmdbid><year>1991</year></movie>", encoding="utf-8"
    )
    ctx = SimpleNamespace(nfo_cache={}, remote_dir_cache={})
    sf1 = SimpleNamespace(local_dir=str(col_dir), name=f1.name, stored_path=str(f1), mount_id=None, provider=None)
    sf2 = SimpleNamespace(local_dir=str(col_dir), name=f2.name, stored_path=str(f2), mount_id=None, provider=None)

    nfo1, _, _ = sc._find_nfos(ctx, sf1, "movie")
    nfo2, _, _ = sc._find_nfos(ctx, sf2, "movie")
    assert nfo1 is None, "多视频合集目录内不得读取共享 movie.nfo 污染其它续集"
    assert nfo2 is not None and nfo2["tmdb_id"] == "280"


# ==================== 3. 快乐大本营日期单集 & part1..part6 分卷剧合并 ====================

def test_happy_camp_date_named_files_parse_as_episodes_under_single_series():
    dir_rel = "/媒体库/国产剧/快乐大本营2019年合集 全52集.Happy.Camp.2019.1080p.WEB-DL.H264.AAC-OurTV"
    files = [
        "快乐大本营.Happy.Camp.20190105.1080p.WEB-DL.H264.AAC-OurTV.mp4",
        "快乐大本营.Happy.Camp.20190615.1080p.WEB-DL.H264.AAC-OurTV.mp4",
        "快乐大本营.Happy.Camp.20191228.1080p.WEB-DL.H264.AAC-OurTV.mp4",
    ]
    series_guids = set()
    series_names = set()
    ep_tuples = []
    for fname in files:
        full_rel = f"{dir_rel}/{fname}"
        parsed = sc.parse_media_filename(full_rel, "tvshows")
        sf = SimpleNamespace(local_dir=None, dir_rel=dir_rel, mount_id=1, stored_path=f"mount://1{full_rel}")
        series_guids.add(sc._series_guid_of(sf))
        series_names.add(sc._series_name_of(sf, parsed))
        ep_tuples.append((parsed["season"], parsed["episode"]))

    assert len(series_guids) == 1
    assert series_names == {"快乐大本营 Happy Camp"}
    assert ep_tuples == [(2019, 105), (2019, 615), (2019, 1228)]

    # 电影库中的数字片名（如 2049 / 1917）不受剧集日期规则影响
    m_parsed = sc.parse_media_filename("/Movies/Blade.Runner.2049.2017.1080p.mkv", "movies")
    assert m_parsed["season"] is None and m_parsed["year"] == 2017


def test_part1_to_part6_series_canonicalization_covers_nested_flat_and_misnested():
    # A) 嵌套在中文主剧目录下（剑侠 part1..part6 → 同一 guid，且拿回中文剧名）
    nested_guids = set()
    nested_names = set()
    for p in range(1, 7):
        d = f"/媒体库/国产剧/剑侠.A.Chinese.Immortal.Story.2014.Complete.4K.WEB-DL.H.264.AAC-OurTV/A.Chinese.Immortal.Story.part{p}.2014.Complete.4K.WEB-DL.H.264.AAC-OurTV"
        ep_path = f"{d}/A.Chinese.Immortal.Story.2014.E{p:02d}.4K.WEB-DL.H.264.AAC-OurTV.mp4"
        parsed = sc.parse_media_filename(ep_path, "tvshows")
        sf = SimpleNamespace(local_dir=None, dir_rel=d, mount_id=1, stored_path=f"mount://1{ep_path}")
        nested_guids.add(sc._series_guid_of(sf))
        nested_names.add(sc._series_name_of(sf, parsed))
    assert len(nested_guids) == 1
    assert all("剑侠" in n and "part" not in n.lower() for n in nested_names)

    # B) 平铺在库根下（Negotiations.Sweetheart.part1..part5 → 同一 guid，且不误并到 /媒体库/国产剧）
    flat_guids = set()
    for p in range(1, 6):
        d = f"/媒体库/国产剧/Negotiations.Sweetheart.part{p}.2014.Complete.4K.WEB-DL.AAC.H265-OurTV"
        sf = SimpleNamespace(local_dir=None, dir_rel=d, mount_id=1, stored_path=f"mount://1{d}/E0{p}.mp4")
        flat_guids.add(sc._series_guid_of(sf))
    root_sf = SimpleNamespace(local_dir=None, dir_rel="/媒体库/国产剧", mount_id=1, stored_path="mount://1/媒体库/国产剧/x.mp4")
    assert len(flat_guids) == 1
    assert next(iter(flat_guids)) != sc._series_guid_of(root_sf)

    # C) 误套在其它剧目录内（传奇大掌柜 2015 下的 New.Legend.of.Chu.Liu.Xiang.part1..2 2012 → 两卷合并，但绝不并进传奇大掌柜）
    parent_d = "/媒体库/国产剧/传奇大掌柜.Chuan.Qi.Da.Zhang.Gui.2015.Complete.4K.WEB-DL.H.264.AAC-OurTV"
    chu_guids = {
        sc._series_guid_of(SimpleNamespace(
            local_dir=None,
            dir_rel=f"{parent_d}/New.Legend.of.Chu.Liu.Xiang.part{p}.2012.WEB-DL.4k.H264.AAC-HDSWEB",
            mount_id=1,
            stored_path="",
        ))
        for p in (1, 2)
    }
    parent_guid = sc._series_guid_of(SimpleNamespace(local_dir=None, dir_rel=parent_d, mount_id=1, stored_path=""))
    assert len(chu_guids) == 1
    assert next(iter(chu_guids)) != parent_guid


def test_consolidate_tv_pseudo_series_dry_run_and_apply():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        lib = em.Library(guid="lib-tv-6", name="国产剧", collection_type="tvshows")
        db.add(lib)
        db.commit()
        db.refresh(lib)

        # 造 3 条日期型快乐大本营伪 series + 2 条汉字紧贴集号《云海玉弓缘EP17/EP18》伪 series + 2 条剑侠 part1/part2 重复 series
        for idx, ymd in enumerate(("20190105", "20190112", "20190119"), start=1):
            db.add(em.MediaItem(
                guid=f"hc-{idx}",
                library_id=lib.id,
                item_type="series",
                name=f"快乐大本营 Happy Camp {ymd[4:]} OurTV",
                file_path=f"mount://1/媒体库/国产剧/快乐大本营2019年合集 全52集.Happy.Camp.2019.1080p-OurTV/快乐大本营.Happy.Camp.{ymd}.1080p-OurTV.mp4",
            ))

        for ep_no in (17, 18):
            db.add(em.MediaItem(
                guid=f"yhygy-{ep_no}",
                library_id=lib.id,
                item_type="series",
                name=f"云海玉弓缘EP{ep_no}",
                file_path=f"mount://1/媒体库/国产剧/云海玉弓缘 (2002)/云海玉弓缘EP{ep_no}.MP4",
            ))

        for p, ep_no in ((1, 3), (2, 12)):
            s = em.MediaItem(
                guid=f"jx-s-{p}",
                library_id=lib.id,
                item_type="series",
                name=f"A Chinese Immortal Story part{p} Complete H 264 OurTV",
                production_year=2014,
            )
            db.add(s)
            db.flush()
            db.add(em.MediaItem(
                guid=f"jx-ep-{p}",
                library_id=lib.id,
                item_type="episode",
                series_id=s.id,
                season_number=1,
                episode_number=ep_no,
                name=f"E{ep_no:02d}",
                file_path=f"mount://1/媒体库/国产剧/剑侠.A.Chinese.Immortal.Story.2014.Complete.4K-OurTV/A.Chinese.Immortal.Story.part{p}.2014.Complete.4K-OurTV/A.Chinese.Immortal.Story.2014.E{ep_no:02d}.4K-OurTV.mp4",
            ))
        db.commit()

        # 0) 汉字紧贴集号扫描解析验证（含反例防误伤带 E 字母片名）
        p_ep17 = sc.parse_media_filename("/港剧/云海玉弓缘 (2002)/云海玉弓缘EP17.MP4", "tvshows")
        assert (p_ep17["name"], p_ep17["year"], p_ep17["season"], p_ep17["episode"]) == ("云海玉弓缘", 2002, 1, 17)
        p_e05 = sc.parse_media_filename("/港剧/云海玉弓缘 (2002)/云海玉弓缘E05.MP4", "tvshows")
        assert (p_e05["name"], p_e05["year"], p_e05["season"], p_e05["episode"]) == ("云海玉弓缘", 2002, 1, 5)
        p_end = sc.parse_media_filename("/港剧/云海玉弓缘 (2002)/云海玉弓缘EP20END.MP4", "tvshows")
        assert (p_end["year"], p_end["season"], p_end["episode"]) == (2002, 1, 20)
        assert sc.parse_media_filename("/科幻剧/未来都市E1区故事.mp4", "tvshows")["episode"] is None
        # 反例：AE86、E3游戏展、STEP17 绝不误判为集号
        assert sc.parse_media_filename("/纪录片/头文字D之AE86传奇.mp4", "tvshows")["episode"] is None
        assert sc.parse_media_filename("/纪录片/某纪录片之E3游戏展.mp4", "tvshows")["episode"] is None
        assert sc.parse_media_filename("/电影/STEP17.2020.mp4", "movies")["episode"] is None

        # 1) dry-run 零改动（3 快乐大本营 + 2 云海玉弓缘 = 5 条伪 series 待转单集）
        dry = consolidate_tv_pseudo_series(db, library_id=lib.id, apply=False)
        assert dry["applied"] is False
        assert dry["converted_episodes"] == 5
        assert dry["merged_series"] == 1
        assert db.query(em.MediaItem).filter_by(item_type="series", merged_into_id=None).count() == 7

        # 2) apply 落库合并：最终只剩 快乐大本营 + 云海玉弓缘 + 剑侠 3 部真 series，二次复跑幂等为 0
        applied = consolidate_tv_pseudo_series(db, library_id=lib.id, apply=True)
        assert applied["applied"] is True
        active_series = db.query(em.MediaItem).filter_by(
            library_id=lib.id, item_type="series", merged_into_id=None, is_hidden=False
        ).all()
        assert len(active_series) == 3
        yh_series = [s for s in active_series if s.name == "云海玉弓缘"]
        assert len(yh_series) == 1 and yh_series[0].production_year == 2002
        yh_eps = db.query(em.MediaItem).filter_by(series_id=yh_series[0].id, item_type="episode").order_by(em.MediaItem.episode_number).all()
        assert [(e.season_number, e.episode_number, e.name) for e in yh_eps] == [
            (1, 17, "云海玉弓缘 S01E17"),
            (1, 18, "云海玉弓缘 S01E18"),
        ]
        again = consolidate_tv_pseudo_series(db, library_id=lib.id, apply=True)
        assert again["converted_episodes"] == 0 and again["merged_series"] == 0
    finally:
        db.close()
        engine.dispose()
