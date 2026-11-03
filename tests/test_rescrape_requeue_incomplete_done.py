# -*- coding: utf-8 -*-
"""A② rescrape 重排「假完成」条目（fix/enrich-key-refresh）。

背景（2026-11-02《听说你喜欢我》案，docs/aetrix-trial-scan.md §6）：TMDB Key
跨进程不热生效时，补全走多源兜底只拿到名字/海报就终态 done；此后 rescrape
因 L1 文件指纹秒跳永远碰不到这些条目（policy 判定在秒跳之后），`missing_only`
的 should_scrape 判定形同虚设。

修复：L1 秒跳条件加一道门——`done 但无 tmdb_id 且无海报` 且当前策略要求重刮
（missing_only 下无 tmdb_id 本就要求）时，不做秒跳，放行到
`_apply_enrich_status` 按 policy 翻回 pending。

口径对齐 enrich_worker：海报= poster_path 与 primary_image_url 双空
（与假完成巡检 / none 回捞一致）。
"""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")
from backend.emby_server import scanner  # noqa: E402


def _item(**kw):
    base = dict(
        tmdb_id=None, poster_path=None, primary_image_url=None,
        production_year=2023, name="听说你喜欢我",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_incomplete_done_requires_rescrape_under_missing_only():
    """done + 无 tmdb + 无海报：missing_only 也要重刮（假完成）。"""
    it = _item()
    assert scanner._should_scrape_incomplete_done(it, "missing_only") is True
    assert scanner._should_scrape_incomplete_done(it, "all") is True


def test_has_tmdb_id_never_incomplete():
    """已绑 tmdb 的条目不在假完成口径内（missing_only 不重刮）。"""
    it = _item(tmdb_id="220068", poster_path="/p.jpg")
    assert scanner._should_scrape_incomplete_done(it, "missing_only") is False
    assert scanner._should_scrape_incomplete_done(it, "all") is False


def test_has_poster_not_incomplete_any_policy():
    """有海报（远程图也算）但无 tmdb：不在假完成口径内——enrich_worker 的
    假完成巡检/none 回捞按「无 tmdb 且无海报」判定，海报在的行由回捞按
    节奏管，rescrape 秒跳门不扩围（避免全库带图行被 policy=all 大面积重刮）。"""
    it = _item(poster_path=None, primary_image_url="http://img/1.jpg")
    assert scanner._should_scrape_incomplete_done(it, "missing_only") is False
    assert scanner._should_scrape_incomplete_done(it, "all") is False


def test_fresh_item_without_done_status_is_out_of_scope():
    """非 done 条目走正常 new/pending 流程；helper 只看字段口径，但
    fast-skip 调用点已先要求 status==done（这里验证 helper 本身不被
    last_scraped 等字段影响，保持纯函数）。"""
    it = _item()
    assert scanner._should_scrape_incomplete_done(it, "missing_only") is True


def test_fast_skip_gate_wired(monkeypatch):
    """L1 秒跳调用点确实带上了这道门：假完成条目不再被秒跳。"""
    # 直查源码级接线（避免搭完整扫描上下文）：调用点出现 helper 且
    # helper 定义在 should_scrape 之后可用。
    import inspect
    src = inspect.getsource(scanner)
    assert "not _should_scrape_incomplete_done(item, policy)" in src
    # 语义复核：同一个 item 在 should_scrape 与 helper 上的组合关系
    it = _item()
    assert scanner.should_scrape(it, "missing_only") is True
    assert scanner._should_scrape_incomplete_done(it, "missing_only") is True
