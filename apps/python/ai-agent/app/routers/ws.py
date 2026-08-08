"""WebSocket 对话接口与调试台前端。

编排是同步的（节点里是阻塞的 HTTP 调用），所以放进线程池跑，
事件用 ``run_in_threadpool`` + 队列桥到事件循环。不把整条链路改成
async，是因为那要连带改掉 httpx、SQLAlchemy 两层的调用方式，
收益（单机演示的并发）远不抵改动面。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ..agent.state import AgentState
from ..agent.streaming import stream_turn
from .chat import _sessions, get_deps

router = APIRouter(tags=["客服"])

_WEB = Path(__file__).resolve().parents[2] / "web"


@router.get("/", response_class=HTMLResponse, summary="对话调试台")
async def index() -> str:
    page = _WEB / "index.html"
    if not page.is_file():  # pragma: no cover
        return "<h1>缺少 web/index.html</h1>"
    return page.read_text(encoding="utf-8")


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()

    try:
        while True:
            payload = await ws.receive_json()

            if payload.get("type") == "feedback":
                await _handle_feedback(ws, payload)
                continue

            message = (payload.get("message") or "").strip()
            if not message:
                continue

            ctx = _sessions.get(
                payload.get("session_id"),
                user_id=payload.get("user_id"),
                current_product_id=payload.get("product_id"),
                category_id=payload.get("category_id"),
            )
            # 会话中途切换商品要跟上，否则会拿 A 商品的知识答 B 商品
            if payload.get("product_id") is not None:
                ctx.current_product_id = payload["product_id"]

            state = AgentState(session=ctx)
            await _pump(ws, loop, message, state)
            _sessions.touch(ctx)

    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        # 连兜底都失败也不能把 traceback 推出去
        try:
            await ws.send_json({
                "type": "error",
                "answer": "系统开小差了，请刷新重试～",
                "detail": f"{type(exc).__name__}",
            })
        except Exception:  # noqa: BLE001
            pass


async def _pump(
    ws: WebSocket, loop: asyncio.AbstractEventLoop, message: str, state: AgentState
) -> None:
    """把同步生成器的事件搬到事件循环里发出去。

    生成器在线程里跑；每产出一个事件就 ``call_soon_threadsafe`` 塞进
    asyncio 队列，主协程取出来发。这样首个 token 一出来就能到前端，
    而不是等整轮跑完。
    """
    q: asyncio.Queue = asyncio.Queue()
    done = object()

    def _produce() -> None:
        try:
            for event in stream_turn(message, state, get_deps()):
                loop.call_soon_threadsafe(q.put_nowait, event)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, done)

    task = asyncio.create_task(asyncio.to_thread(_produce))
    while True:
        event = await q.get()
        if event is done:
            break
        await ws.send_json(event)
    await task


async def _handle_feedback(ws: WebSocket, payload: dict[str, Any]) -> None:
    """点赞点踩。

    走 WebSocket 而不是另开一个 REST 口，是为了让前端不必再管一套
    请求逻辑——反馈按钮就在消息气泡上，能省一次往返就省一次。
    """
    from ..agent.store import MySqlTraceStore

    trace_id = payload.get("trace_id") or ""
    thumb = 1 if payload.get("thumb") == "up" else -1
    ok = False
    try:
        ok = MySqlTraceStore.from_env().record_feedback(
            trace_id, thumb, payload.get("reason", "")
        )
    except Exception:  # noqa: BLE001  反馈失败不该影响对话
        ok = False
    await ws.send_json({"type": "feedback_ack", "trace_id": trace_id, "ok": ok})
