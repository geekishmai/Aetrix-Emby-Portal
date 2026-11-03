"""片头印制广告自动剪除（非凡试点）回归。

锁住的口径（2026-11-03 站主批准：源头不堵、在交付层剪除）：

- 检测只认「片头静止段 + 已知赞助卡指纹命中」双信号：静止但不
  认识的画面（片头 logo 等）一律不剪（宁漏勿误）；命中后按映射
  记账（跳过秒数/置信度/证据），只有高置信记录才允许剪。
- 剪除只走 #1764 既有清单代理通道摘整段：广告结束落在段中间
  时，多丢的正片 ≤2 秒才许扩到段尾，否则整条不剪；无记录/低
  置信/开关关时取流行为与今日逐字一致（302 直发）。
- 检测是后台有界任务：没检测过的线照常播未剪版，绝不让用户
  等检测；检测失败（含源站 403 拉不到片头）只记状态不挡播放。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import cms_ad_skip as adskip
from backend.emby_server import cms_playlist_proxy as proxy
from backend.emby_server import models as em
from backend.emby_server import upstream_playback as up
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "AdSkip#2026"
BOUND = "adskip_bound"
FREE = "adskip_free"
LIB_NAME = "广告剪除测试库"
SRC_NAME = "广告剪除测试源"
MEDIA_URL = "https://vod.ffzy-test.com/2026/xyz/index.m3u8"


def _media_playlist(seg_count=10, seg_dur=6.0):
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(seg_count):
        lines.append(f"#EXTINF:{seg_dur},")
        lines.append(f"seg{i:03d}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 指纹/分析


def _seed_grid():
    seeds = adskip.load_fingerprints()
    assert seeds, "指纹库至少要有播种的天下会卡"
    return tuple(seeds[0]["grid"])


def _samples_from_grids(grids, interval=0.5):
    out = []
    for i, g in enumerate(grids):
        out.append((i * interval, tuple(g), sum(g) / 64.0))
    return out


def test_fingerprint_registry_has_tianxia_seed():
    seeds = adskip.load_fingerprints()
    names = [s["name"] for s in seeds]
    assert any("tianxia" in n for n in names)
    for s in seeds:
        assert len(s["grid"]) == 64
        assert len(s["mask"]) == 64


def test_fingerprint_registry_is_template_families_not_domains():
    # 指纹库按版式模板家族建（摸底口径 2026-11-03）：每条种子必
    # 带 family 且 kind=fullscreen_card；带家族（赞助发布/开云/
    # 世界杯投注）是压正片的字幕带、不在本试点范围，绝不播种。
    seeds = adskip.load_fingerprints()
    assert seeds
    for s in seeds:
        assert s["family"], s["name"]
        assert s["kind"] == "fullscreen_card"
    fams = {s["family"] for s in seeds}
    assert "fullscreen-tianxia" in fams
    assert "fullscreen-unext" in fams


def _unext_grid():
    for s in adskip.load_fingerprints():
        if s["family"] == "fullscreen-unext":
            return s["grid"]
    raise AssertionError("unext seed missing")


def test_analyze_unext_card_quarantined_until_reseeded():
    # U-NEXT 种子已被种子强度门隔离（墨迹格仅 2 格、无判别力）：
    # 2026-11-03 家宽狩猎实证它误中采昌发行 logo（置信 0.80 且
    # 真剪得动）、近黑正片镜头（0.79）等多类正片内容，与唯一真
    # 卡实例（0.82）无阈值可分。宁漏勿误：哪怕片头摆着与种子
    # 一模一样的卡，也只记 static_unknown、绝不剪；待更强签名
    # 重新播种后本例应改回判 ad。
    grid = _unext_grid()
    grids = [grid] * 12 + [tuple((i * 37 + j * 11) % 251 for j in range(64))
                           for i in range(6)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "static_unknown"
    assert res["skip_seconds"] is None
    assert adskip.match_fingerprint(grid) is None


def test_pure_black_head_never_matches_black_card():
    # 纯黑静止片头（淡入/黑场）与黑底卡大面积同黑：全格平均差
    # 只有约 6 也绝不许命中——墨迹门挡住（卡上的字不在）。
    black = tuple([2.0] * 64)
    assert adskip.match_fingerprint(black) is None
    grids = [black] * 14 + [tuple((i * 37 + j * 11) % 251 for j in range(64))
                            for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_hits_static_card_at_head():
    grid = _seed_grid()
    grids = [grid] * 14 + [tuple((i * 37 + j * 11) % 251 for j in range(64))
                            for i in range(6)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    # 14 帧 × 0.5s 静止卡 → 广告结束 7.0s
    assert res["ad_end"] == pytest.approx(7.0)
    assert res["skip_seconds"] == pytest.approx(7.0)
    assert res["confidence"] >= adskip.HIGH_CONFIDENCE


def test_analyze_black_lead_then_card_counts_black_in_skip():
    grid = _seed_grid()
    black = tuple([2.0] * 64)
    grids = [black, black] + [grid] * 12 + \
        [tuple((i * 53 + j * 7) % 239 for j in range(64)) for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    # 黑场 1.0s + 卡 6.0s → 跳过 7.0s
    assert res["skip_seconds"] == pytest.approx(7.0)


def test_analyze_moving_head_is_clean():
    grids = [tuple((i * 37 + j * 11) % 251 for j in range(64))
             for i in range(20)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_static_unknown_never_trims():
    # 静止但指纹不认识（片头 logo 形态）→ 只记 unknown，绝不剪
    logo = tuple([180.0] * 64)
    grids = [logo] * 14 + [tuple((i * 37 + j * 11) % 251 for j in range(64))
                           for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "static_unknown"
    assert res["skip_seconds"] is None


def test_analyze_short_static_card_below_min_duration_is_clean():
    grid = _seed_grid()
    grids = [grid] * 3 + [tuple((i * 37 + j * 11) % 251 for j in range(64))
                          for i in range(8)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_insufficient_samples():
    res = adskip.analyze_head(_samples_from_grids([_seed_grid()] * 2))
    assert res["status"] == "insufficient"
    assert res["skip_seconds"] is None


# ------------------------------------------------ 全窗扫描（1.1 回归）


def _flash_white():
    # 近全白闪帧（曝光/闪光）：无可辨内容，《诅咒2025》片头第 0
    # 帧即此形态——旧版锚点被它占住，静止段 0.5s 即断、真卡够不着。
    return tuple([250.0] * 64)


def _logo_grid():
    # 非均匀的未知静止画面（片头 logo 形态）：与指纹库距离足够远。
    return tuple(205.0 if 24 <= j < 40 else (90.0 if j < 32 else 150.0)
                 for j in range(64))


def test_analyze_flash_frame_then_card_hits():
    grid = _seed_grid()
    grids = [_flash_white()] + [grid] * 12 + \
        [tuple((i * 37 + j * 11) % 251 for j in range(64))
         for i in range(6)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    # 闪帧 0.5s + 卡 6.0s → 跳过 6.5s（闪帧不是正片，一并计入）
    assert res["skip_seconds"] == pytest.approx(6.5)
    assert res["confidence"] >= adskip.HIGH_CONFIDENCE


def test_analyze_black_and_flash_lead_then_card_hits():
    grid = _seed_grid()
    black = tuple([2.0] * 64)
    grids = [black, black, _flash_white()] + [grid] * 12 + \
        [tuple((i * 53 + j * 7) % 239 for j in range(64))
         for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    # 黑场 1.0s + 闪帧 0.5s + 卡 6.0s → 跳过 7.5s
    assert res["skip_seconds"] == pytest.approx(7.5)


def test_analyze_dynamic_intro_then_card_hits():
    grid = _seed_grid()
    grids = [tuple((i * 37 + j * 11) % 251 for j in range(64))
             for i in range(8)] + [grid] * 12 + \
        [tuple((i * 53 + j * 7) % 239 for j in range(64))
         for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    # 动态片头 4.0s + 卡 6.0s → 广告止于 10.0s
    assert res["ad_start"] == pytest.approx(4.0)
    assert res["skip_seconds"] == pytest.approx(10.0)


def test_analyze_unknown_static_then_card_still_hits():
    # 前面有一段达标但不认识的静止段（logo），不能挡住后面真卡
    # 的判定：逐段验指纹，不许在第一段 unknown 上提前收工。
    grid = _seed_grid()
    grids = [_logo_grid()] * 8 + [grid] * 12 + \
        [tuple((i * 37 + j * 11) % 251 for j in range(64))
         for i in range(4)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "ad"
    assert res["skip_seconds"] == pytest.approx(10.0)


def test_analyze_clean_head_with_short_logos_stays_clean():
    # 纯净片头：黑场 + 两段短 logo（各 2.0s，未达静止段下限）+
    # 正片动态画面 → clean，绝不因全窗扫描把 logo 误当卡。
    black = tuple([2.0] * 64)
    grids = [black, black] + [_logo_grid()] * 4 + [black, black] + \
        [_logo_grid()] * 4 + \
        [tuple((i * 37 + j * 11) % 251 for j in range(64))
         for i in range(10)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_burned_in_band_head_is_clean():
    # 字幕带片头：画面本体逐帧在动、只有底部带是压印不动的——
    # 整屏静止段不存在，绝不许误判成整屏卡（带归选线降权管）。
    band = [210.0, 40.0, 200.0, 60.0, 190.0, 30.0, 220.0, 50.0,
            205.0, 45.0, 195.0, 55.0, 215.0, 35.0, 185.0, 65.0]

    def frame(i):
        cells = []
        for j in range(64):
            if j >= 48:
                cells.append(band[j - 48])
            else:
                cells.append(float((i * 67 + j * 29) % 251))
        return tuple(cells)

    grids = [frame(i) for i in range(24)]
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_slow_drift_segment_is_not_a_card():
    # 慢漂移段（台标光晕汇聚/渐亮）不许冒充静止卡。家宽实证
    # （2026-11-03《突如其来》）：diaphana 台标光晕汇聚段逐帧对
    # 段首都过静止双门、中位签名蹭中 U-NEXT 种子（距离 7.9、
    # 置信 0.75），但段首尾距离 7.61——它不是静止画面。本例用
    # 种子签名整体渐暗复现同型漂移：首尾距离 ≈9.2（逐帧对段首
    # 仍在静止门 10.0 之内、段照样成形；连贯门 5.0 之外）、中位
    # 离种子 ≈4.6（置信 0.86），没有连贯门必然误判 ad。
    seed = _seed_grid()
    grids = []
    for k in range(12):
        scale = 1.0 - 0.4 * k / 11
        grids.append(tuple(v * scale for v in seed))
    res = adskip.analyze_head(_samples_from_grids(grids))
    assert res["status"] == "clean"
    assert res["skip_seconds"] is None


def test_analyze_low_confidence_hit_is_not_recorded_as_ad():
    # 离指纹太远的「命中」不记 ad。家宽实证：FANDANGO 发行
    # logo 距 U-NEXT 种子 15.4（置信 0.52）曾被记成 ad——这种
    # 记录永远够不上剪除门槛、只污染台账。ad 的唯一用途是
    # 剪：够不上剪的标准，只配 static_unknown。
    seed = _seed_grid()
    noisy = tuple(
        v + (30.0 if idx % 2 == 0 else 0.0)
        for idx, v in enumerate(seed))
    res = adskip.analyze_head(_samples_from_grids([noisy] * 8))
    assert res["status"] == "static_unknown"
    assert res["skip_seconds"] is None


# ---------------------------------------------------------------- 清单摘段


def test_trim_exact_boundary_drops_whole_segments():
    out = adskip.trim_playlist(_media_playlist(10, 6.0), 12.0)
    assert out is not None
    text, stats = out
    assert stats["trimmed_segments"] == 2
    assert stats["trimmed_duration"] == pytest.approx(12.0)
    assert stats["extra_loss"] == pytest.approx(0.0)
    uris = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 8 and uris[0].endswith("seg002.ts")
    assert "#EXT-X-MEDIA-SEQUENCE:2" in text
    kept = sum(float(l[len("#EXTINF:"):].split(",")[0])
               for l in text.splitlines() if l.startswith("#EXTINF:"))
    assert kept == pytest.approx(60.0 - 12.0)


def test_trim_straddler_within_extra_loss_budget():
    out = adskip.trim_playlist(_media_playlist(10, 6.0), 17.0)
    assert out is not None
    text, stats = out
    # 第 3 段 12–18s，广告止 17s，多丢 1s ≤ 2s → 整段摘掉
    assert stats["trimmed_segments"] == 3
    assert stats["extra_loss"] == pytest.approx(1.0)
    uris = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert uris[0].endswith("seg003.ts")


def test_trim_straddler_beyond_extra_loss_budget_no_trim():
    # 广告止 13s，第 3 段要多丢 5s 正片 → 整条不剪
    assert adskip.trim_playlist(_media_playlist(10, 6.0), 13.0) is None


def test_trim_never_trims_everything_or_zero():
    assert adskip.trim_playlist(_media_playlist(3, 6.0), 17.0) is None
    assert adskip.trim_playlist(_media_playlist(10, 6.0), 0.0) is None
    assert adskip.trim_playlist(_media_playlist(10, 6.0), -1.0) is None


def test_proxy_media_response_with_skip_trims_then_absolutizes():
    def fake_get_text(url, headers, timeout):
        return 200, _media_playlist(10, 6.0)

    text = proxy.build_media_response_text(
        MEDIA_URL, {}, source_id=7, item_id=1,
        http_get_text=fake_get_text, skip_seconds=12.0)
    assert text is not None
    uris = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 8
    assert uris[0] == "https://vod.ffzy-test.com/2026/xyz/seg002.ts"


def test_proxy_media_response_without_skip_is_current_behavior():
    def fake_get_text(url, headers, timeout):
        return 200, _media_playlist(10, 6.0)

    plain = proxy.build_media_response_text(
        MEDIA_URL, {}, source_id=7, item_id=1, http_get_text=fake_get_text)
    skipped_zero = proxy.build_media_response_text(
        MEDIA_URL, {}, source_id=7, item_id=1,
        http_get_text=fake_get_text, skip_seconds=0.0)
    assert plain == skipped_zero
    uris = [l for l in plain.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 10


# ---------------------------------------------------------------- 记账/端点


def _guid():
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name == "广告剪除测试片").all()
    ids = [r.id for r in rows]
    if ids:
        mids = [m.id for m in db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)).all()]
        if mids:
            db.query(em.CmsAdSkipDetection).filter(
                em.CmsAdSkipDetection.mapping_id.in_(mids)
            ).delete(synchronize_session=False)
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    for s in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name == SRC_NAME).all():
        db.delete(s)
    db.query(em.Library).filter(em.Library.name == LIB_NAME).delete()
    for uname in (BOUND, FREE):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == u.id).delete()
            db.query(em.MountCredential).filter(
                em.MountCredential.owner_user_id == u.id).delete()
            db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    users = {}
    for uname in (BOUND, FREE):
        u = models.WebUser(username=uname, password_hash=hash_password(PW),
                           is_active=True, is_staff=True, emby_username=uname,
                           emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=users[BOUND], sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=users[BOUND], provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    src = em.UpstreamSource(
        id=9872, name=SRC_NAME, source_type="applecms",
        base_url="https://cms.example.com", auth_method="none",
        enabled=True, credential_scope="source", health_status="unknown")
    db.add(src)
    db.flush()
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="广告剪除测试片",
        file_path="mount://3/电影/广告剪除测试片 (2026)/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=600 * TICKS,
        probe_status="failed", probe_attempts=99)
    db.add(movie)
    db.flush()
    mapping = em.UpstreamItemMapping(
        local_item_id=movie.id, source_id=src.id,
        upstream_item_id="vod-7", confidence=0.95, evidence="tmdb")
    db.add(mapping)
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "src_id": src.id, "movie": movie.guid, "movie_id": movie.id,
            "mapping_id": mapping.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}AdSkipTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}adskip-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(client, seed):
    return _token(client, BOUND)


def _h(token):
    return {"X-Emby-Token": token}


def _line_url(client, token, guid):
    r = client.get(f"/emby/Items/{guid}", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    lines = [s for s in body.get("MediaSources") or []
             if ":upstream" in str(s.get("Id", ""))]
    assert lines, body.get("MediaSources")
    return lines[0]["DirectStreamUrl"]


def _path_of(url):
    return re.sub(r"^https?://[^/]+", "", url)


@pytest.fixture()
def skip_env(monkeypatch, seed):
    """解析/抓清单全打桩 + 试点源指向种子源 + 后台检测关掉（不启线程）。"""
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("CMS_AD_SKIP_SOURCE_IDS", str(seed["src_id"]))
    monkeypatch.setattr(adskip, "schedule_detection", lambda *a, **k: False)

    def fake_resolve(db, local_item_id, **kwargs):
        return MEDIA_URL

    def fake_get_text(url, headers, timeout):
        return 200, _media_playlist(10, 6.0)

    monkeypatch.setattr(up, "resolve_mapped_upstream_stream", fake_resolve)
    monkeypatch.setattr(up, "_default_http_get_text", fake_get_text)
    db = SessionLocal()
    try:
        db.query(em.CmsAdSkipDetection).filter(
            em.CmsAdSkipDetection.mapping_id == seed["mapping_id"]
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return seed


def _put_record(seed, **kw):
    db = SessionLocal()
    try:
        adskip.upsert_detection(
            db, mapping_id=seed["mapping_id"],
            local_item_id=seed["movie_id"], source_id=seed["src_id"], **kw)
        db.commit()
    finally:
        db.close()


def test_stream_trims_head_ad_with_high_confidence_record(
        client, bound_token, seed, skip_env):
    _put_record(seed, status="ad", skip_seconds=12.0, confidence=0.95,
                ad_start=0.0, ad_end=12.0)
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 200, r.text
    uris = [l for l in r.text.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 8
    assert uris[0] == "https://vod.ffzy-test.com/2026/xyz/seg002.ts"


def test_stream_302_when_record_low_confidence(
        client, bound_token, seed, skip_env):
    _put_record(seed, status="ad", skip_seconds=12.0, confidence=0.40,
                ad_start=0.0, ad_end=12.0)
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MEDIA_URL


def test_stream_302_when_record_clean(client, bound_token, seed, skip_env):
    _put_record(seed, status="clean", skip_seconds=None, confidence=0.0)
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MEDIA_URL


def test_stream_302_when_no_record(client, bound_token, seed, skip_env):
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MEDIA_URL


def test_stream_302_when_master_switch_off(
        client, bound_token, seed, skip_env, monkeypatch):
    monkeypatch.setenv("CMS_AD_SKIP_ENABLED", "0")
    _put_record(seed, status="ad", skip_seconds=12.0, confidence=0.95,
                ad_start=0.0, ad_end=12.0)
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MEDIA_URL


def test_admin_detection_lookup_endpoint(seed, skip_env):
    from backend.api import admin_upstream_sources as admin_ups

    _put_record(seed, status="ad", skip_seconds=12.0, confidence=0.95,
                ad_start=0.0, ad_end=12.0, seed_name="tianxia-club-66109",
                evidence_path="/tmp/ad_skip_evidence/mapping_x.jpg")
    staff = SimpleNamespace(id=1, is_staff=True, username="admin")
    db = SessionLocal()
    try:
        payload = admin_ups.get_ad_skip_detection(
            seed["mapping_id"], staff=staff, db=db)
    finally:
        db.close()
    assert payload["mapping_id"] == seed["mapping_id"]
    assert payload["status"] == "ad"
    assert payload["skip_seconds"] == 12.0
    assert payload["evidence_path"].endswith("mapping_x.jpg")

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            admin_ups.get_ad_skip_detection(99999999, staff=staff, db=db)
        assert exc.value.status_code == 404
    finally:
        db.close()
