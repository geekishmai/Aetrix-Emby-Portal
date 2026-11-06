# -*- coding: utf-8 -*-
"""冷启动首播优化回归测试（perf/cold-start-play）。

覆盖：
1. 第一层路径→文件 ID 的 Redis 跨进程/跨重启持久化（48h TTL）：
   - 容器重启（清空全部进程内缓存与 sidecar 目录缓存）后首播直接命中 Redis，
     跳过全部逐级 ``list_dir`` 调用（list 增量 = 0），只发 1 次 ``download_url``；
   - 挂载配置指纹（SHA-256 摘要）进 Redis 值校验：改挂载配置后旧 Redis 条目自动失配重定位；
   - **零凭据红线**：挂载 ``config`` 即使含明文 ``cookie``，Redis 的全部 key 与 value
     均不含 Cookie / Bearer / URL / 明文路径；
   - 中继 403/404/410（``invalidate_resolve_cache_by_pickcode``）、按路径失效、按挂载失效
     均同步清除 Redis 持久项（跨进程生效，且零 ``KEYS *`` 扫描）；
   - Redis 不可用时静默降级回进程内 ``_PATH_ID_CACHE``，不阻断播放。
2. 预取与播放并发的重复取链消除（线上 +4 根因修复）：
   - 宿主单飞等待上限 ``_RESOLVE_SINGLE_FLIGHT_WAIT_SEC >= 15.0``（覆盖 6–8s 冷目录定位，
     防止 stream 在 5s 处穿透 prefetch 触发双倍取链）；
   - ``P115SidecarHandler`` 内置 ``(scope, cid)`` 与 ``(scope, pickcode)`` 在途单飞：
     即使 ``api``（预取）与 ``ea``（出流）跨进程并发打到同一 sidecar，``list_dir`` 与
     ``download_url`` 也严格只执行 1 轮（大文件 web 50028→app 从 +4 降至 +2，普通文件从 +2 降至 +1），
     完成后 ``_list_inflight`` / ``_dl_inflight`` 立即清空不留持久直链状态。
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

from backend import database as dbmod
from backend.emby_server import models as em
from backend.emby_server import mounts as mnt
from backend.emby_server import streaming
from backend.emby_server import transfer115
from backend.emby_server.mount_sidecar import P115SidecarMount
from backend.emby_server.p115_sidecar import P115SidecarHandler
from backend.emby_server.provider_runtime import RuntimeSession


class _FakePipeline:
    def __init__(self, store: "_FakeRedis"):
        self._store = store
        self._ops = []

    def get(self, k):
        self._ops.append(("get", (k,)))
        return self

    def pttl(self, k):
        self._ops.append(("pttl", (k,)))
        return self

    def set(self, k, v, ex=None):
        self._ops.append(("set", (k, v, ex)))
        return self

    def sadd(self, k, *vals):
        self._ops.append(("sadd", (k, *vals)))
        return self

    def srem(self, k, *vals):
        self._ops.append(("srem", (k, *vals)))
        return self

    def expire(self, k, sec):
        self._ops.append(("expire", (k, sec)))
        return self

    def delete(self, *ks):
        self._ops.append(("delete", ks))
        return self

    def execute(self):
        out = []
        for name, args in self._ops:
            out.append(getattr(self._store, name)(*args))
        self._ops.clear()
        return out


class _FakeRedis:
    """最小内存 Redis 替身（覆盖 string + set + TTL，且拒绝 KEYS 全表扫）。"""

    def __init__(self):
        self.kv: dict[str, tuple[str, int | None]] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    def keys(self, *a, **k):  # pragma: no cover — 调到即违规
        raise AssertionError("禁止在生产路径使用阻塞式 KEYS 扫描")

    def pipeline(self):
        return _FakePipeline(self)

    def get(self, k: str):
        row = self.kv.get(k)
        return row[0].encode("utf-8") if row is not None else None

    def pttl(self, k: str) -> int:
        if k not in self.kv and k not in self.sets:
            return -2
        sec = self.ttls.get(k)
        return sec * 1000 if sec is not None else -1

    def set(self, k: str, v: str, ex: int | None = None):
        self.kv[k] = (str(v), ex)
        if ex is not None:
            self.ttls[k] = int(ex)
        return True

    def sadd(self, k: str, *vals: str):
        bucket = self.sets.setdefault(k, set())
        before = len(bucket)
        for v in vals:
            bucket.add(str(v))
        return len(bucket) - before

    def srem(self, k: str, *vals: str):
        bucket = self.sets.get(k)
        if not bucket:
            return 0
        removed = 0
        for v in vals:
            if str(v) in bucket:
                bucket.remove(str(v))
                removed += 1
        return removed

    def smembers(self, k: str):
        return {v.encode("utf-8") for v in self.sets.get(k, set())}

    def expire(self, k: str, sec: int):
        self.ttls[k] = int(sec)
        return True

    def delete(self, *ks: str):
        n = 0
        for k in ks:
            if k in self.kv:
                del self.kv[k]
                n += 1
            if k in self.sets:
                del self.sets[k]
                n += 1
            self.ttls.pop(k, None)
        return n


@pytest.fixture()
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(dbmod, "redis_call", lambda fn, default=None: fn(r))
    return r


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
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


def _mount(mount_id=1, mount_type="115", config=None, enabled=True, path="115:/"):
    m = em.StorageMount(
        name=f"m-{mount_id}",
        mount_type=mount_type,
        path=path,
        config=mnt.dump_config(config or {"cid": "0"}),
        is_enabled=enabled,
    )
    m.id = mount_id
    return m


# ---------------- 1. 跨重启 Redis 路径→ID 缓存与零凭据校验 ----------------


def test_redis_tier1_survives_restart_skips_list_dir_and_stores_zero_credentials(
    monkeypatch, fake_redis
):
    secret_cookie = "UID=888_SUPER_SECRET; CID=deadbeef; SEID=cafebabe"
    calls = {"list_dir": 0, "download": 0}

    class _Fake115:
        def __init__(self, cookie, ua=""):
            self.cookie = cookie

        def list_dir(self, cid, deadline=None):
            calls["list_dir"] += 1
            tree = {
                "0": [{"cid": "10", "name": "媒体库", "is_dir": True}],
                "10": [{"cid": "20", "name": "华语电影", "is_dir": True}],
                "20": [{"cid": "30", "name": "12金鸭 (2015)", "is_dir": True}],
                "30": [{"cid": "31", "name": "12金鸭.mkv", "is_dir": False,
                        "pickcode": "pc-12ducks", "size": 5_000_000_000}],
            }
            return tree.get(str(cid), [])

        def download_url(self, pickcode, deadline=None):
            calls["download"] += 1
            exp = int(time.time() + 500_000)
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mkv?t={exp}&seq={calls['download']}"

    handler = P115SidecarHandler(client_factory=_Fake115)

    class _Bridge:
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
            return handler.resolve_play_target(
                RuntimeSession(session_id=session_id, data={}), path, pickcode=pickcode
            )

        def close(self):
            pass

    # 故意把明文 cookie 放进 mount.config，验证 SHA256 摘要不会把凭据漏进内存键或 Redis
    mount = _mount(mount_id=1, config={"cookie": secret_cookie, "cid": "0"})
    bridge = _Bridge()
    monkeypatch.setattr(mnt, "load_mount", lambda db, mid: mount)
    monkeypatch.setattr(
        mnt, "build_provider",
        lambda m, db, library=None: P115SidecarMount(m, db, library, client=bridge),
    )

    rel_path = "mount://1//媒体库/华语电影/12金鸭 (2015)/12金鸭.mkv"
    first = mnt.resolve_play_target(rel_path, None)
    assert calls == {"list_dir": 4, "download": 1}
    assert first.headers["Cookie"] == secret_cookie

    # 零凭据与 48h TTL 审计：检查 Redis 全部键值 + 进程内两层缓存键
    redis_blob = repr(fake_redis.kv) + repr(fake_redis.sets)
    mem_blob = repr(mnt._PATH_ID_CACHE) + repr(mnt._RESOLVE_CACHE)
    for leak in (secret_cookie, "888_SUPER_SECRET", "deadbeef", "Cookie", "https://", "12金鸭"):
        assert leak not in redis_blob, f"Redis 持久层不得出现 {leak!r}"
    assert secret_cookie not in mem_blob and "888_SUPER_SECRET" not in mem_blob
    assert any(ttl == 172800 for ttl in fake_redis.ttls.values())

    # 模拟容器重启：清空宿主与 sidecar 的全部进程内存缓存，仅保留 Redis
    with mnt._RESOLVE_CACHE_LOCK:
        mnt._RESOLVE_CACHE.clear()
        mnt._PATH_ID_CACHE.clear()
    handler._dir_cache.clear()
    handler._cid_cache.clear()

    # 重启后首次播放：命中 Redis 持久层，0 次 list_dir，仅 1 次 download_url
    second = mnt.resolve_play_target(rel_path, None)
    assert "seq=2" in second.value
    assert calls["list_dir"] == 4, "容器重启后必须命中 Redis 路径→ID 缓存，不得重新逐级列目录"
    assert calls["download"] == 2
    assert mnt.resolve_cache_stats()["path_id_redis_hits"] == 1

    # 挂载改配置（指纹变）：旧 Redis 条目失配，自动重走目录定位并覆盖
    mount.config = mnt.dump_config({"cookie": secret_cookie, "cid": "0", "sub": "new"})
    with mnt._RESOLVE_CACHE_LOCK:
        mnt._RESOLVE_CACHE.clear()
        mnt._PATH_ID_CACHE.clear()
    mnt.resolve_play_target(rel_path, None)
    assert calls["list_dir"] == 8
    assert calls["download"] == 3


# ---------------- 2. 403/404/410 与路径/挂载失效同步清除 Redis ----------------


class _Dead403Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(403)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"dead")

    def log_message(self, *args):
        pass


def test_relay_403_and_path_invalidation_purge_redis_across_processes(monkeypatch, fake_redis):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    state = {"pc": "pc-old", "locate": 0}

    def _locate(self, rel):
        state["locate"] += 1
        return {"pickcode": state["pc"], "name": "a.mkv", "size": 10}

    monkeypatch.setattr(mnt.Pan115Mount, "_locate", _locate)
    monkeypatch.setattr(
        mnt.Pan115Mount, "_client",
        lambda self: SimpleNamespace(
            download_url=lambda pc: f"https://cdnfhnfile.115cdn.net/{pc}.mkv?t={int(time.time() + 7200)}"
        ),
    )
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "ck-1")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")

    mount = _mount(mount_id=7, config={"cid": "0"})
    monkeypatch.setattr(mnt, "load_mount", lambda db, mid: mount)

    mnt.resolve_play_target("mount://7/电影/a.mkv", None)
    assert len(fake_redis.kv) == 1

    srv = HTTPServer(("127.0.0.1", 0), _Dead403Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        req = Request({
            "type": "http", "method": "GET", "path": "/Videos/1/stream",
            "raw_path": b"/Videos/1/stream", "query_string": b"",
            "headers": [], "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 1234), "root_path": "",
        })
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                streaming.serve_mount_relay_async(
                    f"http://127.0.0.1:{srv.server_port}/dead.mkv",
                    req,
                    {"User-Agent": "ua-115", "Cookie": "ck-1", "X-Pickcode": "pc-old"},
                    "video/mp4",
                )
            )
        assert exc_info.value.status_code == 403
    finally:
        srv.shutdown()
        srv.server_close()

    # Redis 中的路径→ID 记录已被连带删除；另一进程（L1 空）再次解析会拿到新 pickcode
    assert fake_redis.kv == {}
    state["pc"] = "pc-new"
    out = mnt.resolve_play_target("mount://7/电影/a.mkv", None)
    assert "pc-new" in out.value
    assert state["locate"] == 2

    # 按路径与按挂载失效同样删除 Redis 记录
    mnt.invalidate_resolve_cache_for_path("mount://7/电影/a.mkv")
    assert fake_redis.kv == {}
    mnt.resolve_play_target("mount://7/电影/a.mkv", None)
    assert len(fake_redis.kv) == 1
    mnt.invalidate_resolve_cache(mount_id=7)
    assert fake_redis.kv == {}


def test_redis_outage_degrades_cleanly_to_memory(monkeypatch):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    monkeypatch.setattr(dbmod, "redis_call", lambda fn, default=None: default)
    monkeypatch.setattr(
        mnt.Pan115Mount, "_locate",
        lambda self, rel: {"pickcode": "pc-mem", "name": "a.mkv", "size": 10},
    )
    monkeypatch.setattr(
        mnt.Pan115Mount, "_client",
        lambda self: SimpleNamespace(
            download_url=lambda pc: f"https://cdnfhnfile.115cdn.net/{pc}.mkv?t={int(time.time() + 7200)}"
        ),
    )
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: "ck")
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua")
    mount = _mount(mount_id=3)
    monkeypatch.setattr(mnt, "load_mount", lambda db, mid: mount)

    out = mnt.resolve_play_target("mount://3/a.mkv", None)
    assert "pc-mem" in out.value
    assert mnt.resolve_cache_stats()["path_id_entries"] == 1


# ---------------- 3. 并发预取与播放去重（消除 +4 重复取链） ----------------


def test_host_singleflight_wait_covers_cold_walk_and_sidecar_deduplicates_inflight():
    """复现线上 12:04Z 场景：8GB 大文件超 190MiB 阈值直接走 app 通道（省掉必败 web 50028），
    且当预取与播放并发打进 sidecar 时，在途单飞将 list_dir 与 download_url 严格合并为 1 次
    （原 download_url +4 直接降至 +1），结束后 inflight 表为空。
    """
    assert mnt._RESOLVE_SINGLE_FLIGHT_WAIT_SEC >= 15.0

    counts = {"list": 0, "web_dl": 0, "app_dl": 0}
    list_entered = threading.Event()
    dl_entered = threading.Event()
    release_list = threading.Event()
    release_dl = threading.Event()

    class _LargeFileClient(transfer115.Pan115Client):
        def __init__(self, cookie, ua=""):
            self.cookie = cookie
            self.ua = ua

        def list_dir(self, cid="0", limit=1000, deadline=None):
            counts["list"] += 1
            list_entered.set()
            release_list.wait(2.0)
            return [{"cid": "99", "name": "big.mkv", "is_dir": False,
                     "pickcode": "pc-bluray", "size": 8_000_000_000}]

        def _download_url_web(self, pickcode, deadline=None):
            counts["web_dl"] += 1
            raise transfer115.Pan115ChannelLimitError("文件大小超出限制 (50028)")

        def _download_url_app(self, pickcode, deadline=None):
            counts["app_dl"] += 1
            dl_entered.set()
            release_dl.wait(2.0)
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mkv?t={int(time.time() + 7200)}"

    handler = P115SidecarHandler(client_factory=_LargeFileClient)
    handler.session_upsert("s-api", {"cookie": "UID=1_A_1;CID=x;SEID=y", "ua": "UA", "root_cid": "0"})
    handler.session_upsert("s-ea", {"cookie": "UID=1_A_1;CID=x;SEID=y", "ua": "UA", "root_cid": "0"})

    results = []
    errors = []

    def _worker(sid):
        try:
            r = handler.resolve_play_target(
                RuntimeSession(session_id=sid, data={}), "/big.mkv"
            )
            results.append(r["url"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t_prefetch = threading.Thread(target=_worker, args=("s-api",), daemon=True)
    t_stream = threading.Thread(target=_worker, args=("s-ea",), daemon=True)
    t_prefetch.start()
    assert list_entered.wait(2.0)
    t_stream.start()
    time.sleep(0.08)  # 让第二个请求进入同一目录在途单飞等待
    release_list.set()
    assert dl_entered.wait(2.0)
    time.sleep(0.08)  # 让第二个请求进入同一 pickcode 取链在途单飞等待
    release_dl.set()
    t_prefetch.join(3.0)
    t_stream.join(3.0)

    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert counts["list"] == 1, f"并发冷定位只能列目录 1 次，实际 {counts['list']}"
    assert counts["web_dl"] == 0 and counts["app_dl"] == 1, (
        f"8GB 大文件并发预取+播放应直走 app 通道且合并为 1 次（原 +4 降至 +1），实际 {counts}"
    )
    assert handler._list_inflight == {}
    assert handler._dl_inflight == {}


# ---------------- 4. 按文件大小选通道（200MiB 上限留 5% = 190MiB 默认阈值） ----------------


def test_download_url_size_threshold_routes_large_to_app_and_small_to_web(monkeypatch):
    assert transfer115.PAN115_WEB_DOWNLOAD_LIMIT_BYTES == 200 * 1024 * 1024
    assert transfer115.PAN115_WEB_DOWNLOAD_MAX_BYTES == 190 * 1024 * 1024

    seen = {"web": 0, "app": 0}

    class _StubClient(transfer115.Pan115Client):
        def __init__(self):
            self.cookie = "UID=1_A1_1;CID=x;SEID=y"
            self.ua = "UA"

        def _download_url_web(self, pickcode, deadline=None):
            seen["web"] += 1
            if pickcode == "pc-unknown-big":
                raise transfer115.Pan115ChannelLimitError("50028")
            return f"https://web.example/{pickcode}"

        def _download_url_app(self, pickcode, deadline=None):
            seen["app"] += 1
            return f"https://app.example/{pickcode}"

    c = _StubClient()
    # 1) 小文件（157 MiB <= 190 MiB）与恰等于阈值（190 MiB）：维持走 webapi
    assert c.download_url("pc-157m", file_size=157 * 1024 * 1024) == "https://web.example/pc-157m"
    assert c.download_url("pc-190m", file_size=190 * 1024 * 1024) == "https://web.example/pc-190m"
    assert seen == {"web": 2, "app": 0}

    # 2) 超过阈值（190 MiB + 1B / 843 MiB）：直接走 app 通道，0 次 webapi
    assert c.download_url("pc-190m-plus", file_size=190 * 1024 * 1024 + 1) == "https://app.example/pc-190m-plus"
    assert c.download_url("pc-843m", file_size=843_844_273) == "https://app.example/pc-843m"
    assert seen == {"web": 2, "app": 2}

    # 3) 未知大小（file_size=None）：保持原行为（先试 web，撞 50028 回落 app）
    assert c.download_url("pc-unknown-big", file_size=None) == "https://app.example/pc-unknown-big"
    assert seen == {"web": 3, "app": 3}

    # 4) 阈值可配：调低至 50 MiB 后，60 MiB 文件直走 app 通道
    monkeypatch.setattr(transfer115, "PAN115_WEB_DOWNLOAD_MAX_BYTES", 50 * 1024 * 1024)
    assert c.download_url("pc-60m", file_size=60 * 1024 * 1024) == "https://app.example/pc-60m"
    assert seen == {"web": 3, "app": 4}


def test_redis_persists_file_size_so_post_restart_large_file_still_skips_web(
    monkeypatch, fake_redis
):
    """端到端验证：冷播大文件时直走 app（+1），大小随 pickcode 一并落入 Redis；
    容器重启后命中 Redis 跳过 list_dir 且仍带回 file_size 直走 app（+1）；小文件仍走 web（+1）。
    """
    calls = {"list": 0, "web": 0, "app": 0}

    class _Client(transfer115.Pan115Client):
        def __init__(self, cookie, ua=""):
            self.cookie = cookie
            self.ua = ua

        def list_dir(self, cid="0", limit=1000, deadline=None):
            calls["list"] += 1
            return [
                {"cid": "1", "name": "feature.mkv", "is_dir": False,
                 "pickcode": "pc-big", "size": 4_500_000_000},
                {"cid": "2", "name": "trailer.mp4", "is_dir": False,
                 "pickcode": "pc-small", "size": 40 * 1024 * 1024},
            ]

        def _download_url_web(self, pickcode, deadline=None):
            calls["web"] += 1
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mp4?t={int(time.time() + 7200)}&ch=web"

        def _download_url_app(self, pickcode, deadline=None):
            calls["app"] += 1
            return f"https://cdnfhnfile.115cdn.net/{pickcode}.mkv?t={int(time.time() + 7200)}&ch=app"

    handler = P115SidecarHandler(client_factory=_Client)

    class _Bridge:
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
                                *, pickcode: str | None = None,
                                file_size: int | None = None) -> dict:
            return handler.resolve_play_target(
                RuntimeSession(session_id=session_id, data={}),
                path,
                pickcode=pickcode,
                file_size=file_size,
            )

        def close(self):
            pass

    mount = _mount(mount_id=2, config={"cid": "0"})
    bridge = _Bridge()
    monkeypatch.setattr(mnt, "load_mount", lambda db, mid: mount)
    monkeypatch.setattr(
        mnt, "build_provider",
        lambda m, db, library=None: P115SidecarMount(m, db, library, client=bridge, cookie_override="ck"),
    )

    # 1) 冷播 4.5GB 正片 + 40MiB 预告片：大文件直走 app(+1)，小文件走 web(+1)
    u_big_1 = mnt.resolve_play_target("mount://2/feature.mkv", None)
    u_small_1 = mnt.resolve_play_target("mount://2/trailer.mp4", None)
    assert "ch=app" in u_big_1.value
    assert "ch=web" in u_small_1.value
    assert calls == {"list": 1, "web": 1, "app": 1}

    # 2) 模拟容器重启清空全部进程内缓存，仅留 Redis
    with mnt._RESOLVE_CACHE_LOCK:
        mnt._RESOLVE_CACHE.clear()
        mnt._PATH_ID_CACHE.clear()
    handler._dir_cache.clear()
    handler._cid_cache.clear()

    # 3) 重启后再次播放：list 零增长，且 4.5GB 正片凭 Redis 里持久化的 sz 仍直走 app(+1)
    u_big_2 = mnt.resolve_play_target("mount://2/feature.mkv", None)
    u_small_2 = mnt.resolve_play_target("mount://2/trailer.mp4", None)
    assert "ch=app" in u_big_2.value
    assert "ch=web" in u_small_2.value
    assert calls == {"list": 1, "web": 2, "app": 2}
