"""远程 115 旁挂 .nfo 取链降本回归测试（perf/nfo-read-cost，默认全关）。"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from backend.emby_server import enrich_worker as ew
from backend.emby_server import mounts as mount_lib
from backend.emby_server import nfo_cache
from backend.emby_server import scanner as sc


SAMPLE_MOVIE_NFO = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>无间道</title>
  <originaltitle>Infernal Affairs</originaltitle>
  <year>2002</year>
  <plot>香港卧底警匪经典。</plot>
  <tmdbid>10775</tmdbid>
  <imdbid>tt0338564</imdbid>
</movie>
"""

SAMPLE_EPISODE_NFO = """<?xml version="1.0" encoding="utf-8"?>
<episodedetails>
  <title>凛冬将至</title>
  <season>1</season>
  <episode>1</episode>
  <plot>第一集简介</plot>
</episodedetails>
"""


class _Fake115SidecarProvider:
    mount_type = "115_sidecar"
    provider = "p115"
    local_root = None

    def __init__(self, entries_by_dir: dict[str, list[mount_lib.MountEntry]], texts: dict[str, str]):
        self.mount = SimpleNamespace(id=7, mount_type="115_sidecar", path="/媒体库")
        self.config = {"cookie": "UID=999_SUPER_SECRET_COOKIE; CID=deadbeef"}
        self._entries_by_dir = entries_by_dir
        self._texts = texts
        self.list_calls: list[str] = []
        self.read_calls: list[str] = []
        self.fail_next_read = False

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
        if self.fail_next_read:
            self.fail_next_read = False
            raise mount_lib.MountError("115 link bucket 429 temporary error")
        if norm not in self._texts:
            raise mount_lib.MountError(f"not found: {norm}")
        return self._texts[norm]


@pytest.fixture(autouse=True)
def _clean_nfo_cache_state(monkeypatch, tmp_path):
    nfo_cache.reset_for_tests()
    ew._PATH_TAG_DEAD.clear()
    monkeypatch.setenv("EMBY_NFO_CACHE_DIR", str(tmp_path / "nfo_cache"))
    yield
    nfo_cache.reset_for_tests()
    ew._PATH_TAG_DEAD.clear()



def _new_ctx(lib_id: int = 3):
    snap = sc.LibrarySnapshot(
        library_id=lib_id, name="", collection_type="",
        paths=(), scrape_policy="smart",
    )
    return sc._ScanContext(snap=snap, lib_id=lib_id, stats={})


def _enable_fake_tmdb(monkeypatch):
    from backend.emby_server import tmdb as tmdb_lib
    monkeypatch.setattr(tmdb_lib.TmdbClient, "configured", property(lambda self: True))

def _make_scan_file(provider, dir_rel: str, fname: str, mount_id: int = 7):
    rel = f"{dir_rel.rstrip('/')}/{fname}"
    return SimpleNamespace(
        local_dir=None,
        mount_id=mount_id,
        provider=provider,
        dir_rel=dir_rel,
        name=fname,
        stored_path=f"mount://{mount_id}{rel}",
    )


