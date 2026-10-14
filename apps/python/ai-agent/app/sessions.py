"""会话存储：进程内工作副本 + 可选的 Redis 持久层。

**两件事，别混在一起：**

1. **内存里的工作副本**（:class:`SessionStore`）。每一轮对话都在它上面读写。
   原来的实现有两个会在生产里咬人的缺陷（压测量到的）：
   * ``get()`` 每次都调 ``_evict``，而 ``_evict`` **遍历全部会话**——O(n)。
     1 千个会话时每次 0.035ms，5 万个时 1.57ms，而且是在事件循环里；造 20 万个会话要 O(n²)。
     现在 5 万个会话时 get() 是 0.0007ms，造 5 万个 0.30s。
   * 没有数量上限。24 小时 TTL 内来过的每个会话都留着，一次爬虫就能把内存吃光。

   现在按"最近一次完成对话的时间"排序，过期的从队头摘、遇到没过期的就停（摊销 O(1)），
   并有 ``max_sessions`` 上限（LRU）。**正在跑一轮的会话永远不会被淘汰**——
   它的锁被持有着；淘汰它会让下一个请求拿到一把新锁，同一会话就能并发两轮。

2. **Redis 持久层**（:class:`RedisSessionBackend`，``SESSION_BACKEND=redis`` 开启）。
   解决内存版必然的三个问题：重启/发版丢掉所有进行中的对话、WebSocket 断线重连落到另一个实例上
   上下文就断了、导购累积的需求（"藏青、M 码、五百以内"）随进程一起消失。

   **一轮对话的流程**（:meth:`SessionStore.run_exclusive`，在工作线程里跑）：

   1. 抢这个会话的**分布式锁**（``SET NX PX``）——跨实例串行。本进程内的串行靠 asyncio 锁，
      两个实例同时跑同一个会话就靠它；拿不到（别的实例正在跑）等 ``lock_wait`` 秒，
      还不行就拒绝成 ``session_busy``。
   2. 若 Redis 里的版本比本地新，就**原地覆盖**本地副本（保持对象身份：调用方手里还拿着它）。
   3. 跑这一轮。
   4. 版本号 +1，写回 Redis（带 TTL）。
   5. 释放锁——Lua 比对令牌再删，不会删掉别人后来抢到的锁。

   读写 Redis 放在**工作线程**里、用同步客户端：它和 LLM 调用一样是阻塞 I/O，
   线程本来就是干这个的；放事件循环里就得把整条对话链路改成 async。

**Redis 故障时放行（fail-open）。**会话持久化是增强，不是对话的前提：Redis 连不上就退回纯内存
照常聊（不加锁、不读不写），冷却期内不再试，避免每轮都吃一次连接超时。
代价是故障期间多实例的上下文不共享——和没开持久层时一样，不会更糟。

**已知局限（不是疏漏）：**

* 锁没有续期。一轮对话超过 ``lock_ttl``（默认 120s）时锁会过期，另一个实例可能插进来；
  释放时会发现锁已丢并告警，写回是后写者胜。LLM 客户端超时应当远小于 ``lock_ttl``。
* 写回是整份 JSON。实测每轮持久层多花的时间：4KB（20 条历史）p50 0.7ms，21KB（100 条）p50 1.3ms，
  相对一轮对话的几秒 LLM 耗时可以忽略；但历史无限增长时这个数会跟着涨，该做摘要截断，而不是靠这里。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

from .agent.state import SessionContext, Turn
from .overload import Overloaded

log = logging.getLogger(__name__)


class SessionBusy(Overloaded):
    """另一个实例正在跑这个会话的一轮，等不到。继承 Overloaded：入口已有的拒绝处理直接生效，
    REST 返回 429、WebSocket 发 ``type: error, code: session_busy``，连接保持。"""

    def __init__(self) -> None:
        super().__init__("session_busy", 2.0)


# ----------------------------------------------------------------------------- 序列化

SCHEMA = 1


def ctx_to_dict(ctx: SessionContext) -> dict[str, Any]:
    return dataclasses.asdict(ctx)


def apply_ctx(ctx: SessionContext, d: dict[str, Any]) -> None:
    """用 ``d`` 原地覆盖 ``ctx``。**不新建对象**——调用方手里拿着 ctx，换对象等于白读。
    ``session_id`` 不动：它是这条数据的 key。"""
    ctx.user_id = d.get("user_id")
    ctx.current_product_id = d.get("current_product_id")
    ctx.category_id = d.get("category_id")
    ctx.turns = [Turn(role=t["role"], content=t["content"], ts=t.get("ts", 0.0))
                 for t in d.get("turns", [])]
    ctx.summary = d.get("summary", "")
    ctx.summary_until_turn = d.get("summary_until_turn", 0)
    ctx.handover_count = d.get("handover_count", 0)
    ctx.negative_streak = d.get("negative_streak", 0)


def shopping_to_dict(state: Any) -> dict[str, Any]:
    """导购跨轮保留的只有两样：累积的需求和已追问轮数（见 ShoppingState.begin_turn）。
    其余都是单轮产物，每轮开头清掉，不必存。"""
    return {"need": dataclasses.asdict(state.need), "asked": state.asked}


def apply_shopping(state: Any, d: dict[str, Any]) -> None:
    from .agent.shopping.state import ShoppingNeed

    known = {f.name for f in dataclasses.fields(ShoppingNeed)}
    state.need = ShoppingNeed(**{k: v for k, v in d.get("need", {}).items() if k in known})
    state.asked = d.get("asked", 0)


# ----------------------------------------------------------------------------- Redis 后端


class Lease:
    def __init__(self, release: Callable[[], bool]) -> None:
        self._release = release

    def release(self) -> bool:
        """释放。返回 False 表示锁已经不是我们的了（过期后被别人抢走）。"""
        return self._release()


_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisSessionBackend:
    """同步 Redis 客户端，在工作线程里用。线程安全（redis-py 的连接池本身是）。"""

    def __init__(self, client: Any, *, ttl: float = 86400.0, lock_ttl: float = 120.0,
                 lock_wait: float = 3.0, cooldown: float = 5.0, prefix: str = "sm:sess:",
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self.ttl = ttl
        self.lock_ttl = lock_ttl
        self.lock_wait = lock_wait
        self._cooldown = cooldown
        self._prefix = prefix
        self._clock = clock
        self._sleep = sleep
        self._unlock = client.register_script(_UNLOCK)
        self._down_until = 0.0
        self._mu = threading.Lock()
        self.errors = 0

    @property
    def degraded(self) -> bool:
        return self._clock() < self._down_until

    def _fail(self, what: str, exc: BaseException) -> None:
        with self._mu:
            self.errors += 1
            first = self._clock() >= self._down_until
            self._down_until = self._clock() + self._cooldown
        if first:
            log.warning("会话持久层 Redis 不可用（%s），退回纯内存，%.0fs 后重试：%s: %s",
                        what, self._cooldown, type(exc).__name__, exc)

    # ---- 以下方法在 Redis 不可用时抛 RedisDown，调用方（SessionStore）负责 fail-open

    def lock(self, sid: str) -> Lease:
        if self.degraded:
            raise RedisDown()
        key, token = f"{self._prefix}lock:{sid}", uuid.uuid4().hex
        deadline = self._clock() + self.lock_wait
        try:
            while True:
                if self._client.set(key, token, nx=True, px=int(self.lock_ttl * 1000)):
                    return Lease(lambda: self._release(key, token))
                if self._clock() >= deadline:
                    raise SessionBusy()
                self._sleep(0.05)
        except SessionBusy:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail("lock", exc)
            raise RedisDown() from exc

    def _release(self, key: str, token: str) -> bool:
        try:
            return bool(self._unlock(keys=[key], args=[token]))
        except Exception as exc:  # noqa: BLE001  释放失败：锁会在 lock_ttl 后自己过期
            self._fail("unlock", exc)
            return True

    def load(self, sid: str) -> tuple[int, dict[str, Any]] | None:
        if self.degraded:
            raise RedisDown()
        try:
            raw = self._client.get(f"{self._prefix}data:{sid}")
        except Exception as exc:  # noqa: BLE001
            self._fail("load", exc)
            raise RedisDown() from exc
        if raw is None:
            return None
        try:
            doc = json.loads(raw)
            if doc.get("schema") != SCHEMA:
                raise ValueError(f"schema {doc.get('schema')!r}")
            return int(doc["v"]), doc
        except Exception as exc:  # noqa: BLE001  损坏的数据当作不存在，别让一条坏数据让这个会话永远聊不了
            log.warning("会话 %s 的持久化数据无法解析，忽略：%s: %s", sid, type(exc).__name__, exc)
            return None

    def save(self, sid: str, version: int, ctx: dict[str, Any],
             shopping: dict[str, Any] | None) -> None:
        if self.degraded:
            raise RedisDown()
        doc = {"schema": SCHEMA, "v": version, "ctx": ctx, "shopping": shopping}
        try:
            self._client.set(f"{self._prefix}data:{sid}",
                             json.dumps(doc, ensure_ascii=False), ex=int(self.ttl))
        except Exception as exc:  # noqa: BLE001
            self._fail("save", exc)
            raise RedisDown() from exc


class RedisDown(Exception):
    """Redis 当前不可用（或处于冷却期）。SessionStore 据此 fail-open。"""


# ----------------------------------------------------------------------------- 存储


class SessionStore:
    """会话存储。事件循环线程里调用 get/touch/has/discard/lock_for/agent_state；
    :meth:`run_exclusive` 在工作线程里调用，只碰传进来的那个会话。"""

    def __init__(self, ttl: float = 86400.0, *, max_sessions: int = 50_000,
                 backend: RedisSessionBackend | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._data: OrderedDict[str, tuple[float, SessionContext]] = OrderedDict()
        self._agent_state: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, Any] = {}
        self._versions: dict[str, int] = {}
        self.ttl = ttl
        self.max_sessions = max(1, max_sessions)
        self.backend = backend
        self._clock = clock
        self.evicted_ttl = 0
        self.evicted_cap = 0

    # ------------------------------------------------------------------ 本地锁

    def lock_for(self, session_id: str):
        """这个会话的串行锁。**同一个会话的两轮对话不能同时跑。**

        一轮对话现在跑在工作线程里，会读写同一个 ``SessionContext``（历史、摘要、
        转人工计数）。同一个用户连发两条、或者 REST 与 WebSocket 同时进来，
        两个线程就会交错着改同一份历史——答案引用的是另一轮写到一半的上下文。
        不同会话之间不受影响，照常并行。跨实例的串行见 :meth:`run_exclusive`。

        只在事件循环线程里调用（和 ``get``/``touch`` 一样），所以字典本身不需要锁。
        """
        import asyncio

        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    def _busy(self, sid: str) -> bool:
        lock = self._locks.get(sid)
        return lock is not None and lock.locked()

    # ------------------------------------------------------------------ 会话表

    def has(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self._data

    def discard(self, session_id: str) -> None:
        """丢掉一个会话。被过载保护拒绝的**新**会话要丢掉：客户端没拿到 session_id，
        重试时又会新建一个，每次拒绝都留一个没人认领的会话，直到 TTL。"""
        self._drop(session_id)

    def _drop(self, sid: str) -> None:
        self._data.pop(sid, None)
        self._agent_state.pop(sid, None)
        # 锁跟着会话一起走，否则每个来过的会话都在这里留一把锁
        self._locks.pop(sid, None)
        self._versions.pop(sid, None)

    def get(self, session_id: str | None, **kw) -> SessionContext:
        now = self._clock()
        self._evict(now)
        if session_id and session_id in self._data:
            return self._data[session_id][1]
        ctx = SessionContext(**{k: v for k, v in kw.items() if v is not None})
        if session_id:
            ctx.session_id = session_id
        self._data[ctx.session_id] = (now, ctx)
        self._data.move_to_end(ctx.session_id)
        self._enforce_cap()
        return ctx

    def agent_state(self, ctx: SessionContext, key: str, factory) -> Any:
        """取这个会话在某个 Agent 下的私有状态，没有就新建。

        导购必须跨轮记住用户说过的条件（见
        :meth:`~app.agent.shopping.state.ShoppingState.begin_turn`），
        所以它的 state 得比单轮活得久。挂在会话存储里而不是另开一个
        全局字典，是为了让它**和会话一起过期**——单独存的话，
        会话早没了它还占着，就是一处长在演示环境里的内存泄漏。
        """
        bag = self._agent_state.setdefault(ctx.session_id, {})
        if key not in bag:
            bag[key] = factory()
        return bag[key]

    def touch(self, ctx: SessionContext) -> None:
        self._data[ctx.session_id] = (self._clock(), ctx)
        self._data.move_to_end(ctx.session_id)         # 最近用过的排最后：队头永远是最久没动的
        self._enforce_cap()

    def _evict(self, now: float) -> None:
        """摊销 O(过期数)：队头最旧，遇到第一个没过期的就停。"""
        dead = []
        for sid, (ts, _) in self._data.items():
            if now - ts <= self.ttl:
                break
            if not self._busy(sid):                    # 正在跑一轮的不能淘汰
                dead.append(sid)
        for sid in dead:
            self._drop(sid)
        self.evicted_ttl += len(dead)

    def _enforce_cap(self) -> None:
        over = len(self._data) - self.max_sessions
        if over <= 0:
            return
        victims = []
        for sid in self._data:                          # 从最久没动的开始
            if len(victims) >= over:
                break
            if not self._busy(sid):
                victims.append(sid)
        for sid in victims:
            self._drop(sid)
        self.evicted_cap += len(victims)

    def stats(self) -> dict[str, Any]:
        b = self.backend
        return {
            "sessions": len(self._data),
            "max_sessions": self.max_sessions,
            "evicted_ttl": self.evicted_ttl,
            "evicted_cap": self.evicted_cap,
            "backend": "redis" if b is not None else "memory",
            "backend_degraded": bool(b and b.degraded),
            "backend_errors": b.errors if b else 0,
        }

    # ------------------------------------------------------------------ 持久层

    def _adopt_remote(self, ctx: SessionContext, shopping: Any | None) -> None:
        """Redis 里的版本比本地新就覆盖本地。必须在持有分布式锁时调用。"""
        assert self.backend is not None
        loaded = self.backend.load(ctx.session_id)
        if loaded is None:
            return
        version, doc = loaded
        if version <= self._versions.get(ctx.session_id, 0):
            return                                      # 本地不比 Redis 旧
        apply_ctx(ctx, doc["ctx"])
        if shopping is not None and doc.get("shopping"):
            apply_shopping(shopping, doc["shopping"])
        self._versions[ctx.session_id] = version

    def refresh(self, ctx: SessionContext, shopping: Any | None = None) -> None:
        """只读场景（查看历史）：不加锁，Redis 里有更新的就拿过来。Redis 不可用时静默。

        不加锁是安全的：能让 Redis 版本变新的只有持有锁的写者，而此刻本进程没有在跑这个会话；
        别的实例正在写时我们读到的要么是写之前、要么是写之后的完整 JSON（SET 是原子的）。
        """
        if self.backend is None or self._busy(ctx.session_id):
            return
        try:
            self._adopt_remote(ctx, shopping)
        except RedisDown:
            pass

    def run_exclusive(self, ctx: SessionContext, fn: Callable[[], Any], *,
                      shopping: Any | None = None,
                      overrides: dict[str, Any] | None = None) -> Any:
        """在工作线程里跑一轮，并保证跨实例串行、轮前读新、轮后写回。

        ``overrides`` 是**这个请求**带来的会话字段（比如用户切到了另一个商品），
        在读完 Redis 之后重新套上——否则刚从 Redis 读来的旧值会把用户这一刻的选择盖掉。
        没配 Redis 时直接 ``fn()``，零开销。
        """
        be = self.backend
        if be is None:
            return fn()
        try:
            lease = be.lock(ctx.session_id)
        except RedisDown:
            return fn()                                 # fail-open：照常聊，只是不共享
        try:
            try:
                self._adopt_remote(ctx, shopping)
            except RedisDown:
                pass
            for k, v in (overrides or {}).items():
                setattr(ctx, k, v)
            try:
                return fn()
            finally:
                self._persist(ctx, shopping)
        finally:
            if not lease.release():
                log.warning("会话 %s 的分布式锁在这一轮跑完前已过期（一轮超过了 lock_ttl=%.0fs），"
                            "期间别的实例可能插进来过，写回是后写者胜",
                            ctx.session_id, be.lock_ttl)

    def _persist(self, ctx: SessionContext, shopping: Any | None) -> None:
        assert self.backend is not None
        version = self._versions.get(ctx.session_id, 0) + 1
        try:
            self.backend.save(ctx.session_id, version, ctx_to_dict(ctx),
                              shopping_to_dict(shopping) if shopping is not None else None)
            self._versions[ctx.session_id] = version
        except RedisDown:
            pass
        except Exception:  # noqa: BLE001  序列化失败也不能让一轮已经答完的对话报错
            log.exception("会话 %s 写回 Redis 失败", ctx.session_id)


def build_session_store(settings: Any) -> SessionStore:
    """按配置装配。装不出 Redis 客户端时退回纯内存——会话持久化不能让对话起不来。"""
    backend = None
    if settings.session_backend == "redis":
        try:
            from .redis_clients import make_sync_redis_client

            backend = RedisSessionBackend(
                make_sync_redis_client(settings.redis_url, timeout=settings.session_redis_timeout),
                ttl=settings.session_ttl, lock_ttl=settings.session_lock_ttl,
                lock_wait=settings.session_lock_wait)
        except Exception as exc:  # noqa: BLE001
            # ERROR 而不是 WARNING：配置明明写了 redis 却在跑纯内存，下次重启就丢上下文。
            # 瞬时的 Redis 宕机不走这里（客户端是惰性连接的）——能走到这里的是装不出客户端：
            # 没装 redis 库、URL 写错。/info 的 sessions.backend 会如实报告 memory
            log.error("SESSION_BACKEND=redis 但会话 Redis 后端无法初始化，退回纯内存"
                      "（重启会丢上下文，多实例不共享）：%s: %s", type(exc).__name__, exc)
    return SessionStore(ttl=settings.session_ttl, max_sessions=settings.session_max,
                        backend=backend)
