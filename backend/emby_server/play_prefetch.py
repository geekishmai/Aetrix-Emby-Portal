# -*- coding: utf-8 -*-
"""起播取链预取（PlaybackInfo 阶段，2026-11-04 原型，默认关）。

冒烟 play-timing 实测（2026-11-03 20:26，12金鸭 mount://1）：play_target=4369ms、
ttfb=5535ms —— 取链（换链全链：解析路径 → sidecar 往返 → guard link 桶排队 →
downurl）占首帧 4.4s。

根因（代码定位，见 docs/aetrix-trial-scan.md §18）：
1. PlaybackInfo（客户端点播第一步，先于 stream 5.6s）**不碰换链**——
   ``resolve_play_target`` 只在 stream 请求里跑；
2. 首播必然未命中三层缓存（宿主 _RESOLVE_CACHE 600s、mounts._pan115_url_cache
   3h、sidecar pick_cache 进程内）；
3. 换链全链 = 逐级列目录解析 cid（§13 缓存冷时 1 次 list/级）+ parent 目录
   list 找 pickcode（1 次 list）+ guard link 桶（1/s、burst 2，排队上限 5s）
   + downurl 请求本身（web→app 两条通道，首播 app 通道 RSA 往返）。

本原型：PlaybackInfo 成功后**后台线程**对「主 DirectPlayUrl 对应条目」做一次
预取（走既有 resolve_play_target，命中三层缓存），下一个 stream 请求直接吃
缓存 → play_target 段趋近 0。

**115 调用纪律**：预取走既有 ``resolve_play_target``，其换链结果写
``_pan115_url_cache``（3h）与 ``_RESOLVE_CACHE``（600s）——不新增取链次数
（把 stream 时的取链**提前**到 PlaybackInfo，次数不变）；预取本身被
``PLAY_TTFB_PREFETCH_ENABLED``（默认 0）总闸关闭；进程内单飞
``_PREFETCH_INFLIGHT`` 防同条目重复预取。link 桶 1/s 上限不变（guard 是唯一
出口，预取也过它）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

PREFETCH_ENABLED = "PLAY_TTFB_PREFETCH_ENABLED"
#: 预取结果多久内不重复（同条目反复点播详情页时防抖）
PREFETCH_DEDUP_SEC = 60.0

_prefetch_lock = threading.Lock()
_prefetch_inflight: set = set()
_prefetch_done_at: dict = {}          # item_id -> monotonic


def prefetch_enabled() -> bool:
    return env_int(PREFETCH_ENABLED, 0, 0, 1) == 1


def _recently_done(item_id) -> bool:
    now = time.monotonic()
    stamp = _prefetch_done_at.get(item_id)
    return bool(stamp is not None and now - stamp < PREFETCH_DEDUP_SEC)


def try_prefetch(db, item) -> bool:
    """PlaybackInfo 后台预取：解析目标并写缓存（不发 302、不下发任何东西）。

    返回是否真的起了一个预取（调用方不需要等它）。任何失败只记 debug。
    """
    if not prefetch_enabled():
        return False
    item_id = getattr(item, "id", None)
    if item_id is None:
        return False
    file_path = (getattr(item, "file_path", "") or "").strip()
    if not file_path.startswith("mount://"):
        return False                      # 本机/strm/虚拟条目不预取
    with _prefetch_lock:
        if item_id in _prefetch_inflight or _recently_done(item_id):
            return False
        _prefetch_inflight.add(item_id)

    def _job():
        started = time.perf_counter()
        try:
            from backend.emby_server.mounts import resolve_play_target
            # 独立 DB 会话：PlaybackInfo 的请求会话在响应后即关
            from backend.database import SessionLocal
            db2 = SessionLocal()
            try:
                resolve_play_target(file_path, db2, getattr(item, "library", None))
            finally:
                db2.close()
            logger.info("[play-timing] prefetch item=%s done in %.0fms",
                        item_id, (time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 — 预取是纯增益，失败不影响播放
            logger.debug("[play-timing] prefetch item=%s failed: %s",
                         item_id, exc)
        finally:
            with _prefetch_lock:
                _prefetch_inflight.discard(item_id)
                _prefetch_done_at[item_id] = time.monotonic()

    threading.Thread(target=_job, daemon=True,
                     name=f"play-ttfb-prefetch-{item_id}").start()
    return True


__all__ = ["PREFETCH_ENABLED", "prefetch_enabled", "try_prefetch"]
