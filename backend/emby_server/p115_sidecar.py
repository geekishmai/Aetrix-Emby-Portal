"""115 sidecar：独立进程形态的 115 provider（挂载-3 照抄定稿）。

部署形态（照抄 FMBY）：独立 Python 进程，UDS 通信，协议
``external-provider-runtime-v1``，manifest
``external-provider-runtime-manifest-v1``，8 项基础能力 + 115 扩展 4 项。
启动方式（生产接线）：服务启动时由 ``p115_sidecar_supervisor`` 显式拉起
本进程（开闸时；关闸 ``PAN115_SIDECAR_ENABLED=0`` 时不拉起，分发回
进程内 ``Pan115Mount``）。手动起进程仍可用（部署者按需、或单测）：

    python -m backend.emby_server.p115_sidecar --sock /run/aetrix/providers/p115/runtime.sock

收益与代价沿用挂载稿 §3.6：115 逻辑崩了不拖垮主进程、115 可独立部署升级；
代价是进程管理、UDS、排障复杂度——本 PR 只给进程与契约；分发切换在
``mounts.build_provider``（115 类型开闸走 sidecar），管理后台 UI 由管理后台
专线另做，本 PR 不含管理页。

实现复用原则：115 上游协议只实现一份——本 handler 复用
``transfer115.Pan115Client``（list_dir / download_url），不重写 115 API；
凭据（Cookie）只由宿主经 ``session_upsert`` 交到本进程内存，sidecar 不读库、
不读盘上凭据、不记日志；``resolve_play_target`` 回包不含 Cookie（宿主重贴，
见 provider_runtime 的换链/搬字节分离口径）。

扩展 4 项的诚实分期（不编能力）：

- ``p115.full_sync``：真实全量 walk（list_dir 递归 + 目录环 guard + 10 万条封顶，
  与 transfer115 单目录封顶同口径）；``generation`` 是本进程单调计数，
  不是 FMBY 的代际 SQLite——持久代际库归扫描线另议，本 PR 不伪造。
- ``p115.diff_refresh``：真实重列指定目录（stateless，无脏目录库）。
- ``p115.life_poll``：轮询差分（不是 115 原生推送）——对监视目录做
  ``diff_refresh``，与本 sidecar 内保存的上一次快照比对，输出新增/
  删除/变更事件（带 ``generation``）。事件字段只含路径、条目与指纹，
  不含 115 未提供的推送字段；首次轮询只建基线、不报事件。
- ``p115.share_full_sync``：分享根全量同步——transfer115 没有分享 API，
  本扩展不编造分享接口；对给定分享根（``root_cid`` 或分享目录 ``path``）
  做与 ``full_sync`` 同款全量 walk，返回 ``generation``/``entries``/
  ``truncated``，分享链接与权限语义不在此口径内，待 115 真实账号实测。
- 两个扩展默认不自动跑，须显式 ``call_extension`` 才工作；不改
  list/stat/play 等已有行为。
"""
from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import os
import time
import signal
import sys
import threading
from dataclasses import dataclass, field

from backend.emby_server import transfer115
from backend.emby_server.env_util import env_float, env_int
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    P115_EXTENSIONS,
    PROTOCOL,
    ProviderRuntimeError,
    ProviderRuntimeServer,
    default_socket_path,
    default_timeout_sec,
    encode_bytes,
)

PROVIDER = "p115"
DEFAULT_PAGE_SIZE = 1000  # 115 /files 单页上限 1000（QA AC-3 P1：减少根目录页数）
MAX_PAGE_SIZE = 1000
MAX_SYNC_ENTRIES = 100000


def _account_scope(cookie: str) -> str:
    """账号作用域：cookie 的 sha256 前 16 位（与 pan115_guard.scope_of 同口径）。"""
    return hashlib.sha256((cookie or "").encode("utf-8")).hexdigest()[:16]


def _map_115_error(exc: Exception) -> ProviderRuntimeError:
    """115 客户端异常 → 契约错误分级（401/403 定性 cookie_expired，见 BYOS-4）。

    Z-1：限速冷却独立为 ``throttled``——不是 cookie_expired（凭据没问题，
    到点自动恢复），调用方不许据此触发重配 Cookie 或判死账号。
    guard 与 transfer115 各有一个 Pan115CooldownError 类（无继承关系），
    统一按 ``retry_after`` 属性 + 类型名识别。
    """
    if isinstance(exc, transfer115.Pan115AuthError):
        return ProviderRuntimeError("cookie_expired", str(exc))
    if type(exc).__name__ == "Pan115DeadlineExceeded":
        # QA AD-1：宿主截止时刻已过 ≠ 冷却——独立 kind，面板不得显示
        # 「账号冷却中」，Z-2 缓存清理门也不得据此跳过。
        return ProviderRuntimeError(
            "deadline_exceeded", str(exc),
            retry_after=getattr(exc, "retry_after", None))
    if isinstance(exc, transfer115.Pan115CooldownError) \
            or type(exc).__name__ == "Pan115CooldownError":
        return ProviderRuntimeError(
            "throttled", str(exc),
            retry_after=getattr(exc, "retry_after", None))
    if isinstance(exc, transfer115.Pan115Error):
        return ProviderRuntimeError("remote_unavailable", str(exc))
    return ProviderRuntimeError("internal", str(exc))


