"""对话入口的过载保护：准入控制、名额生命周期、会话上限、限流接线。

这些行为单请求测试永远发现不了——每个请求单独跑都是对的，问题只在"同时来很多个"
和"有人中途走掉"的时候出现。所以这里的测试都在制造那两种局面。
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

import app.overload as overload
import app.routers.chat as chat_mod
import app.routers.ws as ws_mod
from app import concurrency
from app.config import settings
from app.overload import ChatGuard, Overloaded, build_guard
from app.ratelimit import MemoryLimiter


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """每个测试一个全新的线程池、会话表和 guard，互不污染。"""
    concurrency.shutdown_chat_executor()
    chat_mod.reset_guard()
    chat_mod._sessions._data.clear()
    chat_mod._sessions._locks.clear()
    chat_mod._sessions._agent_state.clear()
    yield
    concurrency.shutdown_chat_executor()
    chat_mod.reset_guard()
    chat_mod._sessions._data.clear()


def run(coro):
    return asyncio.run(coro)


def mk(**kw) -> ChatGuard:
    base = dict(max_active=2, max_waiting=2, queue_timeout=1.0, session_max_pending=2)
    return ChatGuard(**{**base, **kw})


class Work:
    """线程里跑的"一轮对话"：睡一会儿，并记录同时有几个在跑、每个什么时候开始。"""

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.runs = 0
        self.started_at: list[float] = []

    def __call__(self, *tag) -> str:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.runs += 1
            self.started_at.append(time.perf_counter())
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.running -= 1
        return "ok"


async def one_turn(g: ChatGuard, sid: str, work: Work, locks: dict):
    lock = locks.setdefault(sid, asyncio.Lock())
    async with g.turn(sid, lock) as slot:
        return await slot.submit(work, sid)


async def settle(g: ChatGuard, timeout: float = 3.0) -> None:
    """等所有线程跑完、名额还清。"""
    deadline = time.perf_counter() + timeout
    while g.snapshot()["active"] and time.perf_counter() < deadline:
        await asyncio.sleep(0.01)


# ----------------------------------------------------------------------------- 全局准入


class TestAdmission:
    def test_runs_up_to_max_active_in_parallel(self):
        async def go():
            g, w, locks = mk(max_active=3, max_waiting=0), Work(0.3), {}
            t = time.perf_counter()
            await asyncio.gather(*[one_turn(g, f"s{i}", w, locks) for i in range(3)])
            return time.perf_counter() - t, w.peak
        wall, peak = run(go())
        assert peak == 3 and wall < 0.55

    def test_excess_waits_in_queue_then_runs(self):
        """6 个请求、2 个名额：排队的最终都跑完，不是被丢掉。"""
        async def go():
            g, w, locks = mk(max_active=2, max_waiting=4, queue_timeout=3.0), Work(0.2), {}
            t = time.perf_counter()
            res = await asyncio.gather(*[one_turn(g, f"s{i}", w, locks) for i in range(6)])
            return res, time.perf_counter() - t, w.peak
        res, wall, peak = run(go())
        assert res == ["ok"] * 6
        assert peak == 2, "同时在跑的超过了名额"
        assert 0.55 <= wall < 1.2, "6 个、每波 2 个、每轮 0.2s，应当 3 波"

    def test_queue_full_rejects_immediately(self):
        """等待区满了立刻拒，不能让请求在门口再排一个无界的队。"""
        async def go():
            g, w, locks = mk(max_active=1, max_waiting=1), Work(0.5), {}
            tasks = [asyncio.ensure_future(one_turn(g, f"s{i}", w, locks)) for i in range(2)]
            await asyncio.sleep(0.05)              # 一个在跑，一个在等
            t = time.perf_counter()
            with pytest.raises(Overloaded) as ei:
                await one_turn(g, "late", w, locks)
            rejected_in = time.perf_counter() - t
            await asyncio.gather(*tasks)
            return ei.value, rejected_in, w.runs, g.snapshot()
        exc, rejected_in, runs, snap = run(go())
        assert exc.reason == "busy" and exc.status == 503
        assert rejected_in < 0.05, f"拒绝用了 {rejected_in*1000:.0f}ms：它在排队而不是快速失败"
        assert runs == 2, "被拒绝的请求不该跑到工作线程里"
        assert snap["rejected"] == {"busy": 1}

    def test_queue_timeout_rejects_and_leaves_no_waiter_behind(self):
        """排太久就别跑了：用户多半已经走了，再跑就是白花 LLM 的钱。"""
        async def go():
            g, w, locks = mk(max_active=1, max_waiting=5, queue_timeout=0.2), Work(0.8), {}
            a = asyncio.ensure_future(one_turn(g, "a", w, locks))
            await asyncio.sleep(0.05)
            t = time.perf_counter()
            with pytest.raises(Overloaded) as ei:
                await one_turn(g, "b", w, locks)
            waited = time.perf_counter() - t
            snap = g.snapshot()
            await a
            return ei.value, waited, snap, w.runs
        exc, waited, snap, runs = run(go())
        assert exc.reason == "busy"
        assert 0.15 <= waited <= 0.45, waited
        assert snap["waiting"] == 0, "超时的等待者没有从等待计数里扣掉"
        assert runs == 1

    def test_storm_with_timeouts_and_cancellations_leaks_nothing(self):
        """名额泄漏是这种代码最典型的 bug，而且只在被取消/超时的竞争边缘出现。
        几百个请求、随机取消、大量超时之后，所有计数必须精确归零。"""
        async def go():
            rnd = random.Random(7)
            g = mk(max_active=3, max_waiting=6, queue_timeout=0.03, session_max_pending=3)
            w, locks = Work(0.02), {}

            async def worker(i):
                try:
                    await one_turn(g, f"s{i % 40}", w, locks)
                except Overloaded:
                    pass

            tasks = [asyncio.ensure_future(worker(i)) for i in range(400)]
            for t in tasks:
                if rnd.random() < 0.3:
                    asyncio.get_running_loop().call_later(rnd.random() * 0.15, t.cancel)
            await asyncio.gather(*tasks, return_exceptions=True)
            await settle(g, 5.0)
            return g, w
        g, w = run(go())
        snap = g.snapshot()
        assert snap["active"] == 0 and snap["waiting"] == 0
        assert snap["tracked_sessions"] == 0, "会话待处理表里残留了条目"
        assert g._sem._value == 3, f"信号量剩 {g._sem._value}，应当还回 3"
        assert w.running == 0
        assert snap["rejected"].get("busy", 0) > 0, "这场风暴应该真的触发过拒绝，否则没测到东西"


# ----------------------------------------------------------------------------- 名额生命周期


class TestSlotLifetime:
    def test_slot_is_held_until_the_thread_finishes_even_if_caller_is_cancelled(self):
        """客户端中途断开 → 协程被取消，但线程还在跑。名额若在协程退出时就还，
        新请求立刻进来，线程池里就有了 N+1 个任务。名额必须和线程同生共死。"""
        async def go():
            g, w, locks = mk(max_active=1, max_waiting=5, queue_timeout=3.0), Work(0.5), {}
            a = asyncio.ensure_future(one_turn(g, "a", w, locks))
            await asyncio.sleep(0.1)
            a.cancel()
            await asyncio.gather(a, return_exceptions=True)
            active_after_cancel = g.snapshot()["active"]

            t0 = time.perf_counter()
            await one_turn(g, "b", w, locks)             # 必须等 a 的线程跑完才能进
            b_started = w.started_at[1] - t0
            await settle(g)
            return active_after_cancel, b_started, w.peak, g.snapshot()["active"]
        active, b_started, peak, final = run(go())
        assert active == 1, "协程取消后名额被提前还了，线程还在跑"
        assert b_started >= 0.3, f"b 在 a 的线程结束前 {b_started:.2f}s 就开始跑了"
        assert peak == 1
        assert final == 0

    def test_exception_in_the_job_still_releases_the_slot(self):
        async def go():
            g = mk(max_active=1, max_waiting=0)

            def boom():
                raise ValueError("模型挂了")
            with pytest.raises(ValueError):
                async with g.turn("s", asyncio.Lock()) as slot:
                    await slot.submit(boom)
            await settle(g)
            return g.snapshot()
        assert run(go())["active"] == 0

    def test_failed_submit_releases_the_slot(self, monkeypatch):
        """线程池已经关了之类：job 根本没提交成功，没有线程会来还名额，必须由这里还。"""
        monkeypatch.setattr(overload, "_submit_to_pool",
                            lambda fn, *a: (_ for _ in ()).throw(RuntimeError("pool closed")))

        async def go():
            g = mk(max_active=1, max_waiting=0)
            with pytest.raises(RuntimeError):
                async with g.turn("s", asyncio.Lock()) as slot:
                    slot.submit(lambda: None)
            return g.snapshot()["active"], g._sem._value
        assert run(go()) == (0, 1)

    def test_slot_released_when_nothing_was_submitted(self):
        """进了门又没提交工作（比如后面的参数校验失败）：名额由退出时归还。"""
        async def go():
            g = mk(max_active=1, max_waiting=0)
            async with g.turn("s", asyncio.Lock()):
                inside = g.snapshot()["active"]
            return inside, g.snapshot()["active"]
        assert run(go()) == (1, 0)

    def test_slot_cannot_be_submitted_twice(self):
        async def go():
            g = mk(max_active=1, max_waiting=0)
            async with g.turn("s", asyncio.Lock()) as slot:
                await slot.submit(lambda: None)
                with pytest.raises(RuntimeError):
                    slot.submit(lambda: None)
            await settle(g)
        run(go())


# ----------------------------------------------------------------------------- 会话上限


class TestSessionLimits:
    def test_third_concurrent_message_in_one_session_is_rejected(self):
        """用户连点十次发送，不该排出十轮对话。"""
        async def go():
            g, w, locks = mk(max_active=4, session_max_pending=2), Work(0.3), {}
            first = [asyncio.ensure_future(one_turn(g, "same", w, locks)) for _ in range(2)]
            await asyncio.sleep(0.05)
            t = time.perf_counter()
            with pytest.raises(Overloaded) as ei:
                await one_turn(g, "same", w, locks)
            took = time.perf_counter() - t
            other = await one_turn(g, "other", w, locks)       # 别的会话不受影响
            await asyncio.gather(*first)
            return ei.value, took, other, w.runs
        exc, took, other, runs = run(go())
        assert exc.reason == "session_busy" and exc.status == 429
        assert took < 0.05
        assert other == "ok" and runs == 3

    def test_accepted_turns_of_one_session_still_never_overlap(self):
        async def go():
            g, w, locks = mk(max_active=4, session_max_pending=2), Work(0.15), {}
            await asyncio.gather(*[one_turn(g, "same", w, locks) for _ in range(2)])
            return w.peak
        assert run(go()) == 1

    def test_pending_table_does_not_keep_finished_sessions(self):
        async def go():
            g, w, locks = mk(max_active=4, max_waiting=100, queue_timeout=5.0), Work(0.01), {}
            await asyncio.gather(*[one_turn(g, f"s{i}", w, locks) for i in range(50)])
            await settle(g)
            return g.snapshot()["tracked_sessions"]
        assert run(go()) == 0

    def test_queued_messages_of_one_session_do_not_hold_global_slots(self):
        """顺序：先拿**会话锁**，再拿全局名额。反过来的话，一个用户排队的第二条消息
        拿着名额干等锁，别的用户被它挡在门外——而它们根本不能并行。"""
        async def go():
            g, w, locks = mk(max_active=2, max_waiting=0, session_max_pending=2), Work(0.5), {}
            a1 = asyncio.ensure_future(one_turn(g, "A", w, locks))
            await asyncio.sleep(0.05)
            a2 = asyncio.ensure_future(one_turn(g, "A", w, locks))   # 在会话锁上排队
            await asyncio.sleep(0.05)
            t = time.perf_counter()
            await one_turn(g, "B", w, locks)                         # 应当有名额，立刻开始
            b_started = w.started_at[1] - t
            await asyncio.gather(a1, a2)
            return b_started
        b_started = run(go())
        assert b_started < 0.2, f"B 等了 {b_started:.2f}s 才开始：A 的排队消息占着名额"


# ----------------------------------------------------------------------------- 限流接线


class TestRateCheck:
    def test_ip_and_identity_buckets_both_apply(self):
        async def go():
            g = mk(limiter=MemoryLimiter(), ip_bucket=(3, 0.0001), identity_bucket=(2, 0.0001))
            out = []
            for ip, ident in [("1.1.1.1", "u1"), ("1.1.1.1", "u1"), ("1.1.1.1", "u1"),
                              ("1.1.1.1", "u2"), ("2.2.2.2", "u1")]:
                try:
                    await g.check_rate(ip=ip, identity=ident)
                    out.append("ok")
                except Overloaded as e:
                    out.append(e.reason)
            return out
        # 第 3 个：IP 桶还有，但 u1 的用完了；第 4 个：u2 自己没问题，但这个 IP 的 3 个已用完；
        # 第 5 个：换了 IP，但 u1 的桶是跨 IP 的，仍然是空的
        assert run(go()) == ["ok", "ok", "rate_limited", "rate_limited", "rate_limited"]

    def test_without_identity_only_the_ip_is_checked(self):
        async def go():
            g = mk(limiter=MemoryLimiter(), ip_bucket=(3, 0.0001), identity_bucket=(1, 0.0001))
            out = []
            for _ in range(4):
                try:
                    await g.check_rate(ip="1.1.1.1", identity=None)
                    out.append("ok")
                except Overloaded:
                    out.append("limited")
            return out
        assert run(go()) == ["ok", "ok", "ok", "limited"]

    def test_retry_after_is_whole_seconds_and_at_least_one(self):
        assert Overloaded("busy", 0.2).retry_after == 1
        assert Overloaded("busy", 1.01).retry_after == 2
        assert Overloaded("busy", 5).retry_after == 5

    def test_ws_event_shape_is_what_the_frontend_renders(self):
        ev = Overloaded("busy", 5).ws_event()
        assert ev["type"] == "error" and ev["code"] == "busy"
        assert isinstance(ev["answer"], str) and ev["answer"]
        assert ev["retry_after"] == 5

    def test_frontend_renders_error_events_with_answer(self):
        """前端的契约：它对 type=error 的处理正是清掉"思考中"并把 answer 显示成气泡。
        这条测试读前端源码——有人改了前端的 error 分支，这里会红，而不是线上用户看到空白。"""
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        i = html.index("ev.type === 'error'")
        branch = html[i:i + 260]
        assert "ev.answer" in branch and "cstatus" in branch

    def test_rejection_log_is_throttled(self):
        """过载时每秒可能上千次拒绝，每次都写日志，日志本身就成了负载。"""
        logs: list[str] = []
        clock = FakeClock()
        g = mk(clock=clock, log=logs.append)
        for _ in range(100):
            g._reject("busy", 1)
        assert len(logs) == 1
        clock.t += 1.1
        g._reject("busy", 1)
        assert len(logs) == 2
        assert g.snapshot()["rejected"] == {"busy": 101}, "日志限频不能影响计数"


class TestLimiterIsNotSilentlyReplaced:
    def test_a_fresh_empty_limiter_is_kept_and_actually_limits(self):
        """回归：MemoryLimiter 曾定义 __len__，空的时候是假值，``limiter or NoLimit()``
        把它悄悄换成了不限流——默认配置下限流整个失效，没有任何报错。"""
        async def go():
            lim = MemoryLimiter()
            g = mk(limiter=lim, ip_bucket=(1, 0.0001))
            assert g.limiter is lim
            await g.check_rate(ip="1.1.1.1", identity=None)
            with pytest.raises(Overloaded):
                await g.check_rate(ip="1.1.1.1", identity=None)
        run(go())

    def test_a_falsy_limiter_is_kept_too(self):
        """上面那条守的是 MemoryLimiter 自己；这条守的是 ChatGuard 的写法——
        任何限流器实现都可能是假值，``limiter or NoLimit()`` 都会把它换掉。"""
        class Falsy(MemoryLimiter):
            def __bool__(self):
                return False

        async def go():
            lim = Falsy()
            g = mk(limiter=lim)
            return g.limiter is lim
        assert run(go()) is True

    def test_empty_limiter_is_truthy(self):
        assert bool(MemoryLimiter()) is True


class TestBuildGuard:
    def test_settings_are_mapped(self, monkeypatch):
        monkeypatch.setattr(settings, "chat_rate_backend", "memory")
        monkeypatch.setattr(settings, "chat_rate_ip_per_min", 120)
        monkeypatch.setattr(settings, "chat_rate_identity_per_min", 6)
        monkeypatch.setattr(settings, "chat_max_waiting", 7)
        monkeypatch.setattr(settings, "chat_queue_timeout", 3.5)
        monkeypatch.setattr(settings, "chat_session_max_pending", 4)

        async def go():
            return build_guard(settings, workers=9)
        g = run(go())
        assert (g.max_active, g.max_waiting, g.queue_timeout, g.session_max_pending) == (9, 7, 3.5, 4)
        assert g.ip_bucket[1] == pytest.approx(2.0) and g.identity_bucket[1] == pytest.approx(0.1)
        assert isinstance(g.limiter, MemoryLimiter)

    def test_off_means_no_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "chat_rate_backend", "off")

        async def go():
            g = build_guard(settings, workers=2)
            for _ in range(1000):
                await g.check_rate(ip="1.1.1.1", identity="u")
            return True
        assert run(go())

    def test_redis_backend_that_cannot_initialise_degrades_to_memory(self, monkeypatch):
        """装不出 Redis 客户端（没装库、地址写错）不能让对话起不来。"""
        monkeypatch.setattr(settings, "chat_rate_backend", "redis")
        import app.ratelimit as rl
        monkeypatch.setattr(rl, "make_redis_client",
                            lambda *a, **k: (_ for _ in ()).throw(ImportError("no redis")))
        logs: list[str] = []

        async def go():
            return build_guard(settings, workers=2, log=logs.append)
        g = run(go())
        assert isinstance(g.limiter, MemoryLimiter)
        assert logs and "改用进程内限流" in logs[0]

    def test_redis_backend_wraps_in_fail_open(self, monkeypatch):
        monkeypatch.setattr(settings, "chat_rate_backend", "redis")
        monkeypatch.setattr(settings, "redis_port", 1)          # 没人监听
        from app.ratelimit import FailOpenLimiter

        async def go():
            g = build_guard(settings, workers=2)
            assert isinstance(g.limiter, FailOpenLimiter)
            t = time.perf_counter()
            await g.check_rate(ip="1.1.1.1", identity="u")      # Redis 不可用：放行，不抛、不久等
            return time.perf_counter() - t
        assert run(go()) < 1.0


# ----------------------------------------------------------------------------- REST 接线


@pytest.fixture
def small_pool(monkeypatch):
    """2 个名额、等待区 1 个：6 个并发请求 = 2 跑 + 1 等 + 3 拒。"""
    monkeypatch.setenv("CHAT_WORKERS", "2")
    monkeypatch.setattr(settings, "chat_max_waiting", 1)
    monkeypatch.setattr(settings, "chat_queue_timeout", 2.0)
    monkeypatch.setattr(settings, "chat_rate_backend", "off")
    monkeypatch.setattr(settings, "chat_session_max_pending", 2)


@pytest.fixture
def stub_turn(monkeypatch):
    st = SimpleNamespace(delay=0.4, calls=0, lock=threading.Lock())

    def fake(message, agent_state, deps):
        with st.lock:
            st.calls += 1
        time.sleep(st.delay)
        agent_state.answer = "ok"
        return agent_state

    monkeypatch.setattr(chat_mod, "safe_run_turn", fake)
    monkeypatch.setattr(chat_mod, "get_deps", lambda: object())
    return st


def post_many(bodies, *, sequential=False, headers=None):
    from app.main import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
            if sequential:
                return [await c.post("/chat", json=b, headers=headers) for b in bodies]
            return await asyncio.gather(*[c.post("/chat", json=b, headers=headers) for b in bodies])
    return run(go())


class TestRestOverload:
    def test_excess_requests_get_503_with_retry_after(self, small_pool, stub_turn):
        rs = post_many([{"message": "hi", "session_id": f"s{i}"} for i in range(6)])
        ok = [r for r in rs if r.status_code == 200]
        busy = [r for r in rs if r.status_code == 503]
        assert len(ok) == 3 and len(busy) == 3, [r.status_code for r in rs]
        for r in busy:
            assert r.headers["retry-after"] == "5"
            body = r.json()
            assert body["code"] == 6529 and "繁忙" in body["message"] or "正忙" in body["message"]
        assert stub_turn.calls == 3, "被拒绝的请求不该调到 LLM"

    def test_rejected_new_sessions_are_not_left_behind(self, small_pool, stub_turn):
        """被拒的新会话客户端没拿到 session_id，重试时会再新建一个——
        每次拒绝都留一个没人认领的会话，过载时正是请求最多的时候。"""
        rs = post_many([{"message": "hi"} for _ in range(6)])      # 都不带 session_id
        accepted = sum(r.status_code == 200 for r in rs)
        assert accepted == 3
        assert len(chat_mod._sessions._data) == accepted, \
            f"会话表里有 {len(chat_mod._sessions._data)} 个，只有 {accepted} 个成功"

    def test_rejected_existing_session_is_kept(self, small_pool, stub_turn):
        """已存在的会话（有历史）被拒绝时不能被丢掉——用户重试要接着聊。"""
        chat_mod._sessions.get("keep-me")
        stub_turn.delay = 0.6
        bodies = [{"message": "x", "session_id": f"f{i}"} for i in range(3)]      # 占满 2 跑 + 1 等
        bodies.append({"message": "again", "session_id": "keep-me"})
        rs = post_many(bodies)
        assert rs[-1].status_code == 503
        assert "keep-me" in chat_mod._sessions._data

    def test_info_exposes_load_and_rejections(self, small_pool, stub_turn):
        post_many([{"message": "hi", "session_id": f"s{i}"} for i in range(6)])
        from app.main import app
        with TestClient(app) as c:
            # TestClient 另起了事件循环，旧 guard 绑着已关闭的循环——正式环境只有一个循环
            chat_mod.reset_guard()
            load = c.get("/info").json()["chat_load"]
        assert load["max_active"] == 2 and "rejected" in load and load["active"] == 0


class TestRestRateLimit:
    @pytest.fixture(autouse=True)
    def _limits(self, monkeypatch, stub_turn):
        monkeypatch.setattr(settings, "chat_rate_backend", "memory")
        monkeypatch.setattr(settings, "chat_rate_identity_burst", 3)
        monkeypatch.setattr(settings, "chat_rate_identity_per_min", 0.06)
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 1000)
        stub_turn.delay = 0.0

    def test_same_session_is_limited_after_burst(self, stub_turn):
        rs = post_many([{"message": "hi", "session_id": "chatty"} for _ in range(5)], sequential=True)
        assert [r.status_code for r in rs] == [200, 200, 200, 429, 429]
        assert int(rs[3].headers["retry-after"]) >= 1
        assert rs[3].json()["code"] == 1429
        assert stub_turn.calls == 3, "被限流的消息不该调到 LLM——这正是限流要省的钱"

    def test_users_have_separate_buckets(self, stub_turn):
        bodies = [{"message": "hi", "user_id": u} for u in (1, 2) for _ in range(3)]
        rs = post_many(bodies, sequential=True)
        assert all(r.status_code == 200 for r in rs)

    def test_ip_bucket_limits_across_sessions(self, monkeypatch, stub_turn):
        """自报的 user_id / session_id 想换就换——换着花样绕过用户桶的，由 IP 桶挡住。"""
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 4)
        monkeypatch.setattr(settings, "chat_rate_ip_per_min", 0.06)
        chat_mod.reset_guard()
        rs = post_many([{"message": "hi", "session_id": f"s{i}"} for i in range(7)], sequential=True)
        assert [r.status_code for r in rs] == [200] * 4 + [429] * 3

    def test_rate_limit_runs_before_a_session_is_created(self, monkeypatch, stub_turn):
        """被限流的请求不该往会话表里塞对象——限流挡住了 LLM 调用，却挡不住内存增长，
        等于只做了一半。"""
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 3)
        monkeypatch.setattr(settings, "chat_rate_ip_per_min", 0.06)
        chat_mod.reset_guard()
        rs = post_many([{"message": "hi"} for _ in range(10)], sequential=True)
        assert sum(r.status_code == 200 for r in rs) == 3
        assert len(chat_mod._sessions._data) == 3

    def test_forwarded_for_is_honoured_only_when_configured(self, monkeypatch, stub_turn):
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 2)
        monkeypatch.setattr(settings, "chat_rate_ip_per_min", 0.06)
        # 没配可信代理：不同的 X-Forwarded-For 对限流毫无影响（攻击者想换 IP 换不掉）
        chat_mod.reset_guard()
        a = post_many([{"message": "hi", "session_id": f"a{i}"} for i in range(3)],
                      sequential=True, headers={"x-forwarded-for": "6.6.6.6"})
        b = post_many([{"message": "hi", "session_id": f"b{i}"} for i in range(1)],
                      sequential=True, headers={"x-forwarded-for": "7.7.7.7"})
        assert [r.status_code for r in a] == [200, 200, 429]
        assert b[0].status_code == 429

        # 配了 1 层可信代理：取最右一项，不同真实客户端各用各的桶
        monkeypatch.setattr(settings, "chat_trust_proxy_hops", 1)
        chat_mod.reset_guard()
        c = post_many([{"message": "hi", "session_id": "c1"}], sequential=True,
                      headers={"x-forwarded-for": "6.6.6.6, 203.0.113.1"})
        d = post_many([{"message": "hi", "session_id": "d1"}], sequential=True,
                      headers={"x-forwarded-for": "6.6.6.6, 203.0.113.2"})
        assert c[0].status_code == 200 and d[0].status_code == 200


# ----------------------------------------------------------------------------- WebSocket 接线


@pytest.fixture
def stub_stream(monkeypatch):
    """WebSocket 一轮对话的桩：先等 gate（默认不等），再睡 delay 秒，然后给出 done。"""
    st = SimpleNamespace(delay=0.0, gate=None, calls=0, lock=threading.Lock())

    def fake_stream(message, state, deps):
        with st.lock:
            st.calls += 1
        if st.gate is not None:
            st.gate.wait(10)
        time.sleep(st.delay)
        yield {"type": "done", "answer": "ok", "session_id": state.session.session_id}

    monkeypatch.setattr(ws_mod, "stream_turn", fake_stream)
    monkeypatch.setattr(ws_mod, "get_deps", lambda: object())
    return st


def ws_send(conn, text, **kw):
    conn.send_json({"message": text, **kw})


class TestWebSocketOverload:
    def test_busy_turn_gets_error_event_and_connection_survives(self, monkeypatch, stub_stream):
        monkeypatch.setenv("CHAT_WORKERS", "1")
        monkeypatch.setattr(settings, "chat_max_waiting", 0)
        monkeypatch.setattr(settings, "chat_rate_backend", "off")
        from app.main import app

        stub_stream.gate = threading.Event()
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as w1, \
                    client.websocket_connect("/ws/chat") as w2:
                ws_send(w1, "第一个", session_id="a")
                deadline = time.time() + 5
                while stub_stream.calls < 1 and time.time() < deadline:
                    time.sleep(0.01)
                assert stub_stream.calls == 1

                ws_send(w2, "第二个", session_id="b")           # 唯一的名额被占着、等待区 0
                ev = w2.receive_json()
                assert ev["type"] == "error" and ev["code"] == "busy"
                assert ev["answer"] and ev["retry_after"] >= 1
                assert stub_stream.calls == 1, "被拒绝的消息不该进工作线程"
                assert "b" not in chat_mod._sessions._data, "被拒绝的新会话没有丢掉"

                stub_stream.gate.set()
                assert w1.receive_json()["type"] == "done"

                ws_send(w2, "重发", session_id="b")             # 同一条连接，稍后重试
                assert w2.receive_json()["type"] == "done"

    def test_rate_limited_message_gets_error_event_and_connection_survives(
            self, monkeypatch, stub_stream):
        monkeypatch.setattr(settings, "chat_rate_backend", "memory")
        monkeypatch.setattr(settings, "chat_rate_identity_burst", 2)
        monkeypatch.setattr(settings, "chat_rate_identity_per_min", 0.06)
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 1000)
        from app.main import app

        with TestClient(app) as client, client.websocket_connect("/ws/chat") as w:
            evs = []
            for i in range(3):
                ws_send(w, f"m{i}", session_id="chatty")
                evs.append(w.receive_json())
            assert [e["type"] for e in evs] == ["done", "done", "error"]
            assert evs[2]["code"] == "rate_limited"
            assert stub_stream.calls == 2

    def test_rate_limited_new_session_is_not_created(self, monkeypatch, stub_stream):
        monkeypatch.setattr(settings, "chat_rate_backend", "memory")
        monkeypatch.setattr(settings, "chat_rate_ip_burst", 2)
        monkeypatch.setattr(settings, "chat_rate_ip_per_min", 0.06)
        from app.main import app

        with TestClient(app) as client, client.websocket_connect("/ws/chat") as w:
            for i in range(6):
                ws_send(w, f"m{i}")                              # 不带 session_id
                w.receive_json()
        assert len(chat_mod._sessions._data) == 2

    def test_feedback_is_not_queued_behind_busy_chat_workers(self, monkeypatch, stub_stream):
        """过载时对话线程全被整轮对话占着。一次 1~2ms 的点赞若走同一个线程池，
        就要排在几秒一轮的对话后面——点赞按钮"卡住"，而这恰恰发生在最忙的时候。"""
        monkeypatch.setenv("CHAT_WORKERS", "1")
        monkeypatch.setattr(settings, "chat_rate_backend", "off")
        import app.agent.store as store_mod

        class FastStore:
            def record_feedback(self, trace_id, thumb, reason=""):
                return True
        monkeypatch.setattr(store_mod.MySqlTraceStore, "from_env", classmethod(lambda c: FastStore()))
        from app.main import app

        stub_stream.gate = threading.Event()
        got: list = []
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as w1, \
                    client.websocket_connect("/ws/chat") as w2:
                try:
                    ws_send(w1, "占住唯一的线程", session_id="a")
                    deadline = time.time() + 5
                    while stub_stream.calls < 1 and time.time() < deadline:
                        time.sleep(0.01)

                    w2.send_json({"type": "feedback", "trace_id": "t1", "thumb": "up"})
                    th = threading.Thread(target=lambda: got.append(w2.receive_json()), daemon=True)
                    th.start()
                    th.join(3.0)
                finally:
                    stub_stream.gate.set()
                    w1.receive_json()
        assert got and got[0]["type"] == "feedback_ack" and got[0]["ok"] is True, \
            "点赞被排在了占满的对话线程池后面"
