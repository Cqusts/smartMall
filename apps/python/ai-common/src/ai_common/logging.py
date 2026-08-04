"""结构化日志。

本地开发输出人类可读的彩色行；生产（``LOG_JSON=true``）输出 JSON，便于日志采集。
每条日志自动带上当前请求的 ``request_id``，与响应头 ``X-Request-Id`` 对应，
排查线上问题时可以从一条用户反馈直接定位到全部相关日志。
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _inject_request_id(_logger, _method_name, event_dict):
    rid = request_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def setup_logging(service_name: str, level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # uvicorn 的 access 日志与我们的请求日志重复，关掉它保留自己的
    logging.getLogger("uvicorn.access").disabled = True

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_request_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service_name)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
