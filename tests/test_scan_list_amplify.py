# -*- coding: utf-8 -*-
"""首扫 list 放大修复（fix/scan-list-amplify，2026-11-03）。

四项（对应 Claude 派单 ①②③④）：
① sidecar 列目录分页逐页重试；单页失败不判整根失败（返回已取部分 +
   walk_media 末尾抛错让扫描器记 failed_roots，清理阶段跳过）。
② 115 /files 页大小默认 200 → 1000（QA AC-3 P1）：1020 项根目录 6 页 → 2 页。
③ sidecar 会话目录缓存（cid→items 30s）+ 中间路径 cid 缓存：消除
   「每个条目的 _cid_of/_locate 都重列父目录」造成的 16.6 list/部。
④ 补全抢单兼容外部停泊的 enrich_status='retrying'（旧谓词只认 pending，
   244 行停泊行永远不会被领取）。
"""
import sys
import time

import pytest

sys.path.insert(0, ".")
from backend.emby_server import mounts as _mounts_first  # noqa: F401,E402 — 先导避循环
from backend.emby_server import p115_sidecar as sidecar_mod  # noqa: E402
from backend.emby_server import mount_sidecar  # noqa: E402
from backend.emby_server import transfer115  # noqa: E402
from backend.emby_server.p115_sidecar import P115SidecarHandler  # noqa: E402


# ---------------- ② 页大小 ----------------

def test_transfer115_list_dir_default_page_1000():
    """transfer115.list_dir 默认页 = 1000（115 /files 单页上限）。"""
    import inspect
    sig = inspect.signature(transfer115.Pan115Client.list_dir)
    assert sig.parameters["limit"].default == 1000


def test_sidecar_default_page_size_1000():
    assert sidecar_mod.DEFAULT_PAGE_SIZE == 1000
    assert sidecar_mod.MAX_PAGE_SIZE == 1000


# ---------------- ③ sidecar 目录缓存 ----------------

class _FakeClient:
    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.calls: list[str] = []

    def list_dir(self, cid, limit=200):
        self.calls.append(cid)
        return list(self.pages.get(cid, []))


def _handler_with(pages):
    h = P115SidecarHandler(client_factory=lambda cookie, ua: _FakeClient(pages))
    h.session_upsert("s1", {"cookie": "ck", "ua": "", "root_cid": "0"})
    return h


