"""115 接口保号闸：按账号（Cookie）统一限速 + 风控冷却。

115 的风控按账号算，而 Aetrix 的 api / worker / ea / p115 sidecar 是不同进程，
各进程各限各的等于没限。所以令牌桶和冷却状态都放 Redis（键按 Cookie 摘要分），
Redis 不可用时退回进程内状态（单进程仍有效，多进程只是放宽，不会卡死）。

收口在 ``transfer115.Pan115Client._call``：调用前 :func:`before_call`（冷却中直接
抛 :class:`Pan115CooldownError`，否则按桶等待），调用后 :func:`after_body` /
:func:`after_error` 识别风控信号并进入冷却。

默认值来自 docs/pan115-rate-evidence.md（MoviePilot、OpenList、CloudDrive2 的
经验值，不是 115 官方阈值）：

- 取链（downurl / files/download）1 次/秒，突发 2；列目录 2 次/秒，突发 5；
  同账号所有接口合计 3 次/秒，突发 5。
- HTTP 429 或「访问上限 / 太频繁」：冷却 30 分钟，24 小时内再犯翻倍到
  2 小时封顶，第 4 次起停 24 小时。
- HTTP 405：10 分钟内 3 次按限流处理。
- errno 911（请验证账号）：停 24 小时，需要人工在 115 完成验证后清冷却
  （``redis-cli DEL aetrix:pan115:cooldown:<scope>``）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Optional

from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

_KEY_PREFIX = "aetrix:pan115"

_RATE_LIMIT_WORDS = ("访问上限", "太频繁", "过于频繁", "操作频繁")
_VERIFY_ERRNOS = {"911"}


class Pan115DeadlineExceeded(RuntimeError):
    """宿主调用截止时刻已过，本次请求被丢弃（Q&A AD-1）。

    **不是冷却**：115 账号可能完全健康，只是调用方（宿主）已经放弃等待。
    上层必须与 ``Pan115CooldownError`` 分流——不得展示「账号冷却中」，
    也不得据此跳过直链失效清理（Z-2 门只认冷却）。
    """

    def __init__(self, reason: str, remaining: float = 0.0):
        super().__init__(f"宿主调用超时：{reason}")
        self.reason = reason
        self.retry_after = max(0.0, float(remaining))


class Pan115CooldownError(RuntimeError):
    """账号处于风控冷却期，本次调用未发出（不计入 115 调用次数）"""

    def __init__(self, reason: str, remaining: float):
        super().__init__(f"115 账号冷却中（{reason}），约 {int(remaining // 60) + 1} 分钟后恢复")
        self.reason = reason
        self.remaining = remaining
        # Q&A Z-1：retry_after 别名（= remaining），与 transfer115 / mounts /
        # sidecar 的同义属性统一——上层按「有没有 retry_after」识别冷却。
        self.retry_after = max(1.0, float(remaining))


def enabled() -> bool:
    return str(os.getenv("PAN115_GUARD_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def scope_of(cookie: str) -> str:
    """账号维度：Cookie 摘要（不落 Cookie 原文）"""
    return hashlib.sha256((cookie or "").encode("utf-8")).hexdigest()[:16]


def _bucket_params(kind: str) -> list[tuple[str, float, float]]:
    """``[(桶名, 每秒速率, 突发)]``：先过分类桶，再过账号总桶"""
    buckets = [("total", env_float("PAN115_API_RPS", 3.0), env_float("PAN115_API_BURST", 5.0))]
    if kind == "download_url":
        buckets.insert(0, ("link", env_float("PAN115_LINK_RPS", 1.0), env_float("PAN115_LINK_BURST", 2.0)))
    elif kind == "list":
        buckets.insert(0, ("list", env_float("PAN115_LIST_RPS", 2.0), env_float("PAN115_LIST_BURST", 5.0)))
    return [(name, rate, max(1.0, burst)) for name, rate, burst in buckets if rate > 0]


# ---------------- 令牌桶（GCRA）：Redis 优先，进程内兜底 ----------------

# KEYS[1] 桶键；ARGV: now 秒、发放间隔 T、容差 tau。返回需要再等的秒数（"0" = 放行）
_GCRA_LUA = """
local now = tonumber(ARGV[1])
local t = tonumber(ARGV[2])
local tau = tonumber(ARGV[3])
local tat = tonumber(redis.call('GET', KEYS[1]) or ARGV[1])
if tat < now then tat = now end
local allow_at = tat - tau
if now < allow_at - 1e-6 then return tostring(allow_at - now) end
local new_tat = tat + t
redis.call('SET', KEYS[1], tostring(new_tat), 'PX', math.ceil((new_tat - now) * 1000) + 1000)
return '0'
"""

_mem_lock = threading.Lock()
_mem_tat: dict = {}
_mem_cooldown: dict = {}   # scope -> (until, reason)
_mem_counters: dict = {}   # key -> (count, expires_at)


def _redis():
    from backend.database import get_redis
    return get_redis()


def _gcra_wait(key: str, rate: float, burst: float, now: float) -> float:
    interval = 1.0 / rate
    tau = interval * (burst - 1)
    client = _redis()
    if client is not None:
        try:
            return float(client.eval(_GCRA_LUA, 1, key, now, interval, tau))
        except Exception:  # noqa: BLE001 — Redis 故障退回进程内
            logger.debug("pan115_guard Redis 令牌桶失败，退回进程内", exc_info=True)
    with _mem_lock:
        tat = max(_mem_tat.get(key, now), now)
        allow_at = tat - tau
        if now < allow_at - 1e-6:
            return allow_at - now
        _mem_tat[key] = tat + interval
        return 0.0


# ---------------- 冷却 ----------------

def cooldown_state(scope: str) -> Optional[tuple[str, float]]:
    """``(原因, 剩余秒)``；不在冷却返回 None"""
    client = _redis()
    if client is not None:
        try:
            key = f"{_KEY_PREFIX}:cooldown:{scope}"
            reason = client.get(key)
            if reason is None:
                return None
            ttl = client.pttl(key)
            reason = reason.decode() if isinstance(reason, bytes) else str(reason)
            return reason, max(0.0, (ttl or 0) / 1000.0)
        except Exception:  # noqa: BLE001
            pass
    with _mem_lock:
        entry = _mem_cooldown.get(scope)
        if entry is None:
            return None
        until, reason = entry
        remaining = until - time.time()
        if remaining <= 0:
            _mem_cooldown.pop(scope, None)
            return None
        return reason, remaining


def _set_cooldown(scope: str, reason: str, seconds: float) -> None:
    logger.warning("115 账号进入冷却 %.0f 分钟：%s（scope=%s）", seconds / 60, reason, scope)
    client = _redis()
    if client is not None:
        try:
            client.set(f"{_KEY_PREFIX}:cooldown:{scope}", reason, px=int(seconds * 1000))
            return
        except Exception:  # noqa: BLE001
            pass
    with _mem_lock:
        _mem_cooldown[scope] = (time.time() + seconds, reason)


def _incr(key: str, window: float) -> int:
    """窗口计数（首次计数时设过期）"""
    client = _redis()
    if client is not None:
        try:
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, int(window))
            return count
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    with _mem_lock:
        count, expires = _mem_counters.get(key, (0, 0.0))
        if expires <= now:
            count, expires = 0, now + window
        count += 1
        _mem_counters[key] = (count, expires)
        return count


def _rate_limited(scope: str, reason: str) -> None:
    """429 风控冷却升级：24h 滑窗内按次数翻倍，第 4 次起停 24h。

    Q&A Z-6：**只计 429**（真风控信号）。405 另走 ``_rate_limited_405``
    ——HTTP 405 多是客户端/工具行为问题（语义不唯一，见
    docs/pan115-rate-evidence.md §1.11），不能与 429 共享升级计数
    把运维侧一路顶到 24h，也不能掩盖 bug。
    """
    strikes = _incr(f"{_KEY_PREFIX}:strikes:{scope}", 86400)
    base = env_float("PAN115_429_COOLDOWN_SEC", 1800.0)
    cap = env_float("PAN115_429_COOLDOWN_MAX_SEC", 7200.0)
    if strikes >= 4:
        seconds = 86400.0
    else:
        seconds = min(cap, base * (2 ** (strikes - 1)))
    _set_cooldown(scope, f"{reason}，24 小时内第 {strikes} 次", seconds)


def _rate_limited_405(scope: str, reason: str) -> None:
    """405 专用低倍冷却（Q&A Z-6）：只告警 + 5 分钟固定冷却，
    **不进 429 的升级链**。405 在 10 分钟内反复出现更可能是代码/工具
    bug（或 URL 语义问题），停 24h 会掩盖问题且误伤运维操作；
    5 分钟足够「先停下来看看日志」。PAN115_405_COOLDOWN_SEC 可覆盖。
    """
    strikes = _incr(f"{_KEY_PREFIX}:strikes405:{scope}", 86400)
    seconds = env_float("PAN115_405_COOLDOWN_SEC", 300.0)
    _set_cooldown(scope, f"{reason}（低倍冷却，24h 内第 {strikes} 次；"
                         "405 不是账号风控，请先查调用方日志）", seconds)


# ---------------- 收口 ----------------

def _wait_cap(kind: str) -> float:
    """按调用类型的排队上限秒数（Q&A Z-5）。

    - ``download_url``（播放取链）：**5 秒**——首帧延迟是用户可感知的，
      排队不如快速失败让播放器立刻重试/换路；队列里稍后被放行的请求
      自己会拿到令牌，不会有惊群。
    - ``list`` / ``other``：维持 30 秒——刷库/管理操作对延迟不敏感，
      排队远好于失败重来的整轮成本。
    环境变量可覆盖（0 = 不等待，立即拒绝）。
    """
    global_cap = env_float("PAN115_GUARD_MAX_WAIT_SEC", 30.0)
    if kind == "download_url":
        # 独立更小的上限（默认 5s），但**永不超过全局硬上限**：
        # MAX_WAIT_SEC 是「任何调用最多排队多久」的合同（原测试语义）。
        return min(env_float("PAN115_GUARD_MAX_WAIT_LINK_SEC", 5.0), global_cap)
    return global_cap


def before_call(cookie: str, kind: str, deadline: float | None = None) -> None:
    """发请求前：冷却中直接拒绝；否则按桶排队，等太久也拒绝（不把线程无限挂住）。

    首扫 partial 修法②（fix/sidecar-deadline）：``deadline`` 是调用方（sidecar
    侧）传来的**绝对截止时刻**（``time.time()`` 口径）。排队上限取
    ``min(自身上限, deadline 剩余 - 1s)``；排队等到的请求若将过宿主截止时刻，
    宿主早已放弃，发出去的 115 请求就是纯浪费。过期抛**独立类型**
    ``Pan115DeadlineExceeded``（QA AD-1：不是冷却，账号可能完全健康）。
    """
    if not enabled():
        return
    scope = scope_of(cookie)
    state = cooldown_state(scope)
    if state is not None:
        raise Pan115CooldownError(*state)
    max_wait = _wait_cap(kind)
    if deadline is not None:
        remaining = float(deadline) - time.time()
        if remaining <= 0:
            raise Pan115DeadlineExceeded("宿主截止时刻已过（请求未发出）", 0.0)
        # 排队上限收紧到 deadline 剩余（多留 1s 给真实 HTTP 往返）
        max_wait = min(max_wait, max(0.0, remaining - 1.0))
    waited = 0.0
    for name, rate, burst in _bucket_params(kind):
        key = f"{_KEY_PREFIX}:bucket:{scope}:{name}"
        while True:
            wait = _gcra_wait(key, rate, burst, time.time())
            if wait <= 0:
                break
            if waited + wait > max_wait:
                raise Pan115CooldownError("本机限速排队超时", wait)
            if deadline is not None and time.time() + wait > deadline:
                raise Pan115DeadlineExceeded("宿主截止时刻将过（请求未发出）",
                                             max(0.0, deadline - time.time()))
            time.sleep(wait)
            waited += wait


def after_body(cookie: str, body: dict) -> None:
    """115 业务层失败里识别风控信号（state=false 时的 errno / 文案）"""
    if not enabled() or not isinstance(body, dict) or body.get("state") in (True, 1, "1"):
        return
    errno = str(body.get("errno") or body.get("errNo") or body.get("code") or "")
    message = str(body.get("error") or body.get("message") or body.get("msg") or "")
    scope = scope_of(cookie)
    if errno in _VERIFY_ERRNOS:
        _set_cooldown(scope, "115 要求验证账号（errno 911），需人工处理",
                      env_float("PAN115_VERIFY_COOLDOWN_SEC", 86400.0))
    elif any(word in message for word in _RATE_LIMIT_WORDS):
        _rate_limited(scope, "115 提示访问过频")


def after_error(cookie: str, status: Optional[int]) -> None:
    """HTTP 层失败：429 直接冷却；405 在 10 分钟内累计 3 次 → 5 分钟低倍冷却"""
    if not enabled() or not status:
        return
    scope = scope_of(cookie)
    if status == 429:
        _rate_limited(scope, "115 返回 HTTP 429")
    elif status == 405:
        if _incr(f"{_KEY_PREFIX}:http405:{scope}", 600) >= 3:
            # Z-6：405 走独立低倍冷却（5 分钟），不进 429 的 24h 升级链
            logger.warning("115 连续 HTTP 405（10 分钟内 ≥3 次）：%s 进入 5 分钟低倍冷却"
                           "（不升级 24h——405 更可能是调用方 bug，先查日志）", scope)
            _rate_limited_405(scope, "115 10 分钟内多次 HTTP 405")


def reset_for_tests() -> None:
    with _mem_lock:
        _mem_tat.clear()
        _mem_cooldown.clear()
        _mem_counters.clear()
