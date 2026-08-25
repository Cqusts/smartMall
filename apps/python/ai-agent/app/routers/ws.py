"""WebSocket 对话接口与调试台前端。

编排是同步的（节点里是阻塞的 HTTP 调用），所以放进线程池跑，
事件用 ``run_in_threadpool`` + 队列桥到事件循环。不把整条链路改成
async，是因为那要连带改掉 httpx、SQLAlchemy 两层的调用方式，
收益（单机演示的并发）远不抵改动面。
"""

from __future__ import annotations

import asyncio
import os
import base64
import binascii
import re
from pathlib import Path
from typing import Any

from fastapi import (APIRouter, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse

from ..agent.state import AgentState
from ..agent.streaming import stream_turn
from ..config import settings
from .chat import _sessions, get_deps

router = APIRouter(tags=["客服"])

_WEB = Path(__file__).resolve().parents[2] / "web"

#: 图片文件名白名单。**白名单而不是黑名单**——黑掉 ".." 还剩下 URL 编码、
#: 反斜杠、符号链接一堆绕法，而这里合法的名字本来就只有 "9001.jpg" 这一种形状。
_SAFE_IMG = re.compile(r"[A-Za-z0-9_-]{1,40}\.(?:jpg|jpeg|png|webp)")


@router.get("/", response_class=HTMLResponse, summary="店铺首页（含客服入口）")
async def index() -> str:
    page = _WEB / "index.html"
    if not page.is_file():  # pragma: no cover
        return "<h1>缺少 web/index.html</h1>"
    return page.read_text(encoding="utf-8")


@router.get("/merchant", response_class=HTMLResponse, summary="商家后台")
async def merchant_page() -> str:
    """商家后台。

    与店铺页分成两个页面而不是一页里加开关：两边的受众、权限、数据都不同，
    混在一起的结果是买家页里躺着一堆只有商家能用的代码，而"哪些元素该按
    角色隐藏"迟早会漏一个——漏的方向通常是该藏的没藏。

    **这个路由不做鉴权**，页面本身也不做。能不能操作由 mall-product 的
    ``@RequireMerchant`` 决定：前端藏按钮是体验不是安全，藏了照样能 curl。
    """
    page = _WEB / "merchant.html"
    if not page.is_file():  # pragma: no cover
        return "<h1>缺少 web/merchant.html</h1>"
    return page.read_text(encoding="utf-8")


@router.get("/img/{name}", summary="商品图")
async def product_image(name: str) -> FileResponse:
    """商品图。

    图存在仓库里而不是引外链：整页零外部请求是硬要求，演示环境常常
    没有外网，"打不开"比"不好看"严重得多（见 web/img/README.md）。

    文件名来自数据库的 ``main_image``，但**仍然要当成不可信输入**校验——
    这个口子拼的是真实路径，``../../deploy/.env`` 一旦被接受就是任意
    文件读取，而 .env 里躺着 API key。
    """
    if not _SAFE_IMG.fullmatch(name):
        raise HTTPException(status_code=404)
    path = _WEB / "img" / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/products", summary="商品列表")
async def products() -> dict[str, Any]:
    """店铺首页的商品数据。

    直接读工具层——前端看到的价格库存与客服回答用的**是同一个数据源**。
    分成两条路的话，页面显示"有货"而客服说"缺货"，用户会以为系统在骗人。
    """
    from ..agent.tools import MySqlToolBox

    try:
        box = MySqlToolBox.from_env()
        items = []
        # 列表从表里查，不写死 ID——写死的话，上新商品要改代码，
        # 而且迟早会出现"数据库里有、页面上没有"
        for pid in box.list_on_sale_product_ids():
            detail = box.get_product_detail(pid)
            if not detail:
                continue
            skus = box.get_sku_stock_price(pid)
            prices = [s["price"] for s in skus] or [0]
            detail["price"] = min(prices)
            detail["origin_price"] = next(
                (s["origin_price"] for s in skus if s.get("origin_price")), None
            )
            detail["skus"] = skus
            detail["in_stock"] = any(s["in_stock"] for s in skus)
            detail["size_chart"] = box.get_size_chart(pid)
            items.append(detail)
        return {"ok": True, "items": items}
    except BaseException as exc:  # noqa: BLE001
        # 商品挂了不该让整个页面白屏——前端会退化成只有客服入口。
        #
        # 这里必须是 BaseException 而不是 Exception：pymysql 的依赖链上有
        # cryptography，它的 Rust 扩展装坏时抛的是 pyo3_runtime.PanicException，
        # 那是 BaseException 的直接子类，`except Exception` 兜不住，
        # 整个降级保证就在最需要它的时候失效了（实测踩过）
        #
        # **但降级不等于把原因藏起来。**只回一个异常类名（"ToolError"）时，
        # 页面显示"商品数据读取失败"，日志里一片干净 —— 而真实原因往往是
        # 「应用账号连不上库」这种一句话就能说清的事。实测踩过：用户的库里
        # 没有 smartmall 账号（那是 Docker 镜像自动建的），迁移走 root 成功、
        # 应用走 smartmall 失败，从现象完全看不出来。所以原因要落日志，
        # 也要放进响应体给前端显示。
        import logging

        detail = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "读取商品失败，页面将退化为仅客服入口。连库参数："
            "host=%s user=%s db=%s —— %s",
            os.environ.get("MYSQL_HOST", "localhost"),
            os.environ.get("MYSQL_USER", "smartmall"),
            os.environ.get("MYSQL_DATABASE", "smartmall"),
            detail,
        )
        return {"ok": False, "items": [], "error": type(exc).__name__,
                "detail": detail[:400]}


