"""统一响应信封。

与 Java 侧 ``com.smartmall.common.api.ApiResponse`` 保持同一 JSON 形状，
使前端与服务间调用无需区分后端语言。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import IntEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorCode(IntEnum):
    """全局错误码。分段规则与 Java 侧 ``ErrorCode`` 一致。

    0 成功 / 1xxx 通用 / 2xxx 商品 / 3xxx 素材 / 4xxx 数据中台 /
    5xxx 考核 / 6xxx AI 服务 / 9xxx 系统与下游依赖
    """

    OK = 0

    BAD_REQUEST = 1400
    UNAUTHORIZED = 1401
    FORBIDDEN = 1403
    NOT_FOUND = 1404
    RATE_LIMITED = 1429

    # 6xxx —— Python AI 服务专属
    RETRIEVAL_EMPTY = 6404
    """检索无命中。不是错误，但需要调用方走兜底分支（拒答/转人工）。"""
    MODEL_UNAVAILABLE = 6503
    MODEL_TIMEOUT = 6504
    GPU_BUSY = 6429
    """GPU 独占任务排队中。详见 docs/14-infra.md 的分时调度。"""
    COMPLIANCE_REJECTED = 6422
    """输出未通过合规检查。"""

    INTERNAL_ERROR = 9500
    DEPENDENCY_UNAVAILABLE = 9503
    DEPENDENCY_TIMEOUT = 9504


_MESSAGES: dict[int, str] = {
    ErrorCode.OK: "success",
    ErrorCode.BAD_REQUEST: "请求参数不合法",
    ErrorCode.UNAUTHORIZED: "未认证",
    ErrorCode.FORBIDDEN: "无权限",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.RATE_LIMITED: "请求过于频繁",
    ErrorCode.RETRIEVAL_EMPTY: "检索无命中",
    ErrorCode.MODEL_UNAVAILABLE: "模型服务不可用",
    ErrorCode.MODEL_TIMEOUT: "模型调用超时",
    ErrorCode.GPU_BUSY: "GPU 任务排队中",
    ErrorCode.COMPLIANCE_REJECTED: "内容未通过合规检查",
    ErrorCode.INTERNAL_ERROR: "系统内部错误",
    ErrorCode.DEPENDENCY_UNAVAILABLE: "下游依赖不可用",
    ErrorCode.DEPENDENCY_TIMEOUT: "下游依赖超时",
}


def message_of(code: ErrorCode) -> str:
    return _MESSAGES.get(int(code), "unknown")


class ApiResponse(BaseModel, Generic[T]):
    code: int = int(ErrorCode.OK)
    message: str = "success"
    data: T | None = None
    trace_id: str | None = Field(default=None, alias="traceId")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def ok(cls, data: T | None = None, trace_id: str | None = None) -> "ApiResponse[T]":
        return cls(code=int(ErrorCode.OK), message="success", data=data, traceId=trace_id)

    @classmethod
    def fail(
        cls,
        code: ErrorCode,
        message: str | None = None,
        trace_id: str | None = None,
    ) -> "ApiResponse[T]":
        return cls(
            code=int(code),
            message=message or message_of(code),
            data=None,
            traceId=trace_id,
        )
