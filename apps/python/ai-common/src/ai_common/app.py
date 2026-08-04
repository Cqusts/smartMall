"""FastAPI 应用工厂。

六个 Python AI 服务共用这一个工厂，统一承担横切关注点：
请求 ID、结构化日志、异常兜底、健康与就绪探针。
新增服务只需 ``create_app(settings)`` 再挂自己的业务路由。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import ApiResponse, ErrorCode
from .health import HealthRegistry
from .logging import get_logger, request_id_var, setup_logging
from .settings import ServiceSettings

LifespanHook = Callable[[FastAPI], AsyncIterator[None]]

REQUEST_ID_HEADER = "X-Request-Id"


def create_app(
    settings: ServiceSettings,
    *,
    description: str = "",
    lifespan_hook: LifespanHook | None = None,
) -> FastAPI:
    """构建一个带全套横切能力的 FastAPI 应用。

    Args:
        settings: 服务配置。
        description: OpenAPI 文档里的服务描述。
        lifespan_hook: 服务专属的启动/关闭钩子（加载模型、建连接池等）。
    """
    setup_logging(settings.service_name, settings.log_level, settings.log_json)
    log = get_logger(settings.service_name)
    registry = HealthRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("service.starting", version=settings.version, env=settings.env, port=settings.port)
        if lifespan_hook is None:
            yield
        else:
            async with _wrap(lifespan_hook, app):
                yield
        log.info("service.stopped")

    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        description=description,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.health = registry
    app.state.log = log

    if settings.is_dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        response.headers[REQUEST_ID_HEADER] = rid
        # 探针请求不写日志，否则每秒一条噪声淹没真实请求
        if request.url.path not in ("/health", "/ready"):
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        return response

    # ---------- 异常兜底：任何异常都返回 ApiResponse 形状 ----------

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        detail = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        log.warning("request.invalid", detail=detail, path=request.url.path)
        return JSONResponse(
            status_code=200,
            content=_dump(ApiResponse.fail(ErrorCode.BAD_REQUEST, detail, request_id_var.get())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=_dump(ApiResponse.fail(code, str(exc.detail), request_id_var.get())),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # 未预期异常：堆栈只进日志，响应体只给通用文案
        log.exception("request.unhandled", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content=_dump(ApiResponse.fail(ErrorCode.INTERNAL_ERROR, trace_id=request_id_var.get())),
        )

    # ---------- 探针 ----------

    @app.get("/health", tags=["ops"], summary="存活探针（不探测下游）")
    async def health():
        return ApiResponse.ok(
            {
                "service": settings.service_name,
                "version": settings.version,
                "status": "UP",
                "uptimeSeconds": round(registry.uptime_seconds, 1),
            }
        )

    @app.get("/ready", tags=["ops"], summary="就绪探针（执行依赖检查）")
    async def ready():
        ok, details = await registry.run()
        payload = {
            "service": settings.service_name,
            "status": "UP" if ok else "DOWN",
            "dependencies": details,
        }
        return JSONResponse(
            status_code=200 if ok else 503,
            content=_dump(
                ApiResponse.ok(payload)
                if ok
                else ApiResponse.fail(ErrorCode.DEPENDENCY_UNAVAILABLE, "依赖未就绪")
            )
            | {"data": payload},
        )

    return app


def _dump(resp: ApiResponse) -> dict:
    return resp.model_dump(mode="json", by_alias=True)


@asynccontextmanager
async def _wrap(hook: LifespanHook, app: FastAPI) -> AsyncIterator[None]:
    gen = hook(app)
    await gen.__anext__()
    try:
        yield
    finally:
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
