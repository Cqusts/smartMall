"""会话存储：内存版的扩展性、序列化、Redis 持久层（真 redis-server）、两个真实进程的端到端。

跨实例、重启、并发、Redis 故障——这些单进程单请求的测试永远发现不了，所以每一类都有专门的测试。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from _redis import RedisServer, free_port, needs_redis
from app.agent.shopping.state import ShoppingNeed, ShoppingState
from app.agent.state import SessionContext, Turn
from app.overload import Overloaded
from app.redis_clients import make_sync_redis_client
from app.sessions import (RedisSessionBackend, SessionBusy, SessionStore, apply_ctx,
                          apply_shopping, ctx_to_dict, shopping_to_dict)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ----------------------------------------------------------------------------- 内存版：扩展性


class TestMemoryStoreScales:
    def test_get_cost_does_not_grow_with_the_number_of_sessions(self):
        """原来每次 get() 都遍历全部会话：1 千个 0.035ms、5 万个 1.57ms（在事件循环里）。
        现在摊销 O(1)。5 万个会话时每次 get 应当远低于旧实现的 1.57ms。"""
        store = SessionStore(max_sessions=100_000)
        t = time.perf_counter()
        for i in range(50_000):
            store.get(f"s{i}")
        populate = time.perf_counter() - t
        assert populate < 5.0, f"造 5 万个会话用了 {populate:.1f}s：创建路径是 O(n²)"

        t = time.perf_counter()
        for i in range(2000):
            store.get(f"s{i}")
        per_get_ms = (time.perf_counter() - t) / 2000 * 1000
        assert per_get_ms < 0.2, f"5 万个会话时每次 get() {per_get_ms:.3f}ms（旧实现 1.57ms）"

    def test_ttl_evicts_only_the_expired_and_touch_keeps_a_session_alive(self):
        clock = FakeClock(0)
        store = SessionStore(ttl=15, clock=clock)
        a, b, c = store.get("a"), None, None
        clock.t = 10
        b = store.get("b")
        clock.t = 12
        store.touch(a)                    # a 刚聊过：最后活跃 12
        clock.t = 20
        c = store.get("c")
        clock.t = 26                      # a:14 活着、b:16 过期、c:6 活着
        store.get("trigger")
        assert store.has("a") and store.has("c") and not store.has("b")
        assert store.evicted_ttl == 1

    def test_a_session_in_the_middle_of_a_turn_is_never_evicted_by_ttl(self):
        """淘汰它会让下一个请求拿到一把新锁，同一个会话就能并发跑两轮。"""
        async def go():
            clock = FakeClock(0)
            store = SessionStore(ttl=10, clock=clock)
            store.get("busy")
            store.get("idle")
            lock = store.lock_for("busy")
            await lock.acquire()          # 这个会话正在跑一轮
            clock.t = 100
            store.get("trigger")
            return store.has("busy"), store.has("idle"), store.lock_for("busy") is lock
        assert asyncio.run(go()) == (True, False, True)

    def test_cap_evicts_the_least_recently_used(self):
        clock = FakeClock(0)
        store = SessionStore(max_sessions=3, clock=clock)
        for i, sid in enumerate("abc"):
            clock.t = i
            store.get(sid)
        clock.t = 10
        store.touch(store.get("a"))       # a 最近用过 → 现在最久没动的是 b
        clock.t = 11
        store.get("d")
        assert not store.has("b")
        assert all(store.has(x) for x in "acd")
        assert store.evicted_cap == 1

    def test_cap_never_evicts_a_session_that_is_running(self):
        async def go():
            store = SessionStore(max_sessions=2)
            store.get("oldest")
            store.get("b")
            await store.lock_for("oldest").acquire()
            store.get("c")                # 超限：最久没动的是 oldest，但它在跑 → 淘汰 b
            return store.has("oldest"), store.has("b"), store.has("c")
        assert asyncio.run(go()) == (True, False, True)

    def test_eviction_cleans_every_per_session_structure(self):
        store = SessionStore(max_sessions=1)
        ctx = store.get("old")
        store.lock_for("old")
        store.agent_state(ctx, "shopping", lambda: object())
        store._versions["old"] = 3
        store.get("new")
        assert "old" not in store._data
        assert "old" not in store._locks and "old" not in store._agent_state
        assert "old" not in store._versions, "淘汰后版本表里还留着条目（慢泄漏）"

    def test_stats_report_backend_and_evictions(self):
        s = SessionStore(max_sessions=1)
        s.get("a"); s.get("b")
        st = s.stats()
        assert st["sessions"] == 1 and st["evicted_cap"] == 1 and st["backend"] == "memory"
        assert st["backend_degraded"] is False


# ----------------------------------------------------------------------------- 序列化


class TestSerialization:
    def make(self) -> SessionContext:
        ctx = SessionContext(session_id="s1", user_id=7, current_product_id=42, category_id=3)
        ctx.turns = [Turn("user", "有货吗", 1.5), Turn("assistant", "有的～", 2.5)]
        ctx.summary, ctx.summary_until_turn = "用户问库存", 2
        ctx.handover_count, ctx.negative_streak = 1, 2
        return ctx

    def test_round_trip_preserves_everything(self):
        src = self.make()
        d = json.loads(json.dumps(ctx_to_dict(src), ensure_ascii=False))
        dst = SessionContext(session_id="s1")
        apply_ctx(dst, d)
        assert dst == src

    def test_apply_is_in_place_so_callers_holding_the_object_see_it(self):
        ctx = SessionContext(session_id="s1")
        same = ctx
        apply_ctx(ctx, ctx_to_dict(self.make()))
        assert ctx is same and len(same.turns) == 2 and same.turns[0].role == "user"

    def test_apply_never_changes_the_session_id(self):
        ctx = SessionContext(session_id="mine")
        d = ctx_to_dict(self.make())
        d["session_id"] = "someone-else"
        apply_ctx(ctx, d)
        assert ctx.session_id == "mine"

    def test_shopping_keeps_only_what_survives_across_turns(self):
        st = ShoppingState(session=SessionContext(session_id="s1"))
        st.need = ShoppingNeed(category="外套", price_max=500, colors=["藏青"], sizes=["M"],
                               scene="通勤", notes=["不要连帽"])
        st.asked = 2
        st.answer = "单轮产物，不该被存"
        st.candidates = [{"id": 1}]
        d = json.loads(json.dumps(shopping_to_dict(st), ensure_ascii=False))
        assert set(d) == {"need", "asked"}
        fresh = ShoppingState(session=SessionContext(session_id="s1"))
        apply_shopping(fresh, d)
        assert fresh.need == st.need and fresh.asked == 2
        assert fresh.answer == "" and fresh.candidates == []

    def test_unknown_fields_from_a_newer_version_are_ignored(self):
        st = ShoppingState(session=SessionContext(session_id="s1"))
        apply_shopping(st, {"need": {"category": "裤子", "future_field": 1}, "asked": 1})
        assert st.need.category == "裤子"


# ----------------------------------------------------------------------------- Redis 持久层

needs = needs_redis


@pytest.fixture
def redis_server():
    srv = RedisServer()
    srv.start()
    yield srv
    srv.stop()


def backend(srv: RedisServer, **kw) -> RedisSessionBackend:
    opts = dict(ttl=3600, lock_ttl=10, lock_wait=2.0, cooldown=0.3)
    opts.update(kw)
    return RedisSessionBackend(make_sync_redis_client(srv.url, timeout=0.5), **opts)


def instance(srv: RedisServer, **kw) -> SessionStore:
    """模拟一个 ai-agent 实例：自己的内存工作副本，共用同一个 Redis。"""
    return SessionStore(backend=backend(srv, **kw))


def turn(store: SessionStore, sid: str, text: str, *, delay: float = 0.0,
         shopping=None, overrides=None):
    """跑一轮：返回 fn 开始时已经看得到几条历史。"""
    ctx = store.get(sid)
    seen = []

    def fn():
        seen.append(len(ctx.turns))
        ctx.append("user", text)
        time.sleep(delay)
        ctx.append("assistant", f"echo:{text}")
        return seen[0]
    return store.run_exclusive(ctx, fn, shopping=shopping, overrides=overrides)


@needs
class TestPersistence:
    def test_another_instance_continues_the_conversation(self, redis_server):
        """WebSocket 断线重连落到另一个实例：上下文不能断。"""
        a, b = instance(redis_server), instance(redis_server)
        assert turn(a, "s1", "第一句") == 0
        assert turn(b, "s1", "第二句") == 2, "B 实例没有看到 A 实例聊过的两条历史"
        assert turn(a, "s1", "第三句") == 4, "A 实例没有看到 B 实例追加的历史"
        ctx = a.get("s1")
        assert [t.content for t in ctx.turns if t.role == "user"] == ["第一句", "第二句", "第三句"]

    def test_a_restarted_instance_remembers(self, redis_server):
        """发版/重启不该丢掉所有进行中的对话。"""
        turn(instance(redis_server), "s1", "重启前")
        reborn = instance(redis_server)                # 全新的进程内存
        assert turn(reborn, "s1", "重启后") == 2

    def test_shopping_need_survives_across_instances(self, redis_server):
        """导购累积的需求——"藏青、M 码、五百以内"——是它唯一比客服多出来的东西。"""
        a, b = instance(redis_server), instance(redis_server)
        ctx_a = a.get("s1")
        st_a = ShoppingState(session=ctx_a)
        st_a.need = ShoppingNeed(category="外套", price_max=500, colors=["藏青"])
        st_a.asked = 2
        a.run_exclusive(ctx_a, lambda: None, shopping=st_a)

        ctx_b = b.get("s1")
        st_b = ShoppingState(session=ctx_b)
        b.run_exclusive(ctx_b, lambda: None, shopping=st_b)
        assert st_b.need.category == "外套" and st_b.need.colors == ["藏青"]
        assert st_b.need.price_max == 500 and st_b.asked == 2

    def test_version_increases_by_one_per_turn(self, redis_server):
        a = instance(redis_server)
        for i in range(3):
            turn(a, "s1", f"m{i}")
        doc = json.loads(a.backend._client.get("sm:sess:data:s1"))
        assert doc["v"] == 3 and doc["schema"] == 1

    def test_stale_remote_never_overwrites_newer_local(self, redis_server):
        a = instance(redis_server)
        for i in range(3):
            turn(a, "s1", f"m{i}")                     # 本地 v3，6 条历史
        stale = {"schema": 1, "v": 2, "ctx": ctx_to_dict(SessionContext(session_id="s1")),
                 "shopping": None}
        a.backend._client.set("sm:sess:data:s1", json.dumps(stale))
        assert turn(a, "s1", "m3") == 6, "旧版本的 Redis 数据把本地较新的历史覆盖了"

    def test_redis_flush_does_not_lose_local_state(self, redis_server):
        a = instance(redis_server)
        turn(a, "s1", "m0")
        a.backend._client.flushall()
        assert turn(a, "s1", "m1") == 2
        doc = json.loads(a.backend._client.get("sm:sess:data:s1"))
        assert len(doc["ctx"]["turns"]) == 4, "Redis 被清空后，下一轮应当把本地状态重新写回去"

    def test_request_overrides_win_over_remote(self, redis_server):
        """用户这一刻切到了商品 7；Redis 里是上一轮的商品 3。读完 Redis 要把这个请求的值重新套上，
        否则刚读来的旧值把用户的选择盖掉，答的是 A 商品的知识。"""
        a, b = instance(redis_server), instance(redis_server)
        ctx_a = a.get("s1", current_product_id=3)
        a.run_exclusive(ctx_a, lambda: None)
        ctx_b = b.get("s1")
        b.run_exclusive(ctx_b, lambda: None, overrides={"current_product_id": 7})
        assert ctx_b.current_product_id == 7
        doc = json.loads(b.backend._client.get("sm:sess:data:s1"))
        assert doc["ctx"]["current_product_id"] == 7

    def test_remote_data_without_override_is_adopted(self, redis_server):
        a, b = instance(redis_server), instance(redis_server)
        a.run_exclusive(a.get("s1", current_product_id=3), lambda: None)
        ctx_b = b.get("s1")
        b.run_exclusive(ctx_b, lambda: None)
        assert ctx_b.current_product_id == 3

    def test_keys_expire(self, redis_server):
        a = instance(redis_server, ttl=100)
        turn(a, "s1", "m")
        ttl = a.backend._client.ttl("sm:sess:data:s1")
        assert 90 <= ttl <= 100, "会话数据没有设置过期：每个来过的会话都在 Redis 里永远留一份"

    def test_lock_is_released_after_the_turn(self, redis_server):
        a = instance(redis_server)
        turn(a, "s1", "m")
        assert a.backend._client.exists("sm:sess:lock:s1") == 0

    def test_corrupt_or_foreign_schema_is_treated_as_missing(self, redis_server):
        """一条坏数据不能让这个会话永远聊不了。"""
        a = instance(redis_server)
        a.backend._client.set("sm:sess:data:s1", "{这不是 json")
        assert turn(a, "s1", "m0") == 0
        a.backend._client.set("sm:sess:data:s2", json.dumps({"schema": 99, "v": 5, "ctx": {}}))
        assert turn(a, "s2", "m0") == 0
        doc = json.loads(a.backend._client.get("sm:sess:data:s1"))
        assert doc["schema"] == 1, "坏数据应当被这一轮的正常写回覆盖掉"

    def test_a_failing_turn_still_persists_and_releases(self, redis_server):
        a = instance(redis_server)
        ctx = a.get("s1")

        def boom():
            ctx.append("user", "说了一半")
            raise ValueError("模型挂了")
        with pytest.raises(ValueError):
            a.run_exclusive(ctx, boom)
        assert a.backend._client.exists("sm:sess:lock:s1") == 0, "turn 抛异常后锁没释放"
        doc = json.loads(a.backend._client.get("sm:sess:data:s1"))
        assert len(doc["ctx"]["turns"]) == 1, "已经发生的状态变化应当写回，和内存里一致"

    def test_history_refresh_picks_up_another_instances_turns(self, redis_server):
        a, b = instance(redis_server), instance(redis_server)
        turn(a, "s1", "m0")
        ctx_b = b.get("s1")
        assert ctx_b.turns == []
        b.refresh(ctx_b)
        assert len(ctx_b.turns) == 2


@needs
class TestDistributedLock:
    def test_turns_of_one_session_never_overlap_across_instances(self, redis_server):
        """两个实例同时跑同一个会话：本地 asyncio 锁管不到对方。没有分布式锁就会交错、丢更新。"""
        stores = [instance(redis_server), instance(redis_server)]
        running, peak, lock = 0, 0, threading.Lock()

        def worker(store, tag):
            nonlocal running, peak
            for i in range(6):
                ctx = store.get("shared")

                def fn():
                    nonlocal running, peak
                    with lock:
                        running += 1
                        peak = max(peak, running)
                    ctx.append("user", f"{tag}{i}")
                    time.sleep(0.01)
                    ctx.append("assistant", f"echo:{tag}{i}")
                    with lock:
                        running -= 1
                store.run_exclusive(ctx, fn)

        ths = [threading.Thread(target=worker, args=(s, t)) for s, t in zip(stores, "AB")]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        assert peak == 1, f"同一会话在两个实例上同时跑了 {peak} 轮"
        doc = json.loads(stores[0].backend._client.get("sm:sess:data:shared"))
        users = [t["content"] for t in doc["ctx"]["turns"] if t["role"] == "user"]
        assert len(users) == 12 and doc["v"] == 12, f"丢了更新：只剩 {len(users)} 轮，版本 {doc['v']}"
        assert sorted(users) == sorted(f"{t}{i}" for t in "AB" for i in range(6))

    def test_busy_session_is_rejected_as_session_busy_not_hung(self, redis_server):
        a = instance(redis_server)
        b = instance(redis_server, lock_wait=0.3)
        hold, entered = threading.Event(), threading.Event()
        ran_b = []

        def long_turn():
            ctx = a.get("s1")
            a.run_exclusive(ctx, lambda: (entered.set(), hold.wait(10)))

        th = threading.Thread(target=long_turn)
        th.start()
        assert entered.wait(5)
        try:
            t = time.perf_counter()
            with pytest.raises(SessionBusy) as ei:
                b.run_exclusive(b.get("s1"), lambda: ran_b.append(1))
            waited = time.perf_counter() - t
        finally:
            hold.set()
            th.join(10)
        assert isinstance(ei.value, Overloaded) and ei.value.reason == "session_busy"
        assert ei.value.status == 429
        assert 0.25 <= waited < 1.5
        assert ran_b == [], "没拿到锁，这一轮不该跑"

    def test_a_crashed_holder_does_not_block_the_session_forever(self, redis_server):
        """持锁的实例崩了没释放：锁会过期，别的实例接手。"""
        a = backend(redis_server, lock_ttl=0.3)
        a.lock("s1")                                   # 拿了就“崩溃”，不释放
        b = instance(redis_server, lock_wait=2.0)
        t = time.perf_counter()
        turn(b, "s1", "接手")
        assert time.perf_counter() - t < 1.5

    def test_release_only_deletes_my_own_lock(self, redis_server):
        """我的锁过期后被别人抢走；我迟到的释放不能把别人的锁删了。"""
        a = backend(redis_server, lock_ttl=0.2)
        lease_a = a.lock("s1")
        time.sleep(0.3)                                # a 的锁过期
        b = backend(redis_server, lock_ttl=10)
        lease_b = b.lock("s1")
        assert lease_a.release() is False, "过期后释放应当报告锁已不是自己的"
        assert a._client.exists("sm:sess:lock:s1") == 1, "a 迟到的释放删掉了 b 的锁"
        assert lease_b.release() is True
        assert a._client.exists("sm:sess:lock:s1") == 0


@needs
class TestRedisOutage:
    def test_turn_still_runs_when_redis_is_down(self, redis_server):
        a = instance(redis_server)
        turn(a, "s1", "之前")
        redis_server.stop()
        t = time.perf_counter()
        assert turn(a, "s1", "宕机中") == 2, "Redis 挂了之后，本地已有的上下文也该照常使用"
        assert time.perf_counter() - t < 2.0
        assert a.stats()["backend_degraded"] is True and a.stats()["backend_errors"] >= 1

    def test_during_cooldown_redis_is_not_touched_at_all(self, redis_server):
        """Redis 挂了之后，每一轮都先试一次连接，比不用持久层还糟。"""
        a = instance(redis_server, cooldown=60)

        class Spy:
            def __init__(self, c):
                self._c, self.calls = c, 0

            def set(self, *args, **kw):
                self.calls += 1
                return self._c.set(*args, **kw)

            def __getattr__(self, name):
                return getattr(self._c, name)
        spy = Spy(a.backend._client)
        a.backend._client = spy
        redis_server.stop()
        turn(a, "s1", "m0")                            # 第一轮：发现故障
        before = spy.calls
        for i in range(30):
            turn(a, "s1", f"m{i + 1}")
        assert spy.calls == before, f"冷却期内又访问了 Redis {spy.calls - before} 次"

    def test_recovers_and_writes_back_what_happened_during_the_outage(self, redis_server):
        a = instance(redis_server, cooldown=0.3)
        turn(a, "s1", "宕机前")
        redis_server.stop()
        turn(a, "s1", "宕机中")
        redis_server.start()                           # 同一个端口
        time.sleep(0.4)                                # 冷却结束
        turn(a, "s1", "恢复后")
        assert a.stats()["backend_degraded"] is False
        doc = json.loads(a.backend._client.get("sm:sess:data:s1"))
        users = [t["content"] for t in doc["ctx"]["turns"] if t["role"] == "user"]
        assert users == ["宕机前", "宕机中", "恢复后"], "宕机期间发生的对话没有在恢复后写回"


# ----------------------------------------------------------------------------- REST 接线


class TestRestMapsSessionBusy:
    def test_busy_session_across_instances_is_429_and_a_new_session_is_discarded(self, monkeypatch):
        import app.routers.chat as chat_mod
        from app.config import settings
        from app.main import app

        class Busy:
            ttl, lock_ttl, errors, degraded = 60, 60, 0, False

            def lock(self, sid):
                raise SessionBusy()

        store = SessionStore(backend=Busy())
        monkeypatch.setattr(chat_mod, "_sessions", store)
        monkeypatch.setattr(chat_mod, "get_deps", lambda: object())
        monkeypatch.setattr(settings, "chat_rate_backend", "off")
        chat_mod.reset_guard()

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return await c.post("/chat", json={"message": "hi"})
        r = asyncio.run(go())
        chat_mod.reset_guard()
        assert r.status_code == 429 and r.json()["code"] == 1429
        assert int(r.headers["retry-after"]) >= 1
        assert len(store._data) == 0, "被拒绝的新会话没有丢掉"


# ----------------------------------------------------------------------------- 两个真实进程


class Server:
    def __init__(self, redis_url_port: int, env_extra: dict | None = None) -> None:
        self.port = free_port()
        import os
        env = {**os.environ, "SESSION_BACKEND": "redis", "REDIS_PORT": str(redis_url_port),
               "CHAT_RATE_BACKEND": "off", "CHAT_SESSION_MAX_PENDING": "100",
               "LOG_LEVEL": "WARNING", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_session_server.py")), str(self.port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stderr.read().decode()[-2000:])
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("ai-agent 没起来")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ws_turn(self, sid: str, text: str, **extra) -> dict:
        """走真的 WebSocket 发一条，收到 done 为止，返回 done 事件。"""
        from websockets.sync.client import connect

        with connect(f"ws://127.0.0.1:{self.port}/ws/chat", open_timeout=10) as ws:
            ws.send(json.dumps({"message": text, "session_id": sid, **extra}))
            while True:
                ev = json.loads(ws.recv(timeout=20))
                if ev["type"] in ("done", "error"):
                    return ev

    def chat(self, sid: str, text: str) -> httpx.Response:
        return httpx.post(self.url + "/chat", json={"message": text, "session_id": sid}, timeout=20)

    def history(self, sid: str) -> list[str]:
        d = httpx.get(f"{self.url}/chat/{sid}/history", timeout=20).json()
        return [f"{t['role']}:{t['content']}" for t in d["turns"]]

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)


@needs
class TestTwoRealProcesses:
    @pytest.fixture
    def cluster(self, redis_server):
        started: list[Server] = []

        def start(**kw):
            s = Server(redis_server.port, **kw)
            started.append(s)
            return s
        yield start
        for s in started:
            if s.proc.poll() is None:
                s.stop()

    def test_conversation_follows_the_session_across_instances_and_restarts(self, cluster):
        a, b = cluster(), cluster()
        for srv in (a, b):
            info = httpx.get(srv.url + "/info", timeout=10).json()["sessions"]
            assert info["backend"] == "redis", \
                "配置了 SESSION_BACKEND=redis 却在跑纯内存（后端装配失败被兜底吞掉了）"
        assert a.chat("e2e", "第一句").status_code == 200
        assert b.chat("e2e", "第二句").status_code == 200
        want = ["user:第一句", "assistant:echo:第一句", "user:第二句", "assistant:echo:第二句"]
        assert b.history("e2e") == want
        assert a.history("e2e") == want, "A 实例看不到 B 实例追加的历史（history 接口没有读 Redis）"

        a.stop()                                       # 发版：A 实例被替换
        a2 = cluster()
        assert a2.history("e2e") == want, "重启后的实例读不到之前的对话"
        assert a2.chat("e2e", "第三句").status_code == 200
        assert len(b.history("e2e")) == 6

    def test_websocket_turns_are_persisted_after_the_last_event(self, cluster):
        """WebSocket 的一轮是个生成器，历史在第一个事件**之后**才追加。写回若只包到第一个事件，
        存下去的就是缺了助手回复的半截历史。"""
        a, b = cluster(), cluster()
        assert a.ws_turn("wsx", "ws 第一句")["type"] == "done"
        assert b.ws_turn("wsx", "ws 第二句")["type"] == "done"
        assert a.history("wsx") == [
            "user:ws 第一句", "assistant:echo:ws 第一句",
            "user:ws 第二句", "assistant:echo:ws 第二句"]

    def test_shopping_need_follows_the_session_over_websocket(self, cluster):
        a, b = cluster(), cluster()
        first = a.ws_turn("shop", "我想买件外套", agent="shopping")
        assert first["seen_category"] == "" and first["asked"] == 1
        second = b.ws_turn("shop", "预算五百", agent="shopping")
        assert second["seen_category"] == "外套", "导购在另一个实例上忘了用户要买外套"
        assert second["asked"] == 2, "已追问轮数没有跨实例累计（追问上限会失效）"

    def test_concurrent_turns_on_two_instances_lose_nothing(self, cluster):
        a, b = cluster(), cluster()
        assert a.chat("hot", "开场").status_code == 200
        results: list[int] = []

        def fire(server, i):
            results.append(server.chat("hot", f"并发{i}").status_code)

        ths = [threading.Thread(target=fire, args=(a if i % 2 == 0 else b, i)) for i in range(16)]
        [t.start() for t in ths]
        [t.join(60) for t in ths]
        ok = results.count(200)
        assert ok >= 10 and set(results) <= {200, 429}, results
        hist = a.history("hot")
        assert len(hist) == 2 + 2 * ok, f"{ok} 轮成功但历史只有 {len(hist)} 条：跨实例丢了更新"
        # 每一轮的用户消息紧跟着它自己的回声——没有两轮交错
        for i in range(0, len(hist), 2):
            assert hist[i + 1] == "assistant:echo:" + hist[i].split(":", 1)[1]
