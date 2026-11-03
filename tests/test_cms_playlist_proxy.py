"""CMS 清单代理去广告（播放优化 C 件）回归。

锁住的口径（2026-11-02 站主批准全套之③，证据见采样报告
applecms-playback-sampling-20261102）：

- 唯一上线规则：暴风（源 id=9）媒体清单里路径含 /video/adjump/
  的广告段整段删除（连同其专属 EXTINF 行）；其余形态（DISCONTINUITY
  包夹、短段、异域名、CUE）一律不许当规则——本文件用如意形态的
  高频 DISCONTINUITY 清单断言「不误杀」。
- 正片分段计数与顺序不变，总时长减少量与广告组时长吻合；改写后
  分段 URL 全部为原 CDN 绝对地址（播放器直连，本服务不代理分段）。
- 主清单 variant 改写为本站二级端点地址，定位参数可被二级端点
  正确消费且篡改即拒。
- 回落铁律：代理抓取失败 → 302 直发原地址；开关关 → 302；
  无规则源 → 302；未绑定/签名无效 → 既有门禁照常拦。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import cms_playlist_proxy as proxy
from backend.emby_server import models as em
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "CmsProxy#2026"
BOUND = "cmsproxy_bound"
FREE = "cmsproxy_free"
LIB_NAME = "清单代理测试库"
SRC_NAME = "清单代理测试源"
MASTER_URL = "https://v.fengbao14.com/video/ceshipian/hash123/index.m3u8"
FEIFAN_MASTER_URL = "https://vod.ffzy-bofang.com/2026/abc_def/index.m3u8"

AD_DURATIONS = [3.0] * 8 + [1.76]


def _media_playlist(content_count=6, ad_groups=1):
    """暴风样本同构媒体清单：顺序编号正片 + adjump 广告组（DISCONTINUITY 包夹）。"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    idx = 0
    for g in range(ad_groups + 1):
        take = content_count // (ad_groups + 1) if g < ad_groups else \
            content_count - (content_count // (ad_groups + 1)) * ad_groups
        for _ in range(take):
            lines.append("#EXTINF:3,")
            lines.append(f"{idx:07d}.ts")
            idx += 1
        if g < ad_groups:
            lines.append("#EXT-X-DISCONTINUITY")
            for i, d in enumerate(AD_DURATIONS):
                lines.append(f"#EXTINF:{d},")
                lines.append(f"/video/adjump/time/1793281984887000000{i}.ts")
            lines.append("#EXT-X-DISCONTINUITY")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=1920x1080\n"
    "3000k/hls/mixed.m3u8\n"
)


# ---------------------------------------------------------------- 纯函数


def test_filter_drops_adjump_group_keeps_content_order_and_duration():
    text = _media_playlist(content_count=6, ad_groups=2)
    new_text, stats = proxy.filter_media_playlist(
        text, MASTER_URL, ("/video/adjump/",))
    assert "adjump" not in new_text
    assert stats["dropped_segments"] == 18
    assert stats["kept_segments"] == 6
    assert stats["dropped_duration"] == pytest.approx(2 * 25.76, abs=0.01)
    kept_uris = [l for l in new_text.splitlines() if not l.startswith("#")]
    assert kept_uris == [
        f"https://v.fengbao14.com/video/ceshipian/hash123/{i:07d}.ts"
        for i in range(6)
    ]


def test_filter_rewrites_all_segments_to_absolute_cdn_urls():
    text = _media_playlist(content_count=3, ad_groups=1)
    new_text, _ = proxy.filter_media_playlist(
        text, MASTER_URL, ("/video/adjump/",))
    uris = [l for l in new_text.splitlines() if not l.startswith("#")]
    assert uris and all(u.startswith("https://v.fengbao14.com/") for u in uris)


def test_filter_without_markers_keeps_everything():
    text = _media_playlist(content_count=4, ad_groups=1)
    new_text, stats = proxy.filter_media_playlist(text, MASTER_URL, ())
    assert stats["dropped_segments"] == 0
    assert stats["kept_segments"] == 13
    assert "adjump" in new_text  # 无规则不下刀：原样保留（含广告）


