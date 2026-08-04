"""Kafka 消息统一信封。

与 Java 侧 ``com.smartmall.common.kafka.MessageEnvelope`` 保持同一 JSON 形状
（camelCase 字段名）。所有 topic 的消息体都是这个形状，``payload`` 才是业务载荷。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_RETRY = 3
"""超过此次数进入 ``{topic}.dlq``，由运营后台人工重投。"""


class MessageEnvelope(BaseModel, Generic[T]):
    msg_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="msgId")
    """消费端幂等去重键（Redis SETNX，TTL 24h）。重试时保持不变。"""

    topic: str
    trace_id: str | None = Field(default=None, alias="traceId")
    produced_by: str = Field(alias="producedBy")
    produced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), alias="producedAt"
    )
    retry_count: int = Field(default=0, alias="retryCount")
    payload: T

    model_config = {"populate_by_name": True}

    @classmethod
    def of(
        cls,
        topic: str,
        produced_by: str,
        payload: Any,
        trace_id: str | None = None,
    ) -> "MessageEnvelope":
        return cls(
            topic=topic,
            producedBy=produced_by,
            traceId=trace_id,
            payload=payload,
        )

    def next_retry(self) -> "MessageEnvelope[T]":
        """重试时保留原 msg_id，使消费端的幂等判断仍然成立。"""
        return self.model_copy(update={"retry_count": self.retry_count + 1})

    def exhausted(self, max_retry: int = MAX_RETRY) -> bool:
        return self.retry_count >= max_retry
