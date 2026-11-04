# -*- coding: utf-8 -*-
"""起播取链预取原型的离线单测（默认关，不发起任何真实 115 调用）。"""
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")
from backend.emby_server import play_prefetch as pp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    pp._prefetch_inflight.clear()
    pp._prefetch_done_at.clear()
    yield
    pp._prefetch_inflight.clear()
    pp._prefetch_done_at.clear()


class _FakeDB:
    def close(self):
        pass


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PLAY_TTFB_PREFETCH_ENABLED", raising=False)
    assert pp.prefetch_enabled() is False
    # 关闭时 try_prefetch 直接 False，不起线程
    item = SimpleNamespace(id=1, file_path="mount://1/a.mkv", library=None)
    assert pp.try_prefetch(object(), item) is False


def test_prefetch_resolves_and_dedups(monkeypatch):
    monkeypatch.setenv("PLAY_TTFB_PREFETCH_ENABLED", "1")
    calls = []

    def fake_resolve(path, db, library=None):
        calls.append((path, type(db).__name__))
        return SimpleNamespace(kind="url", value="https://115.example/x")

    monkeypatch.setattr("backend.emby_server.mounts.resolve_play_target",
                        fake_resolve)
    sessions = []

    def fake_session():
        sessions.append(1)
        return _FakeDB()

    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", fake_session)
    item = SimpleNamespace(id="guid-1", file_path="mount://1/a.mkv",
                           library=None)
    assert pp.try_prefetch(object(), item) is True
    # 等后台线程
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [("mount://1/a.mkv", "_FakeDB")]
    # 去重窗口内不再预取
    assert pp.try_prefetch(object(), item) is False
    assert len(calls) == 1


def test_non_mount_item_skipped(monkeypatch):
    monkeypatch.setenv("PLAY_TTFB_PREFETCH_ENABLED", "1")
    monkeypatch.setattr("backend.emby_server.mounts.resolve_play_target",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("本机条目不应预取")))
    item = SimpleNamespace(id=2, file_path="/srv/media/a.mkv", library=None)
    assert pp.try_prefetch(object(), item) is False


def test_resolve_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("PLAY_TTFB_PREFETCH_ENABLED", "1")

    def boom(*a, **k):
        raise RuntimeError("115 down")
    monkeypatch.setattr("backend.emby_server.mounts.resolve_play_target", boom)
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", _FakeDB)
    item = SimpleNamespace(id=3, file_path="mount://1/a.mkv", library=None)
    assert pp.try_prefetch(object(), item) is True
    deadline = time.monotonic() + 2
    while item.id in pp._prefetch_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert item.id not in pp._prefetch_inflight, "失败后 inflight 应清掉"
    # 失败也进去重窗口（防风暴重试）
    assert pp._recently_done(item.id) is True
