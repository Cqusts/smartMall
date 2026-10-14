"""对话入口限流：令牌桶，内存与 Redis 两种后端，语义一致。

**为什么要限流。**一轮对话是几次 LLM 调用，每次都花钱、占一个工作线程。
没有限流时，一个写错循环的客户端、或者一个脚本，就能把 64 个工作线程占满，
其他所有用户跟着排队——这不是"并发高"，是"一个人把店铺挤满了"。

**为什么是令牌桶。**允许短时突发（用户连发两三句很正常），但长期速率有上限。
固定窗口计数在窗口边界上能放过 2 倍流量，令牌桶没有这个洞。

**两种后端：**

* ``memory`` —— 每个进程各记各的。单实例时精确；N 个实例时实际上限是 N 倍。
  对"防滥用"来说够用，但要精确就得上 Redis。
* ``redis`` —— 所有实例共用一份计数，用 Lua 脚本保证"读-算-写"原子；时间取 Redis
  服务器的 ``TIME``，不信各实例自己的时钟（多机时钟差几百毫秒就会让桶多放或少放）。

**限流器出问题时必须放行（fail-open）。**限流是保护措施，不是业务逻辑：Redis 连不上就把
客服也停掉，保护措施本身就成了最大的故障源。Redis 一出错就改用进程内的桶，并且
``cooldown`` 秒内不再试 Redis——否则 Redis 挂了之后，每个请求都要先等一个连接超时。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: 被拒时，还要等多少秒才会有一个令牌；放行时为 0
    retry_after: float = 0.0


class RateLimiter(Protocol):
    async def take(self, key: str, burst: float, rate: float) -> Decision:
        """从 ``key`` 的桶里取 1 个令牌。

        ``burst`` 是桶容量（允许的瞬时突发），``rate`` 是每秒补充的令牌数。
        """


class MemoryLimiter:
    """进程内令牌桶。

    只在事件循环线程里调用，所以不加锁。桶的数量受 ``max_keys`` 限制：
    key 是客户端控制的（IP、session_id），不设上限就是一个可以被打爆内存的口子。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 100_000) -> None:
        self._clock = clock
        self._max_keys = max(1, max_keys)
        # key -> (tokens, last_ts, capacity, rate)；按最近使用排序，淘汰最久没动的
        self._buckets: OrderedDict[str, tuple[float, float, float, float]] = OrderedDict()

    @property
    def size(self) -> int:
        """当前跟踪的桶数。不用 ``__len__``：它会让空限流器变成假值，
        ``limiter or default`` 这种写法会悄悄把它换掉（见 ChatGuard）。"""
        return len(self._buckets)

    async def take(self, key: str, burst: float, rate: float) -> Decision:
        now = self._clock()
        entry = self._buckets.get(key)
        if entry is None:
            tokens = burst
        else:
            tokens, last, _, _ = entry
            tokens = min(burst, tokens + (now - last) * rate)

        if tokens >= 1.0:
            tokens -= 1.0
            decision = Decision(True)
        else:
            decision = Decision(False, (1.0 - tokens) / rate if rate > 0 else 3600.0)

        self._buckets[key] = (tokens, now, burst, rate)
        self._buckets.move_to_end(key)
        if len(self._buckets) > self._max_keys:
            self._shrink(now)
        return decision

    def _shrink(self, now: float) -> None:
        """先清掉已经补满的桶——它们和"没有这个桶"完全等价，删了不影响任何人。
        还超就淘汰最久没动的，**一次淘汰到上限的 90%**：只淘汰一个的话，
        攻击者用海量不同的 key 灌进来，每个请求都要把整张表扫一遍。"""
        full = [k for k, (t, last, cap, r) in self._buckets.items()
                if t + (now - last) * r >= cap]
        for k in full:
            del self._buckets[k]
        if len(self._buckets) > self._max_keys:
            target = max(1, int(self._max_keys * 0.9))
            while len(self._buckets) > target:
                self._buckets.popitem(last=False)


# KEYS[1]=桶 key；ARGV[1]=容量 ARGV[2]=每秒补充数
# 返回 {是否放行, 还需等待的毫秒数}
_LUA = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(d[1])
local ts = tonumber(d[2])
if tokens == nil then
  tokens = cap
  ts = now
