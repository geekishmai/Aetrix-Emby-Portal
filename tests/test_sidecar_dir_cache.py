# -*- coding: utf-8 -*-
"""列目录缓存跨进程（§13，fix/sidecar-dir-cache）。

基线 = dd5398a7 的 sidecar 会话级 dir_cache/cid_cache（30s）。本分支把缓存
提到 **handler 级**（键 (account_scope, cid) / (account_scope, rel)），
TTL 默认 300s，修复「宿主每次 build_provider 都 session_upsert → 整体替换
_SessionState → 缓存被清空、30s TTL 覆盖不到扫描批次」的缺口。

覆盖 Claude 决议 5 条补充要求：
1. handler 级缓存加锁（RLock；读写/淘汰/回填不交错）。
2. cookie 刷新（scope 变）整块释放旧 scope。
3. 失效方向自检：TTL 内旧列表只会让扫描多保留、不会误删；播放/取链
   not_found 清祖先链并重试一次。
4. fresh=True 仅人工刷新（管理端刷新/目录选择器）；扫描与 chase-new 不传。
5. 分母口径见 docs §13.3（added+updated+unchanged），与 night-window.sh 一致。
"""
import sys
import threading
import time

import pytest

sys.path.insert(0, ".")
from backend.emby_server import mounts as _mounts_first  # noqa: F401,E402 — 先导避循环
from backend.emby_server import mount_sidecar  # noqa: E402
from backend.emby_server.p115_sidecar import P115SidecarHandler  # noqa: E402
from backend.emby_server.provider_runtime import ProviderRuntimeError  # noqa: E402


class _FakeClient:
    def __init__(self, pages: dict[str, list[dict]], delay: float = 0.0):
        self.pages = pages
        self.calls: list[str] = []
        self.delay = delay
        self._lock = threading.Lock()

    def list_dir(self, cid, limit=1000, deadline=None):
        with self._lock:
            self.calls.append(cid)
        if self.delay:
            time.sleep(self.delay)
        return list(self.pages.get(cid, []))


def _handler(pages, delay=0.0):
    h = P115SidecarHandler(
        client_factory=lambda cookie, ua: _FakeClient(pages, delay=delay))
    return h


def _upsert(h, session_id="s1", cookie="ck-A", root_cid="0"):
    h.session_upsert(session_id, {"cookie": cookie, "ua": "", "root_cid": root_cid})
    return h._states[session_id]


def _scope(h, session_id="s1"):
    return h._states[session_id].scope


# ---------------- 1) 跨会话/跨进程共享 + upsert 保留 ----------------

def test_cache_shared_across_sessions_same_account():
    """同账号两个会话（不同宿主进程）共用同一份 handler 级缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}]}
    h = _handler(pages)
    st1 = _upsert(h, "s1", cookie="ck-A")
    st2 = _upsert(h, "s2", cookie="ck-A")
    client = st1.client
    h._raw_list(st1, "0")
    h._raw_list(st2, "0")
    assert client.calls == ["0"]


def test_session_upsert_same_scope_preserves_cache():
    """同 cookie 重复 upsert（每次 build_provider 都会发生）不清缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}]}
    h = _handler(pages)
    st = _upsert(h, "s1", cookie="ck-A")
    h._raw_list(st, "0")
    # 模拟扫描/来源解析再次构造 provider → 同 session upsert
    st2 = _upsert(h, "s1", cookie="ck-A")
    h._raw_list(st2, "0")
    assert st.client.calls == ["0"], "upsert 不应清掉同 scope 缓存"


def test_cookie_refresh_drops_old_scope():
    """cookie 刷新（scope 变）→ 旧 scope 缓存整块释放。"""
    pages = {"0": []}
    h = _handler(pages)
    st = _upsert(h, "s1", cookie="ck-OLD")
    old_scope = st.scope
    h._raw_list(st, "0")
    assert any(k[0] == old_scope for k in h._dir_cache)
    _upsert(h, "s1", cookie="ck-NEW")
    assert not any(k[0] == old_scope for k in h._dir_cache)
    assert not any(k[0] == old_scope for k in h._cid_cache)


def test_accounts_do_not_share_cache():
    """不同账号（不同 scope）缓存隔离。"""
    pages = {"0": []}
    h = _handler(pages)
    st_a = _upsert(h, "sa", cookie="ck-A")
    st_b = _upsert(h, "sb", cookie="ck-B")
    h._raw_list(st_a, "0")
    h._raw_list(st_b, "0")
    assert st_a.client.calls == ["0"]
    assert st_b.client.calls == ["0"]


