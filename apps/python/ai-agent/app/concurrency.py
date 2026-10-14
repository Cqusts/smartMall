"""对话链路的线程池。

**为什么不用 ``asyncio.to_thread``。** 它用事件循环的默认线程池，上限是
``min(32, CPU 核数 + 4)``——4 核机器上只有 **8** 个。而一轮对话基本全是等待：
意图分类、改写、生成各是一次几秒的 LLM 调用，线程绝大部分时间在睡觉。
第 9 个同时在线的用户要排队等前面某个人的整轮对话结束，哪怕机器是闲的。

对话是 I/O 密集的，线程便宜，上限该由内存与下游（LLM 限流、数据库连接池）决定，
而不是 CPU 核数。默认给 64，``CHAT_WORKERS`` 可调。

注意这只解决"线程不够"。线程数之上还有两道要靠别的手段挡的东西：

* 数据库连接池（默认 10+10）：64 个线程同时要连接时会在池前面排队，这里没有挡；
* 过载保护：这个线程池的任务队列是无界的，全部线程都忙时新请求会无限排队。
  **准入控制与限流在 :mod:`app.overload`** —— 名额数 = 线程数，所以线程池里永远没有队列；
  超出的请求在池子**前面**排队、有上限、有超时，满了就快速拒绝。
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def chat_workers() -> int:
    try:
        n = int(os.environ.get("CHAT_WORKERS", "64"))
    except ValueError:
        return 64
    return max(1, n)


def get_chat_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=chat_workers(), thread_name_prefix="chat")
    return _executor


def shutdown_chat_executor() -> None:
    """关掉线程池。测试之间、或要换 ``CHAT_WORKERS`` 时调用。"""
    global _executor
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)


def submit(fn: Callable[..., T], *args: Any) -> "asyncio.Future[T]":
    """把阻塞函数丢进对话线程池，返回可 await 的 Future。

    和 ``asyncio.to_thread`` 一样**复制当前 contextvars**——日志里的 trace_id、
    请求上下文靠它带进工作线程。直接 ``run_in_executor`` 不会带，
    换了线程池后日志里的上下文字段会悄悄丢失。
    """
    ctx = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(
        get_chat_executor(), ctx.run, fn, *args)


async def run_blocking(fn: Callable[..., T], *args: Any) -> T:
    return await submit(fn, *args)
