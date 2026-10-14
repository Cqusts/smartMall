"""对话入口的过载保护：准入控制 + 限流，合在一个门里。

**问题。**线程池（见 :mod:`app.concurrency`）只解决"线程够不够"，它的任务队列是无界的：
64 个线程全在跑、每轮 8 秒，第 500 个请求要等 ~55 秒才轮到——而用户早在 10 秒时就关掉了页面。
更糟的是**没人取消它**：线程池照样把这些已经没人要的对话一个个跑完，每一轮都白白花掉
几次 LLM 调用的钱，同时把真正还在等的新用户继续往后挤。这是过载下典型的死亡螺旋：
活干得越多，有效产出越少。

**做法：宁可快速拒绝，也不要无限排队。**一个请求要通过四道关，顺序是有讲究的：

1. **限流**（:meth:`ChatGuard.check_rate`）：按 IP、按用户/会话的令牌桶。必须在**创建会话之前**——
   否则被限流的请求照样往会话表里塞对象，限流挡住了 LLM 调用却挡不住内存增长。
2. **会话待处理上限**：同一会话最多 ``session_max_pending`` 个在途（默认 2：一个在跑、一个在等）。
   用户连点十次发送，不该排出十轮对话。
3. **会话锁**：同一会话串行（见 ``SessionStore.lock_for``）。放在全局名额**之前**——先拿名额再等锁，
   一个用户的几条排队消息就能占住好几个全局名额，而它们根本不能并行。
4. **全局名额**：最多 ``max_active`` 轮同时在跑（= 线程数，所以线程池里永远没有队列），
   再多就进等待区；等待区满了立刻拒，等太久（``queue_timeout``）也拒。

**名额与线程同生共死。**名额不是在协程退出时还，而是在**工作线程真正结束时**还
（:meth:`Slot.submit`）。WebSocket 中途断开时协程会被取消，但线程还在跑——名额在这时就还，
新请求立刻进来，线程池里就有了 65 个任务。名额绑到线程上，"线程数 ≤ 名额数"才是真的。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from .concurrency import submit as _submit_to_pool
from .ratelimit import NoLimit, RateLimiter

#: 拒绝原因 -> (HTTP 状态码, 给用户的话)。前端对 WebSocket 的 ``type: error`` 直接把
#: ``answer`` 显示成一个气泡，所以文案是写给最终用户的，不是技术细节
REASONS: dict[str, tuple[int, str]] = {
    "busy": (503, "客服正忙，请稍后再试～"),
    "session_busy": (429, "上一条消息还在回答中，请稍等～"),
    "rate_limited": (429, "发送太频繁了，请稍后再试～"),
}


class Overloaded(Exception):
    """请求被过载保护拒绝。不是故障，是系统在按设计工作。"""

    def __init__(self, reason: str, retry_after: float) -> None:
        self.reason = reason
        self.retry_after = max(1, int(retry_after + 0.999))   # 向上取整，至少 1 秒
        self.status, self.message = REASONS[reason]
        super().__init__(f"{reason} (retry in {self.retry_after}s)")

    def ws_event(self) -> dict[str, Any]:
        """WebSocket 事件。用 ``type: error`` 是为了**前端不用改**：它对 error 的处理
        正是清掉"思考中"状态、把 ``answer`` 显示成一条消息。``code`` 是给想区分的客户端的。"""
        return {"type": "error", "code": self.reason, "answer": self.message,
                "retry_after": self.retry_after}


class Slot:
    """一个全局名额。名额在**工作线程结束时**归还，见模块说明。"""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._released = False
        self._handed_off = False

    def _release_once(self) -> None:
        if not self._released:
            self._released = True
            self._release()

    def submit(self, fn: Callable[..., Any], *args: Any) -> "asyncio.Future[Any]":
        """在对话线程池里跑 ``fn``；线程结束（无论成败、无论调用方还在不在等）时还名额。"""
        if self._handed_off:
            raise RuntimeError("一个名额只能提交一次")
        loop = asyncio.get_running_loop()

        def job() -> Any:
            try:
                return fn(*args)
            finally:
                try:
                    loop.call_soon_threadsafe(self._release_once)
                except RuntimeError:     # 事件循环已经关了（进程在退出），没人等这个名额了
                    pass

        self._handed_off = True
        try:
            return _submit_to_pool(job)
        except BaseException:
            self._handed_off = False     # 没提交成功，名额由 close() 还
            raise

    def close(self) -> None:
        """退出 ``async with`` 时调用：没交给线程的名额在这里还，交了的由线程还。"""
        if not self._handed_off:
            self._release_once()


class ChatGuard:
    def __init__(
        self,
        *,
        max_active: int,
        max_waiting: int = 64,
        queue_timeout: float = 8.0,
        session_max_pending: int = 2,
        limiter: RateLimiter | None = None,
        ip_bucket: tuple[float, float] = (30.0, 1.0),
        identity_bucket: tuple[float, float] = (6.0, 0.2),
        busy_retry_after: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active 至少为 1")
        self.max_active = max_active
        self.max_waiting = max(0, max_waiting)
        self.queue_timeout = queue_timeout
        self.session_max_pending = max(1, session_max_pending)
        # 不能写 ``limiter or NoLimit()``：限流器可能定义了 __len__/__bool__，空的时候是假值，
        # 会被悄悄换成"不限流"——默认配置下限流整个失效，而且没有任何报错
        self.limiter: RateLimiter = limiter if limiter is not None else NoLimit()
        self.ip_bucket = ip_bucket
        self.identity_bucket = identity_bucket
        self.busy_retry_after = busy_retry_after
        self._clock = clock
        self._log = log

        self._sem = asyncio.Semaphore(max_active)
        self._active = 0
        self._waiting = 0
        self._pending: dict[str, int] = {}
        self.rejected: Counter[str] = Counter()
        self._last_warn = 0.0

    # ------------------------------------------------------------------ 观测

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "waiting": self._waiting,
            "max_active": self.max_active,
            "max_waiting": self.max_waiting,
            "tracked_sessions": len(self._pending),
            "rejected": dict(self.rejected),
        }

    def _reject(self, reason: str, retry_after: float) -> Overloaded:
        self.rejected[reason] += 1
        now = self._clock()
        # 过载时每秒可能上千次拒绝，每次都写日志，日志本身就成了负载
        if self._log is not None and now - self._last_warn >= 1.0:
            self._last_warn = now
            self._log(f"chat 拒绝 reason={reason} active={self._active} "
                      f"waiting={self._waiting} 累计拒绝={dict(self.rejected)}")
        return Overloaded(reason, retry_after)

    # ------------------------------------------------------------------ 限流

    async def check_rate(self, *, ip: str, identity: str | None) -> None:
        """按 IP、按用户/会话各取一个令牌，任一个没有就拒绝。

        ``identity`` 是客户端自报的（这个接口的 ``user_id`` 没有认证），换一个就是一个新的桶，
        所以它只能防"写坏了的客户端"，**防不了有意绕过的人——那是 IP 桶的活**。
        """
        burst, rate = self.ip_bucket
        d = await self.limiter.take(f"ip:{ip}", burst, rate)
        if not d.allowed:
            raise self._reject("rate_limited", d.retry_after)
        if identity:
            burst, rate = self.identity_bucket
            d = await self.limiter.take(f"id:{identity}", burst, rate)
            if not d.allowed:
                raise self._reject("rate_limited", d.retry_after)

    # ------------------------------------------------------------------ 准入

    def _enter_session(self, sid: str) -> None:
        n = self._pending.get(sid, 0)
        if n >= self.session_max_pending:
            raise self._reject("session_busy", 2.0)
        self._pending[sid] = n + 1

    def _exit_session(self, sid: str) -> None:
        n = self._pending.get(sid, 0) - 1
        if n <= 0:
            self._pending.pop(sid, None)      # 不留空条目，否则每个来过的会话都占一格
        else:
            self._pending[sid] = n

    def _release_slot(self) -> None:
        self._active -= 1
        self._sem.release()

    @asynccontextmanager
    async def _slot(self) -> AsyncIterator[Slot]:
        sem = self._sem
        if sem.locked():                       # 没有空名额（或前面还有人在排）
            if self._waiting >= self.max_waiting:
                raise self._reject("busy", self.busy_retry_after)
            self._waiting += 1
            try:
                async with asyncio.timeout(self.queue_timeout):
                    await sem.acquire()
            except TimeoutError:
                raise self._reject("busy", self.busy_retry_after) from None
            finally:
                self._waiting -= 1
        else:
            await sem.acquire()
        self._active += 1
        slot = Slot(self._release_slot)
        try:
            yield slot
        finally:
            slot.close()

    @asynccontextmanager
    async def turn(self, session_id: str, lock: asyncio.Lock) -> AsyncIterator[Slot]:
        """一轮对话的准入。进入时可能抛 :class:`Overloaded`；产出的 :class:`Slot` 用来提交工作线程。"""
        self._enter_session(session_id)
        try:
            async with lock:
                async with self._slot() as slot:
                    yield slot
        finally:
            self._exit_session(session_id)


def build_guard(settings: Any, *, workers: int, log: Callable[[str], None] | None = None) -> ChatGuard:
    """按配置装配。Redis 后端出任何问题都退回内存——限流不能比被它保护的东西更脆弱。"""
    from .ratelimit import FailOpenLimiter, MemoryLimiter, RedisLimiter, make_redis_client

    backend = settings.chat_rate_backend
    limiter: RateLimiter
    if backend == "off":
        limiter = NoLimit()
    elif backend == "memory":
        limiter = MemoryLimiter()
    else:
        limiter = MemoryLimiter()
        try:
            client = make_redis_client(settings.redis_url,
                                       timeout=settings.chat_rate_redis_timeout)
            limiter = FailOpenLimiter(
                RedisLimiter(client), MemoryLimiter(),
                on_error=lambda e: log and log(
                    f"限流 Redis 不可用，改用进程内限流：{type(e).__name__}: {e}"),
            )
        except Exception as exc:  # noqa: BLE001  没装 redis 库之类
            if log:
                log(f"限流 Redis 后端无法初始化，改用进程内限流：{type(exc).__name__}: {exc}")

    return ChatGuard(
        max_active=workers,
        max_waiting=settings.chat_max_waiting,
        queue_timeout=settings.chat_queue_timeout,
        session_max_pending=settings.chat_session_max_pending,
        limiter=limiter,
        ip_bucket=(settings.chat_rate_ip_burst, settings.chat_rate_ip_per_min / 60.0),
        identity_bucket=(settings.chat_rate_identity_burst,
                         settings.chat_rate_identity_per_min / 60.0),
        busy_retry_after=settings.chat_retry_after,
        log=log,
    )
