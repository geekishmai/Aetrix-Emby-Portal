# -*- coding: utf-8 -*-
"""播放源呈现口径（站主 2026-11-02 亲定）回归。

背景：第三方播放器里别家源卡显示「Dolby＋17.4 GB＋19.02Mbps」并按
分辨率进 4K 页签，我方源卡只剩「文件」二字、无大小无码率、落进
「其他」页签。钉死的新口径：

1. MediaSource.Name 一律纯画质标签（分辨率＋HDR/杜比组合，如
   「4K Dolby」「1080P HDR」），不带片名/源名/服务器名；探测
   （宽高/video_range）优先、文件名解析补缺；判不出回落条目名
   （Emby 默认形态），不许编造。
2. 本地条目 Size/Bitrate/MediaStreams 从真实探测数据下发；
   无探测时除文件名画质标外留空（补探测走既有按需入队）。
3. CMS 托管条目：每条可展示映射一条 MediaSource（Id 带映射
   id），画质取自取流路径回填的缓存（m3u8 主清单/备注标记）；
   取流口按 MediaSourceId 定向解析所选映射，选错硬报错 404，
   绝不偷偷换线；旧形态（只到 :upstream）保持最佳映射口径。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.database import CacheManager, SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import quality_label as ql
from backend.emby_server import upstream_playback as up
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "MsPres#2026"
BOUND = "mspres_bound"
LIB_LOCAL = "画质呈现测试库"
LIB_CMS = "画质测试在线库"
SRC_A = "画质源甲"
SRC_B = "画质源乙"
CMS_MOVIE = "画质托管片"


# ---------------------------------------------------------------------------
# 纯函数：画质标签与 m3u8 主清单解析
# ---------------------------------------------------------------------------

def test_quality_label_from_probe_values():
    assert ql.label_for(3840, 2160, "HDR10") == "4K HDR"
    assert ql.label_for(3840, 2160, "DolbyVision") == "4K Dolby"
    assert ql.label_for(1920, 1080, "SDR") == "1080P"
    assert ql.label_for(1280, 720, None) == "720P"
    assert ql.label_for(0, 0, "DolbyVision") == "Dolby"
    assert ql.label_for(0, 0, "SDR") is None
    assert ql.label_for(0, 0, None) is None


def test_quality_label_filename_fallback_and_no_guessing():
    assert ql.label_for(0, 0, None,
                        "某片.2024.2160p.Dolby.Vision.WEB-DL.mkv") == "4K Dolby"
    assert ql.label_for(0, 0, None, "某片.1080p.x264.mkv") == "1080P"
    # DVDRip 不是杜比：裸 DV 只在独立成词时才算标记
    assert ql.label_for(0, 0, None, "某片.DVDRip.mkv") is None
    # 探测与文件名分量独立：探测有分辨率时不因文件名没标记丢范围
    assert ql.label_for(3840, 2160, None, "某片.HDR.mkv") == "4K HDR"
    # 文件名没数据不许猜
    assert ql.label_for(0, 0, None, "plain.mkv") is None


def test_quality_label_from_cms_text_marks():
    assert ql.label_from_text("4K") == "4K"
    assert ql.label_from_text("BD1080P") == "1080P"
    assert ql.label_from_text("杜比视界 2160P") == "4K Dolby"
    # 裸 HD 语义含糊，不猜
    assert ql.label_from_text("HD") is None
    assert ql.label_from_text("更新至第12集") is None
    assert ql.label_from_text("") is None


def test_parse_m3u8_master_picks_top_bandwidth():
    text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,'
        'CODECS="avc1.64001f,mp4a.40.2"\n'
        "720.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=18500000,RESOLUTION=3840x2160,'
        'CODECS="dvhe.08.06,mp4a.40.2"\n'
        "2160.m3u8\n"
    )
    master = ql.parse_m3u8_master(text)
    assert master == {"width": 3840, "height": 2160,
                      "bandwidth": 18500000, "video_range": "DolbyVision"}
    assert ql.label_for_master(master) == "4K Dolby"
    # 单码率清单没有变体行 → 判不出，不猜
    assert ql.parse_m3u8_master("#EXTM3U\n#EXTINF:10,\nseg.ts\n") is None
    assert ql.parse_m3u8_master("") is None


# ---------------------------------------------------------------------------
# TestClient 种子（本地条目 + CMS 托管条目同库盘）
# ---------------------------------------------------------------------------

def _guid():
    return uuid.uuid4().hex


def _cleanup(db):
    for lib_name in (LIB_LOCAL, LIB_CMS):
        for lib in db.query(em.Library).filter(
                em.Library.name == lib_name).all():
            ids = [r[0] for r in db.query(em.MediaItem.id).filter(
                em.MediaItem.library_id == lib.id).all()]
            if ids:
                db.query(em.UpstreamItemMapping).filter(
                    em.UpstreamItemMapping.local_item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.MediaStream).filter(
                    em.MediaStream.item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.MediaItem).filter(
                    em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(
                em.Library.id == lib.id).delete(synchronize_session=False)
    for src in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_A, SRC_B])).all():
        db.delete(src)
    u = db.query(models.WebUser).filter(
        models.WebUser.username == BOUND).first()
    if u is not None:
        cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                    .filter(em.UserStorageBinding.user_id == u.id).all()]
        db.query(em.UserStorageBinding).filter(
            em.UserStorageBinding.user_id == u.id).delete(
            synchronize_session=False)
        if cred_ids:
            db.query(em.MountCredential).filter(
                em.MountCredential.id.in_(cred_ids)).delete(
                synchronize_session=False)
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == u.id).delete(
            synchronize_session=False)
        db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    user = models.WebUser(username=BOUND, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=BOUND,
                          emby_password=PW)
    db.add(user)
    db.flush()
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=user.id, sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    # account_label 必须全仓唯一：共享文件库里别家种子（如
    # test_emby_views_compat）teardown 删用户不删绑定，孤儿绑定残留后
    # 新用户复用其 id 时会撞 (user_id, provider, account_label) 唯一键
    # ——多家共用「种子盘」正是本文件 CI setup 5 连 ERROR 的成因。
    db.add(em.UserStorageBinding(user_id=user.id, provider="drive",
                                 account_label="画质呈现种子盘",
                                 credential_id=cred.id, status="active"))
    lib = em.Library(guid=_guid(), name=LIB_LOCAL,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    probed = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质探测片",
        file_path="mount://3/电影/画质探测片 (2024)/画质探测片.mkv",
        container="mkv", size=17_400_000_000, bitrate=19_020_000,
        width=3840, height=2160, duration_ticks=2 * 3600 * TICKS,
        probe_status="done")
    db.add(probed)
    db.flush()
    db.add(em.MediaStream(item_id=probed.id, stream_index=0,
                          stream_type="Video", codec="hevc",
                          video_range="HDR10", bit_rate=19_020_000,
                          display_title="4K HEVC", is_default=True))
    db.add(em.MediaStream(item_id=probed.id, stream_index=1,
                          stream_type="Audio", codec="aac", channels=2,
                          is_default=True))
    dv_named = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质文件名片",
        file_path="mount://3/电影/某片 (2024)/某片.2024.2160p.Dolby.Vision.WEB-DL.mkv",
        container="mkv", size=8_000_000_000, duration_ticks=0,
        probe_status="failed", probe_attempts=99)
    db.add(dv_named)
    plain = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质无标记片",
        file_path="mount://3/电影/无标记/plain.mkv",
        container="mkv", size=1_000_000_000, duration_ticks=0,
        probe_status="failed", probe_attempts=99)
    db.add(plain)
    # CMS 托管行：两个源各一条映射，甲源预置画质缓存（模拟已点播过
    # 一次、取流路径回填），乙源无缓存。
    cms_lib = em.Library(guid=_guid(), name=LIB_CMS,
                         collection_type="movies", is_enabled=True,
                         kind="applecms")
    db.add(cms_lib)
    db.flush()
    src_a = em.UpstreamSource(name=SRC_A, source_type="applecms",
                              base_url="http://cms-a.example", enabled=True)
    src_b = em.UpstreamSource(name=SRC_B, source_type="applecms",
                              base_url="http://cms-b.example", enabled=True)
    db.add_all([src_a, src_b])
    db.flush()
    cms_item = em.MediaItem(guid=_guid(), library_id=cms_lib.id,
                            item_type="movie", name=CMS_MOVIE,
                            production_year=2024,
                            upstream_source_id=src_a.id,
                            probe_status="done")
    db.add(cms_item)
    db.flush()
    map_a = em.UpstreamItemMapping(local_item_id=cms_item.id,
                                   source_id=src_a.id,
                                   upstream_item_id="101",
                                   confidence=0.9, evidence="title+year")
    map_b = em.UpstreamItemMapping(local_item_id=cms_item.id,
                                   source_id=src_b.id,
                                   upstream_item_id="55",
                                   confidence=0.8, evidence="title+year")
    db.add_all([map_a, map_b])
    db.commit()
    up.store_cms_quality(map_a.id, {
        "width": 3840, "height": 2160, "bandwidth": 18_500_000,
        "video_range": "DolbyVision", "label": "4K Dolby"})
    data = {"probed": probed.guid, "dv_named": dv_named.guid,
            "plain": plain.guid, "cms": cms_item.guid,
            "cms_id": cms_item.id,
            "map_a": map_a.id, "map_b": map_b.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        for mid in (data["map_a"], data["map_b"]):
            CacheManager.delete(up.cms_quality_cache_key(mid))
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    q = chr(34)
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": BOUND, "Pw": PW},
        headers={"X-Emby-Authorization":
                 f"MediaBrowser Client={q}MsPres{q}, Device={q}iOS{q}, "
                 f"DeviceId={q}mspres-dev-1{q}, Version={q}1.0{q}"})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _playback_sources(client, token, guid):
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo",
                    headers={"X-Emby-Token": token}, json={})
    assert r.status_code == 200, r.text
    return r.json()["MediaSources"]


def test_local_probed_movie_source_presentation(client, token, seed):
    src = _playback_sources(client, token, seed["probed"])[0]
    assert src["Name"] == "4K HDR"
    assert src["Size"] == 17_400_000_000
    assert src["Bitrate"] == 19_020_000
    video = [s for s in src["MediaStreams"] if s["Type"] == "Video"][0]
    assert video["Width"] == 3840 and video["Height"] == 2160
    assert video["VideoRange"] == "HDR10"


def test_local_unprobed_movie_name_from_filename_only(client, token, seed):
    src = _playback_sources(client, token, seed["dv_named"])[0]
    # 画质标来自文件名解析（Emby 版本名惯例），不是编的探测值
    assert src["Name"] == "4K Dolby"
    # 无探测：轨道留空、码率不推导（无时长），不许编造
    assert src["MediaStreams"] == []
    assert "Bitrate" not in src


def test_local_unknown_quality_falls_back_to_item_name(client, token, seed):
    src = _playback_sources(client, token, seed["plain"])[0]
    assert src["Name"] == "画质无标记片"


def test_cms_item_one_source_per_mapping_with_quality(client, token, seed):
    sources = _playback_sources(client, token, seed["cms"])
    assert len(sources) == 2
    line_a, line_b = sources
    # 偏好序：置信度高的甲源在前；Id/地址都带各自映射 id
    assert line_a["Id"] == f"{seed['cms']}:upstream:{seed['map_a']}"
    assert line_b["Id"] == f"{seed['cms']}:upstream:{seed['map_b']}"
    for line, mid in ((line_a, seed["map_a"]), (line_b, seed["map_b"])):
        q = parse_qs(urlsplit(line["DirectStreamUrl"]).query)
        assert q["MediaSourceId"][0] == f"{seed['cms']}:upstream:{mid}"
        # 名字里绝不出现源名
        assert SRC_A not in line["Name"] and SRC_B not in line["Name"]
    # 甲源有画质缓存：纯画质标 + 真实带宽/轨道摘要
    assert line_a["Name"] == "4K Dolby"
    assert line_a["Bitrate"] == 18_500_000
    video = [s for s in line_a["MediaStreams"] if s["Type"] == "Video"][0]
    assert video["Width"] == 3840 and video["Height"] == 2160
    assert video["VideoRange"] == "DolbyVision"
    # Size 推不出就不报实数（0/缺失都是未知口径），不编造
    assert not line_a.get("Size")
    # 乙源无缓存：回落 Emby 默认形态（条目名），不拿源名凑数
    assert line_b["Name"] == CMS_MOVIE


def test_upstream_stream_endpoint_directed_and_hard_errors(
        client, token, seed, monkeypatch):
    calls = {}

    def fake_resolve(db, local_item_id, *, mapping_id=None, **kwargs):
        calls["mapping_id"] = mapping_id
        return "https://cdn.example.com/picked.m3u8"

    monkeypatch.setattr(up, "resolve_mapped_upstream_stream", fake_resolve)
    headers = {"X-Emby-Token": token}
    guid = seed["cms"]
    r = client.get(
        f"/emby/Videos/{guid}/upstream/stream",
        params={"MediaSourceId": f"{guid}:upstream:{seed['map_b']}"},
        headers=headers, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "https://cdn.example.com/picked.m3u8"
    assert calls["mapping_id"] == seed["map_b"]
    # 旧形态（只到 :upstream）→ 最佳映射口径（mapping_id=None）
    calls.clear()
    r = client.get(
        f"/emby/Videos/{guid}/upstream/stream",
        params={"MediaSourceId": f"{guid}:upstream"},
        headers=headers, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert calls["mapping_id"] is None
    # 形态错误（别条目的 id / id 段非数字）→ 硬 404，绝不进解析
    calls.clear()
    for bad in (f"deadbeef:upstream:{seed['map_b']}",
                f"{guid}:upstream:abc", f"{guid}:local"):
        r = client.get(
            f"/emby/Videos/{guid}/upstream/stream",
            params={"MediaSourceId": bad},
            headers=headers, follow_redirects=False)
        assert r.status_code == 404, (bad, r.text)
        assert r.json()["code"] == "upstream_mapping_not_found"
    assert calls == {}


# ---------------------------------------------------------------------------
# 函数级桩链：定向解析 + m3u8 画质回填（内存库，无网络）
# ---------------------------------------------------------------------------

@pytest.fixture()
def stub_db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    from backend import models as base_models
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _stub_seed(db):
    for sid, host in ((7, "a.example"), (8, "b.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"桩源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(guid="lib-stub", name="桩库", collection_type="movies",
                     is_enabled=True, kind="applecms",
                     upstream_source_ids="7,8")
    db.add(lib)
    db.flush()
    item = em.MediaItem(guid="stub-item", library_id=lib.id,
                        item_type="movie", name="桩片",
                        upstream_source_id=7, probe_status="done")
    db.add(item)
    db.flush()
    map_a = em.UpstreamItemMapping(local_item_id=item.id, source_id=7,
                                   upstream_item_id="101", confidence=0.9,
                                   evidence="title+year")
    map_b = em.UpstreamItemMapping(local_item_id=item.id, source_id=8,
                                   upstream_item_id="55", confidence=0.8,
                                   evidence="title+year")
    other = em.MediaItem(guid="stub-other", library_id=lib.id,
                         item_type="movie", name="桩片二",
                         upstream_source_id=7, probe_status="done")
    db.add_all([map_a, map_b, other])
    db.flush()
    map_other = em.UpstreamItemMapping(local_item_id=other.id, source_id=7,
                                       upstream_item_id="999",
                                       confidence=0.9, evidence="title+year")
    db.add(map_other)
    db.commit()
    # 隔离：内存库的映射 id 会与 module 级 seed 夹具（文件库）撞号，
    # 而画质缓存按映射 id 存进程级 CacheManager——不清掉 seed 预置的
    # 完整缓存，PA-073 新鲜度门禁会让本夹具的取流回填被旧缓存挡住。
    for mid in (map_a.id, map_b.id, map_other.id):
        CacheManager.delete(up.cms_quality_cache_key(mid))
    return item, map_a, map_b, map_other


_DETAILS = {
    "a.example": {"vod_id": 101, "vod_name": "桩片",
                  "vod_remarks": "1080P",
                  "vod_play_from": "线路A",
                  "vod_play_url": "正片$https://cdn-a.example/movie.mp4"},
    "b.example": {"vod_id": 55, "vod_name": "桩片",
                  "vod_play_from": "线路B",
                  "vod_play_url": "正片$https://cdn-b.example/master.m3u8"},
}

_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,'
    'CODECS="avc1.640028,mp4a.40.2"\n'
    "1080.m3u8\n"
)


def _stub_detail_get(url, headers=None, timeout=None):
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc
    return 200, {"code": 1, "list": [_DETAILS[host]]}


def _stub_text_get(url, headers=None, timeout=None):
    return 200, _MASTER


def test_directed_resolve_picks_selected_mapping(stub_db):
    item, map_a, map_b, _ = _stub_seed(stub_db)
    url = up.resolve_mapped_upstream_stream(
        stub_db, item.id, mapping_id=map_b.id,
        http_get=_stub_detail_get, http_probe=lambda *a: (200, None),
        http_get_text=_stub_text_get)
    # 选乙源就只出乙源的地址，不许回落最佳（甲源）
    assert url == "https://cdn-b.example/master.m3u8"
    # 取流顺手回填画质缓存（m3u8 主清单真实数值）
    cached = up.read_cms_quality(map_b.id)
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["bandwidth"] == 8_000_000
    assert cached["label"] == "1080P"
    # 甲源走最佳口径仍是甲源；备注标记也回填成标签
    url_a = up.resolve_mapped_upstream_stream(
        stub_db, item.id, http_get=_stub_detail_get,
        http_probe=lambda *a: (200, None))
    assert url_a == "https://cdn-a.example/movie.mp4"
    assert up.read_cms_quality(map_a.id)["label"] == "1080P"


def test_directed_resolve_hard_errors_no_fallback(stub_db):
    item, map_a, map_b, map_other = _stub_seed(stub_db)
    # 映射属于别的条目 → 404，绝不换线
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=map_other.id,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    assert ei.value.code == "upstream_mapping_not_found"
    # 不存在的映射 id → 404
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=424242,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    # 所选映射的源被停用 → 按读契约不可见，同 404 硬报错
    src = stub_db.query(em.UpstreamSource).filter(
        em.UpstreamSource.id == 8).one()
    src.enabled = False
    stub_db.commit()
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=map_b.id,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    assert ei.value.code == "upstream_mapping_not_found"
