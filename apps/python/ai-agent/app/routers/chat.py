"""客服对话接口。

会话状态目前放进程内存。Redis 版本在下一个增量——先把对话链路跑通，
单实例演示够用，换存储时只需替换 :class:`SessionStore` 的实现，
上层一行不用改。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ai_common.api import ApiResponse, ErrorCode
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..agent import AgentState, SessionContext, safe_run_turn
from ..agent.nodes import Deps
from ..concurrency import chat_workers
from ..overload import ChatGuard, Overloaded, build_guard
from ..ratelimit import client_ip

router = APIRouter(prefix="/chat", tags=["客服"])
log = logging.getLogger(__name__)


class SessionStore:
    """会话存储。

    进程内字典 + TTL。够单实例演示用；多实例部署必须换 Redis，
    否则同一个用户的两条消息落到不同实例上，上下文就断了。
    """

    def __init__(self, ttl: float = 86400.0) -> None:
        self._data: dict[str, tuple[float, SessionContext]] = {}
        self._agent_state: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.ttl = ttl

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """这个会话的串行锁。**同一个会话的两轮对话不能同时跑。**

        一轮对话现在跑在工作线程里，会读写同一个 ``SessionContext``（历史、摘要、
        转人工计数）。同一个用户连发两条、或者 REST 与 WebSocket 同时进来，
        两个线程就会交错着改同一份历史——答案引用的是另一轮写到一半的上下文。
        不同会话之间不受影响，照常并行。

        只在事件循环线程里调用（和 ``get``/``touch`` 一样），所以字典本身不需要锁。
        """
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    def has(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self._data

    def discard(self, session_id: str) -> None:
        """丢掉一个会话。被过载保护拒绝的**新**会话要丢掉：客户端没拿到 session_id，
        重试时又会新建一个，每次拒绝都留一个没人认领的会话，直到 24 小时 TTL。"""
        self._data.pop(session_id, None)
        self._agent_state.pop(session_id, None)
        self._locks.pop(session_id, None)

    def get(self, session_id: str | None, **kw) -> SessionContext:
        now = time.time()
        self._evict(now)
        if session_id and session_id in self._data:
            return self._data[session_id][1]
        ctx = SessionContext(**{k: v for k, v in kw.items() if v is not None})
        if session_id:
            ctx.session_id = session_id
        self._data[ctx.session_id] = (now, ctx)
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
        self._data[ctx.session_id] = (time.time(), ctx)

    def _evict(self, now: float) -> None:
        dead = [k for k, (ts, _) in self._data.items() if now - ts > self.ttl]
        for k in dead:
            self._data.pop(k, None)
            self._agent_state.pop(k, None)
            # 锁跟着会话一起过期，否则每个来过的会话都在这里留一把锁
            self._locks.pop(k, None)


_sessions = SessionStore()
_deps: Deps | None = None
_deps_lock = threading.Lock()
_guard: ChatGuard | None = None


def get_guard() -> ChatGuard:
    """对话入口的过载保护（准入控制 + 限流），惰性创建，原因同 :func:`get_deps`。

    只在事件循环线程里调用：它内部的 ``asyncio.Semaphore`` 属于创建时所在的循环。
    """
    global _guard
    if _guard is None:
        from ..config import settings

        _guard = build_guard(settings, workers=chat_workers(), log=log.warning)
    return _guard


def reset_guard() -> None:
    """测试之间、或改了配置要重新装配时调用。"""
    global _guard
    _guard = None


def peer_of(request_or_ws: Any) -> str | None:
    c = request_or_ws.client
    return c.host if c else None


def rate_keys(request_or_ws: Any, *, user_id: Any, session_id: str | None) -> tuple[str, str | None]:
    """限流的两个 key：(IP, 用户或会话)。有 user_id 按用户，没有按会话，都没有就只按 IP。"""
    from ..config import settings

    ip = client_ip(peer_of(request_or_ws),
                   request_or_ws.headers.get("x-forwarded-for"),
                   settings.chat_trust_proxy_hops)
    identity = f"u{user_id}" if user_id is not None else (f"s{session_id}" if session_id else None)
    return ip, identity


def get_deps() -> Deps:
    """惰性装配。

    进程启动时不建连接：模型网关或数据库暂时不可用不应该让整个服务
    起不来——健康探针会如实报告依赖状态，而 /health 仍然可用。

    装配走 :func:`~app.agent.assembly.build_deps`，与 CLI **共用同一份**。
    这里曾经自己 new 了一个 AgentConfig()，于是服务端拿着网关别名
    chat-default 去调 DeepSeek，直接 400，最后表现成"答不上来"——
    装配逻辑重复一次就会漂移一次。

    **要加锁，因为它现在会被多个线程同时首次调用。**装配很重——
    ``LocalRetriever`` 要把整个知识库向量从 MySQL 读进内存、建 BM25 索引，
    秒级、吃内存。冷启动时一批并发请求同时发现 ``_deps is None``，不加锁就是
    每个线程各装配一份：N 份知识库向量同时驻留内存。
    """
    global _deps
    if _deps is None:
        with _deps_lock:
            if _deps is None:
                from ..agent.assembly import build_deps
                from ..config import settings

                # 检索后端由配置决定，见 Settings.retriever_backend。
                # 原先这里不传 rag_url，服务端于是永远走本地暴力检索
                rag_url = (settings.rag_base_url
                           if settings.retriever_backend == "http" else None)
                # with_media=True：商家后台的素材生成走这一份 Deps。
                # 装不上（没 key、没建表）也不影响对话——那两个组件只是留空
                _deps = build_deps(with_media=True, rag_url=rag_url,
                                   log=lambda m: None)
    return _deps


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    session_id: str | None = Field(None, description="不传则新建会话")
    user_id: int | None = None
    product_id: int | None = Field(None, description="当前商品，决定检索过滤范围")
    category_id: int | None = None


class CitationOut(BaseModel):
    item_id: int
    title: str
    score: float


class ChatResponse(BaseModel):
    session_id: str
    trace_id: str
    answer: str
    citations: list[CitationOut] = []
    handover: bool = False
    handover_reason: str = ""
    handover_summary: dict[str, Any] = {}
    intent: str = ""
    postcheck_flags: list[str] = []


def _run_turn(message: str, ctx: SessionContext):
    """工作线程里跑的一轮。get_deps() 也在这里调——首次装配是阻塞的。"""
    return safe_run_turn(message, AgentState(session=ctx), get_deps())


def overloaded_response(exc: Overloaded) -> JSONResponse:
    """过载拒绝的 REST 响应：HTTP 状态码 + ``Retry-After`` + 统一信封。

    用真实的 HTTP 状态码（503/429）而不是 200 里塞错误码：网关、负载均衡、
    客户端重试库都认状态码，它们才是会据此退避的那一层。
    """
    code = ErrorCode.AGENT_BUSY if exc.status == 503 else ErrorCode.RATE_LIMITED
    return JSONResponse(
        status_code=exc.status,
        content=ApiResponse.fail(code, exc.message).model_dump(mode="json", by_alias=True),
        headers={"Retry-After": str(exc.retry_after)},
    )


@router.post("", response_model=ChatResponse, summary="发送一条消息",
             responses={429: {"description": "限流 / 同一会话消息太多"},
                        503: {"description": "客服繁忙，见 Retry-After"}})
async def chat(req: ChatRequest, request: Request):
    guard = get_guard()
    try:
        # 限流必须在创建会话**之前**：被限流的请求不该往会话表里塞对象
        ip, identity = rate_keys(request, user_id=req.user_id, session_id=req.session_id)
        await guard.check_rate(ip=ip, identity=identity)
    except Overloaded as exc:
        return overloaded_response(exc)

    created = not _sessions.has(req.session_id)
    ctx = _sessions.get(
        req.session_id,
        user_id=req.user_id,
        current_product_id=req.product_id,
        category_id=req.category_id,
    )
    # 会话中途切换商品要能跟上，否则会拿 A 商品的知识回答 B 商品
    if req.product_id is not None:
        ctx.current_product_id = req.product_id

    # **整轮对话（含首次装配 get_deps）放进线程池。**原来这里是 async def 里直接同步
    # 跑：一轮要几次 LLM 往返，几秒到十几秒，期间整个进程的其它请求全部排队。
    # 准入控制与按会话串行见 ChatGuard.turn / SessionStore.lock_for。
    try:
        async with guard.turn(ctx.session_id, _sessions.lock_for(ctx.session_id)) as slot:
            state = await slot.submit(_run_turn, req.message, ctx)
    except Overloaded as exc:
        if created:
            _sessions.discard(ctx.session_id)
        return overloaded_response(exc)
    _sessions.touch(ctx)

    return ChatResponse(
        session_id=ctx.session_id,
        trace_id=state.trace.trace_id,
        answer=state.answer,
        citations=[
            CitationOut(item_id=c.item_id, title=c.title, score=c.dense_score)
            for c in state.citations
        ],
        handover=state.handover,
        handover_reason=(
            state.handover_reason.value if state.handover_reason else ""
        ),
        handover_summary=state.handover_summary,
        intent=state.intent.value,
        postcheck_flags=state.postcheck_flags,
    )


@router.get("/{session_id}/history", summary="查看会话历史")
async def history(session_id: str) -> dict[str, Any]:
    ctx = _sessions.get(session_id)
    return {
        "session_id": ctx.session_id,
        "turns": [
            {"role": t.role, "content": t.content, "ts": t.ts} for t in ctx.turns
        ],
        "summary": ctx.summary,
        "handover_count": ctx.handover_count,
    }
