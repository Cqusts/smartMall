"""对话入口限流：令牌桶（内存 / Redis）、故障放行、客户端 IP 解析。

Redis 部分跑在**真的 redis-server 进程**上，不是 mock：限流的正确性全押在 Lua 脚本的
原子性和 Redis 的时间语义上，用假实现测这个等于没测。机器上没有 redis-server 时跳过
（而不是悄悄通过）。
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from app.ratelimit import (Decision, FailOpenLimiter, MemoryLimiter, NoLimit,
                           RedisLimiter, client_ip, make_redis_client)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- 内存令牌桶


class TestMemoryBucket:
    def test_burst_then_reject(self):
        async def go():
            lim = MemoryLimiter(clock=FakeClock())
            return [(await lim.take("k", 3, 1.0)).allowed for _ in range(5)]
        assert run(go()) == [True, True, True, False, False]

    def test_retry_after_is_time_until_next_token(self):
        async def go():
            clock = FakeClock()
            lim = MemoryLimiter(clock=clock)
            for _ in range(2):
                await lim.take("k", 2, 0.5)          # 每 2 秒补一个
            d = await lim.take("k", 2, 0.5)
            clock.advance(0.5)
            d2 = await lim.take("k", 2, 0.5)
            return d, d2
        d, d2 = run(go())
        assert not d.allowed and d.retry_after == pytest.approx(2.0)
        assert not d2.allowed and d2.retry_after == pytest.approx(1.5)

    def test_refills_with_time_and_waiting_is_enough(self):
        async def go():
            clock = FakeClock()
            lim = MemoryLimiter(clock=clock)
            for _ in range(2):
                await lim.take("k", 2, 1.0)
            assert not (await lim.take("k", 2, 1.0)).allowed
            clock.advance(1.0)
            return (await lim.take("k", 2, 1.0)).allowed
        assert run(go()) is True

    def test_idle_time_never_banks_more_than_burst(self):
        """闲一天回来也只能突发 burst 条，不能"攒"出一天的额度。"""
        async def go():
            clock = FakeClock()
            lim = MemoryLimiter(clock=clock)
            await lim.take("k", 3, 1.0)
            clock.advance(86400)
            return [(await lim.take("k", 3, 1.0)).allowed for _ in range(5)]
        assert run(go()) == [True, True, True, False, False]

    def test_keys_are_independent(self):
        async def go():
            lim = MemoryLimiter(clock=FakeClock())
            for _ in range(3):
                await lim.take("a", 3, 1.0)
            return (await lim.take("a", 3, 1.0)).allowed, (await lim.take("b", 3, 1.0)).allowed
        assert run(go()) == (False, True)

    def test_sustained_rate_matches_configuration(self):
        """长期速率 = rate：60 秒、每 0.1 秒发一条、速率 2/s，放行的应当约 120+burst。"""
        async def go():
            clock = FakeClock()
            lim = MemoryLimiter(clock=clock)
            allowed = 0
            for _ in range(600):
                allowed += (await lim.take("k", 5, 2.0)).allowed
                clock.advance(0.1)
            return allowed
        n = run(go())
        assert 120 <= n <= 130, n


class TestMemoryBucketIsBounded:
    """key 是客户端控制的（IP、session_id）。不设上限就是一个能打爆内存的口子。"""

    def test_flood_of_distinct_active_keys_stays_bounded(self):
        async def go():
            lim = MemoryLimiter(clock=FakeClock(), max_keys=1000)
            for i in range(10_000):
                await lim.take(f"k{i}", 1, 0.001)    # 每个 key 取走唯一的令牌：不会"补满"
            return lim.size
        assert run(go()) <= 1000

    def test_full_buckets_are_dropped_first_and_nobody_notices(self):
        """补满的桶与"没有这个桶"等价，清掉它们不改变任何人的待遇。

        被限流的 busy 是**最老**的一个桶：直接按"最久没动"淘汰的话，第一个被扔掉的就是它，
        于是被限流的人白得一次满额突发。先清补满的，才轮不到它。"""
        async def go():
            clock = FakeClock()
            lim = MemoryLimiter(clock=clock, max_keys=100)
            await lim.take("busy", 2, 0.001)
            await lim.take("busy", 2, 0.001)                  # busy 取空，几乎补不回来
            for i in range(60):
                await lim.take(f"idle{i}", 2, 10.0)           # 用掉 1 个，马上补满
            clock.advance(5.0)                                # idle* 全补满了，busy 还是空的
            for i in range(60):
                await lim.take(f"new{i}", 2, 10.0)            # 超过上限，触发清理
            return lim.size, (await lim.take("busy", 2, 0.001)).allowed
        size, busy_allowed = run(go())
        assert size <= 100
        assert busy_allowed is False, "没补满的桶被清掉了：被限流的人白得了一次突发额度"

    def test_shrink_is_amortised_not_per_request(self):
        """上限满了之后只淘汰一个的话，攻击者每个请求都让我们把整张表扫一遍。"""
        async def go():
            lim = MemoryLimiter(clock=FakeClock(), max_keys=1000)
            for i in range(1000):
                await lim.take(f"k{i}", 1, 0.001)
            scans = 0
            real = lim._shrink

            def counting(now):
                nonlocal scans
                scans += 1
                real(now)
            lim._shrink = counting
            for i in range(1000, 1500):
                await lim.take(f"k{i}", 1, 0.001)
            return scans
        # 淘汰到 90%：每 100 次插入才触发一次扫描，500 次插入 ≈ 5 次；
        # 只淘汰一个的话是 500 次
        assert run(go()) <= 6


# ----------------------------------------------------------------------------- 故障放行


class Boom:
    """可控的"主限流器"：计数、可开关故障。"""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    async def take(self, key, burst, rate):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        return Decision(False, 7.0)


class TestFailOpen:
    def test_primary_error_falls_back_instead_of_raising(self):
        async def go():
            lim = FailOpenLimiter(Boom(), MemoryLimiter(clock=FakeClock()))
            return await lim.take("k", 3, 1.0)
        assert run(go()).allowed is True

    def test_fallback_still_limits(self):
        """故障放行不是"不限流"：退回进程内的桶，滥用照样被挡。"""
        async def go():
            lim = FailOpenLimiter(Boom(), MemoryLimiter(clock=FakeClock()))
            return [(await lim.take("k", 2, 1.0)).allowed for _ in range(4)]
        assert run(go()) == [True, True, False, False]

    def test_primary_is_not_retried_during_cooldown(self):
        """Redis 挂了之后，每个请求都先吃一次连接超时，比不限流还糟。"""
        async def go():
            clock = FakeClock()
            primary = Boom()
            lim = FailOpenLimiter(primary, MemoryLimiter(clock=clock),
                                  cooldown=5.0, clock=clock)
            for _ in range(50):
                await lim.take("k", 1000, 1000)
            during = primary.calls
            clock.advance(5.1)
            await lim.take("k", 1000, 1000)
            return during, primary.calls, lim.degraded_calls
        during, after, degraded = run(go())
        assert during == 1, f"冷却期内主限流器被调了 {during} 次"
        assert after == 2
        # 第 1 次（主的失败后退回）+ 冷却期内 49 次 + 冷却结束后再试主的、又失败退回的那 1 次
        assert degraded == 51

    def test_recovers_when_primary_comes_back(self):
        async def go():
            clock = FakeClock()
            primary = Boom()
            lim = FailOpenLimiter(primary, MemoryLimiter(clock=clock),
                                  cooldown=5.0, clock=clock)
            await lim.take("k", 100, 1)
            assert lim.degraded
            primary.fail = False
            clock.advance(5.1)
            d = await lim.take("k", 100, 1)         # 主的回答：拒绝，等 7 秒
            return d, lim.degraded
        d, degraded = run(go())
        assert d == Decision(False, 7.0) and degraded is False

    def test_error_hook_is_called_with_the_exception(self):
        seen = []

        async def go():
            lim = FailOpenLimiter(Boom(), NoLimit(), on_error=seen.append)
            await lim.take("k", 1, 1)
        run(go())
        assert len(seen) == 1 and isinstance(seen[0], ConnectionError)


# ----------------------------------------------------------------------------- 客户端 IP


class TestClientIp:
    def test_direct_connection_uses_peer(self):
        assert client_ip("1.2.3.4", None, 0) == "1.2.3.4"

    def test_untrusted_forwarded_header_is_ignored_by_default(self):
        """攻击者自己写 X-Forwarded-For 就能选自己的 IP——默认不能信。"""
        assert client_ip("1.2.3.4", "9.9.9.9", 0) == "1.2.3.4"

    def test_trusts_only_the_entries_our_proxies_added(self):
        """攻击者发 `X-Forwarded-For: 6.6.6.6`，我们的代理在右边追加真实 IP。取最右一项，
        取最左一项就等于让攻击者选。"""
        assert client_ip("10.0.0.1", "6.6.6.6, 203.0.113.5", 1) == "203.0.113.5"

    def test_two_trusted_hops(self):
        assert client_ip("10.0.0.1", "6.6.6.6, 203.0.113.5, 10.0.0.2", 2) == "203.0.113.5"

    def test_fewer_entries_than_hops_falls_back_to_peer(self):
        assert client_ip("10.0.0.1", "203.0.113.5", 2) == "10.0.0.1"

    def test_missing_header_with_trusted_hops_uses_peer(self):
        assert client_ip("10.0.0.1", None, 1) == "10.0.0.1"
        assert client_ip("10.0.0.1", "", 1) == "10.0.0.1"

    def test_no_peer_does_not_crash(self):
        assert client_ip(None, None, 0) == "unknown"


# ----------------------------------------------------------------------------- 真 Redis


from _redis import RedisServer, needs_redis, redis_asyncio  # noqa: E402,F401


@pytest.fixture
def redis_server():
    srv = RedisServer()
    srv.start()
    yield srv
    srv.stop()


def _client(srv: RedisServer, timeout: float = 2.0):
    """**用生产同一个工厂建客户端**——测试自己另配一套，就测不到生产配置里的坑。"""
    return make_redis_client(f"redis://127.0.0.1:{srv.port}/0", timeout=timeout)


@needs_redis
class TestRedisBucket:
    def test_burst_then_reject(self, redis_server):
        async def go():
            c = _client(redis_server)
            lim = RedisLimiter(c)
            out = [await lim.take("k", 3, 0.5) for _ in range(5)]
            await c.aclose()
            return out
        out = run(go())
        assert [d.allowed for d in out] == [True, True, True, False, False]
        assert 1.5 < out[3].retry_after <= 2.0           # 0.5/s：约 2 秒后有下一个

    def test_refills_in_real_time(self, redis_server):
        async def go():
            c = _client(redis_server)
            lim = RedisLimiter(c)
            for _ in range(2):
                await lim.take("k", 2, 10.0)
            blocked = (await lim.take("k", 2, 10.0)).allowed
            await asyncio.sleep(0.25)                    # 10/s：0.25s 补 2.5 个，封顶 2
            back = [(await lim.take("k", 2, 10.0)).allowed for _ in range(3)]
            await c.aclose()
            return blocked, back
        blocked, back = run(go())
        assert blocked is False
        assert back == [True, True, False]

    def test_concurrent_takes_never_overshoot_the_burst(self, redis_server):
        """原子性。200 个并发请求抢同一个桶（容量 50、几乎不补），放行数必须正好 50。
        读-算-写若不是原子的，几个请求读到同一个旧值、各自放行，总数会超。"""
        async def go():
            c = _client(redis_server)
            lim = RedisLimiter(c)
            res = await asyncio.gather(*[lim.take("hot", 50, 0.0001) for _ in range(200)])
            await c.aclose()
            return sum(d.allowed for d in res)
        assert run(go()) == 50

    def test_control_a_naive_get_set_limiter_does_overshoot(self, redis_server):
        """对照实验：证明上面那条测试有区分力。同样 200 个并发，换成朴素的 GET 再 SET，
        放行数会明显超过 50——说明 Lua 的原子性是真的在起作用，不是这个负载本来就撞不上。"""
        async def naive_take(c, key, burst):
            v = await c.get(key)
            tokens = float(v) if v is not None else float(burst)
            if tokens >= 1:
                await c.set(key, tokens - 1)
                return True
            return False

        async def go():
            c = _client(redis_server)
            res = await asyncio.gather(*[naive_take(c, "naive", 50) for _ in range(200)])
            await c.aclose()
            return sum(res)
        assert run(go()) > 50

    def test_instances_share_one_bucket(self, redis_server):
        """限流放 Redis 的意义：两个实例（两条连接）共用一份额度，而不是各 N 份。"""
        async def go():
            ca, cb = _client(redis_server), _client(redis_server)
            a, b = RedisLimiter(ca), RedisLimiter(cb)
            out = []
            for _ in range(3):
                out.append((await a.take("shared", 5, 0.0001)).allowed)
                out.append((await b.take("shared", 5, 0.0001)).allowed)
            await ca.aclose()
            await cb.aclose()
            return out
        out = run(go())
        assert sum(out) == 5, out

    def test_keys_are_prefixed(self, redis_server):
        async def go():
            c = _client(redis_server)
            await RedisLimiter(c, prefix="rl:chat:").take("ip:1.2.3.4", 3, 1.0)
            keys = [k.decode() for k in await c.keys("*")]
            await c.aclose()
            return keys
        assert run(go()) == ["rl:chat:ip:1.2.3.4"]

    def test_bucket_keys_expire_instead_of_piling_up(self, redis_server):
        """每个来过的 IP 在 Redis 里留一个 key，没有过期就是一个慢泄漏。"""
        async def go():
            c = _client(redis_server)
            lim = RedisLimiter(c)
            await lim.take("k", 1, 50.0)                 # 补满只要 20ms，TTL ≈ 40ms
            ttl = await c.pttl("rl:chat:k")
            await asyncio.sleep(0.2)
            gone = await c.exists("rl:chat:k")
            await c.aclose()
            return ttl, gone
        ttl, gone = run(go())
        assert 0 < ttl <= 100, ttl
        assert gone == 0

    def test_slow_trickle_keeps_the_bucket_alive(self, redis_server):
        """补得慢的桶不能在补满之前就过期——否则被限流的人等一会儿就莫名其妙回到满额。"""
        async def go():
            c = _client(redis_server)
            lim = RedisLimiter(c)
            for _ in range(3):
                await lim.take("slow", 3, 0.001)          # 补满要 3000 秒
            ttl = await c.pttl("rl:chat:slow")
            await c.aclose()
            return ttl
        assert run(go()) >= 2_990_000        # 取空后补满要 3000 秒；留几毫秒的流逝


class TestRedisClientFactory:
    """不需要 redis-server：用一个"接上就挂"的假服务器，数连接尝试次数。"""

    def test_connection_failure_is_attempted_once(self):
        """默认构造的 Redis 客户端连接失败会重试 10 次、耗时 4 秒多——在每条消息的路径上，
        而失败之后我们本来就会放行。要数次数而不是只量耗时：量耗时对机器快慢敏感。"""
        attempts = {"n": 0}
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(50)
        port = srv.getsockname()[1]

        def accept_and_hang_up():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                attempts["n"] += 1
                conn.close()

        import threading
        threading.Thread(target=accept_and_hang_up, daemon=True).start()

        async def go():
            c = make_redis_client(f"redis://127.0.0.1:{port}/0", timeout=0.25)
            t = time.perf_counter()
            with pytest.raises(Exception):
                await RedisLimiter(c).take("k", 1, 1)
            return time.perf_counter() - t
        elapsed = run(go())
        srv.close()
        assert attempts["n"] == 1, f"一次取令牌失败后又重试了 {attempts['n'] - 1} 次"
        assert elapsed < 1.0


@needs_redis
class TestRedisOutage:
    def test_chat_keeps_being_limited_not_blocked_when_redis_dies(self, redis_server):
        async def go():
            c = _client(redis_server, timeout=0.25)
            lim = FailOpenLimiter(RedisLimiter(c), MemoryLimiter(), cooldown=60.0)
            before = [(await lim.take("k", 2, 0.0001)).allowed for _ in range(3)]
            redis_server.stop()
            t = time.perf_counter()
            after = [(await lim.take("z", 2, 0.0001)).allowed for _ in range(3)]
            elapsed = time.perf_counter() - t
            await c.aclose()
            return before, after, elapsed, lim.degraded
        before, after, elapsed, degraded = run(go())
        assert before == [True, True, False]       # Redis 在时：正常限流
        assert after == [True, True, False], "Redis 挂了之后限流没有退回进程内：被放空或被误杀"
        assert degraded is True
        # redis-py 默认带指数退避重试：不关掉，仅第一次失败就要 4 秒多（见 make_redis_client）
        assert elapsed < 0.8, f"Redis 挂了之后 3 次取令牌用了 {elapsed:.2f}s"

    def test_only_the_first_request_pays_for_the_outage(self, redis_server):
        async def go():
            c = _client(redis_server, timeout=0.25)
            lim = FailOpenLimiter(RedisLimiter(c), MemoryLimiter(), cooldown=60.0)
            redis_server.stop()
            await lim.take("warm", 1, 1)               # 第一次：吃一次失败
            t = time.perf_counter()
            for i in range(200):
                await lim.take(f"k{i}", 1, 1)
            elapsed = time.perf_counter() - t
            await c.aclose()
            return elapsed
        assert run(go()) < 0.2

    def test_goes_back_to_redis_after_it_recovers(self, redis_server):
        async def go():
            c = _client(redis_server, timeout=0.25)
            lim = FailOpenLimiter(RedisLimiter(c), MemoryLimiter(), cooldown=0.3)
            redis_server.stop()
            await lim.take("k", 5, 1)
            assert lim.degraded
            redis_server.start()                       # 同一个端口
            await asyncio.sleep(0.4)                   # 冷却结束
            await lim.take("k", 5, 1)
            ok = not lim.degraded
            keys = await c.keys("rl:chat:*")
            await c.aclose()
            return ok, keys
        ok, keys = run(go())
        assert ok, "Redis 恢复后没有切回去"
        assert keys, "切回去之后计数没有落到 Redis"