# ---------------- 2) TTL / fresh ----------------

def test_ttl_expiry_relists():
    h = _handler({"0": []})
    st = _upsert(h)
    h._raw_list(st, "0")
    key = (st.scope, "0")
    exp, items = h._dir_cache[key]
    h._dir_cache[key] = (time.monotonic() - 1, items)
    h._raw_list(st, "0")
    assert st.client.calls == ["0", "0"]


def test_fresh_bypasses_and_refills_cache():
    """fresh=True（人工刷新）绕过并回填；随后普通读取命中新缓存。"""
    h = _handler({"0": [{"name": "old", "is_dir": False, "cid": "", "pickcode": "p1"}]})
    st = _upsert(h)
    h._raw_list(st, "0")
    # 模拟 115 上新增了一个文件
    st.client.pages["0"] = [{"name": "new", "is_dir": False, "cid": "", "pickcode": "p2"}]
    fresh_items = h._raw_list(st, "0", fresh=True)
    assert [e["name"] for e in fresh_items] == ["new"]
    cached_items = h._raw_list(st, "0")
    assert [e["name"] for e in cached_items] == ["new"]
    assert st.client.calls == ["0", "0"]


def test_fresh_not_used_by_scan_paths():
    """扫描/chase-new 走默认 fresh=False（源码级接线锚定）。"""
    import inspect
    src = inspect.getsource(mount_sidecar.SidecarMountProvider.walk_media)
    assert "fresh=True" not in src
    src2 = inspect.getsource(mount_sidecar.SidecarMountProvider.list_dir)
    assert "fresh: bool = False" in src2


# ---------------- 3) not_found 重试 + 旧列表只保留不误删 ----------------

def test_resolve_play_target_retries_once_after_stale_cid():
    """播放/取链：旧 cid 找不到 → 清祖先链并重试一次成功。"""
    pages = {
        "0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
        "10": [{"name": "剧", "is_dir": True, "cid": "20"}],
        "20": [{"name": "ep.mkv", "is_dir": False, "cid": "", "pickcode": "pc1"}],
    }
    h = _handler(pages)
    st = _upsert(h)
    st.client.download_url = lambda pickcode, deadline=None: "http://cdn/" + pickcode
    # 预热 cid 缓存：/媒体库/剧 -> 20
    assert h._cid_of(st, "/媒体库/剧") == "20"
    # 目录被移动：20 变空，10 下出现新 cid 21
    st.client.pages["20"] = []
    st.client.pages["10"] = [{"name": "剧", "is_dir": True, "cid": "21"}]
    st.client.pages["21"] = [
        {"name": "ep.mkv", "is_dir": False, "cid": "", "pickcode": "pc1"}]
    result = h.resolve_play_target(_Session("s1"), "/媒体库/剧/ep.mkv")
    assert result["url"] == "http://cdn/pc1"
    # 旧 cid 已被清、重试解析到新 cid 并回填缓存
    assert h._cid_cache[(st.scope, "/媒体库/剧")][1] == "21"


class _Session:
    def __init__(self, sid):
        self.session_id = sid


def test_stale_listing_only_retains_never_deletes():
    """失效方向自检（§13 要求 3）。

    缓存 TTL 内拿到的是**旧列表**：已删除的文件仍在 → 扫描把它当「见过」，
    只保留不删除（scanner 清理条件是 guid not in seen_guids）；新增文件不在
    旧列表 → 本轮不入库（延迟到 TTL 后），也不会报错/误删。方向是安全的。
    """
    h = _handler({
        "0": [{"name": "gone.mkv", "is_dir": False, "cid": "", "pickcode": "p1"}],
    })
    st = _upsert(h)
    entries = h._raw_list(st, "0")
    assert [e["name"] for e in entries] == ["gone.mkv"], "旧列表仍返回已删文件（seen → 保留）"
    # 新文件不存在于旧列表：本轮就是看不到（不是错误）
    assert all(e["name"] != "new.mkv" for e in entries)
    # 扫描器清理守卫：只有不在 seen 的条目才可能被删（源码锚定）
    import inspect
    from backend.emby_server import scanner
    src = inspect.getsource(scanner)
    assert "if guid in seen_guids:" in src and "continue" in src


