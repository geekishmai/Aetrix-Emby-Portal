# -*- coding: utf-8 -*-
"""运行中扫描的协作式取消探针（2026-11-03）。

背景：``DELETE /api/admin/emby/scan-queue/{lib}`` 过去只能取消排队中的任务，
运行中返回 409；唯一停法是重启进程，而重启后 run 会被标 interrupted 并自动
续跑（run 10 实证）。这里提供 worker 侧的取消检查点。

设计（见 docs/aetrix-trial-scan.md §15）：

- 权威标志是 ``emby_scan_runs.cancel_requested_at``（DB 列，按 run 隔离）；
  API 进程写、worker 进程读，不依赖 Redis。
- ``CancelProbe`` 每 ``SCAN_CANCEL_POLL_SEC``（默认 2s）用**独立短 Session**
  查一次；扫描主体是「一批一提交」的长事务，复用同一 Session 可能读到本事务
  快照，独立 Session 才能看到 API 刚提交的取消行。
- 检查点通过 ``contextvars`` 绑定当前 probe；未绑定时 ``checkpoint()`` 是
  no-op——门户浏览等其它调用 walk_media 的路径完全不受影响。
- 取消是协作式的：已发出的网络请求不中断（等它返回），只是不再发下一页；
  下一个检查点抛 ``ScanCancelled``。
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Callable, Optional

from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

#: 检查点间隔（秒）。越小取消越快，DB 查询越多；2s 是「可感知但不压库」的折中。
#: 测试/排障可用 SCAN_CANCEL_POLL_SEC 覆盖（0 = 每个检查点都查）。
POLL_SEC_DEFAULT = env_float("SCAN_CANCEL_POLL_SEC", 2.0)


class ScanCancelled(RuntimeError):
    """扫描被管理员取消（不是失败，收尾状态记 cancelled）。"""


_probe_var: contextvars.ContextVar[Optional["CancelProbe"]] = contextvars.ContextVar(
    "aetrix_scan_cancel_probe", default=None)


def bind(probe: "CancelProbe"):
    """把 probe 绑到当前执行上下文（扫描线程），返回 token 供 unbind。"""
    return _probe_var.set(probe)


def unbind(token) -> None:
    try:
        _probe_var.reset(token)
    except (ValueError, RuntimeError):   # token 不属于当前上下文：忽略
        pass


def current_probe() -> Optional["CancelProbe"]:
    return _probe_var.get()


def checkpoint(where: str = "") -> None:
    """检查点：已请求取消则抛 ``ScanCancelled``；未绑定 probe 时 no-op。"""
    probe = _probe_var.get()
    if probe is not None:
        probe.check(where)


class CancelProbe:
    """按 run 轮询取消标志；线程安全（多个检查点可能在不同线程）。"""

    def __init__(self, run_id: int, *, poll_sec: Optional[float] = None,
                 session_factory: Optional[Callable] = None):
        self.run_id = int(run_id)
        self.poll_sec = (POLL_SEC_DEFAULT if poll_sec is None
                         else float(poll_sec))
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._last_check: Optional[float] = None
        self._cancelled = False
        self._phase = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self, where: str = "") -> None:
        if self._cancelled:
            raise ScanCancelled(self._phase or where or "cancelled")
        now = time.monotonic()
        with self._lock:
            if self._cancelled:
                raise ScanCancelled(self._phase or where or "cancelled")
            if (self._last_check is not None
                    and now - self._last_check < self.poll_sec):
                return
            self._last_check = now
            if self._read_flag():
                self._cancelled = True
                self._phase = where or "cancelled"
                logger.info("扫描取消探针命中：run_id=%s 检查点=%s",
                            self.run_id, self._phase)
                raise ScanCancelled(self._phase)

    def _read_flag(self) -> bool:
        """读取消列；读失败按「本轮未知」处理（下次再查），绝不误判为取消。"""
        try:
            factory = self._session_factory
            if factory is None:
                from backend.database import SessionLocal
                factory = SessionLocal
            db = factory()
            try:
                from backend.emby_server import models as em
                row = (db.query(em.ScanRun.cancel_requested_at)
                       .filter(em.ScanRun.id == self.run_id)
                       .first())
                return bool(row is not None and row[0] is not None)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — 读不到不能当成取消
            logger.warning("扫描取消探针读取失败（忽略本轮，下次再查）：%s", exc)
            return False
