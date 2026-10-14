"""Redis 客户端工厂。限流（异步）与会话持久层（同步，在工作线程里用）共用同一套配置原则。

**不能用 ``Redis(host, port, ...)`` 默认构造**，真 Redis 测试里撞到过两个坑：

* **默认构造带重试。**连接失败会指数退避重试 10 次（redis-py 8.1 实测：连不上时 11 次连接尝试、
  耗时 4.4 秒）。这两处调用都在每条消息的路径上，而且失败了我们本来就会放行/退回内存——
  重试只会让用户白等。显式构造连接池就没有这个重试；这里仍然写明 ``Retry(NoBackoff(), 0)``
  把意图钉死，免得库升级后池级别的默认值变了，这个卡顿悄悄回来
  （``test_connection_failure_is_attempted_once`` 数连接尝试次数来守这一点）。
* **异步连接池满了直接抛 ``MaxConnectionsError``，不排队**（默认上限 100）。
  一次突发 200 个并发消息，Redis 明明健康，也会因为"连接不够"被判成故障、整体降级。
  换成 ``BlockingConnectionPool``：连接不够时排队，等不到才算故障。
  上限 50 足够——一次 Lua 调用不到 1ms，50 条连接撑得住每秒几万次。
"""

from __future__ import annotations

from typing import Any


def make_redis_client(url: str, *, timeout: float = 0.25, max_connections: int = 50) -> Any:
    """异步客户端（``redis.asyncio``）。限流用。"""
    import redis.asyncio as aioredis
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff

    pool = aioredis.BlockingConnectionPool.from_url(
        url,
        max_connections=max_connections,
        timeout=timeout,                   # 等空闲连接的时间
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        retry=Retry(NoBackoff(), 0),
    )
    return aioredis.Redis(connection_pool=pool)


def make_sync_redis_client(url: str, *, timeout: float = 0.5, max_connections: int = 64) -> Any:
    """同步客户端。会话持久层在工作线程里用——和 LLM 调用一样是阻塞 I/O，线程本来就是干这个的。
    上限默认等于对话线程数：每个线程同一时刻最多占一条连接。"""
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    pool = redis.BlockingConnectionPool.from_url(
        url,
        max_connections=max_connections,
        timeout=timeout,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        retry=Retry(NoBackoff(), 0),
    )
    return redis.Redis(connection_pool=pool)