def test_concurrent_reads_and_refills_are_locked():
    """多线程并发读同一 cid：缓存条目完整（不出现半截列表）。"""
    pages = {"0": [{"name": f"f{i}", "is_dir": False, "cid": "", "pickcode": f"p{i}"}
                   for i in range(50)]}
    h = _handler(pages, delay=0.01)
    st = _upsert(h)
    results = []
    errors = []

    def worker():
        try:
            results.append(len(h._raw_list(st, "0")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results == [50] * 8
    assert len(h._dir_cache[(st.scope, "0")][1]) == 50


# ---------------- AH P2：not_found 负缓存 ----------------

def test_negative_cache_skips_repeat_resolution():
    """路径解析失败后短期记负缓存：重复解析不再打 115。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
             "10": []}
    h = _handler(pages)
    st = _upsert(h)
    from backend.emby_server.provider_runtime import ProviderRuntimeError
    with pytest.raises(ProviderRuntimeError):
        h._cid_of(st, "/媒体库/不存在")
    n1 = len(st.client.calls)
    with pytest.raises(ProviderRuntimeError):
        h._cid_of(st, "/媒体库/不存在")
    assert len(st.client.calls) == n1, "负缓存期内不应再发 list"


def test_negative_cache_expires_and_recovers():
    """负缓存到期后重新解析；目录出现后解析成功并清负缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
             "10": []}
    h = _handler(pages)
    st = _upsert(h)
    from backend.emby_server.provider_runtime import ProviderRuntimeError
    with pytest.raises(ProviderRuntimeError):
        h._cid_of(st, "/媒体库/新目录")
    # 到期：手拨负缓存时间戳
    key = (st.scope, "/媒体库/新目录")
    with h._cache_lock:
        h._negative_cache[key] = time.monotonic() - h._negative_ttl - 1
    # 目录出现了
    st.client.pages["10"] = [{"name": "新目录", "is_dir": True, "cid": "20"}]
    assert h._cid_of(st, "/媒体库/新目录") == "20"
    assert not h._negative_hit(st.scope, "/媒体库/新目录")


def test_locate_negative_cache_for_files():
    """文件级 not_found 也进负缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
             "10": [{"name": "剧", "is_dir": True, "cid": "20"}],
             "20": []}
    h = _handler(pages)
    st = _upsert(h)
    from backend.emby_server.provider_runtime import ProviderRuntimeError
    with pytest.raises(ProviderRuntimeError):
        h._locate(st, "/媒体库/剧/无此文件.mkv")
    n1 = len(st.client.calls)
    with pytest.raises(ProviderRuntimeError):
        h._locate(st, "/媒体库/剧/无此文件.mkv")
    assert len(st.client.calls) == n1


# ---------------- AH P2：空目录不再清链 ----------------

def test_empty_listing_keeps_cid_cache():
    """空目录（真实空目录）不再清 cid 链：下一轮解析直接命中缓存。"""
    pages = {"0": [{"name": "媒体库", "is_dir": True, "cid": "10"}],
             "10": [{"name": "空季", "is_dir": True, "cid": "20"}],
             "20": []}
    h = _handler(pages)
    st = _upsert(h)
    # 第一次 list_page：解析出 cid=20 并列出（空）
    h.list_page(_Session("s1"), "/媒体库/空季", None, None)
    n_after_first = len(st.client.calls)
    assert (st.scope, "/媒体库/空季") in h._cid_cache, "cid 缓存应保留"
    # 第二次 list_page：零新增调用（旧实现会清链 → 重解析祖先）
    h.list_page(_Session("s1"), "/媒体库/空季", None, None)
    assert len(st.client.calls) == n_after_first


# ---------------- AH P2：淘汰先淘汰浅层 ----------------

def test_eviction_prefers_shallow_dirs():
    """超限淘汰：浅层（根/一级）先被淘汰，深层保留。"""
    h = _handler({})
    st = _upsert(h)
    h._dir_cache_ttl = 300.0
    h._cache_max_dirs = 4
    # 预填 4 个深层 + 触发浅层回填 → 浅层应先被淘汰
    for depth in range(3, 7):
        cid = str(1000 + depth)
        h._dir_cache[(st.scope, "/" * depth + "deep")] = (
            time.monotonic() + 300, [])
    # 回填浅层（触发淘汰）
    h._raw_list(st, "0")
    assert (st.scope, "0") not in h._dir_cache, "浅层（根）应先被淘汰"
    deep_keys = [k for k in h._dir_cache if k[1].startswith("////")]
    assert len(deep_keys) >= 3, "深层应尽量保留"
