# -*- coding: utf-8 -*-
"""sidecar 115 解析结果必须进 ``mounts._RESOLVE_CACHE``（2026-11-04 生产实证）。

线上症状：VidHub 同一部片 7 个 Range 请求，每次 play_target 700–5000ms，
``pan115_calls.download_url`` 6 → 22（每个区间请求重新取链）。

根因：``SidecarMountProvider`` 没覆写 ``cacheable_play_target``，基类保守
口径「headers 含 Cookie 就不缓存」→ sidecar 路径解析结果永远进不了缓存，
每个请求都重走 sidecar 换链 + 115 download_url。

本文件钉死：
1. sidecar provider 的 cacheable/reattach 与 Pan115Mount 同口径：缓存值
   不含 Cookie、保留 X-Pickcode，命中后宿主现挂当前 Cookie；
2. 两次 resolve_play_target 只向 sidecar 换链一次（第二次命中缓存）；
3. 换挂载配置（指纹变）/ 按 pickcode 失效后重新换链。
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

# 先导 mounts 再导 mount_sidecar（既有循环，见 test_provider_runtime_115_sidecar）
from backend.emby_server import mounts as mounts
from backend.emby_server.mount_sidecar import P115SidecarMount


class FakeSidecarClient:
    """只实现宿主 provider 用到的四口：handshake/upsert/换链/关闭。"""

    def __init__(self):
        self.resolves = 0
        self.upserts = 0

    def handshake(self, required_extensions=None):
        return {
            "protocol": "external-provider-runtime-v1",
            "provider": "p115",
            "capabilities": ["list_page", "open_range", "resolve_play_target"],
            "extensions": [],
        }

    def session_upsert(self, session_id, data):
        self.upserts += 1
        return {}

    def resolve_play_target(self, session_id, path):
        self.resolves += 1
        return {
            "url": f"https://down.example/{path.strip('/')}",
            "headers": {"User-Agent": "UA", "X-Pickcode": "pc-1"},
        }

    def close(self):
        pass


def _mount(mount_id=7, cookie="cookie-123"):
    return SimpleNamespace(
        id=mount_id, mount_type="115", name="test-115", is_enabled=True,
        config=json.dumps({"cookie": cookie, "cid": "0"}), path="")


@pytest.fixture()
def provider():
    client = FakeSidecarClient()
    prov = P115SidecarMount(_mount(), client=client)
    yield prov, client
    prov.close()


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    mounts.reset_resolve_cache()
    monkeypatch.setattr(mounts, "RESOLVE_TARGET_CACHE_TTL_SEC", 600)
    yield
    mounts.reset_resolve_cache()


def test_cacheable_strips_cookie_keeps_pickcode(provider):
    prov, _ = provider
    target = prov.resolve_play_target("/a.mkv")
    assert target.headers["Cookie"] == "cookie-123"

    stored = prov.cacheable_play_target(target)
    assert stored is not None
    assert "Cookie" not in stored.headers
    assert "Authorization" not in stored.headers
    assert stored.headers["X-Pickcode"] == "pc-1"

    restored = prov.reattach_play_target(stored)
    assert restored.headers["Cookie"] == "cookie-123"
    assert restored.headers["X-Pickcode"] == "pc-1"
    assert "Cookie" not in stored.headers  # 缓存对象不被改写


def test_resolve_target_cached_across_requests(provider, monkeypatch):
    prov, client = provider
    monkeypatch.setattr(mounts, "load_mount", lambda db, mid: prov.mount)
    monkeypatch.setattr(mounts, "build_provider",
                        lambda mount, db, library=None: prov)

    first = mounts.resolve_play_target("mount://7/a.mkv", None)
    second = mounts.resolve_play_target("mount://7/a.mkv", None)

    assert client.resolves == 1, "第二次必须命中解析缓存，不许再向 sidecar 换链"
    assert first.value == second.value
    # 命中后仍带当前 Cookie（凭据不进缓存值，命中时现挂）
    assert second.headers["Cookie"] == "cookie-123"
    assert mounts.resolve_cache_stats()["entries"] == 1
    assert mounts.resolve_cache_stats()["hits"] >= 1


def test_config_change_invalidates_cache(provider, monkeypatch):
    prov, client = provider
    monkeypatch.setattr(mounts, "load_mount", lambda db, mid: prov.mount)
    monkeypatch.setattr(mounts, "build_provider",
                        lambda mount, db, library=None: prov)

    mounts.resolve_play_target("mount://7/a.mkv", None)
    prov.config = {"cookie": "cookie-123", "cid": "9"}  # 配置指纹变
    mounts.resolve_play_target("mount://7/a.mkv", None)
    assert client.resolves == 2


def test_pickcode_invalidation_drops_cached_entry(provider, monkeypatch):
    prov, client = provider
    monkeypatch.setattr(mounts, "load_mount", lambda db, mid: prov.mount)
    monkeypatch.setattr(mounts, "build_provider",
                        lambda mount, db, library=None: prov)

    mounts.resolve_play_target("mount://7/a.mkv", None)
    mounts.invalidate_resolve_cache_by_pickcode("pc-1")
    mounts.resolve_play_target("mount://7/a.mkv", None)
    assert client.resolves == 2, "pickcode 失效后下一次必须重新换链"
