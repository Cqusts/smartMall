"""对话链路的并发行为。

对应压测里的发现：REST ``POST /api/chat`` 是 ``async def``，里面直接同步跑
``safe_run_turn``——一轮要几次 LLM 往返，几秒到十几秒，期间整个进程的其它请求
（含 ``/health``）全部排队。WebSocket 那条路虽然放进了线程，却用 ``asyncio.to_thread``，
默认线程池在 4 核机器上只有 8 个，第 9 个同时在线的用户要排队。

单请求测试永远发现不了这些：每个请求单独跑都是对的。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app import concurrency
from app.agent import AgentState
import app.routers.chat as chat_mod


def _lag_watcher(lags, n=80):
    async def watch():
        for _ in range(n):
            want = time.perf_counter() + 0.01
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - want)
    return watch()


@pytest.fixture(autouse=True)
def _fresh_executor(monkeypatch):
    """这个文件测的是"并行 / 串行 / 不卡事件循环"，不是过载保护。

    所以把保护放开：同一会话 6 条消息同时进来是这里的测试输入，默认配置（每会话最多 2 个
    在途）会合理地拒掉其中 4 条，那是 test_overload.py 的主题。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chat_session_max_pending", 1000)
    monkeypatch.setattr(settings, "chat_rate_backend", "off")
    monkeypatch.setattr(settings, "chat_max_waiting", 10_000)
    chat_mod.reset_guard()
    concurrency.shutdown_chat_executor()
    yield
    concurrency.shutdown_chat_executor()
    chat_mod.reset_guard()


@pytest.fixture
def slow_turn(monkeypatch):
    """把整轮对话换成"睡 delay 秒"的桩，并记录同一时刻有几轮在跑。"""
    state = SimpleNamespace(delay=0.4, inflight=0, peak=0, per_session={}, lock=threading.Lock(),
                            threads=set())

    def fake(message, agent_state, deps):
        sid = agent_state.session.session_id
        with state.lock:
            state.inflight += 1
            state.peak = max(state.peak, state.inflight)
            state.per_session[sid] = state.per_session.get(sid, 0) + 1
            state.per_session.setdefault(("peak", sid), 0)
            state.per_session[("peak", sid)] = max(state.per_session[("peak", sid)],
                                                   state.per_session[sid])
            state.threads.add(threading.current_thread().name)
        time.sleep(state.delay)
        with state.lock:
            state.inflight -= 1
            state.per_session[sid] -= 1
        agent_state.answer = "ok"
        return agent_state

    monkeypatch.setattr(chat_mod, "safe_run_turn", fake)
    monkeypatch.setattr(chat_mod, "get_deps", lambda: object())
    return state


def _post_many(app, bodies):
    async def run(lags):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
            t = time.perf_counter()
            rs = await asyncio.gather(
                _lag_watcher(lags),
                *[c.post("/chat", json=b) for b in bodies])
            return time.perf_counter() - t, rs[1:]
    lags: list[float] = []
    wall, rs = asyncio.run(run(lags))
    return wall, rs, max(lags)


class TestRestChatDoesNotBlock:
    def test_concurrent_turns_overlap_and_loop_stays_free(self, slow_turn):
        """20 个不同会话、每轮 0.4s：串行是 8s，并行应接近 0.4s；事件循环不该被占住。"""
        from app.main import app

        bodies = [{"message": f"你好{i}", "session_id": f"s{i}"} for i in range(20)]
        wall, rs, worst_lag = _post_many(app, bodies)

        assert all(r.status_code == 200 for r in rs), [r.text for r in rs if r.status_code != 200][:1]
        assert wall < 2.0, f"20 轮并发用了 {wall:.1f}s，接近串行的 8s"
        assert slow_turn.peak > 8, f"同时在跑的最多只有 {slow_turn.peak} 轮，被默认线程池卡住了？"
        assert worst_lag < 0.15, f"事件循环被占住了 {worst_lag*1000:.0f}ms"

    def test_turns_run_in_chat_pool_threads(self, slow_turn):
        from app.main import app

        _post_many(app, [{"message": "hi", "session_id": f"t{i}"} for i in range(4)])
        assert slow_turn.threads, "没有记录到线程"
        assert all(n.startswith("chat") for n in slow_turn.threads), slow_turn.threads


class TestSameSessionIsSerialized:
    def test_same_session_never_runs_two_turns_at_once(self, slow_turn):
        """同一个会话的两轮对话同时跑，会交错着改同一份历史。"""
        from app.main import app

        slow_turn.delay = 0.15
        bodies = [{"message": f"m{i}", "session_id": "same"} for i in range(6)]
        wall, rs, _ = _post_many(app, bodies)

        assert all(r.status_code == 200 for r in rs)
        assert slow_turn.per_session[("peak", "same")] == 1, "同一会话出现了并发的两轮"
        assert wall >= 0.15 * 6 * 0.9, "同一会话的 6 轮应当首尾相接"

    def test_different_sessions_still_run_in_parallel(self, slow_turn):
        """串行锁只能管同一个会话——锁成全局的会把并发整个吃掉。"""
        from app.main import app

        slow_turn.delay = 0.3
        wall, rs, _ = _post_many(app, [{"message": "x", "session_id": f"d{i}"} for i in range(10)])
        assert wall < 1.5
        assert slow_turn.peak >= 5