def _make_item(**kw):
    defaults = dict(
        id=101,
        library_id=3,
        item_type="movie",
        name="无间道",
        file_path="mount://7/华语电影/无间道 (2002) {tmdb-10775}/无间道.mkv",
        size=10_000_000_000,
        container="mkv",
        production_year=2002,
        poster_path=None,
        primary_image_url=None,
        imdb_id=None,
        aliases=None,
        tmdb_id=None,
        upstream_source_id=None,
        repair_requested_at=None,
        overview=None,
        series_id=None,
        parent_id=None,
        season_number=None,
        episode_number=None,
        metadata_locked=False,
        enrich_attempts=0,
        enrich_claim_token=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_defaults_off_preserves_legacy_nfo_reads(monkeypatch):
    """默认双开关全关：即使目录带 {tmdb-N}，行为与改前一致仍读旁挂 .nfo。"""
    monkeypatch.delenv("ENRICH_NFO_SMART_SKIP", raising=False)
    monkeypatch.delenv("EMBY_NFO_PICKCODE_CACHE", raising=False)

    dir_rel = "/华语电影/无间道 (2002) {tmdb-10775}"
    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_rel: [
                mount_lib.MountEntry("无间道.mkv", f"{dir_rel}/无间道.mkv", False, 1000, file_id="pcmkv1"),
                mount_lib.MountEntry("无间道.nfo", f"{dir_rel}/无间道.nfo", False, len(SAMPLE_MOVIE_NFO), file_id="pcnfo1"),
            ]
        },
        texts={f"{dir_rel}/无间道.nfo": SAMPLE_MOVIE_NFO},
    )
    sf = _make_scan_file(prov, dir_rel, "无间道.mkv")
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf)
    _enable_fake_tmdb(monkeypatch)

    item = _make_item()
    res1, _ = ew._enrich_fetch_pre(item, holder={})
    res2, _ = ew._enrich_fetch_pre(item, holder={})
    assert res1["nfo_data"]["tmdb_id"] == "10775"
    assert res2["nfo_data"]["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 2


def test_smart_skip_path_tag_skips_nfo_and_rescues_on_404_miss(monkeypatch):
    """ENRICH_NFO_SMART_SKIP=1：
    1) 有效 {tmdb-N} 零次读 .nfo，直接拉 details；
    2) {tmdb-99999999} 死号 404 且标题 search 未命中时，按需懒读 1 次 .nfo 救活身份。
    """
    monkeypatch.setenv("ENRICH_NFO_SMART_SKIP", "1")
    _enable_fake_tmdb(monkeypatch)
    monkeypatch.setattr("backend.emby_server.tmdb.prewarm_images", lambda *a, **kw: 0)
    monkeypatch.setattr(ew, "_should_multisource_fill_missing", lambda *a, **kw: False)
    monkeypatch.setattr(ew, "_douban_fallback_on_tmdb_miss", lambda *a, **kw: False)

    # 1) 有效 {tmdb-10775}：完全不读 .nfo
    dir_ok = "/华语电影/无间道 (2002) {tmdb-10775}"
    prov_ok = _Fake115SidecarProvider(
        entries_by_dir={
            dir_ok: [
                mount_lib.MountEntry("无间道.mkv", f"{dir_ok}/无间道.mkv", False, 1000, file_id="pcmkv1"),
                mount_lib.MountEntry("无间道.nfo", f"{dir_ok}/无间道.nfo", False, len(SAMPLE_MOVIE_NFO), file_id="pcnfo1"),
            ]
        },
        texts={f"{dir_ok}/无间道.nfo": SAMPLE_MOVIE_NFO},
    )
    sf_ok = _make_scan_file(prov_ok, dir_ok, "无间道.mkv")
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf_ok)
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client.images_with_language", lambda *a, **kw: None)
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_details",
        lambda pairs, return_transient=False: (
            [{"id": 10775, "title": "无间道", "release_date": "2002-12-12", "poster_path": "/p.jpg"}],
            set(),
        ),
    )

    fetched_ok = ew._enrich_fetch(_make_item(file_path=f"mount://7{dir_ok}/无间道.mkv"), holder={})
    assert prov_ok.read_calls == []
    assert fetched_ok["tmdb_id"] == "10775"
    assert fetched_ok["tmdb_details"]["id"] == 10775

    # 2) 死号 {tmdb-99999999} 404 + search miss → 自动懒读 .nfo 救回 10775
    dir_dead = "/华语电影/乱码发布名 (2002) {tmdb-99999999}"
    prov_dead = _Fake115SidecarProvider(
        entries_by_dir={
            dir_dead: [
                mount_lib.MountEntry("x.mkv", f"{dir_dead}/x.mkv", False, 1000, file_id="pcmkv2"),
                mount_lib.MountEntry("x.nfo", f"{dir_dead}/x.nfo", False, len(SAMPLE_MOVIE_NFO), file_id="pcnfo2"),
            ]
        },
        texts={f"{dir_dead}/x.nfo": SAMPLE_MOVIE_NFO},
    )
    sf_dead = _make_scan_file(prov_dead, dir_dead, "x.mkv")
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf_dead)
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_details",
        lambda pairs, return_transient=False: ([None for _ in pairs], set()),
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.batch_search",
        lambda queries, return_transient=False: ([None for _ in queries], set()),
    )
    monkeypatch.setattr(
        "backend.emby_server.tmdb.tmdb_client.details",
        lambda tid, kind: {"id": int(tid), "title": "无间道", "release_date": "2002-12-12"}
        if str(tid) == "10775" else None,
    )

    fetched_rescued = ew._enrich_fetch(
        _make_item(name="乱码发布名", file_path=f"mount://7{dir_dead}/x.mkv"), holder={}
    )
    assert len(prov_dead.read_calls) == 1
    assert "99999999" in ew._PATH_TAG_DEAD
    assert fetched_rescued["tmdb_id"] == "10775"
    assert fetched_rescued["nfo_data"]["imdb_id"] == "tt0338564"


