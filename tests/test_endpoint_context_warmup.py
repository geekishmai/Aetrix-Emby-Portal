"""端点上下文预热：FastAPI 首请求的源码提取不许压在事件循环上。

FastAPI（fastapi.routing）在每个端点**第一次**被请求时，会在请求路径上同步调
``_extract_endpoint_context`` → ``inspect.getsourcelines``，把端点所在文件整份
读进来（linecache + tokenize），按端点缓存、只付一次。大文件在忙的机器上就是
0.16–0.22s 的事件循环空洞——CI 冒烟 hot_paths 第 5.2 节反复红在此：量测窗里
第一个撞上 ``POST /api/admin/servers/{id}/test`` 的请求，替全站付了
``backend/api/servers.py``（948 行）的首读。``backend.main`` 必须在导入期
（还没有任何请求进来时）替全部端点把这笔账预付清。
"""
import linecache
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmppath}")

import pytest

fastapi_routing = pytest.importorskip("fastapi.routing")

_extract = getattr(fastapi_routing, "_extract_endpoint_context", None)
_ctx_cache = getattr(fastapi_routing, "_endpoint_context_cache", None)
if _extract is None or _ctx_cache is None:
    pytest.skip("此 FastAPI 版本无端点上下文提取机制，跳过",
                allow_module_level=True)

_THIS_FILE = os.path.abspath(__file__)
_THIS_BASE = os.path.basename(_THIS_FILE)


def _count_reads_of_this_file(monkeypatch):
    """数 linecache 现读本文件的次数（monkeypatch 打模块函数，非实例属性）。"""
    hits = {"n": 0}
    real = linecache.updatecache

    def counting(filename, module_globals=None):
        if os.path.basename(str(filename)) == _THIS_BASE:
            hits["n"] += 1
        return real(filename, module_globals)

    monkeypatch.setattr(linecache, "updatecache", counting)
    return hits


async def _dummy_endpoint_a():
    return {"ok": True}


async def _dummy_endpoint_b():
    return {"ok": True}


def test_first_extraction_really_reads_source_on_the_spot(monkeypatch):
    """守卫自证：没人预付时，首次提取确实现读源码文件（证明下面抓得到这笔账）。"""
    _ctx_cache.pop(id(_dummy_endpoint_a), None)
    linecache.cache.pop(_THIS_FILE, None)
    hits = _count_reads_of_this_file(monkeypatch)
    _extract(_dummy_endpoint_a)
    assert hits["n"] >= 1, "首次提取没有现读源码文件，本文件的量法失效"


def test_warm_endpoint_contexts_prepays_first_hit(monkeypatch):
    """预热后：端点上下文都在缓存里，再提取不许再读文件。"""
    from fastapi import FastAPI

    from backend.main import _warm_endpoint_contexts

    app = FastAPI()
    app.get("/a")(_dummy_endpoint_a)
    app.get("/b")(_dummy_endpoint_b)
    for fn in (_dummy_endpoint_a, _dummy_endpoint_b):
        _ctx_cache.pop(id(fn), None)
    linecache.cache.pop(_THIS_FILE, None)

    warmed = _warm_endpoint_contexts(app)
    assert warmed >= 2
    assert id(_dummy_endpoint_a) in _ctx_cache
    assert id(_dummy_endpoint_b) in _ctx_cache

    hits = _count_reads_of_this_file(monkeypatch)
    _extract(_dummy_endpoint_a)
    _extract(_dummy_endpoint_b)
    assert hits["n"] == 0, "预热后首次提取仍在现读源码文件，预热没付到账"


def test_main_app_prewarms_servers_test_endpoint_at_import():
    """回归本体：导入 backend.main 后，§5.2 那个端点的上下文必须已在缓存里。"""
    import backend.main  # noqa: F401
    from backend.api import servers as servers_api

    assert id(servers_api.test_server) in _ctx_cache, (
        "导入 backend.main 后 /api/admin/servers/{id}/test 的端点上下文应已预热，"
        "否则首个请求会在事件循环上现读 backend/api/servers.py")