class TestLocksAreCleanedUp:
    def test_lock_expires_with_its_session(self):
        """锁跟着会话一起过期——否则每个来过的会话都在这里留一把锁，是个慢泄漏。"""
        store = chat_mod.SessionStore(ttl=0.0)
        ctx = store.get("old")
        store.lock_for("old")
        assert "old" in store._locks
        time.sleep(0.01)
        store.get("other")                  # get() 会触发过期清理
        assert "old" not in store._locks


class TestDepsAssembly:
    def test_deps_are_built_once_under_concurrent_first_turns(self, monkeypatch):
        """装配很重（LocalRetriever 要把整个知识库向量读进内存）。冷启动时一批并发请求
        同时发现 _deps is None，不加锁就是每个线程各装一份——N 份知识库同时驻留内存。"""
        import app.agent.assembly as assembly

        monkeypatch.setattr(chat_mod, "_deps", None)
        built = []

        def slow_build(**kw):
            built.append(1)
            time.sleep(0.2)
            return object()
        monkeypatch.setattr(assembly, "build_deps", slow_build)

        threads = [threading.Thread(target=chat_mod.get_deps) for _ in range(12)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert len(built) == 1, f"12 个并发首轮对话装配了 {len(built)} 份"


class TestChatExecutor:
    def test_default_is_much_larger_than_asyncio_default(self):
        """asyncio 默认池是 min(32, 核数+4)——4 核上只有 8。对话是 I/O 密集的，线程便宜。"""
        assert concurrency.get_chat_executor()._max_workers == 64

    def test_workers_configurable(self, monkeypatch):
        monkeypatch.setenv("CHAT_WORKERS", "5")
        assert concurrency.get_chat_executor()._max_workers == 5

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHAT_WORKERS", "many")
        assert concurrency.chat_workers() == 64

    def test_contextvars_are_carried_into_the_worker_thread(self):
        """asyncio.to_thread 会复制 contextvars，直接 run_in_executor 不会。
        换线程池后日志里的 trace_id 之类上下文若悄悄丢了，没有任何报错。"""
        import contextvars

        var = contextvars.ContextVar("rid", default="none")

        async def run():
            var.set("req-42")
            return await concurrency.run_blocking(var.get)

        assert asyncio.run(run()) == "req-42"


class TestWebSocketRelay:
    """_relay 是 WebSocket 一轮对话的出口。"""

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_json(self, ev):
            self.sent.append(ev)

    def test_relay_runs_in_chat_pool_not_asyncios_default(self):
        """确定性的证据：线程名。比"并发 N 个看总耗时"稳——后者在核数很多的机器上
        默认池也够用，会让这条测试失去区分力。"""
        import app.routers.ws as ws

        names = []

        def events():
            names.append(threading.current_thread().name)
            yield {"type": "token", "text": "a"}

        async def run():
            fake = self.FakeWs()
            await ws._relay(fake, asyncio.get_running_loop(), events)
            return fake.sent

        sent = asyncio.run(run())
        assert sent == [{"type": "token", "text": "a"}]
        assert names and names[0].startswith("chat"), names

    def test_many_concurrent_relays_overlap(self):
        import app.routers.ws as ws

        def events():
            time.sleep(0.3)
            yield {"type": "done"}

        async def run():
            fake = self.FakeWs()
            t = time.perf_counter()
            await asyncio.gather(*[
                ws._relay(fake, asyncio.get_running_loop(), events) for _ in range(24)])
            return time.perf_counter() - t

        wall = asyncio.run(run())
        # 24 个 × 0.3s：8 线程的默认池要 3 波 ≈ 0.9s，64 线程一波 ≈ 0.3s
        assert wall < 0.7, f"24 个并发对话用了 {wall:.2f}s"


class TestFeedbackDoesNotBlock:
    def test_slow_feedback_write_does_not_freeze_the_loop(self, monkeypatch):
        import app.agent.store as store_mod
        import app.routers.ws as ws

        class SlowStore:
            def record_feedback(self, trace_id, thumb, reason=""):
                time.sleep(0.4)                 # 库慢的时候这是秒级
                return True

        monkeypatch.setattr(store_mod.MySqlTraceStore, "from_env", classmethod(lambda cls: SlowStore()))

        class FakeWs:
            def __init__(self): self.sent = []
            async def send_json(self, ev): self.sent.append(ev)

        async def run():
            lags, fake = [], FakeWs()
            await asyncio.gather(
                _lag_watcher(lags, 60),
                ws._handle_feedback(fake, {"trace_id": "t1", "thumb": "up"}))
            return max(lags), fake.sent

        worst, sent = asyncio.run(run())
        assert sent[0]["ok"] is True
        assert worst < 0.15, f"写反馈期间事件循环被占住了 {worst*1000:.0f}ms"
