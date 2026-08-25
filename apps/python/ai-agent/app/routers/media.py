"""商家后台的素材生成接口。

**这个路由与 ws.py 里那批 ``/api/admin/*`` 不是一回事，所以单独一个文件。**
那批是纯转发：请求原样送到 mall-product，权限由那边的 ``@RequireMerchant``
判定，Python 这一层连令牌都不解析。这里不同——生成在本进程里跑，
没有任何下游会替它检查身份。

于是就撞上项目里那条老规矩：**鉴权要放在被访问的那一端。** 这里被访问的
就是本服务，那这道闸门只能长在这里。不做的话，任何人 curl 一下就能把
免费额度烧光，而且每烧一次还往库里塞一条待审素材。

判定本身仍然交给 mall-product（``GET /api/product/auth/me``），不在这边
自己验签：角色规则只该有一份实现，两份迟早分叉，而先松掉的那份不会有人
发现。代价是每次生成多一次内部往返——相对于一次几秒到几分钟的模型调用，
这点开销可以忽略。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..agent.assembly import asset_dir
from ..config import settings
from .chat import get_deps

router = APIRouter(tags=["商家素材"])


def _ok(data: Any) -> dict[str, Any]:
    return {"code": 0, "message": "OK", "data": data}


def _fail(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}


# ---------------------------------------------------------------- 鉴权


async def _principal(request: Request) -> dict[str, Any] | None:
    """问 mall-product「拿这个令牌的是谁」。

    **拿不到答案时返回 None，也就是当成没登录。** 下游不可用的时候放行，
    等于给了任何人一条"把订单服务打挂就能生成"的路子——而这道闸门守的
    正是花钱的那个动作。
    """
    import httpx

    token = request.headers.get("Authorization")
    if not token:
        return None
    url = f"{settings.order_base_url.rstrip('/')}/api/product/auth/me"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"Authorization": token})
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


async def _denied(request: Request) -> dict[str, Any] | None:
    """不是商家就返回一个拒绝响应，是商家返回 None。

    买家令牌 1403，没令牌 1401——与 mall-product 同一套码，前端不必
    为"Python 侧拒绝"另写一套判断。

    **返回而不是抛 HTTPException**：那个异常会被渲染成 ``{"detail": ...}``，
    和全站统一的 ``{code, message, data}`` 信封对不上，页面拿到的
    ``d.message`` 会是 undefined，于是只能显示一句「操作失败」。
    """
    who = await _principal(request)
    if who is None:
        return _fail(1401, "请先登录")
    if who.get("role") != "merchant":
        return _fail(1403, "只有商家能生成素材")
    return None


# ---------------------------------------------------------------- 生成


@router.post("/api/admin/media", summary="生成商品图 / 宣传视频（商家）")
async def generate(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """跑一次生成。

    图是同步的，秒级返回；视频只创建任务，返回 ``queued``，页面之后调
    ``/poll`` 取结果——挂在这个请求上等五分钟必然超时。

    ``review_status`` 与 ``ai_generated`` **不在入参里**，这是刻意的：
    能传参数就意味着某天会有人传 approved（见 011 migration 的文件头）。
    """
    denied = await _denied(request)
    if denied:
        return denied

    from ..agent.marketing.media_flow import MediaBrief, safe_run_media

    try:
        product_id = int(payload.get("productId") or 0)
    except (TypeError, ValueError):
        product_id = 0
    if product_id <= 0:
        return _fail(1400, "要指定商品")

    kind = "video" if payload.get("kind") == "video" else "image"
    usage = str(payload.get("usage") or "white")
    if usage not in ("white", "scene", "detail"):
        usage = "white"

    brief = MediaBrief(product_id=product_id, kind=kind, usage=usage)
    # 编排是同步的（里面是阻塞的 HTTP 调用），丢线程里跑，
    # 不然一次生成会把整个事件循环卡住——包括别人的对话
    state = await asyncio.to_thread(safe_run_media, brief, get_deps())

    data = {
        "assetId": state.asset_id,
        "kind": kind,
        "usage": usage,
        "outcome": state.outcome,
        "prompt": state.prompt,
        "flags": state.flags,
        "localPath": state.local_path,
        "taskId": state.task.task_id if state.task else None,
        # 页面上要显示"AI 生成"角标。**这个值不从前端传也不由前端决定**
        "aiGenerated": True,
    }
    if state.outcome in ("generated", "queued"):
        return _ok(data)
    # 没生成出来不是 HTTP 错误：合规没过、属性表是空的都是正常结果，
    # 而页面要把 flags 原样显示出来，用户才知道该改什么
    return {"code": 1409, "message": "、".join(state.flags) or state.outcome,
            "data": data}


@router.post("/api/admin/media/poll", summary="查视频任务（商家）")
async def poll(request: Request, limit: int = 20) -> dict[str, Any]:
    denied = await _denied(request)
    if denied:
        return denied

    from ..agent.marketing.media_flow import poll_pending

    report = await asyncio.to_thread(poll_pending, get_deps(), limit=limit)
    return _ok(report.as_dict())


@router.get("/api/admin/media", summary="素材列表（商家）")
async def list_assets(request: Request, product_id: int | None = None,
                      limit: int = 50) -> dict[str, Any]:
    denied = await _denied(request)
    if denied:
        return denied

    store = get_deps().asset_store
    if store is None:
        return _fail(9503, "素材库没接上（建表：deploy/sql/migrations/"
                           "011_marketing_asset.sql）")
    rows = await asyncio.to_thread(store.list_assets, product_id,
                                   max(1, min(limit, 200)))
    return _ok([{
        "id": r.get("id"),
        "productId": r.get("product_id"),
        "kind": r.get("kind"),
        "usage": r.get("usage_tag"),
        "localPath": r.get("local_path") or "",
        "taskStatus": r.get("task_status"),
        "error": r.get("error") or "",
        "model": r.get("model") or "",
        "reviewStatus": r.get("review_status"),
        "aiGenerated": bool(r.get("ai_generated", 1)),
        "createdAt": str(r.get("created_at") or ""),
    } for r in rows])


# ---------------------------------------------------------------- 取文件

#: 生成文件名白名单。形状由 ``media_flow._filename`` 决定
#: （``9002-white-1755000000-a1b2c3.png``），这里只认那一种。
#:
#: **白名单而不是黑名单**，与 /img 同一个理由：黑掉 ".." 还剩下 URL 编码、
#: 反斜杠、符号链接一堆绕法，而这个口子拼的是真实路径——
#: ``../../deploy/.env`` 一旦被接受就是任意文件读取，而 .env 里躺着 API key。
_SAFE_ASSET = re.compile(r"[A-Za-z0-9_-]{1,80}\.(?:png|jpg|jpeg|webp|mp4)")

_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".mp4": "video/mp4"}


@router.get("/generated/{name}", summary="AI 生成的素材文件")
async def generated_file(name: str) -> FileResponse:
    """把落地的素材发出去。

    **这个口子不鉴权**，与 /img 一致：素材最终是要挂在商品详情页上给
    所有买家看的，加了令牌反而是买家页打不开图。真正不该外泄的是
    "有哪些待审素材"这份清单，那在 /api/admin/media 上，那里是鉴权的。
    """
    if not _SAFE_ASSET.fullmatch(name):
        raise HTTPException(status_code=404)
    path = Path(asset_dir()) / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path, media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=86400"})