@dataclass
class _SessionState:
    cookie: str
    ua: str
    root_cid: str
    client: object = None
    # 账号作用域（sha256(cookie)[:16]，与 pan115_guard 同口径）：缓存按
    # (scope, cid) / (scope, rel) 键，多账号互不串。
    scope: str = ""
    pick_cache: dict = field(default_factory=dict)
    # life_poll 快照：key = 监视目录元组，value = {path: 指纹}；
    # 只存本 sidecar 进程内存，不落盘、不跨进程（与 full_sync 的 generation 同口径）。
    life_snapshots: dict = field(default_factory=dict)


class P115SidecarHandler:
    """115 sidecar handler：会话内存态 + Pan115Client 直调。"""

    provider = PROVIDER
    extensions = P115_EXTENSIONS

    def __init__(self, client_factory=None, http_client_factory=None):
        # client_factory(cookie, ua) -> 有 list_dir/download_url 的客户端；
        # 默认即 transfer115.Pan115Client。测试注入假客户端用，不改生产路径。
        self._client_factory = client_factory or (
            lambda cookie, ua: transfer115.Pan115Client(cookie, ua=ua))
        self._http_client_factory = http_client_factory or _default_http_client
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()
        self._generation = 0
        # handler 级缓存（2026-11-03 §13）：所有宿主进程共用同一 sidecar 进程，
        # 缓存必须挂在 handler 上、按 (account_scope, cid) 键——不能挂在
        # _SessionState（session_upsert 每次整体替换会清空，30s TTL 覆盖不到
        # 扫描批次）。TTL 默认 300s 覆盖一次扫描 + 紧随的 enrich 旁挂读取。
        # 锁用 RLock：_drop_scope 会在持锁路径里调用。
        self._cache_lock = threading.RLock()
        self._dir_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._cid_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._dir_cache_ttl = env_float("P115_SIDECAR_DIR_CACHE_SEC", 300.0, 0.0)
        self._cid_cache_ttl = env_float(
            "P115_SIDECAR_CID_CACHE_SEC", self._dir_cache_ttl, 0.0)
        self._cache_max_dirs = env_int(
            "P115_SIDECAR_CACHE_MAX_DIRS", 4096, 16, 100000)
        # not_found 负缓存（2026-11-04 AH P2）：路径解析失败后短期记「不存在」，
        # 期间重复解析不再打 115（播放器对死链常连探多次）。TTL 短（默认 60s），
        # 目录移动/新建场景下最多延迟 60s 可见。
        self._negative_cache: dict[tuple[str, str], float] = {}
        self._negative_ttl = env_float(
            "P115_SIDECAR_NEGATIVE_CACHE_SEC", 60.0, 0.0)
        # 并发在途单飞（跨宿主进程 api/ea 或预取与播放同刻到达时，合并同一
        # (scope, cid) 的 list_dir 与同一 (scope, pickcode) 的 download_url，
        # 完成后 finally 立即弹出，不在 sidecar 留持久直链副本）。
        self._inflight_lock = threading.Lock()
        self._list_inflight: dict[tuple[str, str], Future] = {}
        self._dl_inflight: dict[tuple[str, str], Future] = {}

    # ---- 会话 ----
    def session_upsert(self, session_id: str, data: dict) -> None:
        cookie = transfer115.normalize_cookie((data or {}).get("cookie") or "")
        if not cookie:
            raise ProviderRuntimeError("invalid_request", "session data 缺 cookie")
        ua = str((data or {}).get("ua") or "")
        root_cid = str((data or {}).get("root_cid") or "0")
        try:
            client = self._client_factory(cookie, ua)
        except transfer115.Pan115AuthError as exc:
            raise ProviderRuntimeError("invalid_request", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — 工厂异常按上游失败计
            raise _map_115_error(exc) from exc
        scope = _account_scope(cookie)
        state = _SessionState(cookie=cookie, ua=ua, root_cid=root_cid,
                              client=client, scope=scope)
        with self._lock:
            old = self._states.get(session_id)
            self._states[session_id] = state
        # cookie 刷新（scope 变了）→ 旧账号缓存整块释放（不只是不复用）
        if old is not None and old.scope and old.scope != scope:
            self._drop_scope(old.scope)
        # 同 scope：缓存保留（handler 级，本就与 session 对象解耦）

    def session_remove(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)

    def _state(self, session) -> _SessionState:
        state = self._states.get(session.session_id)
        if state is None:
            raise ProviderRuntimeError("session_unknown", f"未知会话：{session.session_id!r}")
        return state

    # ---- 目录解析（与 Pan115Mount._cid_of/_locate 同语义，sidecar 内单份） ----

    def _drop_scope(self, scope: str) -> None:
        """整块释放某账号作用域的缓存（cookie 刷新时调用）。"""
        if not scope:
            return
        with self._cache_lock:
            for cache in (self._dir_cache, self._cid_cache):
                for key in [k for k in cache if k[0] == scope]:
                    cache.pop(key, None)

    def _drop_cid_prefix(self, scope: str, rel: str) -> None:
        """清掉某路径及其子路径的 cid 缓存（目录移动/重命名/不存在时）。"""
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        prefix = rel.rstrip("/") + "/"
        with self._cache_lock:
            for key in [k for k in self._cid_cache
                        if k[0] == scope and (k[1] == rel or k[1].startswith(prefix))]:
                self._cid_cache.pop(key, None)

    def _drop_cid_chain(self, scope: str, rel: str) -> None:
        """清掉某路径的**祖先链 + 自身 + 子孙** cid 缓存。

        目录被移动/重命名时，失效的不止目标本身——祖先的 cid 也可能已变
        （整棵子树被移走）。播放/取链 not_found 重试前调用，保证从根重新
        解析一次。
        """
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        parts = [p for p in rel.split("/") if p]
        ancestors = {"/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)}
        child_prefix = rel.rstrip("/") + "/"
        with self._cache_lock:
            stale_cids = {v for k, (_exp, v) in self._cid_cache.items()
                          if k[0] == scope and (
                              k[1] in ancestors or k[1] == rel
                              or k[1].startswith(child_prefix))}
            for key in [k for k in self._cid_cache if k[0] == scope and (
                    k[1] in ancestors or k[1] == rel
                    or k[1].startswith(child_prefix))]:
                self._cid_cache.pop(key, None)
            # 祖先目录的「旧列表」也一起清：否则重试会从旧列表里重新解析出
            # 同一个死 cid（目录移动后列表已变）。
            for cid in stale_cids:
                self._dir_cache.pop((scope, cid), None)

    def _negative_hit(self, scope: str, rel: str) -> bool:
        """负缓存命中（路径近期确认过不存在）；0 TTL 或过期 = 未命中。"""
        if self._negative_ttl <= 0:
            return False
        now = time.monotonic()
        with self._cache_lock:
            stamp = self._negative_cache.get((scope, rel))
            return bool(stamp is not None and now - stamp < self._negative_ttl)

    def _negative_put(self, scope: str, rel: str) -> None:
        """记录「该路径不存在」；同时淘汰过期负缓存项。"""
        if self._negative_ttl <= 0:
            return
        now = time.monotonic()
        with self._cache_lock:
            self._negative_cache[(scope, rel)] = now
            if len(self._negative_cache) > self._cache_max_dirs:
                expired = [k for k, t in self._negative_cache.items()
                           if now - t >= self._negative_ttl]
                for k in expired:
                    self._negative_cache.pop(k, None)
                # 仍超限：清一半最旧的
                if len(self._negative_cache) > self._cache_max_dirs:
                    ordered = sorted(self._negative_cache.items(),
                                     key=lambda kv: kv[1])
                    for k, _t in ordered[:len(ordered) // 2]:
                        self._negative_cache.pop(k, None)

    def _negative_clear(self, scope: str, rel: str) -> None:
        """清掉某路径的负缓存（目录重新出现/创建后调用）。"""
        with self._cache_lock:
            self._negative_cache.pop((scope, rel), None)

    @staticmethod
    def _ancestor_rels(rel: str) -> list:
        """rel 的祖先链 + 自身（浅 → 深）：负缓存清理用。"""
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        parts = [p for p in rel.split("/") if p]
        return ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)]

    def _raw_list(self, state: _SessionState, cid: str,
                  deadline: float | None = None,
                  fresh: bool = False) -> list[dict]:
        """列目录（handler 级缓存，键 = (account_scope, cid)，TTL 默认 300s）。

        2026-11-03 §13：缓存从 _SessionState 提到 handler——宿主每次
        build_provider 都会 session_upsert 整体替换会话状态，挂在会话上的
        缓存会被反复清空，30s TTL 覆盖不到扫描批次。handler 级缓存被
        api/worker/EA 三个宿主进程共用（都落到同一 sidecar 进程）。
        fresh=True 仅限人工刷新：绕过并回填（扫描/chase-new 不传）。
        """
        key = (state.scope, cid or "0")
        now = time.monotonic()
        if not fresh and self._dir_cache_ttl > 0:
            with self._cache_lock:
                hit = self._dir_cache.get(key)
                if hit is not None and hit[0] > now:
                    return list(hit[1])

        def _fetch_dir() -> list[dict]:
            if not fresh and self._dir_cache_ttl > 0:
                now_inner = time.monotonic()
                with self._cache_lock:
                    hit_inner = self._dir_cache.get(key)
                    if hit_inner is not None and hit_inner[0] > now_inner:
                        return list(hit_inner[1])
            try:
                fetched = state.client.list_dir(cid or "0", deadline=deadline)
            except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
                raise _map_115_error(exc) from exc
            if self._dir_cache_ttl > 0:
                with self._cache_lock:
                    self._dir_cache[key] = (
                        time.monotonic() + self._dir_cache_ttl,
                        list(fetched),
                    )
                    if len(self._dir_cache) > self._cache_max_dirs:
                        self._evict_dir_cache_locked(state.scope)
            return list(fetched)

        if fresh:
            return _fetch_dir()
        return list(self._run_singleflight(self._list_inflight, key, _fetch_dir, deadline=deadline))

    def _run_singleflight(self, table: dict[tuple[str, str], Future],
                          key: tuple[str, str], fn,
                          deadline: float | None = None):
        with self._inflight_lock:
            fut = table.get(key)
            is_leader = fut is None
            if is_leader:
                fut = Future()
                table[key] = fut
        if is_leader:
            try:
                res = fn()
            except BaseException as exc:
                fut.set_exception(exc)
                raise
            else:
                fut.set_result(res)
                return res
            finally:
                with self._inflight_lock:
                    if table.get(key) is fut:
                        table.pop(key, None)
        wait_sec = max(0.05, deadline - time.time()) if deadline is not None else 30.0
        try:
            return fut.result(timeout=wait_sec)
        except FutureTimeoutError as exc:
            raise ProviderRuntimeError("deadline_exceeded", "sidecar 在途单飞等待超时") from exc

    def _evict_dir_cache_locked(self, scope: str) -> None:
        """超限时淘汰一半目录缓存（须持 _cache_lock）。

        AH P2：**先淘汰浅层（根目录/一级分类目录）**。深层目录整轮扫描
        只问一次，其列表当轮内就该被消费；浅层目录下一轮还会再问。旧实现
        按「最早过期」淘汰，大库超限时热点根目录先被挤出去 → 下一轮从根
        重新解析整条祖先链（又一批 list）。排序键 (深度, 过期)：深度小
        （浅）的排前面 = 先淘汰。
        """

        ordered = sorted(self._dir_cache.items(),
                         key=lambda kv: (kv[0][1].count("/") if kv[0][1] not in ("", "/") else 0,
                                         kv[1][0]))
        for k, _v in ordered[:max(1, len(ordered) // 2)]:
            self._dir_cache.pop(k, None)

    def _cid_of(self, state: _SessionState, rel: str,
                deadline: float | None = None,
                fresh: bool = False) -> str:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        now = time.monotonic()
        if not fresh and self._cid_cache_ttl > 0:
            with self._cache_lock:
                hit = self._cid_cache.get((state.scope, rel))
                if hit is not None and hit[0] > now:
                    return hit[1]
        if self._negative_hit(state.scope, rel):
            raise ProviderRuntimeError(
                "not_found", f"115 上找不到目录：{rel}（近期已确认不存在）")
        cid = state.root_cid
        prefix = ""
        for part in [p for p in rel.split("/") if p]:
            prefix = f"{prefix}/{part}"
            cached = None
            if not fresh and self._cid_cache_ttl > 0:
                with self._cache_lock:
                    hit = self._cid_cache.get((state.scope, prefix))
                    if hit is not None and hit[0] > now:
                        cached = hit[1]
            if cached:
                cid = cached
                continue
            child = next(
                (e for e in self._raw_list(state, cid, deadline=deadline,
                                           fresh=fresh)
                 if e.get("name") == part and e.get("is_dir")),
                None,
            )
            if child is None:
                # AE-3②：路径不存在 → 清该前缀（可能目录被移动/改名）；
                # AH P2：记负缓存，短期重复解析不再打 115。
                self._drop_cid_chain(state.scope, prefix)
                self._negative_put(state.scope, rel)
                raise ProviderRuntimeError("not_found", f"115 上找不到目录：{rel}")
            cid = str(child.get("cid") or "")
            if self._cid_cache_ttl > 0:
                with self._cache_lock:
                    self._cid_cache[(state.scope, prefix)] = (
                        now + self._cid_cache_ttl, cid)
        if self._cid_cache_ttl > 0:
            with self._cache_lock:
                self._cid_cache[(state.scope, rel)] = (
                    now + self._cid_cache_ttl, cid)
        # 解析成功 = 路径存在：负缓存作废（目录被新建/移动回来的场景）
        self._negative_clear(state.scope, rel)
        return cid

    def _locate(self, state: _SessionState, rel: str,
                deadline: float | None = None,
                fresh: bool = False) -> dict:
        rel = "/" + (rel or "").lstrip("/")
        if rel in state.pick_cache:
            return {"pickcode": state.pick_cache[rel], "name": os.path.basename(rel)}
        parent, _, name = rel.rpartition("/")
        if self._negative_hit(state.scope, rel):
            raise ProviderRuntimeError(
                "not_found", f"115 上找不到文件：{rel}（近期已确认不存在）")
        for item in self._raw_list(
                state,
                self._cid_of(state, parent or "/", deadline=deadline, fresh=fresh),
                deadline=deadline, fresh=fresh):
            if item.get("name") == name and not item.get("is_dir"):
                return {"pickcode": str(item.get("pickcode") or ""), "name": name,
                        "size": int(item.get("size") or 0)}
        self._negative_put(state.scope, rel)
        raise ProviderRuntimeError("not_found", f"115 上找不到文件：{rel}")

    @staticmethod
    def _entry(item: dict, parent_rel: str) -> dict:
        name = item.get("name") or ""
        rel = (parent_rel.rstrip("/") + "/" + name) if parent_rel != "/" else ("/" + name)
        is_dir = bool(item.get("is_dir"))
        return {
            "name": name,
            "path": rel,
            "is_dir": is_dir,
            "size": int(item.get("size") or 0),
            # 文件稳定 id 即 pickcode；目录没有 pickcode，file_id 为空、entry_id 为 cid。
            "file_id": "" if is_dir else str(item.get("pickcode") or ""),
            "entry_id": str(item.get("cid") or ""),
        }

    # ---- 8 项基础能力 ----
    def list_page(self, session, path: str, page_token, page_size,
                  deadline: float | None = None,
                  fresh: bool = False) -> dict:
        state = self._state(session)
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        cid = self._cid_of(state, rel, deadline=deadline, fresh=fresh)
        try:
            offset = int(page_token) if page_token is not None else 0
        except (TypeError, ValueError):
            raise ProviderRuntimeError("invalid_request", f"page_token 非法：{page_token!r}")
        if offset < 0:
            raise ProviderRuntimeError("invalid_request", "page_token 为负")
        size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
        size = max(1, min(size, MAX_PAGE_SIZE))
        items = self._raw_list(state, cid, deadline=deadline, fresh=fresh)
        # AH P2（空目录清链放大）：不再「列空即清链」。真实空目录（常见：
        # 空季目录、刚建的分类目录）每轮都会命中这里，旧实现把 cid 链清掉
        # → 下一轮重解析又要 list 祖先链，放大成「空目录数 × 深度」次 list。
        # 现在只信任 cid 缓存本身：cid 是解析时从父列表核实的；目录被移动/
        # 重命名的失效场景由 not_found 重试路径（_locate_with_retry →
        # _drop_cid_chain）兜底，不在这里预防性清。
        # _drop_cid_chain(state.scope, rel)  # 已移除，见上注释
        page = items[offset:offset + size]
        entries = [self._entry(item, rel) for item in page]
        nxt = offset + size
        return {
            "path": rel,
            "entries": entries,
            "next_page_token": str(nxt) if nxt < len(items) else None,
            "total": len(items),
        }

    def stat(self, session, path: str) -> dict:
        state = self._state(session)
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        if rel == "/":
            return {"path": "/", "name": "", "is_dir": True, "size": 0,
                    "file_id": "", "entry_id": state.root_cid}
        parent, _, name = rel.rpartition("/")
        for item in self._raw_list(state, self._cid_of(state, parent or "/")):
            if item.get("name") == name:
                return self._entry(item, parent or "/")
        raise ProviderRuntimeError("not_found", f"115 上找不到：{rel}")

    def _fetch_range(self, state: _SessionState, rel: str,
                     byte_offset: int, byte_length,
                     deadline: float | None = None) -> bytes:
        if byte_offset < 0 or (byte_length is not None and byte_length <= 0):
            raise ProviderRuntimeError(
                "invalid_request", f"无效的取段范围：offset={byte_offset} length={byte_length}")
        entry = self._locate_with_retry(state, rel, deadline=deadline)
        pickcode = entry.get("pickcode") or ""
        if not pickcode:
            raise ProviderRuntimeError("not_found", f"115 未返回该文件的 pickcode：{rel}")
        def _fetch_dl() -> str:
            dl_fn = state.client.download_url
            dl_kw: dict = {}
            try:
                import inspect as _inspect
                params = _inspect.signature(dl_fn).parameters
                if "app_first" in params:
                    # 小文件（如 .nfo/.srt）走 webapi /files/download 会返回 f=3 动态
                    # Set-Cookie 绑定的 CDN 链接，无对应会话 Cookie 直拉必返 HTTP 403；
                    # app 通道（proapi /app/chrome/downurl）签出的链接仅绑 UA，实测稳定 206。
                    dl_kw["app_first"] = True
                if "deadline" in params:
                    dl_kw["deadline"] = deadline
            except (TypeError, ValueError):
                dl_kw["deadline"] = deadline
            try:
                return dl_fn(pickcode, **dl_kw)
            except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
                raise _map_115_error(exc) from exc

        url = self._run_singleflight(
            self._dl_inflight, (state.scope, pickcode), _fetch_dl, deadline=deadline
        )
        end = None if byte_length is None else byte_offset + byte_length - 1
        range_header = f"bytes={byte_offset}-" if end is None else f"bytes={byte_offset}-{end}"
        headers = {
            "User-Agent": state.ua or transfer115.PAN115_UA,
            "Referer": "https://115.com/",
            "Range": range_header,
        }
        if state.cookie:
            headers["Cookie"] = state.cookie
        client = self._http_client_factory()
        try:
            resp = client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 — 传输异常按上游不可用计
            raise ProviderRuntimeError("remote_unavailable", f"115 取段传输失败：{exc}") from exc
        status = resp.status_code
        if status in (401, 403):
            raise ProviderRuntimeError("cookie_expired", f"115 取段被拒：HTTP {status}")
        if status >= 400:
            raise ProviderRuntimeError("remote_unavailable", f"115 取段失败：HTTP {status}")
        body = resp.content or b""
        if status == 206:
            return body
        # 上游无视 Range 回 200：本地切片兜底（与 Pan115Mount.open_range 同口径，
        # 正确性有保证，只是多拉了字节）。
        return body[byte_offset:] if byte_length is None else body[byte_offset:byte_offset + byte_length]

    def open_range(self, session, path: str, byte_offset: int, byte_length,
                   deadline: float | None = None) -> dict:
        state = self._state(session)
        body = self._fetch_range(state, path, int(byte_offset or 0), byte_length,
                                 deadline=deadline)
        return {"data_b64": encode_bytes(body), "byte_offset": int(byte_offset or 0),
                "byte_length": len(body)}

    def resolve_display_asset(self, session, path: str, byte_offset: int, byte_length,
                              deadline: float | None = None) -> dict:
        """展示资产直接复用 open_range（FMBY 口径：display_asset 不另起取图路径）。"""
        return self.open_range(session, path, byte_offset, byte_length,
                               deadline=deadline)

    def _locate_with_retry(self, state: _SessionState, rel: str,
                           deadline: float | None = None) -> dict:
        """播放/取链专用：not_found 时清 cid 前缀并重试一次（§13 要求 3）。

        旧缓存里的 cid 可能因目录移动/重命名失效；清前缀后重新解析一次，
        仍找不到才按 not_found 返回。
        """
        try:
            return self._locate(state, rel, deadline=deadline)
        except ProviderRuntimeError as exc:
            if exc.kind != "not_found":
                raise
            self._drop_cid_chain(state.scope, rel)
            # AH P2：负缓存也要一起清（祖先链 + 自身），否则重试会被
            # 「近期已确认不存在」直接顶回来，自愈失效。
            for part_rel in self._ancestor_rels(rel):
                self._negative_clear(state.scope, part_rel)
            return self._locate(state, rel, deadline=deadline)

    def resolve_play_target(self, session, path: str,
                            deadline: float | None = None,
                            pickcode: str | None = None,
                            file_size: int | None = None) -> dict:
        """换链：只回直链与非秘密头，不搬字节、不回 Cookie（宿主重贴）。

        宿主第一层路径→pickcode 缓存（48h）命中但直链过期时直接传 ``pickcode``
        与 ``file_size``，跳过逐级目录定位，并让大文件直接选 app 通道（只调一次 ``download_url``）。
        """
        state = self._state(session)
        pickcode = str(pickcode or "").strip()
        resolved_size = 0
        if file_size is not None:
            try:
                resolved_size = max(0, int(file_size))
            except (TypeError, ValueError):
                resolved_size = 0
        if not pickcode:
            entry = self._locate_with_retry(state, path, deadline=deadline)
            pickcode = entry.get("pickcode") or ""
            if not resolved_size:
                resolved_size = max(0, int(entry.get("size") or 0))
        if not pickcode:
            raise ProviderRuntimeError("not_found", f"115 未返回该文件的 pickcode：{path}")

        def _fetch_dl() -> str:
            dl_fn = state.client.download_url
            dl_kw: dict = {"deadline": deadline}
            if resolved_size > 0:
                try:
                    import inspect as _inspect
                    if "file_size" in _inspect.signature(dl_fn).parameters:
                        dl_kw["file_size"] = resolved_size
                except (TypeError, ValueError):
                    pass
            try:
                return dl_fn(pickcode, **dl_kw)
            except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
                raise _map_115_error(exc) from exc

        url = self._run_singleflight(
            self._dl_inflight, (state.scope, pickcode), _fetch_dl, deadline=deadline
        )
        return {
            "url": url,
            "size": resolved_size,
            "headers": {
                "User-Agent": state.ua or transfer115.PAN115_UA,
                "Referer": "https://115.com/",
                "X-Pickcode": pickcode,
            },
        }
    def health(self, session) -> dict:
        return {"ok": True, "provider": PROVIDER, "protocol": PROTOCOL,
                "capabilities": list(BASE_CAPABILITIES), "extensions": list(P115_EXTENSIONS)}

    # ---- 115 扩展 4 项 ----
    def call_extension(self, session, name: str, params: dict) -> object:
        state = self._state(session)
        if name == "p115.full_sync":
            return self._full_sync(state, params or {})
        if name == "p115.diff_refresh":
            return self._diff_refresh(state, params or {})
        if name == "p115.life_poll":
            return self._life_poll(state, params or {})
        if name == "p115.share_full_sync":
            return self._share_full_sync(state, params or {})
        raise ProviderRuntimeError("unknown_method", f"未知扩展：{name}")

    def _full_sync(self, state: _SessionState, params: dict) -> dict:
        root_cid = str(params.get("root_cid") or state.root_cid)
        root_rel = "/" + str(params.get("root_rel") or "/").strip("/")
        root_rel = root_rel.rstrip("/") or "/"
        entries: list[dict] = []
        seen: set[str] = set()
        stack: list[tuple[str, str]] = [(root_rel, root_cid)]
        while stack:
            rel, cid = stack.pop()
            if cid in seen and cid != root_cid:
                continue  # 目录环 guard（115 正常不会出现，但同步不能因此死循环）
            seen.add(cid)
            for item in self._raw_list(state, cid):
                entry = self._entry(item, rel)
                entry["parent_id"] = cid
                entries.append(entry)
                if len(entries) >= MAX_SYNC_ENTRIES:
                    break
                if item.get("is_dir"):
                    stack.append((entry["path"], str(item.get("cid") or "")))
            if len(entries) >= MAX_SYNC_ENTRIES:
                break
        with self._lock:
            self._generation += 1
            generation = self._generation
        return {"generation": generation, "entries": entries, "truncated": len(entries) >= MAX_SYNC_ENTRIES}

    def _diff_refresh(self, state: _SessionState, params: dict) -> dict:
        directories = params.get("directories") or []
        entries: list[dict] = []
        missing: list[str] = []
        for rel in directories:
            rel = "/" + str(rel or "").lstrip("/")
            rel = rel.rstrip("/") or "/"
            try:
                cid = self._cid_of(state, rel)
            except ProviderRuntimeError:
                missing.append(rel)
                continue
            for item in self._raw_list(state, cid):
                entry = self._entry(item, rel)
                entry["parent_id"] = cid
                entries.append(entry)
        return {"entries": entries, "missing": missing}

    @staticmethod
    def _snapshot_fingerprint(entry: dict) -> dict:
        """快照指纹：只用条目自身已有的字段，不编造上游字段。"""
        return {
            "entry_id": entry.get("entry_id") or "",
            "file_id": entry.get("file_id") or "",
            "is_dir": bool(entry.get("is_dir")),
            "size": int(entry.get("size") or 0),
        }

    def _life_poll(self, state: _SessionState, params: dict) -> dict:
        """轮询差分 life_poll：115 无原生推送，此处明确不是推送。

        对监视目录做 _diff_refresh，与上一次快照比对：新增 / 删除 /
        变更（指纹变化）。首次轮询只建基线、不报事件，避免把存量
        全量误报成新增。快照只存本进程内存，sidecar 重启后重新建基线。
        """
        directories = params.get("directories") or []
        if not directories and params.get("path"):
            directories = [params.get("path")]
        if not directories:
            directories = ["/"]
        normalized: list[str] = []
        for rel in directories:
            rel = "/" + str(rel or "").lstrip("/")
            rel = rel.rstrip("/") or "/"
            if rel not in normalized:
                normalized.append(rel)
        refreshed = self._diff_refresh(state, {"directories": normalized})
        new_snapshot = {
            entry["path"]: self._snapshot_fingerprint(entry)
            for entry in refreshed["entries"]
        }
        by_path = {entry["path"]: entry for entry in refreshed["entries"]}
        key = tuple(normalized)
        old_snapshot = state.life_snapshots.get(key)
        baseline = old_snapshot is None
        events: list[dict] = []
        if not baseline:
            for path in sorted(set(new_snapshot) | set(old_snapshot)):
                if path not in old_snapshot:
                    events.append({"type": "added", "path": path,
                                   "entry": by_path[path], "previous": None})
                elif path not in new_snapshot:
                    events.append({"type": "deleted", "path": path,
                                   "entry": None, "previous": old_snapshot[path]})
                elif new_snapshot[path] != old_snapshot[path]:
                    events.append({"type": "changed", "path": path,
                                   "entry": by_path[path],
                                   "previous": old_snapshot[path]})
        with self._lock:
            self._generation += 1
            generation = self._generation
            state.life_snapshots[key] = new_snapshot
        return {
            "generation": generation,
            "events": events,
            "directories": normalized,
            "missing": refreshed["missing"],
            "source": "poll_diff",
            "is_poll_diff": True,
            "baseline": baseline,
            "snapshot_size": len(new_snapshot),
            "note": "轮询差分：115 无原生 life 推送，本扩展以重列结果与上一次快照"
                    "比对生成事件；事件字段只含路径、条目与指纹，不含 115 未"
                    "提供的推送字段。",
        }

    def _share_full_sync(self, state: _SessionState, params: dict) -> dict:
        """分享根全量同步：transfer115 无分享 API，不编造分享接口。

        给定分享根（root_cid / share_root_cid / share_cid，或分享目录
        path）做与 full_sync 同款全量 walk。返回 generation / entries /
        truncated，与 full_sync 同款语义；分享链接、权限、过期等分享
        语义不在此口径内，待 115 真实账号实测。
        """
        root_cid = (
            params.get("root_cid") or params.get("share_root_cid")
            or params.get("share_cid") or ""
        )
        path = params.get("path") or params.get("share_path") or params.get("root_path")
        root_rel = "/"
        if path:
            root_rel = "/" + str(path).lstrip("/")
            root_rel = root_rel.rstrip("/") or "/"
            if not root_cid:
                root_cid = self._cid_of(state, root_rel)
        if not root_cid:
            root_cid = state.root_cid
        result = self._full_sync(
            state, {"root_cid": str(root_cid), "root_rel": root_rel})
        result.update({
            "share_root_cid": str(root_cid),
            "share_mode": "full_sync_via_root",
            "note": "分享口待 115 真实账号实测：transfer115 无分享 API，本扩展"
                    "未调用任何 115 分享接口，仅对给定分享根做全量同步；"
                    "分享链接、权限、过期等分享语义不在此口径内。",
        })
        return result


def _default_http_client():
    import httpx

    return httpx.Client(timeout=default_timeout_sec(), follow_redirects=True)


def main(argv=None) -> int:
    """sidecar 进程入口：绑 UDS，阻塞服务；SIGINT/SIGTERM 优雅停服清 socket。"""
    parser = argparse.ArgumentParser(description="115 provider sidecar (external-provider-runtime-v1)")
    parser.add_argument("--sock", default=None,
                        help="UDS 路径（默认 PROVIDER_RUNTIME_SOCK_DIR/p115/runtime.sock）")
    args = parser.parse_args(argv)
    sock_path = args.sock or default_socket_path(PROVIDER)
    handler = P115SidecarHandler()
    server = ProviderRuntimeServer(handler, sock_path)
    stop = threading.Event()

    def _on_signal(signum, _frame):
        stop.set()
        server.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    print(f"p115 sidecar listening on {sock_path} protocol={PROTOCOL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
