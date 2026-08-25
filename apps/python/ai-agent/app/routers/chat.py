"""客服对话接口。

会话状态目前放进程内存。Redis 版本在下一个增量——先把对话链路跑通，
单实例演示够用，换存储时只需替换 :class:`SessionStore` 的实现，
上层一行不用改。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..agent import AgentState, SessionContext, safe_run_turn
from ..agent.nodes import Deps

router = APIRouter(prefix="/chat", tags=["客服"])


class SessionStore:
    """会话存储。

    进程内字典 + TTL。够单实例演示用；多实例部署必须换 Redis，
    否则同一个用户的两条消息落到不同实例上，上下文就断了。
    """

    def __init__(self, ttl: float = 86400.0) -> None:
        self._data: dict[str, tuple[float, SessionContext]] = {}
        self._agent_state: dict[str, dict[str, Any]] = {}
        self.ttl = ttl

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


_sessions = SessionStore()
_deps: Deps | None = None


def get_deps() -> Deps:
    """惰性装配。

    进程启动时不建连接：模型网关或数据库暂时不可用不应该让整个服务
    起不来——健康探针会如实报告依赖状态，而 /health 仍然可用。

    装配走 :func:`~app.agent.assembly.build_deps`，与 CLI **共用同一份**。
    这里曾经自己 new 了一个 AgentConfig()，于是服务端拿着网关别名
    chat-default 去调 DeepSeek，直接 400，最后表现成"答不上来"——
    装配逻辑重复一次就会漂移一次。
    """
    global _deps
    if _deps is None:
        from ..agent.assembly import build_deps

        # with_media=True：商家后台的素材生成走这一份 Deps。
        # 装不上（没 key、没建表）也不影响对话——那两个组件只是留空
        _deps = build_deps(with_media=True, log=lambda m: None)
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


@router.post("", response_model=ChatResponse, summary="发送一条消息")
async def chat(req: ChatRequest) -> ChatResponse:
    ctx = _sessions.get(
        req.session_id,
        user_id=req.user_id,
        current_product_id=req.product_id,
        category_id=req.category_id,
    )
    # 会话中途切换商品要能跟上，否则会拿 A 商品的知识回答 B 商品
    if req.product_id is not None:
        ctx.current_product_id = req.product_id

    state = safe_run_turn(req.message, AgentState(session=ctx), get_deps())
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
