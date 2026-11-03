"""CMS 片头印制广告「借帧修复」产品化（第一批）回归。

锁住的口径（2026-11-03 站主亲定：口子 60 秒、按热度先修一批）：

- 入选 fail-closed：带长 ≤ 口子（默认 60 秒）＋有干净兄弟＋逐帧
  门控全过，任一不过不修、记原因、原样播；角落水印型长带（带
  持续到探测上限）单独计数、不在本批范围。
- 交付：清单层只换修复覆盖的头部分段地址，其余分段仍是原 CDN
  绝对地址；分段时长对不上（清单已变）→ 整条不换、维持原样。
- 只有 verified 的修复才许被播放路径查到；修了没验的一律不发。
- 队列按条目近 14 天起播量排序，只收降权名单源的映射。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from PIL import Image

from backend.database import SessionLocal, init_db
from backend.emby_server import cms_graft as graft
from backend.emby_server import cms_playlist_proxy as proxy
from backend.emby_server import models as em

init_db()

SRC_DIRTY = 98901     # 假毒源（测试内自洽，不依赖生产名单）
SRC_CLEAN = 98902     # 假干净源
SRC_OTHER = 98903
LIB_NAME = "借帧测试库"


def _guid():
    return uuid.uuid4().hex


def _playlist(durations):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10"]
    for i, d in enumerate(durations):
        lines.append(f"#EXTINF:{d},")
        lines.append(f"seg{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------- 清单换段


def test_apply_graft_segments_replaces_head_only():
    text = _playlist([6.0, 6.0, 6.0, 6.0])
    segs = [{"idx": 0, "dur": 6.0}, {"idx": 1, "dur": 6.0}]
    out, stats = proxy.apply_graft_segments(
        text, segs, lambda i: f"https://local/graftseg?i={i}")
    uris = [l for l in out.splitlines() if not l.startswith("#")]
    assert uris[0] == "https://local/graftseg?i=0"
    assert uris[1] == "https://local/graftseg?i=1"
    assert uris[2] == "seg2.ts" and uris[3] == "seg3.ts"
    assert stats["graft_applied"] is True
    assert stats["graft_segments"] == 2


def test_apply_graft_segments_duration_mismatch_aborts():
    text = _playlist([6.0, 9.5, 6.0])
    segs = [{"idx": 0, "dur": 6.0}, {"idx": 1, "dur": 6.0}]
    out, stats = proxy.apply_graft_segments(
        text, segs, lambda i: f"https://local/graftseg?i={i}")
    assert out == text
    assert stats["graft_applied"] is False


def test_apply_graft_segments_short_playlist_aborts():
    text = _playlist([6.0])
    segs = [{"idx": 0, "dur": 6.0}, {"idx": 1, "dur": 6.0}]
    out, stats = proxy.apply_graft_segments(
        text, segs, lambda i: f"https://local/graftseg?i={i}")
    assert out == text
    assert stats["graft_applied"] is False


def test_apply_graft_segments_empty_is_noop():
    text = _playlist([6.0, 6.0])
    out, stats = proxy.apply_graft_segments(text, [], lambda i: "x")
    assert out == text
    assert stats["graft_applied"] is False


# ---------------------------------------------------------- 纯判定


def test_locate_strip_finds_top_band():
    # 100 行画面：第 4..13 行与干净版差异大（顶部带），其余一致
    profile = [1.0] * 100
    for r in range(4, 14):
        profile[r] = 40.0
    strip = graft.locate_strip(profile)
    assert strip is not None
    y0, y1 = strip
    assert y0 <= 4 and y1 >= 14
    assert (y1 - y0) <= 35  # 不许把大半个画面当带


def test_locate_strip_none_when_uniform():
    assert graft.locate_strip([1.0] * 100) is None
    assert graft.locate_strip([30.0] * 100) is None  # 整帧都不同=不是带


def test_band_end_from_series():
    # 25fps：带持续 2 秒（strip 差高），之后回落
    diffs = [30.0] * 50 + [1.0] * 100
    end = graft.band_end_from_series(diffs, fps=25.0)
    assert end is not None and 1.5 <= end <= 3.0


def test_band_end_persists_returns_none():
    diffs = [30.0] * 200
    assert graft.band_end_from_series(diffs, fps=25.0) is None


def test_decide_eligibility():
    ok = graft.decide_eligibility(
        band_end_sec=24.0, sibling_found=True, mouth_sec=60.0)
    assert ok == "eligible"
    assert graft.decide_eligibility(
        band_end_sec=75.0, sibling_found=True,
        mouth_sec=60.0) == "band_too_long"
    assert graft.decide_eligibility(
        band_end_sec=None, sibling_found=True,
        mouth_sec=60.0) == "watermark_or_overlong"
    assert graft.decide_eligibility(
        band_end_sec=24.0, sibling_found=False,
        mouth_sec=60.0) == "no_clean_sibling"


def test_frame_similarity_outside_strip():
    base = Image.new("RGB", (64, 64), (100, 100, 100))
    same = base.copy()
    diff = Image.new("RGB", (64, 64), (100, 100, 100))
    for x in range(64):
        for y in range(30, 40):  # 带外区域改动（亮度大变）
            diff.putpixel((x, y), (230, 230, 230))
    strip = (0, 8)
    assert graft.outside_similarity(base, same, strip) < 1.0
    assert graft.outside_similarity(base, diff, strip) > 12.0


def test_process_pair_resolve_error_is_fetch_failed(monkeypatch):
    # 回归（第一批首跑实证）：resolve 抛 UpstreamPlaybackError 时
    # 必须落 failed/fetch_failed，不许在异常处理器里二次崩掉丢账
    from types import SimpleNamespace

    from backend.emby_server.upstream_playback import (
        UpstreamPlaybackError)

    def _boom(db, mapping):
        raise UpstreamPlaybackError(
            404, "upstream_mapping_not_found", "所选线路已不可用")

    monkeypatch.setattr(graft, "resolve_stream", _boom)
    dirty = SimpleNamespace(id=1, local_item_id=2, source_id=14)
    sibling = SimpleNamespace(id=3, local_item_id=2, source_id=9)
    out = graft.process_pair(None, dirty, sibling, mouth=60.0)
    assert out["status"] == "failed"
    assert out["reason"] == "fetch_failed"
    assert "upstream_mapping_not_found" in out["detail"]


def test_partition_candidates_resume_semantics():
    # 断点续跑口径（第一批实证）：running 是死残留必须重处理，
    # 不许静默丢；repaired 只补验证；终局状态跳过。
    from backend.emby_server.cms_graft_worker import partition_candidates

    queue = [{"mapping_id": i} for i in range(1, 8)]
    existing = {2: "running", 3: "repaired", 4: "verified",
                5: "skipped", 6: "failed", 7: "verify_failed"}
    todo, verify_only = partition_candidates(queue, existing)
    assert [c["mapping_id"] for c in todo] == [1, 2]
    assert [c["mapping_id"] for c in verify_only] == [3]


def test_host_ok_breaker():
    assert graft.host_ok(4_000_000, 10.0) is True     # KB, load1
    assert graft.host_ok(1_000_000, 10.0) is False    # 可用内存 <1.5GB
    assert graft.host_ok(4_000_000, 45.0) is False    # 负载 >40


# ---------------------------------------------------------- DB 口径


def _cleanup(db):
    ids = (SRC_DIRTY, SRC_CLEAN, SRC_OTHER)
    db.query(em.CmsGraftRepair).delete(synchronize_session=False)
    db.query(em.PlayStartEvent).filter(
        em.PlayStartEvent.item_id.in_(
            db.query(em.MediaItem.id).filter(
                em.MediaItem.name.like("借帧%")))).delete(
        synchronize_session=False)
    db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.source_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(em.MediaItem).filter(
        em.MediaItem.name.like("借帧%")).delete(synchronize_session=False)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.id.in_(ids)).delete(synchronize_session=False)
    db.query(em.Library).filter(
        em.Library.name == LIB_NAME).delete(synchronize_session=False)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    for sid, name in ((SRC_DIRTY, "借帧毒源"), (SRC_CLEAN, "借帧净源"),
                      (SRC_OTHER, "借帧他源")):
        db.add(em.UpstreamSource(
            id=sid, name=name, source_type="applecms",
            base_url="https://cms.example.com", auth_method="none",
            enabled=True, credential_scope="source",
            health_status="unknown"))
    lib = em.Library(guid=_guid(), name=LIB_NAME,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()

    def _item(name, tmdb=None, itype="movie", parent_id=None,
              season=None, episode=None, series_id=None):
        it = em.MediaItem(
            guid=_guid(), library_id=lib.id, item_type=itype, name=name,
            file_path=f"mount://3/{name}.mp4", container="mp4",
            size=1_000_000, duration_ticks=600_000_000,
            probe_status="ok", tmdb_id=tmdb, parent_id=parent_id,
            series_id=series_id,
            season_number=season, episode_number=episode)
        db.add(it)
        db.flush()
        return it

    def _map(item, sid, ep_idx=None):
        m = em.UpstreamItemMapping(
            local_item_id=item.id, source_id=sid,
            upstream_item_id=f"vod-{item.id}-{sid}", confidence=0.9,
            evidence="tmdb", episode_index=ep_idx)
        db.add(m)
        db.flush()
        return m

    hot = _item("借帧热门片", tmdb="111")
    hot_dirty = _map(hot, SRC_DIRTY)
    hot_clean = _map(hot, SRC_CLEAN)
    cold = _item("借帧冷门片", tmdb="222")
    cold_dirty = _map(cold, SRC_DIRTY)
    twin = _item("借帧热门片别行", tmdb="111")
    twin_clean = _map(twin, SRC_CLEAN)
    solo = _item("借帧无兄弟片", tmdb="333")
    solo_dirty = _map(solo, SRC_DIRTY)
    cross = _item("借帧跨行片", tmdb="444")
    cross_dirty = _map(cross, SRC_DIRTY)
    cross_twin = _item("借帧跨行片别行", tmdb="444")
    cross_twin_clean = _map(cross_twin, SRC_CLEAN)
    s1 = _item("借帧剧甲", tmdb="555", itype="series")
    s2 = _item("借帧剧乙", tmdb="555", itype="series")
    e1 = _item("借帧剧甲第3集", itype="episode", parent_id=s1.id,
               season=1, episode=3, series_id=s1.id)
    e2 = _item("借帧剧乙第3集", itype="episode", parent_id=s2.id,
               season=1, episode=3, series_id=s2.id)
    e3 = _item("借帧剧乙第4集", itype="episode", parent_id=s2.id,
               season=1, episode=4, series_id=s2.id)
    ep_dirty = _map(e1, SRC_DIRTY, ep_idx=3)
    ep_clean = _map(e2, SRC_CLEAN, ep_idx=3)
    _map(e3, SRC_CLEAN, ep_idx=4)
    # 热度：热门片 5 次、冷门片 1 次起播（近 14 天）
    for _ in range(5):
        db.add(em.PlayStartEvent(
            user_id=1, item_id=hot.id, provider="upstream",
            method="upstream", result="ok"))
    db.add(em.PlayStartEvent(
        user_id=1, item_id=cold.id, provider="upstream",
        method="upstream", result="ok"))
    db.commit()
    data = {"lib": lib.id, "hot": hot.id, "cold": cold.id,
            "hot_dirty": hot_dirty.id, "hot_clean": hot_clean.id,
            "cold_dirty": cold_dirty.id, "twin_clean": twin_clean.id,
            "solo_dirty": solo_dirty.id, "cross_dirty": cross_dirty.id,
            "cross_twin_clean": cross_twin_clean.id,
            "ep_dirty": ep_dirty.id, "ep_clean": ep_clean.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


def test_config_defaults(seed):
    db = SessionLocal()
    try:
        assert graft.graft_enabled(db) is True
        assert graft.mouth_sec(db) == 60.0
        assert graft.concurrency(db) == 2
        assert graft.home_concurrency(db) == 1
        assert SRC_DIRTY not in graft.home_source_ids(db)
        assert 11 in graft.home_source_ids(db)
    finally:
        db.close()


def test_lookup_only_verified(seed):
    db = SessionLocal()
    try:
        graft.upsert_repair(
            db, mapping_id=seed["hot_dirty"], local_item_id=seed["hot"],
            source_id=SRC_DIRTY, status="repaired",
            segments=[{"idx": 0, "dur": 6.0, "file": "seg_000.ts"}])
        db.commit()
        # 修了没验 → 播放路径查不到
        assert graft.lookup_verified(
            db, seed["hot"], seed["hot_dirty"], SRC_DIRTY) is None
        graft.upsert_repair(
            db, mapping_id=seed["hot_dirty"], local_item_id=seed["hot"],
            source_id=SRC_DIRTY, status="verified")
        db.commit()
        got = graft.lookup_verified(
            db, seed["hot"], seed["hot_dirty"], SRC_DIRTY)
        assert got is not None and got[0]["file"] == "seg_000.ts"
    finally:
        db.close()


def test_find_sibling_same_item(seed):
    db = SessionLocal()
    try:
        sib = graft.find_sibling(db, seed["hot_dirty"], {SRC_CLEAN})
        assert sib is not None and sib.id == seed["hot_clean"]
    finally:
        db.close()


def test_find_sibling_cross_item_movie(seed):
    db = SessionLocal()
    try:
        # 跨行片同行无净线，只能从同 tmdb 的别行借
        sib = graft.find_sibling(db, seed["cross_dirty"], {SRC_CLEAN})
        assert sib is not None and sib.id == seed["cross_twin_clean"]
        sib = graft.find_sibling(db, seed["solo_dirty"], {SRC_CLEAN})
        assert sib is None  # 无同行、无同身份别行
    finally:
        db.close()


def test_find_sibling_cross_item_episode(seed):
    db = SessionLocal()
    try:
        # 集级跨行：经系列同 tmdb 找到别行同季同集的净线，
        # 不许拿第 4 集冒充第 3 集
        sib = graft.find_sibling(db, seed["ep_dirty"], {SRC_CLEAN})
        assert sib is not None and sib.id == seed["ep_clean"]
    finally:
        db.close()


def test_queue_heat_order_and_demote_only(seed):
    db = SessionLocal()
    try:
        queue = graft.build_candidate_queue(
            db, demoted_source_ids={SRC_DIRTY}, limit=10)
        ids = [q["mapping_id"] for q in queue]
        assert ids[0] == seed["hot_dirty"]       # 热度最高在前
        assert seed["cold_dirty"] in ids
        assert seed["hot_clean"] not in ids      # 只收降权源
    finally:
        db.close()


def test_queue_excludes_disabled_sources(seed):
    # 停用中的源 resolve 必 not_found，不许进队列白跑
    # （第一批实证：无尽停用期间 11 条全灭）。
    db = SessionLocal()
    try:
        src = db.query(em.UpstreamSource).get(SRC_DIRTY)
        src.enabled = False
        db.commit()
        queue = graft.build_candidate_queue(
            db, demoted_source_ids={SRC_DIRTY}, limit=10)
        assert queue == []
    finally:
        src = db.query(em.UpstreamSource).get(SRC_DIRTY)
        src.enabled = True
        db.commit()
        db.close()


def test_playlist_not_m3u8_rejected(monkeypatch):
    # 源站把 HTML 页当清单回 → 入口就拦（第一批实证 rc=3 乱码段）。
    monkeypatch.setattr(
        graft, "fetch_with_route",
        lambda url, headers, **kw: b"<!DOCTYPE html><html>verify</html>")
    with pytest.raises(graft.FetchError, match="playlist_not_m3u8"):
        graft.fetch_media_playlist_text(
            "https://x/list.m3u8", {}, source_id=1, db=None)


def test_parse_playlist_extracts_aes_key():
    # AES-128 加密源（第一批 32 条 segment_not_ts 的真身）：清单
    # 必须把密钥信息交给下载层，不许把密文当坏段。
    text = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:7\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\","
            "IV=0x00000000000000000000000000000000\n"
            "#EXTINF:10.0,\nseg0.ts\n#EXTINF:10.0,\nseg1.ts\n")
    segs, init, key = graft.parse_media_playlist(
        text, "https://h/a/index.m3u8")
    assert len(segs) == 2 and init is None
    assert key["method"] == "AES-128"
    assert key["key_url"] == "https://h/a/enc.key"
    assert key["iv"] == bytes(16)
    assert key["sequence"] == 7


def test_download_segments_decrypts_aes(monkeypatch, tmp_path):
    # 用 openssl 把一段假 TS 加密，下载层必须先解再落盘。
    import subprocess as sp

    key = bytes(range(16))
    plain = bytes([0x47]) + b"A" * 47
    enc = sp.run(["openssl", "enc", "-aes-128-cbc", "-K", key.hex(),
                  "-iv", bytes(16).hex()], input=plain,
                 capture_output=True, check=True).stdout
    assert enc[0] != 0x47  # 密文确实看不出 TS 同步字节
    monkeypatch.setattr(
        graft, "fetch_with_route", lambda url, headers, **kw: enc)
    paths, _tot = graft._download_segments(
        [(5.0, "https://x/seg0.ts")], {}, source_id=1, db=None,
        stats=None, work_dir=tmp_path, prefix="d", cover_sec=5.0,
        key_info={"method": "AES-128", "key_url": "https://x/k",
                  "iv": bytes(16), "sequence": 0},
        key_bytes=key)
    assert paths[0].read_bytes() == plain


def test_fetch_key_bytes_rejects_other_methods():
    with pytest.raises(graft.FetchError, match="unsupported_key_method"):
        graft.fetch_key_bytes(
            {"method": "SAMPLE-AES", "key_url": "https://x/k",
             "iv": None, "sequence": 0}, {}, source_id=1, db=None)


def test_segment_not_ts_rejected(monkeypatch, tmp_path):
    # 段内容不是 TS/fMP4（HTML 错误页）→ 门口拦下，不进解码。
    monkeypatch.setattr(
        graft, "fetch_with_route",
        lambda url, headers, **kw: b"<html>404</html>")
    with pytest.raises(graft.FetchError, match="segment_not_ts"):
        graft._download_segments(
            [(5.0, "https://x/seg0.ts")], {}, source_id=1, db=None,
            stats=None, work_dir=tmp_path, prefix="d", cover_sec=5.0)