#: 订单号白名单。转发时它会被拼进下游 URL，不校验就等于把路径拼接权
#: 交给调用方（`../../actuator/env` 之类）。合法订单号只有「20 位数字」
#: 这一种形状——与 OrderService.nextOrderNo 的生成规则对齐
_SAFE_ORDER_NO = re.compile(r"\d{14,24}")


async def _forward(method: str, path: str, *,
                   json_body: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None,
                   authorization: str | None = None) -> dict[str, Any]:
    """把请求原样转给 mall-product。

    **这里只是转发，不是实现。**下单是写操作，而本服务的工具层是刻意全只读的
    （见 ``agent/tools.py``：AI 误触发的退款、改价是不可逆的资金损失）。
    在 Python 侧再写一份扣库存逻辑，等于把那道只读边界开个口子，还会出现
    两份实现漂移——库存到底以谁为准就说不清了。所以扣库存、幂等、事务、
    超时释放全在 mall-product 一处，这里连参数都不解释。

    转发而不是让浏览器直连 8081，纯粹是因为演示页由本服务托管，跨域调
    另一个端口要么开 CORS 要么改 host，都比在这里转一次麻烦。
    """
    import httpx

    url = f"{settings.order_base_url.rstrip('/')}{path}"
    # 令牌**原样透传**，这里不解析也不重签。这一层只是转发，它没有密钥、
    # 也不该有——身份的判定权在 mall-product 那一端（见 JwtService 的类注释：
    # 鉴权要放在被访问的那一端，不是放在你希望别人走的那条路上）
    headers = {"Authorization": authorization} if authorization else None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(method, url, json=json_body,
                                        params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001
        # 订单服务没起来是演示时最常见的情况，错误要说清楚是哪个服务、
        # 怎么起——只回一句"失败"，用户会以为是代码坏了
        return {
            "code": 9503,
            "message": f"订单服务不可用（{settings.order_base_url}）："
                       f"{type(exc).__name__}",
            "data": None,
        }

    try:
        return resp.json()
    except ValueError:
        return {
            "code": 9503,
            "message": f"订单服务返回了非 JSON 响应（HTTP {resp.status_code}）",
            "data": None,
        }


#: 用户侧转发。**身份不再作为参数出现在这一层**——它曾经是 ?user_id=，
#: 也就是浏览器里改一个数字就能操作别人的订单。现在只透传令牌，
#: 判定权在 mall-product。
def _auth(request: Request) -> str | None:
    return request.headers.get("Authorization")


@router.post("/api/auth/login", summary="登录（转发到 mall-product）")
async def login(payload: dict[str, Any]) -> dict[str, Any]:
    """换取令牌。

    **这一层不碰密码也不签令牌**：它没有密钥，签发权在 mall-product。
    转发存在的唯一理由和订单一样——演示页由本服务托管，跨域调 8081 更麻烦。
    """
    return await _forward("POST", "/api/product/auth/login", json_body=payload)


@router.post("/api/orders", summary="下单（转发到 mall-product）")
async def create_order(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    return await _forward("POST", "/api/product/orders", json_body=payload,
                          authorization=_auth(request))


@router.post("/api/orders/{order_no}/pay", summary="支付（转发到 mall-product）")
async def pay_order(order_no: str, request: Request) -> dict[str, Any]:
    if not _SAFE_ORDER_NO.fullmatch(order_no):
        raise HTTPException(status_code=400, detail="订单号格式不合法")
    return await _forward("POST", f"/api/product/orders/{order_no}/pay",
                          authorization=_auth(request))


@router.post("/api/orders/{order_no}/cancel", summary="取消（转发到 mall-product）")
async def cancel_order(order_no: str, request: Request) -> dict[str, Any]:
    if not _SAFE_ORDER_NO.fullmatch(order_no):
        raise HTTPException(status_code=400, detail="订单号格式不合法")
    return await _forward("POST", f"/api/product/orders/{order_no}/cancel",
                          authorization=_auth(request))


@router.post("/api/orders/{order_no}/confirm", summary="确认收货（转发）")
async def confirm_order(order_no: str, request: Request) -> dict[str, Any]:
    if not _SAFE_ORDER_NO.fullmatch(order_no):
        raise HTTPException(status_code=400, detail="订单号格式不合法")
    return await _forward("POST", f"/api/product/orders/{order_no}/confirm",
                          authorization=_auth(request))


@router.post("/api/orders/{order_no}/refund", summary="申请退款（转发）")
async def refund_order(order_no: str, request: Request,
                       payload: dict[str, Any]) -> dict[str, Any]:
    if not _SAFE_ORDER_NO.fullmatch(order_no):
        raise HTTPException(status_code=400, detail="订单号格式不合法")
    return await _forward("POST", f"/api/product/orders/{order_no}/refund",
                          json_body=payload, authorization=_auth(request))


# ---------------------------------------------------------------- 商家后台
#
# 全部原样透传，**这一层不做任何权限判断**——判定在 mall-product 的
# @RequireMerchant 上。在这里再写一份角色检查的结果是两份规则渐行渐远，
# 而先漏的那份不会有人发现。

@router.get("/api/admin/products", summary="商品列表（商家）")
async def admin_products(request: Request, limit: int = 50) -> dict[str, Any]:
    return await _forward("GET", "/api/product/admin/products",
                          params={"limit": limit}, authorization=_auth(request))


@router.post("/api/admin/products", summary="新建商品（商家）")
async def admin_create_product(payload: dict[str, Any],
                               request: Request) -> dict[str, Any]:
    return await _forward("POST", "/api/product/admin/products",
                          json_body=payload, authorization=_auth(request))


@router.put("/api/admin/products/{pid}", summary="改商品（商家）")
async def admin_update_product(pid: int, payload: dict[str, Any],
                               request: Request) -> dict[str, Any]:
    return await _forward("PUT", f"/api/product/admin/products/{pid}",
                          json_body=payload, authorization=_auth(request))


@router.put("/api/admin/products/{pid}/skus", summary="改价改库存（商家）")
async def admin_upsert_sku(pid: int, payload: dict[str, Any],
                           request: Request) -> dict[str, Any]:
    return await _forward("PUT", f"/api/product/admin/products/{pid}/skus",
                          json_body=payload, authorization=_auth(request))


#: 上下架。白名单映射而不是把动作直接拼进 URL——否则调用方就能自己
#: 指定下游路径（与订单那边同一个理由）
_SHELF_ACTIONS = {"on-shelf": "on-shelf", "off-shelf": "off-shelf"}


@router.post("/api/admin/products/{pid}/{action}", summary="上下架（商家）")
async def admin_shelf(pid: int, action: str, request: Request) -> dict[str, Any]:
    if action not in _SHELF_ACTIONS:
        raise HTTPException(status_code=404, detail=f"未知动作：{action}")
    return await _forward("POST", f"/api/product/admin/products/{pid}/{action}",
                          authorization=_auth(request))


@router.get("/api/admin/orders", summary="订单列表（商家）")
async def admin_orders(request: Request, status: str | None = None,
                       limit: int = 50) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return await _forward("GET", "/api/product/admin/orders",
                          params=params, authorization=_auth(request))


#: 商家侧动作。演示页要能把整条链路点完，否则「发货 → 客服答得出物流」
#: 这个最有说服力的环节没法演。
#:
#: 下游那几个接口现在要求 merchant 角色（OrderAdminController 上的
#: @RequireMerchant），这里只负责把令牌透传过去。买家令牌会拿到 1403，
#: 不带令牌拿到 1401——两种都由 mall-product 判定，不在这一层。
_ADMIN_ACTIONS = {
    "ship": "ship",
    "deliver": "deliver",
    "refund-approve": "refund/approve",
    "refund-reject": "refund/reject",
}


@router.post("/api/admin/orders/{order_no}/{action}", summary="商家侧动作（演示用转发）")
async def admin_order_action(order_no: str, action: str, request: Request,
                             payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _SAFE_ORDER_NO.fullmatch(order_no):
        raise HTTPException(status_code=400, detail="订单号格式不合法")
    # 白名单映射而不是把 action 直接拼进 URL——否则调用方就能自己指定下游路径
    downstream = _ADMIN_ACTIONS.get(action)
    if downstream is None:
        raise HTTPException(status_code=404, detail=f"未知动作：{action}")
    return await _forward(
        "POST", f"/api/product/admin/orders/{order_no}/{downstream}",
        json_body=payload if payload is not None else {},
        authorization=_auth(request))


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
            image, mime = _decode_image(payload.get("image"))
            # 只发图不打字是常见用法，所以不能要求 message 非空
            if not message and not image:
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

            if payload.get("agent") == "shopping":
                await _pump_shopping(ws, loop, message, ctx)
            else:
                state = AgentState(session=ctx, image=image, image_mime=mime)
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


#: data URL 前缀。前端 FileReader 读出来就是这个形状
_DATA_URL = re.compile(r"^data:(image/[a-z+]{2,12});base64,(.+)$", re.S)

#: base64 后的上限。比 MAX_IMAGE_BYTES 略宽（base64 涨 4/3），
#: 在这里先挡一道是为了**不把超大串解码出来**——解完再判大小，
#: 内存已经吃进去了
_MAX_B64 = 12 * 1024 * 1024


def _decode_image(raw: Any) -> tuple[bytes | None, str]:
    """把前端传来的 data URL 解成字节。

    解不出来就当没有图，让下游按纯文本处理——这里返回错误没有意义，
    用户看到的会是一个技术细节，而他能做的仍然只是重发一次。
    真正的格式与大小校验在 vision.check_image，那里有给用户的话术。
    """
    if not isinstance(raw, str) or len(raw) > _MAX_B64:
        return None, ""
    m = _DATA_URL.match(raw.strip())
    if not m:
        return None, ""
    try:
        return base64.b64decode(m.group(2), validate=True), m.group(1)
    except (ValueError, binascii.Error):
        return None, ""


async def _relay(ws: WebSocket, loop: asyncio.AbstractEventLoop, events) -> None:
    """把同步生成器的事件搬到事件循环里发出去。

    生成器在线程里跑；每产出一个事件就 ``call_soon_threadsafe`` 塞进
    asyncio 队列，主协程取出来发。这样首个 token 一出来就能到前端，
    而不是等整轮跑完。

    ``events`` 是个**无参可调用**而不是生成器，因为生成器要在工作线程里
    才开始跑——传进来一个已经创建好的生成器，第一次 ``next`` 仍然发生在
    这里，等于白搭一个线程。
    """
    q: asyncio.Queue = asyncio.Queue()
    done = object()

    def _produce() -> None:
        try:
            for event in events():
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


async def _pump(
    ws: WebSocket, loop: asyncio.AbstractEventLoop, message: str, state: AgentState
) -> None:
    await _relay(ws, loop, lambda: stream_turn(message, state, get_deps()))


async def _pump_shopping(
    ws: WebSocket, loop: asyncio.AbstractEventLoop, message: str, ctx
) -> None:
    """导购一轮。

    state 从会话存储里取——**不能每轮新建**。新建的话用户说过的条件
    全丢，系统会一遍遍问同样的问题，而这恰恰是导购唯一比客服多出来的
    能力（见 ShoppingState 的类注释）。
    """
    from ..agent.shopping.state import ShoppingState
    from ..agent.streaming import stream_shopping_turn

    state = _sessions.agent_state(
        ctx, "shopping", lambda: ShoppingState(session=ctx)
    )
    await _relay(ws, loop,
                 lambda: stream_shopping_turn(message, state, get_deps()))


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
