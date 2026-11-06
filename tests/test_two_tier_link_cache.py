# -*- coding: utf-8 -*-
"""两层链接缓存回归测试（feat/two-tier-link-cache）。

覆盖不变量：
1. URL 到期解析与提前 5 分钟失效：
   - 115 直链 ``?t=<unix_epoch>`` 与通用 ``?expires=<unix_epoch>`` 均识别（含毫秒纪元），
     TTL = ``expiry - now - 300``；
   - 剩余 ≤ 5 分钟或已过期时返回 0.0，不写入第二层缓存（不把临期/过期链喂进缓存）；
   - 无 ``t=``/``expires=``、非数字或 ``?t=1`` 类小整数旗标时退回 ``3600s``；
   - 超长有效期（如线上实测 115 直链 ``t`` 约 +5~6.4 天）受 ``RESOLVE_TARGET_CACHE_TTL_SEC``
     （默认可配到 48h = 172800s）封顶。
2. 两层分层命中（进程内 ``Pan115Mount`` + 生产 ``P115SidecarMount``/``P115SidecarHandler`` + ``DriveMount``）：
   - 第二层（直链层）在 ``t - 300s`` 内命中：0 次目录定位、0 次取链；
   - 超过 ``t - 300s`` 但未超 48h：第二层提前自然过期（不吃 403），第一层路径→pickcode/file_id
     命中，跳过逐级目录 ``list``（0 次目录定位），只发 1 次 ``download_url`` 换新直链；
   - 超过 48h：第一层亦过期，重新走完整目录定位 + 取链。
3. 文件移动/删除兜底失效：
   - 中继收到上游 403 / 404 / 410 时，``streaming.serve_mount_relay_async`` 连带同时清掉
     第二层直链缓存与第一层 48h 路径→ID 缓存，下一次解析立即重走目录定位拿到新 pickcode；
   - 按 ``pickcode`` 直取失败（非冷却期）时自动淘汰第一层陈旧映射，下次解析自愈；
   - 账号处于 ``pan115_guard`` 冷却期时保留两层缓存不误清（Z-2 一致性）。
4. 零凭据红线：
   - 两层缓存（``_PATH_ID_CACHE``、``_RESOLVE_CACHE``、``_pan115_url_cache``）的键与值均不含
     Cookie / Bearer / Authorization 明文；轮换后命中现挂最新凭据。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.emby_server import drive_auth
from backend.emby_server import models as em
from backend.emby_server import mount_drive
from backend.emby_server import mounts as mnt
from backend.emby_server import pan115_guard
from backend.emby_server import streaming
from backend.emby_server.mount_sidecar import P115SidecarMount
from backend.emby_server.p115_sidecar import P115SidecarHandler
from backend.emby_server.provider_runtime import RuntimeSession


@pytest.fixture(autouse=True)
def _clean_all_caches(monkeypatch):
    mnt.reset_resolve_cache()
    with mnt._pan115_url_cache_lock:
        mnt._pan115_url_cache.clear()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mnt, "RESOLVE_TARGET_CACHE_TTL_SEC", 172800)
    yield
    mnt.reset_resolve_cache()
    with mnt._pan115_url_cache_lock:
        mnt._pan115_url_cache.clear()


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


def _mount(mount_id=7, mount_type="115", config=None, enabled=True, path=""):
    m = em.StorageMount(
        name=f"m-{mount_id}",
        mount_type=mount_type,
        path=path,
        config=mnt.dump_config(config or {"cid": "0"}),
        is_enabled=enabled,
    )
    m.id = mount_id
    return m


# ---------------- 1. URL 过期时间解析与提前 5 分钟失效 ----------------


def test_compute_url_ttl_parses_115_t_and_subtracts_5_minutes():
    now = 1_793_924_524.0
    exp = int(now + 3600)
    url = (
        f"https://cdnfhnfile.115cdn.net/abc/movie.mp4"
        f"?t={exp}&u=12345&s=1024&d=vip&c=2&f=3&k=deadbeef&us=1024&uc=10&v=1"
    )
    assert mnt.parse_url_expiry_epoch(url) == float(exp)
    assert mnt.compute_url_cache_ttl(url, now_epoch=now) == pytest.approx(3300.0)


def test_compute_url_ttl_parses_expires_and_ms_epoch():
    now = 1_793_924_524.0
    exp_sec = int(now + 7200)
    url_sec = f"https://cdn.example.com/v.mkv?Expires={exp_sec}&sig=xyz"
    assert mnt.compute_url_cache_ttl(url_sec, now_epoch=now) == pytest.approx(6900.0)

    exp_ms = (int(now) + 1800) * 1000
    url_ms = f"https://cdn.example.com/v.mkv?t={exp_ms}"
    assert mnt.compute_url_cache_ttl(url_ms, now_epoch=now) == pytest.approx(1500.0)


def test_compute_url_ttl_within_5m_or_expired_returns_zero():
    now = 1_793_924_524.0
    # 恰剩 300s（进入 5 分钟安全余量）与已过期均返回 0，绝不退回 3600
    url_300 = f"https://cdnfhnfile.115cdn.net/a.mp4?t={int(now + 300)}&k=x"
    url_200 = f"https://cdnfhnfile.115cdn.net/a.mp4?t={int(now + 200)}&k=x"
    url_past = f"https://cdnfhnfile.115cdn.net/a.mp4?t={int(now - 60)}&k=x"
    assert mnt.compute_url_cache_ttl(url_300, now_epoch=now) == 0.0
    assert mnt.compute_url_cache_ttl(url_200, now_epoch=now) == 0.0
    assert mnt.compute_url_cache_ttl(url_past, now_epoch=now) == 0.0


def test_compute_url_ttl_unparseable_or_flag_falls_back_to_3600():
    now = 1_793_924_524.0
    assert mnt.compute_url_cache_ttl("https://cdn.example.com/a.mp4", now_epoch=now) == 3600.0
    assert mnt.compute_url_cache_ttl("https://cdn.example.com/a.mp4?t=abc", now_epoch=now) == 3600.0
    # ?t=1 类小整数不是 Unix 纪元时间戳，不误判为过期，退回 3600
    assert mnt.compute_url_cache_ttl("https://cdn.example.com/a.mp4?t=1", now_epoch=now) == 3600.0


def test_long_115_t_capped_at_48h_resolve_ttl(monkeypatch):
    """线上实测 115 直链 t 距当前约 +550208s（6.37 天）：受 48h 配置上限封顶。"""
    base_wall = 1_793_924_524.0
    base_mono = 10_000.0
    monkeypatch.setattr(mnt.time, "time", lambda: base_wall)
    monkeypatch.setattr(mnt.time, "monotonic", lambda: base_mono)

    t_6days = int(base_wall + 550_208)
    stored = mnt.PlayTarget(
        "url",
        f"https://cdnfhnfile.115cdn.net/a.mp4?t={t_6days}&k=sig",
        {"X-Pickcode": "pc-long"},
    )
    key = (7, "115", "fp", "/a.mkv")
    mnt._path_id_cache_put(key, "pc-long")
    mnt._resolve_cache_put(key, stored)

    with mnt._RESOLVE_CACHE_LOCK:
        url_exp_mono, _ = mnt._RESOLVE_CACHE[key]
        id_exp_mono, _ = mnt._PATH_ID_CACHE[key]
    assert url_exp_mono - base_mono == pytest.approx(172800.0)
    assert id_exp_mono - base_mono == pytest.approx(172800.0)


def test_near_expiry_url_not_cached_in_tier2_but_tier1_keeps_pickcode(monkeypatch):
    """直链剩余 <5 分钟时不进第二层（防下次吃 403），第一层仍保留 pickcode 48h。"""
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    now_wall = 1_793_924_524.0
    monkeypatch.setattr(mnt.time, "time", lambda: now_wall)

    calls = {"locate": 0, "download": 0}

    def _locate(self, rel):
        calls["locate"] += 1
        return {"pickcode": "pc-short", "name": "a.mkv", "size": 10}

    def _client(self):
        def download_url(pickcode):
            calls["download"] += 1
            # 第一次只剩 120s（<300s 余量），第二次给足 3600s
            exp = int(now_wall + (120 if calls["download"] == 1 else 3600))
            return f"https://cdnfhnfile.115cdn.net/a.mp4?t={exp}&n={calls['download']}"

        return SimpleNamespace(download_url=download_url)

    monkeypatch.setattr(mnt.Pan115Mount, "_locate", _locate)
    monkeypatch.setattr(mnt.Pan115Mount, "_client", _client)
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "secret-cookie-1")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_type="115")
    db = _FakeDB([mount])

    t1 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "n=1" in t1.value
    stats1 = mnt.resolve_cache_stats()
    assert stats1["entries"] == 0, "剩余 <5m 的直链不得写入第二层缓存"
    assert stats1["path_id_entries"] == 1, "第一层路径→pickcode 应照常缓存 48h"

    t2 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "n=2" in t2.value
    assert calls["locate"] == 1, "第二次应命中第一层 pickcode，不重走目录定位"
    assert calls["download"] == 2, "直链未缓存故重取新直链"
    assert mnt.resolve_cache_stats()["entries"] == 1


# ---------------- 2. 两层时间线：进程内 Pan115Mount 与生产 Sidecar 路径 ----------------


def test_inproc_115_two_tier_timeline_skips_locate_until_48h(monkeypatch):
    """进程内 115：直链在 t-5m 提前过期后只重调 download_url，48h 内不重调 _locate。"""
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    clock = {"wall": 1_793_924_524.0, "mono": 50_000.0}
    monkeypatch.setattr(mnt.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(mnt.time, "monotonic", lambda: clock["mono"])

    calls = {"locate": 0, "download": 0}

    def _locate(self, rel):
        calls["locate"] += 1
        return {"pickcode": "pc-timeline", "name": "a.mkv", "size": 100}

    def _client(self):
        def download_url(pickcode):
            calls["download"] += 1
            exp = int(clock["wall"] + 1800)  # 30m 有效期 → 缓存 25m (1500s)
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mp4?t={exp}&seq={calls['download']}"

        return SimpleNamespace(download_url=download_url)

    monkeypatch.setattr(mnt.Pan115Mount, "_locate", _locate)
    monkeypatch.setattr(mnt.Pan115Mount, "_client", _client)
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "ck-secret-A")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_type="115")
    db = _FakeDB([mount])

    # T=0：冷启动，locate=1, download=1
    r1 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert calls == {"locate": 1, "download": 1}
    assert "seq=1" in r1.value

    # T=+20m (1200s < 1500s)：第二层直链命中，0 新增调用
    clock["wall"] += 1200
    clock["mono"] += 1200
    r2 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert r2.value == r1.value
    assert calls == {"locate": 1, "download": 1}

    # T=+26m (1560s > 1500s，距真实过期 1800s 仍有 4 分钟，不吃 403)：
    # 第二层过期，第一层 48h 命中 → locate 仍为 1，download 变 2
    clock["wall"] += 360
    clock["mono"] += 360
    r3 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "seq=2" in r3.value
    assert calls == {"locate": 1, "download": 2}
    assert mnt.resolve_cache_stats()["path_id_hits"] == 1

    # T=+48h+10s：第一层也过期 → locate 变 2，download 变 3
    clock["wall"] += 172810
    clock["mono"] += 172810
    r4 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "seq=3" in r4.value
    assert calls == {"locate": 2, "download": 3}


def test_sidecar_115_two_tier_skips_list_dir_via_pickcode_fastpath(monkeypatch):
    """生产默认 sidecar 路径：直链在 t-5m 过期后，P115SidecarMount 传缓存 pickcode
    直调 P115SidecarHandler.resolve_play_target，即使 sidecar 300s 目录缓存早已过期，
    也 0 次 list_dir、只调 1 次 download_url。
    """
    clock = {"wall": 1_793_924_524.0, "mono": 80_000.0}
    monkeypatch.setattr(mnt.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(mnt.time, "monotonic", lambda: clock["mono"])

    calls = {"list_dir": 0, "download": []}

    class _Fake115Client:
        def __init__(self, cookie, ua=""):
            self.cookie = cookie
            self.ua = ua

        def list_dir(self, cid, deadline=None):
            calls["list_dir"] += 1
            if str(cid) == "0":
                return [{"cid": "10", "name": "电影", "is_dir": True}]
            if str(cid) == "10":
                return [{"cid": "11", "name": "a.mkv", "is_dir": False,
                         "pickcode": "pc-sidecar-99", "size": 999}]
            return []

        def download_url(self, pickcode, deadline=None):
            calls["download"].append(pickcode)
            exp = int(clock["wall"] + 1800)  # 30m 过期 → 提前 5m 在 25m 失效
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mp4?t={exp}&n={len(calls['download'])}"

    handler = P115SidecarHandler(client_factory=_Fake115Client)

    class _InprocRuntimeBridge:
        """直连真实 P115SidecarHandler（绕开 Windows 无 AF_UNIX 限制，验证真实签名与分发）。"""

        def handshake(self, required_extensions=None):
            return {
                "protocol": "external-provider-runtime-v1",
                "provider": "p115",
                "capabilities": ["list_page", "open_range", "resolve_play_target"],
                "extensions": list(handler.extensions),
            }

        def session_upsert(self, session_id, data):
            handler.session_upsert(session_id, data)
            return {"ok": True}

        def resolve_play_target(self, session_id: str, path: str,
                                *, pickcode: str | None = None) -> dict:
            sess = RuntimeSession(session_id=session_id, data={})
            return handler.resolve_play_target(sess, path, pickcode=pickcode)

        def close(self):
            pass

    mount = _mount(mount_id=9, mount_type="115", config={"cid": "0"})
    bridge = _InprocRuntimeBridge()

    def _build_sidecar(m, db, library=None):
        return P115SidecarMount(m, db, library, client=bridge, cookie_override="ck-sidecar-secret")

    monkeypatch.setattr(mnt, "load_mount", lambda db, mid: mount)
    monkeypatch.setattr(mnt, "build_provider", _build_sidecar)

    # T=0：首播冷解析 → list_dir 走两级目录(2 次) + download_url 1 次
    t1 = mnt.resolve_play_target("mount://9/电影/a.mkv", None)
    assert calls["list_dir"] == 2
    assert calls["download"] == ["pc-sidecar-99"]
    assert t1.headers["Cookie"] == "ck-sidecar-secret"

    # 清空 sidecar 的 300s 目录缓存（模拟已过去数小时）
    handler._dir_cache.clear()
    handler._cid_cache.clear()

    # T=+26m (1560s)：第二层直链已提前 5m 失效，第一层 48h pickcode 命中
    clock["wall"] += 1560
    clock["mono"] += 1560
    t2 = mnt.resolve_play_target("mount://9/电影/a.mkv", None)
    assert "n=2" in t2.value
    assert calls["list_dir"] == 2, "第一层 48h pickcode 命中时不得再列 115 目录"
    assert calls["download"] == ["pc-sidecar-99", "pc-sidecar-99"]


def test_drive_two_tier_keeps_file_id_for_48h_after_3600s_url_fallback(monkeypatch):
    """Drive 直链无 t=/expires=：第二层按 3600s 退回过期，第一层 file_id 保活 48h 省掉 _locate。"""
    clock = {"mono": 20_000.0}
    monkeypatch.setattr(mnt.time, "monotonic", lambda: clock["mono"])

    holder = {"locate": 0, "token": "tok-1"}
    monkeypatch.setattr(
        mount_drive.DriveMount, "_locate",
        lambda self, rel: (holder.__setitem__("locate", holder["locate"] + 1)
                           or SimpleNamespace(file_id="DRIVE_FID_48H")))
    monkeypatch.setattr(
        drive_auth, "drive_auth_headers",
        lambda url, affinity_key=None: {"Authorization": f"Bearer {holder['token']}"})

    mount = _mount(mount_id=11, mount_type="drive", path="drive:SHARED", config={})
    db = _FakeDB([mount])

    r1 = mnt.resolve_play_target("mount://11/片/a.mkv", db)
    assert holder["locate"] == 1
    assert r1.headers["Authorization"] == "Bearer tok-1"

    # +2h (7200s > 3600s fallback，但 < 48h)：第二层过期，第一层 file_id 命中
    clock["mono"] += 7200
    holder["token"] = "tok-2"
    r2 = mnt.resolve_play_target("mount://11/片/a.mkv", db)
    assert holder["locate"] == 1, "48h 内 Drive 不得重复走 _locate 列目录"
    assert r2.headers["Authorization"] == "Bearer tok-2"


# ---------------- 3. 403 / 404 / 410 连带失效兜底（文件移动/删除） ----------------


class _StatusHandler(BaseHTTPRequestHandler):
    code = 403

    def do_GET(self):
        self.send_response(type(self).code)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"dead")

    def log_message(self, *args):
        pass


@pytest.fixture()
def status_upstream():
    srv = HTTPServer(("127.0.0.1", 0), _StatusHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _relay_req():
    return Request({
        "type": "http", "method": "GET", "path": "/Videos/1/stream",
        "raw_path": b"/Videos/1/stream", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000), "root_path": "",
    })


@pytest.mark.parametrize("status_code", [403, 404, 410])
def test_relay_403_404_410_evicts_both_tiers_and_relocates_moved_file(
    monkeypatch, status_upstream, status_code
):
    """文件在 115 上移动/替换后上游返回 403/404/410：两层缓存同时清空，下次立即重定位新 pickcode。"""
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    state = {"pickcode": "pc-before-move", "locate": 0}

    def _locate(self, rel):
        state["locate"] += 1
        return {"pickcode": state["pickcode"], "name": "a.mkv", "size": 10}

    monkeypatch.setattr(mnt.Pan115Mount, "_locate", _locate)
    monkeypatch.setattr(
        mnt.Pan115Mount, "_client",
        lambda self: SimpleNamespace(
            download_url=lambda pc: f"https://cdnfhnfile.115cdn.net/{pc}.mp4?t={int(time.time() + 7200)}"
        ),
    )
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "ck-1")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_type="115")
    db = _FakeDB([mount])

    first = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "pc-before-move" in first.value
    assert mnt.resolve_cache_stats()["entries"] == 1
    assert mnt.resolve_cache_stats()["path_id_entries"] == 1

    # 模拟上游文件已被替换成新 pickcode，旧直链在中继时返回 403/404/410
    state["pickcode"] = "pc-after-move"
    _StatusHandler.code = status_code
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            streaming.serve_mount_relay_async(
                f"{status_upstream}/dead.mp4",
                _relay_req(),
                {"User-Agent": "ua-115", "Cookie": "ck-1", "X-Pickcode": "pc-before-move"},
                "video/mp4",
            )
        )
    assert exc_info.value.status_code == status_code
    assert mnt.resolve_cache_stats()["entries"] == 0
    assert mnt.resolve_cache_stats()["path_id_entries"] == 0, "403/404/410 必须连带清掉 48h 路径→ID 层"

    second = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert "pc-after-move" in second.value
    assert state["locate"] == 2


def test_by_id_error_evicts_stale_path_id_unless_in_cooldown(monkeypatch):
    """第一层命中但按旧 pickcode 换链报错：非冷却期淘汰第一层映射；冷却期（Z-2）保留。"""
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    clock = {"wall": 1_793_924_524.0, "mono": 90_000.0}
    monkeypatch.setattr(mnt.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(mnt.time, "monotonic", lambda: clock["mono"])

    state = {"pickcode": "pc-v1", "fail_pc": None}
    monkeypatch.setattr(
        mnt.Pan115Mount, "_locate",
        lambda self, rel: {"pickcode": state["pickcode"], "name": "a.mkv", "size": 10},
    )

    def _dl(pc):
        if pc == state["fail_pc"]:
            raise mnt.transfer115.Pan115Error("file deleted")
        return f"https://cdnfhnfile.115cdn.net/{pc}.mp4?t={int(clock['wall'] + 1800)}"

    monkeypatch.setattr(mnt.Pan115Mount, "_client", lambda self: SimpleNamespace(download_url=_dl))
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "ck-z2")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_type="115")
    db = _FakeDB([mount])

    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert mnt.resolve_cache_stats()["path_id_entries"] == 1

    # 推进 26m 让第二层过期；模拟冷却期内报错 → 第一层保留
    clock["wall"] += 1560
    clock["mono"] += 1560
    state["fail_pc"] = "pc-v1"
    monkeypatch.setattr(pan115_guard, "enabled", lambda: True)
    monkeypatch.setattr(pan115_guard, "cooldown_state", lambda scope: {"until": clock["wall"] + 60})
    with pytest.raises(mnt.MountError):
        mnt.resolve_play_target("mount://7/a.mkv", db)
    assert mnt.resolve_cache_stats()["path_id_entries"] == 1, "冷却期内不得误清第一层缓存"

    # 冷却解除后报错 → 第一层淘汰，下一次重定位拿到 pc-v2
    monkeypatch.setattr(pan115_guard, "cooldown_state", lambda scope: None)
    with pytest.raises(mnt.MountError):
        mnt.resolve_play_target("mount://7/a.mkv", db)
    assert mnt.resolve_cache_stats()["path_id_entries"] == 0

    state["pickcode"] = "pc-v2"
    state["fail_pc"] = None
    recovered = mnt.resolve_play_target("mount://7/a.mkv", db)
    assert "pc-v2" in recovered.value


# ---------------- 4. 双层零凭据红线 ----------------


def test_neither_tier_stores_any_credentials(monkeypatch):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    secret_cookie = "UID=999_SECRET_COOKIE; CID=abc; SEID=xyz"
    monkeypatch.setattr(
        mnt.Pan115Mount, "_locate",
        lambda self, rel: {"pickcode": "pc-sec", "name": "a.mkv", "size": 10},
    )
    monkeypatch.setattr(
        mnt.Pan115Mount, "_client",
        lambda self: SimpleNamespace(
            download_url=lambda pc: f"https://cdnfhnfile.115cdn.net/{pc}.mp4?t={int(time.time() + 7200)}"
        ),
    )
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: secret_cookie)
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_type="115", config={"cid": "0"})
    db = _FakeDB([mount])
    out = mnt.resolve_play_target("mount://7/a.mkv", db)
    assert out.headers["Cookie"] == secret_cookie

    with mnt._RESOLVE_CACHE_LOCK:
        r_dump = repr(mnt._RESOLVE_CACHE)
        p_dump = repr(mnt._PATH_ID_CACHE)
    with mnt._pan115_url_cache_lock:
        u_dump = repr(mnt._pan115_url_cache)

    for dump_name, dump_val in (
        ("_RESOLVE_CACHE", r_dump),
        ("_PATH_ID_CACHE", p_dump),
        ("_pan115_url_cache", u_dump),
    ):
        assert secret_cookie not in dump_val, f"{dump_name} 泄漏了 Cookie 明文"
        assert "UID=999_SECRET_COOKIE" not in dump_val, f"{dump_name} 泄漏了凭据片段"
        assert "Cookie" not in dump_val, f"{dump_name} 残留了 Cookie 头键"
        assert "Authorization" not in dump_val, f"{dump_name} 残留了 Authorization 头键"
