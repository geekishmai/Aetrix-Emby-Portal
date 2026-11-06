"""旁挂 NFO 按 115 pickcode 永久缓存（perf/nfo-read-cost，默认关闭）。

背景（2026-11-06 入库实测）：
- 远程 115 挂载读旁挂 ``.nfo`` 走 ``open_range`` → ``p115_sidecar._fetch_range`` →
  ``download_url(pickcode)``，每次读取消耗 1 次 115 直链配额（``link`` 桶 1/s）；
- 两队列刮削（``identify`` + ``fill``）与后续重扫 / 巡检回捞会对同一 ``.nfo``
  重复发起直链请求，15 分钟实测取链 576 次。

不变量与安全契约：
- **内容寻址**：115 的 ``pickcode`` 绑定不可变文件对象（覆盖重传会生成新对象与新
  ``pickcode``，移动 / 改名保持 ``pickcode`` 不变）；信封内同时记录 ``size`` 字节数，
  若已知大小变化自动视为未命中并覆写。
- **永久缓存（无 TTL 过期）**：L1 进程内 LRU（上限 4096）+ L2 本地分片 JSON 文件
  （原子写临时文件 + ``os.replace``）；默认优先落持久化数据卷 ``/data/nfo_cache``
  （跨容器重建保留），容量上限 ``EMBY_NFO_CACHE_MB``（默认 256 MB，约 50 万份解析后 NFO）。
- **零凭据 / 零明文标识**：内存键与磁盘文件名均为 ``sha256("nfo:v1:" + pickcode)``
  不可逆摘要；信封仅存 ``parse_nfo`` 解析后的元数据字典（或 ``None``），绝不落
  Cookie、115 直链 URL、挂载路径或明文 ``pickcode``。
- **故障不固化**：底层读取抛异常（网络超时 / 429 / MountError）绝不写缓存，条目照常进重试；
  仅在读取成功返回文本后缓存解析结果（含空/非 Kodi XML 的 ``None`` 结果，避免坏文件永久重取）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

_L1_MAX_ENTRIES = 4096
_PRUNE_TRIGGER_BYTES = 16 * 1024 * 1024
_PRUNE_MIN_INTERVAL = 300.0
_KEEP_RATIO = 0.8

_mem_cache: "OrderedDict[str, tuple[int, Any]]" = OrderedDict()
_mem_lock = threading.Lock()

_inflight_guard = threading.Lock()
_inflight_locks: dict[str, threading.Lock] = {}

_stats_lock = threading.Lock()
_stats: dict[str, int] = {
    "hits": 0,
    "l1_hits": 0,
    "disk_hits": 0,
    "misses": 0,
    "writes": 0,
    "evicted": 0,
    "errors": 0,
}

_prune_guard = threading.Lock()
_written_since_prune = 0
_last_prune = 0.0
_disabled = False
_warned = False


def _cfg_enabled() -> bool:
    return (os.getenv("EMBY_NFO_PICKCODE_CACHE", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _cfg_dir() -> str:
    explicit = os.getenv("EMBY_NFO_CACHE_DIR", "").strip()
    if explicit:
        return explicit
    cache_root = os.getenv("EMBY_CACHE_DIR", "").strip()
    if cache_root:
        return os.path.join(cache_root, "nfo")
    # 生产 docker-compose.netcup.yml 将持久化卷挂在 /data（EMBY_IMAGE_DIR=/data/images）：
    # 优先落 /data/nfo_cache 保证跨容器重建不丢永久缓存；开发/测试无 /data 时回落临时目录。
    image_dir = os.getenv("EMBY_IMAGE_DIR", "").strip()
    if image_dir:
        parent = os.path.dirname(image_dir.rstrip("/\\"))
        if parent and os.path.isdir(parent) and os.access(parent, os.W_OK):
            return os.path.join(parent, "nfo_cache")
    return os.path.join("/tmp/aetrix_cache", "nfo")


def _cfg_max_bytes() -> int:
    return max(0, env_int("EMBY_NFO_CACHE_MB", 256)) * 1024 * 1024


def enabled() -> bool:
    return _cfg_enabled() and not _disabled


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def _warn_once(reason: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("NFO pickcode 永久缓存降级为仅内存（不影响刮削）: %s", reason)


def _digest_of(pickcode: str) -> str:
    norm = (pickcode or "").strip().lower()
    return hashlib.sha256(f"nfo:v1:{norm}".encode("utf-8")).hexdigest()


def _key_path(digest: str) -> str:
    return os.path.join(_cfg_dir(), digest[:2], f"{digest}.json")


def _size_matches(expected_size: int, cached_size: int) -> bool:
    if expected_size > 0 and cached_size > 0:
        return expected_size == cached_size
    return True


def _l1_get(digest: str, size: int) -> tuple[bool, Any]:
    with _mem_lock:
        row = _mem_cache.get(digest)
        if row is None:
            return False, None
        cached_size, data = row
        if not _size_matches(size, cached_size):
            _mem_cache.pop(digest, None)
            return False, None
        _mem_cache.move_to_end(digest)
        return True, copy.deepcopy(data)


def _l1_put(digest: str, size: int, data: Any) -> None:
    with _mem_lock:
        _mem_cache[digest] = (max(0, int(size or 0)), copy.deepcopy(data))
        _mem_cache.move_to_end(digest)
        while len(_mem_cache) > _L1_MAX_ENTRIES:
            _mem_cache.popitem(last=False)


def _disk_get(digest: str, size: int) -> tuple[bool, Any, int]:
    if _disabled:
        return False, None, 0
    path = _key_path(digest)
    try:
        with open(path, "r", encoding="utf-8") as f:
            env = json.load(f)
    except FileNotFoundError:
        return False, None, 0
    except (OSError, ValueError, TypeError) as exc:
        _bump("errors")
        logger.debug("读取 NFO 缓存文件失败 %s: %s", path, exc)
        return False, None, 0
    if not isinstance(env, dict) or env.get("v") != 1 or "data" not in env:
        return False, None, 0
    try:
        cached_size = max(0, int(env.get("size") or 0))
    except (TypeError, ValueError):
        cached_size = 0
    if not _size_matches(size, cached_size):
        return False, None, 0
    return True, env.get("data"), cached_size


def _disk_put(digest: str, size: int, data: Any) -> None:
    global _disabled, _written_since_prune
    if _disabled:
        return
    path = _key_path(digest)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        _disabled = True
        _warn_once(f"目录创建失败 ({exc})")
        return
    payload = json.dumps(
        {
            "v": 1,
            "ts": int(time.time()),
            "size": max(0, int(size or 0)),
            "data": data,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except OSError as exc:
        _bump("errors")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.debug("写入 NFO 缓存文件失败 %s: %s", path, exc)
        return
    _bump("writes")
    with _stats_lock:
        _written_since_prune += len(payload.encode("utf-8"))
        need_prune = (
            _written_since_prune >= _PRUNE_TRIGGER_BYTES
            or (time.monotonic() - _last_prune) >= _PRUNE_MIN_INTERVAL
        )
    if need_prune:
        _maybe_prune()


def _maybe_prune() -> None:
    global _written_since_prune, _last_prune
    max_bytes = _cfg_max_bytes()
    if max_bytes <= 0 or _disabled:
        return
    if not _prune_guard.acquire(blocking=False):
        return
    try:
        with _stats_lock:
            _written_since_prune = 0
            _last_prune = time.monotonic()
        root = _cfg_dir()
        if not os.path.isdir(root):
            return
        files: list[tuple[float, int, str]] = []
        total = 0
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, fp))
                total += st.st_size
        if total <= max_bytes:
            return
        target = int(max_bytes * _KEEP_RATIO)
        files.sort(key=lambda r: r[0])
        evicted = 0
        for _mtime, fsize, fp in files:
            if total <= target:
                break
            try:
                os.unlink(fp)
                total -= fsize
                evicted += 1
            except OSError:
                continue
        if evicted:
            _bump("evicted", evicted)
    finally:
        _prune_guard.release()


def _inflight_lock(digest: str) -> threading.Lock:
    with _inflight_guard:
        lock = _inflight_locks.get(digest)
        if lock is None:
            if len(_inflight_locks) >= 8192:
                _inflight_locks.clear()
            lock = threading.Lock()
            _inflight_locks[digest] = lock
        return lock


def get_or_load(
    pickcode: str,
    size: int,
    loader: Callable[[], str],
    parser: Callable[[str], Any],
) -> tuple[bool, Any]:
    """按 pickcode 查永久缓存；未命中则单飞调用 loader() + parser() 并写回。

    返回 ``(from_cache, parsed_data)``。``loader()`` 抛异常时原样上抛、不写缓存。
    """
    clean_pc = (pickcode or "").strip()
    norm_size = max(0, int(size or 0))
    if not enabled() or not clean_pc:
        text = loader()
        return False, parser(text)

    digest = _digest_of(clean_pc)
    hit, data = _l1_get(digest, norm_size)
    if hit:
        _bump("hits")
        _bump("l1_hits")
        return True, data

    with _inflight_lock(digest):
        hit, data = _l1_get(digest, norm_size)
        if hit:
            _bump("hits")
            _bump("l1_hits")
            return True, data
        disk_hit, disk_data, disk_size = _disk_get(digest, norm_size)
        if disk_hit:
            _l1_put(digest, norm_size or disk_size, disk_data)
            _bump("hits")
            _bump("disk_hits")
            return True, copy.deepcopy(disk_data)

        _bump("misses")
        text = loader()
        parsed = parser(text)
        _l1_put(digest, norm_size, parsed)
        _disk_put(digest, norm_size, parsed)
        return False, copy.deepcopy(parsed)


def stats() -> dict[str, Any]:
    with _stats_lock:
        snap = dict(_stats)
    with _mem_lock:
        snap["l1_entries"] = len(_mem_cache)
    snap["enabled"] = enabled()
    snap["cache_dir"] = _cfg_dir()
    return snap


def reset_for_tests() -> None:
    global _disabled, _warned, _written_since_prune, _last_prune
    with _mem_lock:
        _mem_cache.clear()
    with _inflight_guard:
        _inflight_locks.clear()
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0
        _written_since_prune = 0
        _last_prune = 0.0
    _disabled = False
    _warned = False