end
tokens = math.min(cap, tokens + (now - ts) / 1000 * rate)
local allowed = 0
local wait_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  if rate > 0 then wait_ms = math.ceil((1 - tokens) / rate * 1000) else wait_ms = 3600000 end
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', now)
-- 补满所需时间的两倍后过期：桶补满了就等价于不存在，不必一直占着 Redis
local ttl = 3600000
if rate > 0 then ttl = math.ceil(cap / rate * 1000) * 2 end
redis.call('PEXPIRE', KEYS[1], ttl)
return {allowed, wait_ms}
"""


def make_redis_client(url: str, *, timeout: float = 0.25, max_connections: int = 50) -> Any:
    """限流专用的 Redis 客户端。**不能用 ``Redis(host, port, ...)`` 默认构造**，真 Redis 测试里撞到过两个坑：

    * **默认构造带重试。**连接失败会指数退避重试 10 次（redis-py 8.1 实测：连不上时 11 次连接尝试、
      耗时 4.4 秒）。限流在每条消息的路径上，而且失败了我们本来就会放行并改用进程内限流——
      重试只会让用户白等。显式构造连接池就没有这个重试；这里仍然写明 ``Retry(NoBackoff(), 0)``
      把意图钉死，免得库升级后池级别的默认值变了，这个卡顿悄悄回来
      （``test_connection_failure_is_attempted_once`` 数连接尝试次数来守这一点）。
    * **异步连接池满了直接抛 ``MaxConnectionsError``，不排队**（默认上限 100）。
      一次突发 200 个并发消息，Redis 明明健康，也会因为"连接不够"被判成故障、
      整体降级。换成 ``BlockingConnectionPool``：连接不够时排队，等不到才算故障。
      上限 50 足够——一次 Lua 调用不到 1ms，50 条连接撑得住每秒几万次。
    """
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


class RedisLimiter:
    """Redis 令牌桶。``client`` 是 ``redis.asyncio.Redis``，用 :func:`make_redis_client` 建。"""

    def __init__(self, client: Any, prefix: str = "rl:chat:") -> None:
        self._client = client
        self._prefix = prefix
        self._script = client.register_script(_LUA)

    async def take(self, key: str, burst: float, rate: float) -> Decision:
        allowed, wait_ms = await self._script(
            keys=[self._prefix + key], args=[repr(float(burst)), repr(float(rate))])
        return Decision(bool(int(allowed)), int(wait_ms) / 1000.0)


class FailOpenLimiter:
    """主限流器出错时放行，并改用备用限流器。

    ``cooldown`` 内直接用备用的，不再试主的：Redis 不可用时，每个请求都先吃一次
    连接超时，比不限流还糟。
    """

    def __init__(self, primary: RateLimiter, fallback: RateLimiter, *,
                 cooldown: float = 5.0,
                 clock: Callable[[], float] = time.monotonic,
                 on_error: Callable[[BaseException], None] | None = None) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cooldown = cooldown
        self._clock = clock
        self._on_error = on_error
        self._retry_primary_at = 0.0
        self.degraded_calls = 0

    @property
    def degraded(self) -> bool:
        return self._clock() < self._retry_primary_at

    async def take(self, key: str, burst: float, rate: float) -> Decision:
        if self._clock() >= self._retry_primary_at:
            try:
                return await self._primary.take(key, burst, rate)
            except Exception as exc:  # noqa: BLE001  任何故障都不能让客服跟着停
                self._retry_primary_at = self._clock() + self._cooldown
                if self._on_error is not None:
                    self._on_error(exc)
        self.degraded_calls += 1
        return await self._fallback.take(key, burst, rate)


class NoLimit:
    async def take(self, key: str, burst: float, rate: float) -> Decision:
        return Decision(True)


def client_ip(peer: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """限流用的客户端地址。

    直连时就是对端地址。**站在反向代理后面时，对端是代理，所有用户共用一个桶**——
    一个用户刷屏，全站被限。所以要读 ``X-Forwarded-For``。

    但这个头是客户端可以随便写的：直接取第一项等于让攻击者自己选自己的 IP。
    正确做法是只信**自己的代理加上去的那几项**——``trusted_hops`` 是你前面有几层可信
    代理，取从右数第 ``trusted_hops`` 项（最右一项是离我们最近的代理写的）。
    默认 0 = 不信任任何转发头，只用对端地址。
    """
    if trusted_hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= trusted_hops:
            return parts[-trusted_hops]
    return peer or "unknown"