def test_smart_skip_bound_item_fill_stage_skips_nfo_and_avoids_search(monkeypatch, tmp_path):
    """两队列 fill 段（条目已绑 tmdb_id，目录无 {tmdb-N}）：
    跳过远程 .nfo 读取，且 _tmdb_plan_for_item 直走 details 而非回落 search；
    本机文件（local_dir 非空）不受跳过影响。
    """
    monkeypatch.setenv("ENRICH_NFO_SMART_SKIP", "1")
    _enable_fake_tmdb(monkeypatch)

    dir_rel = "/外语电影/Inception (2010)"
    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_rel: [
                mount_lib.MountEntry("Inception.mkv", f"{dir_rel}/Inception.mkv", False, 1000, file_id="pcm1"),
                mount_lib.MountEntry("Inception.nfo", f"{dir_rel}/Inception.nfo", False, 400, file_id="pcn1"),
            ]
        },
        texts={f"{dir_rel}/Inception.nfo": SAMPLE_MOVIE_NFO},
    )
    sf = _make_scan_file(prov, dir_rel, "Inception.mkv")
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf)
    monkeypatch.setattr(ew, "_shared_ctx", lambda item, holder: _new_ctx())

    bound_item = _make_item(
        name="Inception",
        production_year=2010,
        file_path=f"mount://7{dir_rel}/Inception.mkv",
        tmdb_id="27205",
        poster_path="/already_poster.jpg",
        overview=None,  # 简介仍空 → fill 段需拉 details，但不读 .nfo、不跑 search
    )
    res, tmdb_ctx = ew._enrich_fetch_pre(bound_item, holder={})
    assert prov.read_calls == []
    assert res["nfo_data"] is None
    assert tmdb_ctx["tmdb_plan"]["op"] == "details"
    assert tmdb_ctx["tmdb_plan"]["tmdb_id"] == "27205"

    # 本机文件即使带 {tmdb-N} 仍正常读本地 NFO（本地磁盘无 115 保号风险）
    local_dir = tmp_path / "local_movie {tmdb-10775}"
    local_dir.mkdir()
    (local_dir / "movie.nfo").write_text(SAMPLE_MOVIE_NFO, encoding="utf-8")
    local_sf = SimpleNamespace(
        local_dir=str(local_dir),
        mount_id=None,
        provider=None,
        dir_rel="",
        name="movie.mkv",
        stored_path=str(local_dir / "movie.mkv"),
    )
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: local_sf)
    local_item = _make_item(file_path=str(local_dir / "movie.mkv"))
    local_res, _ = ew._enrich_fetch_pre(local_item, holder={})
    assert local_res["nfo_data"]["tmdb_id"] == "10775"


def test_smart_skip_episode_with_own_nfo_uses_pure_inherit_when_parent_has_tmdb(monkeypatch):
    """单集智能跳过：父级剧有 tmdb_id 时，即使目录含 <集>.nfo 也走 pure_inherit 零读 NFO；
    父级剧无 tmdb_id 时仍保留单集 .nfo 读取。
    """
    monkeypatch.setenv("ENRICH_NFO_SMART_SKIP", "1")
    _enable_fake_tmdb(monkeypatch)

    dir_rel = "/欧美剧/权力的游戏 {tmdb-1399}/Season 1"
    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_rel: [
                mount_lib.MountEntry("S01E01.mkv", f"{dir_rel}/S01E01.mkv", False, 1000, file_id="pcep1"),
                mount_lib.MountEntry("S01E01.nfo", f"{dir_rel}/S01E01.nfo", False, len(SAMPLE_EPISODE_NFO), file_id="pcepnfo1"),
                mount_lib.MountEntry("season.nfo", f"{dir_rel}/season.nfo", False, 120, file_id="pcseason"),
            ],
            "/欧美剧/权力的游戏 {tmdb-1399}": [
                mount_lib.MountEntry("tvshow.nfo", "/欧美剧/权力的游戏 {tmdb-1399}/tvshow.nfo", False, 200, file_id="pctvshow"),
            ],
        },
        texts={
            f"{dir_rel}/S01E01.nfo": SAMPLE_EPISODE_NFO,
            f"{dir_rel}/season.nfo": "<season><title>S1</title></season>",
            "/欧美剧/权力的游戏 {tmdb-1399}/tvshow.nfo": "<tvshow><title>GoT</title><tmdbid>1399</tmdbid></tvshow>",
        },
    )
    sf = _make_scan_file(prov, dir_rel, "S01E01.mkv")
    monkeypatch.setattr(ew, "_scanfile_from_item", lambda item: sf)
    monkeypatch.setattr(ew, "_shared_ctx", lambda item, holder: _new_ctx())
    monkeypatch.setattr(
        ew, "_fetch_episode_tmdb",
        lambda item, parent, result: result.update({"episode_tmdb": {"name": "凛冬将至"}}),
    )

    ep_item = _make_item(
        item_type="episode",
        name="S01E01",
        file_path=f"mount://7{dir_rel}/S01E01.mkv",
        series_id=50,
        season_number=1,
        episode_number=1,
    )
    res_with_parent, tmdb_ctx = ew._enrich_fetch_pre(
        ep_item, holder={}, inherit_parent={"series_id": 50, "tmdb_id": "1399", "negative": False}
    )
    assert tmdb_ctx is None
    assert res_with_parent["pure_inherit"] is True
    assert res_with_parent["episode_tmdb"]["name"] == "凛冬将至"
    assert prov.read_calls == []

    # 父级无 tmdb_id（如自制剧只有海报、无 TMDB 词条）→ 保留读单集 NFO
    res_no_tmdb, _ = ew._enrich_fetch_pre(
        ep_item, holder={}, inherit_parent={"series_id": 50, "tmdb_id": None, "negative": False}
    )
    assert res_no_tmdb.get("pure_inherit") is not True
    assert res_no_tmdb["nfo_data"]["title"] == "凛冬将至"
    assert len(prov.read_calls) >= 1


