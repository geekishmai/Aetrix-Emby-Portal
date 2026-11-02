"""图片下载重试与日志（问题一：以前单次尝试、失败只打 INFO 看不见）。

修复前（复现脚本 1/10 里的两条 ❌）：``_download`` 单次尝试，网络抖一下就退回
远程地址，最终失败只有一条 INFO——「图片下不下来」在日志里完全不存在。

修复后：
- 瞬态失败（网络异常 / 429 / 5xx）按 ``EMBY_IMAGE_RETRIES``（默认 2）指数退避
  重试（0.5s 起翻倍 + 抖动）；确定性失败（404/超大/类型不对）不浪费重试；
- 重试耗尽打 **WARNING**；
- IO 阶段的 ``prewarm`` 带重试（刮削链主战场）；取图时的按需自愈仍 1 次
  （那里要快，失败了客户端还能退回远程地址）。

全部用假 httpx，不发真请求。
"""
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from backend.emby_server import image_store


class _Resp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


@pytest.fixture()
def httpx_state(monkeypatch):
    """可编排的假 httpx。

    ``state.script``：按序弹出的动作 ——
    ``("exc",)`` 抛网络异常 / ``("status", 503)`` 状态码 / ``("bytes", b"..")`` 成功；
    弹空后循环最后一个动作（模拟持续故障）。
    ``state.calls`` 记录真实发起了几次下载尝试。
    """
    state = SimpleNamespace(script=[], calls=0)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            state.calls += 1
            action = state.script.pop(0) if len(state.script) > 1 else state.script[0]
            if action[0] == "exc":
                raise ConnectionError("cdn unreachable")
            if action[0] == "status":
                return _Resp(status=action[1])
            return _Resp(status=200, content=action[1], headers={"content-type": "image/jpeg"})

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_Client))
    return state


@pytest.fixture(autouse=True)
def _stub_outbound_verify(monkeypatch):
    """本文件测的是 httpx 传输重试逻辑，不是 DNS：出站校验桩成放行。

    假 httpx 本来就不发真请求，校验若走真 DNS 会把重试用例耦合到
    运行环境的 DNS 解析结果（沙箱里 image.tmdb.org 曾解析到
    198.18.x 被判私网）。真校验另有覆盖：PA-076 新用例（字面 IP，
    零 DNS）与 test_audit_a053_upstream_ssrf（verify 单元）。
    """
    monkeypatch.setattr(image_store, "verify_outbound_url", lambda url: None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """退避不真睡，但记录睡了几次（要断言退避真的发生）"""
    slept: list = []
    monkeypatch.setattr(image_store.time, "sleep", lambda s: slept.append(s))
    return slept


URL = "https://image.tmdb.org/t/p/w500/abc.jpg"


def test_retried_then_succeeds(httpx_state, no_sleep):
    """网络抖两下、第三次成功 → 拿到内容，且中间真的退避过"""
    httpx_state.script = [("exc",), ("status", 503), ("bytes", b"IMG")]
    assert image_store._download(URL) == b"IMG"
    assert httpx_state.calls == 3
    assert len(no_sleep) == 2, "两次重试之间都应退避"


def test_exhausted_logs_warning(httpx_state, caplog):
    """持续故障 → 默认 3 次尝试后放弃，并留下 WARNING（旧实现是 0 条）"""
    httpx_state.script = [("exc",)]
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.image_store"):
        assert image_store._download(URL) is None
    assert httpx_state.calls == 1 + int(os.getenv("EMBY_IMAGE_RETRIES", "2"))
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("图片下载失败" in r.getMessage() for r in warns)


def test_deterministic_404_not_retried(httpx_state, caplog):
    """404 是确定性失败：1 次就够，不浪费重试、不打 WARNING"""
    httpx_state.script = [("status", 404)]
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.image_store"):
        assert image_store._download(URL) is None
    assert httpx_state.calls == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_env_retries_zero_single_attempt(httpx_state, monkeypatch):
    """EMBY_IMAGE_RETRIES=0 → 只试一次"""
    monkeypatch.setenv("EMBY_IMAGE_RETRIES", "0")
    httpx_state.script = [("exc",)]
    assert image_store._download(URL) is None
    assert httpx_state.calls == 1


def test_on_demand_localize_stays_single_attempt(httpx_state):
    """取图时的按需自愈保持 1 次（要快；失败了客户端退回远程地址）"""
    httpx_state.script = [("exc",)]
    assert image_store.localize(URL) == ""
    assert httpx_state.calls == 1


def test_prewarm_uses_retries(httpx_state):
    """IO 阶段预热（刮削链主战场）带满重试"""
    httpx_state.script = [("exc",)]
    assert image_store.prewarm([URL]) == 0
    assert httpx_state.calls == 1 + int(os.getenv("EMBY_IMAGE_RETRIES", "2"))