def test_discontinuity_alone_is_not_a_filter_rule():
    # 如意形态反证：DISCONTINUITY 每几段一个是编码分块，绝不许误删。
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6"]
    for i in range(12):
        if i % 6 == 0:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append("#EXTINF:3,")
        lines.append(f"seg{i:03d}.ts")
    new_text, stats = proxy.filter_media_playlist(
        "\n".join(lines) + "\n", MASTER_URL, ("/video/adjump/",))
    assert stats["kept_segments"] == 12
    assert stats["dropped_segments"] == 0


def test_real_baofeng_sample_counts():
    sample = Path(
        "~/workspace/reports/applecms-playlist-samples-20261102/"
        "baofeng_vod165487_master.m3u8").expanduser()
    if not sample.exists():
        pytest.skip("采样原始清单不在本机（CI 无此文件）")
    text = sample.read_text(encoding="utf-8")
    base = "https://v.fengbao14.com/video/xunzongzhe/hash/index.m3u8"
    new_text, stats = proxy.filter_media_playlist(
        text, base, ("/video/adjump/",))
    assert "adjump" not in new_text
    assert stats["dropped_segments"] == 18
    assert stats["kept_segments"] == 855 - 18
    assert stats["dropped_duration"] == pytest.approx(2 * 25.76, abs=0.01)


def test_master_rewrite_and_locator_roundtrip():
    seen = {}

    def maker(absolute):
        seen["absolute"] = absolute
        loc = proxy.make_locator(42, "guid123", 9999999999, absolute)
        return f"https://aetrix.example/emby/Videos/guid123/upstream/playlist?u={loc}"

    out = proxy.rewrite_master_playlist(MASTER_PLAYLIST, FEIFAN_MASTER_URL, maker)
    assert seen["absolute"] == \
        "https://vod.ffzy-bofang.com/2026/abc_def/3000k/hls/mixed.m3u8"
    variant_line = [l for l in out.splitlines() if not l.startswith("#")][0]
    loc = parse_qs(urlsplit(variant_line).query)["u"][0]
    assert proxy.read_locator(loc, 42, "guid123", 9999999999) == seen["absolute"]
    # 篡改一位即拒；换用户/条目也拒
    bad = loc[:-1] + ("0" if loc[-1] != "0" else "1")
    assert proxy.read_locator(bad, 42, "guid123", 9999999999) is None
    assert proxy.read_locator(loc, 43, "guid123", 9999999999) is None
    assert proxy.read_locator(loc, 42, "other", 9999999999) is None


def test_engages_only_rule_source_and_m3u8(monkeypatch):
    monkeypatch.delenv("CMS_PLAYLIST_PROXY_ENABLED", raising=False)
    assert proxy.engages(9, MASTER_URL) is True
    assert proxy.engages(7, MASTER_URL) is False
    assert proxy.engages(13, MASTER_URL) is False
    assert proxy.engages(9, "https://x.example.com/movie.mp4") is False
    monkeypatch.setenv("CMS_PLAYLIST_PROXY_ENABLED", "0")
    assert proxy.engages(9, MASTER_URL) is False
    assert proxy.playlist_proxy_enabled() is False


def test_build_master_falls_back_to_none_on_fetch_failure():
    def boom(url, headers, timeout):
        return None, None

    out = proxy.build_master_response_text(
        MASTER_URL, {}, lambda u: u, source_id=9, item_id=1,
        http_get_text=boom)
    assert out is None


# ---------------------------------------------------------------- 端点


def _guid():
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name == "清单代理测试片").all()
    ids = [r.id for r in rows]
    if ids:
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
    # 直插源行（绕开 create_source 的 CMS 总闸口径，与
    # test_applecms_followup 等同形态）；id 取高位避免与它模块种子撞。
    src = em.UpstreamSource(
        id=9871, name=SRC_NAME, source_type="applecms",
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
        name="清单代理测试片",
        file_path="mount://3/电影/清单代理测试片 (2026)/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=118 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    db.add(movie)
    db.flush()
    db.add(em.UpstreamItemMapping(
        local_item_id=movie.id, source_id=src.id,
        upstream_item_id="vod-1", confidence=0.95, evidence="tmdb"))
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "src_id": src.id, "movie": movie.guid}
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
    return (f"MediaBrowser Client={q}ProxyTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}proxy-dev-1{q}, Version={q}1.0{q}")


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


@pytest.fixture(scope="module")
def free_token(client, seed):
    return _token(client, FREE)


def _h(token):
    return {"X-Emby-Token": token}