def test_pickcode_permanent_cache_survives_restart_rename_and_stores_zero_secrets(
    monkeypatch, tmp_path
):
    """EMBY_NFO_PICKCODE_CACHE=1：
    - 首次按 pickcode 读 1 次并落盘；
    - 清空进程 L1 内存（模拟重启）+ 目录移动改名后，仍按同一 pickcode 100% 命中磁盘缓存（0 次重取）；
    - 文件大小变化（模拟同提取码覆盖异常）自动失效重拉；
    - 落盘文件名与内容零 Cookie、零明文 pickcode、零挂载路径。
    """
    monkeypatch.setenv("EMBY_NFO_PICKCODE_CACHE", "1")
    cache_dir = tmp_path / "persistent_nfo_cache"
    monkeypatch.setenv("EMBY_NFO_CACHE_DIR", str(cache_dir))

    secret_pickcode = "epc_secret_pickcode_888"
    dir_v1 = "/华语电影/无间道_旧目录"
    dir_v2 = "/港片精选/无间道 (2002) 移动改名后"
    nfo_bytes = len(SAMPLE_MOVIE_NFO.encode("utf-8"))

    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_v1: [
                mount_lib.MountEntry("a.mkv", f"{dir_v1}/a.mkv", False, 1000, file_id="pcmkv"),
                mount_lib.MountEntry("a.nfo", f"{dir_v1}/a.nfo", False, nfo_bytes, file_id=secret_pickcode),
            ],
            dir_v2: [
                mount_lib.MountEntry("infernal.mkv", f"{dir_v2}/infernal.mkv", False, 1000, file_id="pcmkv"),
                mount_lib.MountEntry("infernal.nfo", f"{dir_v2}/infernal.nfo", False, nfo_bytes, file_id=secret_pickcode),
            ],
        },
        texts={
            f"{dir_v1}/a.nfo": SAMPLE_MOVIE_NFO,
            f"{dir_v2}/infernal.nfo": SAMPLE_MOVIE_NFO,
        },
    )

    # 第一次扫描读取：miss → 调 1 次 read_sidecar_text 并落盘
    sf1 = _make_scan_file(prov, dir_v1, "a.mkv")
    nfo1, _, _ = sc._find_nfos(_new_ctx(), sf1, "movie")
    assert nfo1["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 1

    # 调用方篡改返回字典不得污染缓存副本
    nfo1["tmdb_id"] = "MUTATED"

    # 清空 L1 模拟容器重启 + 跨目录改名移动后重扫：直接命中磁盘缓存，read_calls 仍为 1
    with nfo_cache._mem_lock:
        nfo_cache._mem_cache.clear()
    sf2 = _make_scan_file(prov, dir_v2, "infernal.mkv")
    nfo2, _, _ = sc._find_nfos(_new_ctx(), sf2, "movie")
    assert nfo2["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 1
    assert nfo_cache.stats()["disk_hits"] == 1

    # 检查磁盘文件零明文凭据 / 零明文 pickcode / 零挂载路径
    stored_files = [
        os.path.join(dp, fn)
        for dp, _, fns in os.walk(cache_dir)
        for fn in fns if fn.endswith(".json")
    ]
    assert len(stored_files) == 1
    raw_content = open(stored_files[0], "r", encoding="utf-8").read()
    assert "SUPER_SECRET_COOKIE" not in raw_content
    assert secret_pickcode not in raw_content
    assert secret_pickcode not in stored_files[0]
    assert "无间道_旧目录" not in raw_content

    # 若文件字节大小变化 → 视为未命中重拉
    prov._entries_by_dir[dir_v2][1] = mount_lib.MountEntry(
        "infernal.nfo", f"{dir_v2}/infernal.nfo", False, nfo_bytes + 64, file_id=secret_pickcode
    )
    nfo3, _, _ = sc._find_nfos(_new_ctx(), sf2, "movie")
    assert nfo3["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 2


def test_pickcode_cache_skips_transient_errors_singleflights_and_ignores_non115(monkeypatch):
    """1) 瞬态读取异常不落盘固化；2) 同 pickcode 并发读取单飞去重；3) 非 115 挂载不走 pickcode 缓存。"""
    monkeypatch.setenv("EMBY_NFO_PICKCODE_CACHE", "1")

    dir_rel = "/华语电影/英雄 (2002)"
    prov = _Fake115SidecarProvider(
        entries_by_dir={
            dir_rel: [
                mount_lib.MountEntry("hero.mkv", f"{dir_rel}/hero.mkv", False, 1000, file_id="pcmkv"),
                mount_lib.MountEntry("hero.nfo", f"{dir_rel}/hero.nfo", False, 256, file_id="pchero"),
            ]
        },
        texts={f"{dir_rel}/hero.nfo": SAMPLE_MOVIE_NFO},
    )
    prov.fail_next_read = True
    sf = _make_scan_file(prov, dir_rel, "hero.mkv")

    # 首次因 429 抛错 → _read_nfo_cached 返 None，但永久缓存绝不固化失败
    nfo_err, _, _ = sc._find_nfos(_new_ctx(), sf, "movie")
    assert nfo_err is None
    assert nfo_cache.stats()["writes"] == 0

    # 下一轮重试恢复 → 正常读取并写入永久缓存，再下一轮零读取命中
    nfo_ok, _, _ = sc._find_nfos(_new_ctx(), sf, "movie")
    assert nfo_ok["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 2
    nfo_cached, _, _ = sc._find_nfos(_new_ctx(), sf, "movie")
    assert nfo_cached["tmdb_id"] == "10775"
    assert len(prov.read_calls) == 2

    # 同 pickcode 多线程并发 get_or_load → loader 只执行 1 次
    barrier = threading.Barrier(4)
    load_count = 0
    count_lock = threading.Lock()

    def _slow_loader() -> str:
        nonlocal load_count
        with count_lock:
            load_count += 1
        return SAMPLE_MOVIE_NFO

    results = []

    def _worker():
        barrier.wait(timeout=5)
        hit, data = nfo_cache.get_or_load("pcconcurrent1", 300, _slow_loader, sc.nfo_lib.parse_nfo)
        results.append((hit, data["tmdb_id"]))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert load_count == 1
    assert len(results) == 4
    assert sum(1 for hit, _ in results if not hit) == 1
    assert sum(1 for hit, _ in results if hit) == 3

    # 非 115 挂载（如 rclone）原地覆盖可能复用同 file_id → extract_sidecar_pickcode 返空不走缓存
    rclone_prov = SimpleNamespace(mount_type="rclone", provider="rclone")
    entry = mount_lib.MountEntry("a.nfo", "/m/a.nfo", False, 100, file_id="gdrive_id_1")
    assert mount_lib.extract_sidecar_pickcode(rclone_prov, "/m/a.nfo", entry) == ("", 0)


def test_inproc_pan115_mount_list_dir_populates_nfo_pickcode(monkeypatch):
    """进程内 Pan115Mount.list_dir 亦记录非目录项（含 .nfo）的 pickcode 供永久缓存零 API 提取。"""
    mount_row = SimpleNamespace(id=9, mount_type="115", path="/", config="{}")
    prov = mount_lib.Pan115Mount(mount_row, cookie_override="UID=1_A1_1; CID=a; SEID=b")
    monkeypatch.setattr(prov, "_cid_of", lambda rel: "0")
    monkeypatch.setattr(
        prov,
        "_raw_list",
        lambda cid: [
            {"name": "movie.mkv", "is_dir": False, "size": 9000, "cid": "11", "pickcode": "pcmkv9"},
            {"name": "movie.nfo", "is_dir": False, "size": 384, "cid": "12", "pickcode": "pcnfo9"},
        ],
    )
    entries = prov.list_dir("/片库/无间道")
    nfo_entry = next(e for e in entries if e.name == "movie.nfo")
    assert mount_lib.extract_sidecar_pickcode(prov, "/片库/无间道/movie.nfo", nfo_entry) == ("pcnfo9", 384)
