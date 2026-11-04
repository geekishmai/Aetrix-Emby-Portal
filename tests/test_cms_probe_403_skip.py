# -*- coding: utf-8 -*-
"""探活分级（件一）：恒 403 主机跳过探活（回归）。

口径：同一主机连续 403 满 3 次后，探活入口不再发请求、直接返回
(403, None)，resolve 侧走既有软放行分支，播放决策与实测 403 一致，
只省 1.5–3.8 秒等待；计数 TTL 6 小时，到期恢复实测自愈；非 403 的
确定性状态清键，超时／断连不改计数。全部离线：_probe_single_hop
桩注入，CacheManager 走测试内存在线（REDIS_ENABLED=false）。
各用例用专属主机名，防进程级计数串扰。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import upstream_playback as up


def _never_called_stub(url, headers, timeout):
    raise AssertionError(f"probe should have been skipped: {url}")


def test_three_consecutive_403_then_skip(monkeypatch):
    host = "probe403-skip.example.com"
    calls = []

    def stub(url, headers, timeout):
        calls.append(url)
        return 403, None, None

    monkeypatch.setattr(up, "_probe_single_hop", stub)
    url = f"https://{host}/x.m3u8"
    for _ in range(3):
        assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 3
    # 第 4 发：桩换成必败替身，证明入口根本没发请求
    monkeypatch.setattr(up, "_probe_single_hop", _never_called_stub)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 3


def test_non_403_clears_counter(monkeypatch):
    host = "probe403-reset.example.com"
    calls = []
    results = [(403, None, None), (403, None, None),
               (206, None, None),
               (403, None, None), (403, None, None), (403, None, None)]

    def stub(url, headers, timeout):
        calls.append(url)
        return results.pop(0)

    monkeypatch.setattr(up, "_probe_single_hop", stub)
    url = f"https://{host}/x.m3u8"
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (206, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 6
    # 206 已清键：3 次 403 后才跳过，第 4 发必不发请求
    monkeypatch.setattr(up, "_probe_single_hop", _never_called_stub)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 6


def test_timeout_does_not_change_counter(monkeypatch):
    host = "probe403-timeout.example.com"
    calls = []
    results = [403, 403, None, 403]

    def stub(url, headers, timeout):
        calls.append(url)
        code = results.pop(0)
        return (code, None, None) if code is not None else (None, None, None)

    monkeypatch.setattr(up, "_probe_single_hop", stub)
    url = f"https://{host}/x.m3u8"
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert up._default_http_range_probe(url, {}, 5) == (None, None)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 4
    # 超时没改计数：第 3 个 403 已满阈值，下一发跳过
    monkeypatch.setattr(up, "_probe_single_hop", _never_called_stub)
    assert up._default_http_range_probe(url, {}, 5) == (403, None)
    assert len(calls) == 4


def test_other_host_unaffected(monkeypatch):
    host_a = "probe403-a.example.com"
    host_b = "probe403-b.example.com"
    calls = []

    def stub(url, headers, timeout):
        calls.append(url)
        return 403, None, None

    monkeypatch.setattr(up, "_probe_single_hop", stub)
    for _ in range(3):
        assert up._default_http_range_probe(
            f"https://{host_a}/x.m3u8", {}, 5) == (403, None)
    # A 已跳过，但 B 的首发 403 仍真实发出
    assert up._default_http_range_probe(
        f"https://{host_b}/x.m3u8", {}, 5) == (403, None)
    assert len(calls) == 4
    assert any(host_b in u for u in calls)
