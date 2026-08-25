"""跨 Agent 任务的形状。

一条任务就是「A 把一件活交给 B」这件事的记录。字段里有一半是为了
**事后能说清这条链是怎么走的**（source/target/parent/root）——
少了它们，一条「更新文案」任务看起来就像凭空冒出来的。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 常量


class Kind:
    """任务类型。**不是随便一个字符串**——runner 按它分发，
    拼错一个字母的任务会永远没人认领，而且不报任何错。"""

    WRITE_KNOWLEDGE = "write_knowledge"
    REFRESH_COPY = "refresh_copy"

    ALL = (WRITE_KNOWLEDGE, REFRESH_COPY)


class Status:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    NEEDS_HUMAN = "needs_human"
    """跑完了，结论是「机器做不了，得人来」。

    **和 failed 分开是刻意的。** failed 是"跑挂了，下次可能就好"；
    这个是"跑对了，答案就是要人写"。混成一个的话，重试循环会一遍遍
    去跑一件机器永远做不成的事，而真正的故障淹没在里面看不见。
    """
    FAILED = "failed"
    CANCELLED = "cancelled"

    OPEN = (PENDING, RUNNING)
    """还没有结论的。``open_key`` 只在这两个状态下有值。"""
    TERMINAL = (DONE, NEEDS_HUMAN, FAILED, CANCELLED)


#: 谁在派活 / 谁在干活。与 /info 里的 capabilities 对齐
class Agent:
    CUSTOMER_SERVICE = "customer_service"
    KNOWLEDGE_OPS = "knowledge_ops"
    MARKETING = "marketing"


# ---------------------------------------------------------------- 去重键

#: 归一化时丢掉的字符：空白与标点。
#:
#: **只做这一层，不做同义词归并。** "怎么退货" 和 "退货怎么弄" 在这里是
#: 两条任务，而它们确实是同一个盲点——那一层由知识运维自己的聚类去合
#: （cluster.similarity，阈值是量出来的）。
#:
#: 两层各干各的：这里挡的是"同一句话被问了 50 遍"，那里挡的是"同一件事
#: 有五种问法"。想在这里就把同义词并掉，等于把一个需要分词与相似度的
#: 判断塞进客服的回复路径里，而它每一轮对话都要跑。
_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    return _NOISE.sub("", str(text or "")).lower()


def dedupe_key(kind: str, subject: str, product_id: int | None = None) -> str:
    """``write_knowledge:9001:3f2a...``

    带上 ``kind`` 与 ``product_id``：同一句话在不同商品下是不同的盲点
    （"这件会起球吗"问的是哪件，决定了要补的知识完全不同）。
    """
    digest = hashlib.sha1(normalize(subject).encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{product_id or 0}:{digest}"


# ---------------------------------------------------------------- 任务


@dataclass
class AgentTask:
    """一条任务。"""

    kind: str
    dedupe_key: str
    payload: dict[str, Any] = field(default_factory=dict)

    id: int | None = None
    status: str = Status.PENDING
    source_agent: str = ""
    target_agent: str = ""
    times: int = 1
    priority: int = 0
    product_id: int | None = None
    parent_id: int | None = None
    root_id: int | None = None
    attempts: int = 0
    max_attempts: int = 3
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def subject(self) -> str:
        """人能读的一句话，用来在列表里认出这条任务是干什么的。"""
        p = self.payload or {}
        return str(p.get("question") or p.get("reason") or
                   (f"商品 #{self.product_id}" if self.product_id else "-"))

    @property
    def retriable(self) -> bool:
        return self.attempts < self.max_attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "times": self.times, "priority": self.priority,
            "product_id": self.product_id, "subject": self.subject,
            "source": self.source_agent, "target": self.target_agent,
            "attempts": self.attempts, "error": self.error,
            "root_id": self.root_id, "parent_id": self.parent_id,
        }


@dataclass
class RunReport:
    """一轮 worker 的结果。"""

    claimed: int = 0
    done: int = 0
    needs_human: int = 0
    failed: int = 0
    dispatched: int = 0
    """本轮又派出去的下一环任务数。**这个数字就是「闭环」本身**——
    一直是 0 说明链断在第一环，四个 Agent 还是各跑各的。"""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"claimed": self.claimed, "done": self.done,
                "needs_human": self.needs_human, "failed": self.failed,
                "dispatched": self.dispatched, "notes": list(self.notes)}