def test_sidecar_dir_cache_reuses_listing_within_ttl():
    """同一 cid 的多次列举在 30s 内只打一次 115。"""
    h = _handler_with({"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}]})
    client = h._states["s1"].client
    st = h._states["s1"]
    h._raw_list(st, "0")
    h._raw_list(st, "0")
    assert client.calls == ["0"]


def test_sidecar_dir_cache_expires_after_ttl(monkeypatch):
    h = _handler_with({"0": []})
    st = h._states["s1"]
    client = st.client
    h._raw_list(st, "0")
    # 手动把缓存过期时间拨到过去
    exp, items = st.dir_cache["0"]
    st.dir_cache["0"] = (time.monotonic() - 1, items)
    h._raw_list(st, "0")
    assert client.calls == ["0", "0"]


def test_sidecar_cid_cache_caches_intermediate_prefixes():
    """路径解析缓存每一级前缀：第二个同父目录的条目不再重列父目录。"""
    pages = {
        "0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
        "10": [{"name": "华语电影", "is_dir": True, "cid": "20"}],
        "20": [{"name": "电影A", "is_dir": True, "cid": "30"},
               {"name": "电影B", "is_dir": True, "cid": "31"}],
    }
    h = _handler_with(pages)
    st = h._states["s1"]
    client = st.client
    assert h._cid_of(st, "/媒体库/华语电影/电影A") == "30"
    calls_after_first = list(client.calls)
    assert h._cid_of(st, "/媒体库/华语电影/电影B") == "31"
    # 第二次解析：前缀缓存命中 + 父目录列举已进 dir_cache（30s），
    # 零新增 115 调用（旧实现要重列 0 → 10 → 20 共 3 次）
    new_calls = client.calls[len(calls_after_first):]
    assert new_calls == [], new_calls


# ---------------- ① 分页重试 + partial ----------------

def _provider_with_client(client):
    prov = mount_sidecar.P115SidecarMount.__new__(mount_sidecar.P115SidecarMount)
    prov.session_id = "s"
    prov.client = client
    prov._partial_listing = False
    return prov


class _FlakyClient:
    def __init__(self, failures_before_success=0, total_pages=3):
        self.calls = 0
        self.failures_before_success = failures_before_success
        self.total_pages = total_pages

    def list_page(self, session_id, rel, token=None):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise mount_sidecar.ProviderRuntimeError("timeout", "sidecar 调用超时：list_page")
        idx = int(token or 0)
        entries = [{"name": f"f{idx}", "path": f"{rel}/f{idx}", "is_dir": False,
                    "size": 1, "entry_id": "", "file_id": ""}]
        nxt = str(idx + 1) if idx + 1 < self.total_pages else None
        return {"entries": entries, "next_page_token": nxt}


def test_list_dir_retries_single_page_failure(monkeypatch):
    """单页失败重试成功 → 正常返回，不标记 partial。"""
    monkeypatch.setattr(mount_sidecar.time, "sleep", lambda s: None)
    prov = _provider_with_client(_FlakyClient(failures_before_success=2, total_pages=1))
    entries = prov.list_dir("/")
    assert len(entries) == 1
    assert prov._partial_listing is False


def test_list_dir_partial_returns_collected_pages(monkeypatch):
    """某页重试 3 次仍失败 → 返回已拿到的部分并标记 partial。"""
    monkeypatch.setattr(mount_sidecar.time, "sleep", lambda s: None)

    class AlwaysFailAfterFirst:
        def __init__(self):
            self.calls = 0
        def list_page(self, session_id, rel, token=None):
            self.calls += 1
            if token is None:
                return {"entries": [{"name": "a", "path": "/a", "is_dir": False,
                                     "size": 1, "entry_id": "", "file_id": ""}],
                        "next_page_token": "1"}
            raise mount_sidecar.ProviderRuntimeError("timeout", "超时")

    prov = _provider_with_client(AlwaysFailAfterFirst())
    entries = prov.list_dir("/")
    assert [e.name for e in entries] == ["a"]
    assert prov._partial_listing is True


def test_walk_media_raises_after_yielding_partial(monkeypatch):
    """walk_media：partial 时先产出可见文件，遍历结束再抛 MountError
    （扫描器据此记 failed_roots，清理阶段跳过）。"""
    monkeypatch.setattr(mount_sidecar.time, "sleep", lambda s: None)

    class PartialClient:
        def list_page(self, session_id, rel, token=None):
            if rel == "/" and token is None:
                return {"entries": [
                    {"name": "movie.mkv", "path": "/movie.mkv", "is_dir": False,
                     "size": 10, "entry_id": "", "file_id": ""},
                    {"name": "dir", "path": "/dir", "is_dir": True,
                     "size": 0, "entry_id": "", "file_id": ""}],
                    "next_page_token": "1"}
            if rel == "/dir":
                return {"entries": [], "next_page_token": None}
            raise mount_sidecar.ProviderRuntimeError("timeout", "超时")

    prov = _provider_with_client(PartialClient())
    got = []
    with pytest.raises(mount_sidecar.MountError) as ei:
        for f in prov.walk_media("/"):
            got.append(f.name)
    assert got == ["movie.mkv"]
    assert "部分分页失败" in str(ei.value)


# ---------------- ④ 抢单兼容 retrying ----------------

def test_claimable_statuses_include_retrying():
    from backend.emby_server import enrich_worker
    assert "pending" in enrich_worker._CLAIMABLE_STATUSES
    assert "retrying" in enrich_worker._CLAIMABLE_STATUSES


def test_claim_batch_picks_due_retrying_row(db):
    """停泊的 retrying 行到期后可被领取（旧谓词只认 pending）。"""
    from datetime import datetime, timedelta
    from backend.emby_server import enrich_worker
    it = _mk(db, enrich_status="retrying",
             enrich_next_retry_at=datetime.now() - timedelta(minutes=1))
    claimed = enrich_worker._claim_batch(db, 10)
    assert [c.id for c in claimed] == [it.id]
    db.refresh(it)
    assert it.enrich_status == "enriching"


def test_claim_batch_skips_not_due_retrying_row(db):
    """停泊未到期（next_retry_at 在未来）的 retrying 行不被领取。"""
    from datetime import datetime, timedelta
    from backend.emby_server import enrich_worker
    _mk(db, enrich_status="retrying",
        enrich_next_retry_at=datetime.now() + timedelta(hours=10))
    assert enrich_worker._claim_batch(db, 10) == []


@pytest.fixture()
def db():
    """独立建表（不走 init_db 的跨进程 fcntl 锁）：Windows 也能跑抢单谓词。"""
    from backend.database import Base, SessionLocal, engine
    from backend import models as base_models  # noqa: F401 — web_users 等基础表
    from backend.emby_server import models as em  # noqa: F401 — 注册表
    try:
        import tzdata  # noqa: F401 — Windows venv 缺 tzdata 时 zoneinfo 不可用
    except ImportError:
        pytest.skip("Windows venv 缺 tzdata，跳过 DB 用例（CI/Linux 有系统时区库）")
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过 DB 用例：{exc}")
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _mk(db, **kw):
    import uuid
    from backend.emby_server import models as em
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"), file_path="/tmp/x.mp4",
        enrich_status=kw.pop("enrich_status", "pending"), **kw)
    db.add(it)
    db.commit()
    return it


def test_claim_predicates_wired_to_claimable_statuses():
    """源码级接线：抢单组查询/行查询/原子 UPDATE 都走 _CLAIMABLE_STATUSES。"""
    import inspect
    from backend.emby_server import enrich_worker as ew
    src = inspect.getsource(ew._claim_batch)
    assert src.count("_CLAIMABLE_STATUSES") == 2, "groups/rows 两处谓词"
    src2 = inspect.getsource(ew._claim_group_atomic)
    assert "_CLAIMABLE_STATUSES" in src2, "原子 UPDATE 谓词"
    # 不再有只认 pending 的旧谓词
    assert 'enrich_status == "pending", due' not in src


# ---------------- AE-3①：count 校验（不静默少条目） ----------------

def test_list_dir_count_complete(monkeypatch):
    """count=3、两页返回 3 条 → 正常返回 3 条。"""
    from backend.emby_server import transfer115 as t115
    from backend.emby_server import pan115_guard as pg
    client = t115.Pan115Client.__new__(t115.Pan115Client)
    client.cookie = "ck-c1"
    client.ua = ""
    pages = [
        {"state": 1, "count": 3, "data": [{"fid": "1", "n": "a"}, {"fid": "2", "n": "b"}]},
        {"state": 1, "count": 3, "data": [{"fid": "3", "n": "c"}]},
    ]
    calls = {"n": 0}
    def fake_http(method, url, **kw):
        body = pages[min(calls["n"], 1)]
        calls["n"] += 1
        return body
    monkeypatch.setattr(t115, "_http_json", fake_http)
    monkeypatch.setattr(pg, "before_call", lambda *a, **kw: None)
    out = client.list_dir("cid", limit=2)
    assert [e["name"] for e in out] == ["a", "b", "c"]


def test_list_dir_count_mismatch_raises(monkeypatch):
    """count=5 但数据提前耗尽 → 抛错（绝不静默少条目）。"""
    from backend.emby_server import transfer115 as t115
    from backend.emby_server import pan115_guard as pg
    client = t115.Pan115Client.__new__(t115.Pan115Client)
    client.cookie = "ck-c2"
    client.ua = ""
    pages = [
        {"state": 1, "count": 5, "data": [{"fid": "1", "n": "a"}]},
        {"state": 1, "count": 5, "data": []},
    ]
    calls = {"n": 0}
    def fake_http(method, url, **kw):
        body = pages[min(calls["n"], 1)]
        calls["n"] += 1
        return body
    monkeypatch.setattr(t115, "_http_json", fake_http)
    monkeypatch.setattr(pg, "before_call", lambda *a, **kw: None)
    with pytest.raises(t115.Pan115Error) as ei:
        client.list_dir("cid", limit=2)
    assert "不完整" in str(ei.value)


# ---------------- AE-3②：cid 缓存 TTL / 失效 ----------------

def test_cid_cache_ttl_expires(monkeypatch):
    """cid 缓存 30s 过期：过期后重新解析（列父目录）。"""
    pages = {
        "0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
        "10": [{"name": "华语电影", "is_dir": True, "cid": "20"}],
    }
    h = _handler_with(pages)
    st = h._states["s1"]
    client = st.client
    assert h._cid_of(st, "/媒体库/华语电影") == "20"
    n1 = len(client.calls)
    # 缓存命中：不新增调用
    assert h._cid_of(st, "/媒体库/华语电影") == "20"
    assert len(client.calls) == n1
    # 把 TTL 拨到过去 → 重新解析（会再列父目录；dir_cache 30s 仍命中，
    # 因此这里断言的是 cid 缓存本身失效：清掉 dir_cache 才看得到调用）
    st.cid_cache_at["/媒体库/华语电影"] = 0.0
    st.dir_cache.clear()
    assert h._cid_of(st, "/媒体库/华语电影") == "20"
    assert len(client.calls) > n1


def test_cid_prefix_dropped_on_not_found():
    """路径不存在 → 清该前缀 cid 缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
             "10": []}
    h = _handler_with(pages)
    st = h._states["s1"]
    from backend.emby_server.provider_runtime import ProviderRuntimeError
    with pytest.raises(ProviderRuntimeError):
        h._cid_of(st, "/媒体库/不存在")
    assert "/媒体库/不存在" not in st.cid_cache
    # 父级缓存保留（仍有效）
    assert st.cid_cache.get("/媒体库") == "10"


def test_retrying_with_null_next_retry_is_claimable_expected(db):
    """AE-3③：retrying 且 next_retry_at=NULL = 立即到期，可被领取（期望行为）。

    停泊行必须显式写 next_retry_at 才会被推迟；NULL 与 pending 同口径
    （无退避等待），不是漏洞。
    """
    from backend.emby_server import enrich_worker
    it = _mk(db, enrich_status="retrying", enrich_next_retry_at=None)
    claimed = enrich_worker._claim_batch(db, 10)
    assert [c.id for c in claimed] == [it.id]