def _detail(client, token, guid):
    r = client.get(f"/emby/Items/{guid}", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


def _line_url(client, token, guid):
    body = _detail(client, token, guid)
    lines = [s for s in body.get("MediaSources") or []
             if ":upstream" in str(s.get("Id", ""))]
    assert lines, body.get("MediaSources")
    return lines[0]["DirectStreamUrl"]


def _path_of(url):
    return re.sub(r"^https?://[^/]+", "", url)


@pytest.fixture()
def proxy_env(monkeypatch, seed):
    """规则源指向种子源 + resolve/抓清单全打桩（不发真网络）。"""
    monkeypatch.setitem(proxy.SEGMENT_PATH_FILTERS, seed["src_id"],
                        ("/video/adjump/",))
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    state = {"resolved": MASTER_URL, "fetch": None}

    def fake_resolve(db, local_item_id, **kwargs):
        return state["resolved"]

    def fake_get_text(url, headers, timeout):
        if state["fetch"] is not None:
            return state["fetch"](url)
        if url == MASTER_URL:
            return 200, _media_playlist(content_count=6, ad_groups=2)
        if url == FEIFAN_MASTER_URL:
            return 200, MASTER_PLAYLIST
        # variant 媒体清单
        return 200, _media_playlist(content_count=4, ad_groups=1)

    monkeypatch.setattr(up, "resolve_mapped_upstream_stream", fake_resolve)
    monkeypatch.setattr(up, "_default_http_get_text", fake_get_text)
    return state


def test_stream_media_playlist_filtered_via_proxy(
        client, bound_token, seed, proxy_env):
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "adjump" not in r.text
    uris = [l for l in r.text.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 6
    assert all(u.startswith("https://v.fengbao14.com/") for u in uris)


def test_stream_master_rewritten_then_media_endpoint_filters(
        client, bound_token, seed, proxy_env):
    proxy_env["resolved"] = FEIFAN_MASTER_URL
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "#EXT-X-STREAM-INF" in r.text
    variant = [l for l in r.text.splitlines()
               if l and not l.startswith("#")][0]
    assert "/upstream/playlist" in variant
    assert "u=" in variant and "sign=" in variant
    r2 = client.get(_path_of(variant), headers=_h(bound_token),
                    follow_redirects=False)
    assert r2.status_code == 200, r2.text
    assert "adjump" not in r2.text
    uris = [l for l in r2.text.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 4
    assert all(u.startswith("https://vod.ffzy-bofang.com/") for u in uris)


def test_media_endpoint_rejects_tampered_locator(
        client, bound_token, seed, proxy_env):
    proxy_env["resolved"] = FEIFAN_MASTER_URL
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    variant = [l for l in r.text.splitlines()
               if l and not l.startswith("#")][0]
    bad = variant[:-1] + ("0" if variant[-1] != "0" else "1")
    r2 = client.get(_path_of(bad), headers=_h(bound_token),
                    follow_redirects=False)
    assert r2.status_code == 403, r2.text


def test_stream_falls_back_to_302_when_playlist_fetch_fails(
        client, bound_token, seed, proxy_env):
    proxy_env["fetch"] = lambda url: (None, None)
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MASTER_URL


def test_stream_302_when_switch_off(client, bound_token, seed, proxy_env,
                                   monkeypatch):
    monkeypatch.setenv("CMS_PLAYLIST_PROXY_ENABLED", "0")
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MASTER_URL


def test_stream_302_for_source_without_rule(
        client, bound_token, seed, proxy_env, monkeypatch):
    monkeypatch.delitem(proxy.SEGMENT_PATH_FILTERS, seed["src_id"])
    url = _line_url(client, bound_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == MASTER_URL


def test_stream_unbound_user_blocked_by_existing_gate(
        client, free_token, seed, proxy_env):
    url = _line_url(client, free_token, seed["movie"])
    r = client.get(_path_of(url), headers=_h(free_token),
                   follow_redirects=False)
    assert r.status_code == 403, r.text
    assert "location" not in r.headers


def test_stream_bad_play_sign_rejected(client, bound_token, seed, proxy_env):
    url = _line_url(client, bound_token, seed["movie"])
    bad = re.sub(r"sign=[0-9a-f]+", "sign=" + "0" * 32, url)
    assert bad != url
    r = client.get(_path_of(bad), headers=_h(bound_token),
                   follow_redirects=False)
    assert r.status_code == 403, r.text
