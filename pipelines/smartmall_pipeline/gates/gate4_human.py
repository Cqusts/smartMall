"""关卡④ 人工清洗与补充（Label Studio 接入）。

**核心思路：人工不看全部，只看模型拿不准的。**
100 万会话人工看不完，主动学习是这一关可行的前提。

两条独立流程：

1. **清洗** —— 把关卡③的低置信度产出推给人工确认/修正/丢弃。
2. **补充** —— 有些知识对话里根本没有，必须人工补写。
   由覆盖度矩阵找出空白格生成补写任务（见 coverage.py）。

推送策略按优先级排队，人工先处理 P0。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Iterable, Protocol

from ..models import GateStats, KnowledgeItem, ReviewStatus


class Priority(IntEnum):
    """推送优先级。数值越小越优先。"""

    P0 = 0
    """必须人工看：低置信度、含不确定表述"""
    P1 = 1
    """应该人工看：高频问题的代表样本、与商品属性冲突"""
    P2 = 2
    """抽样质检：随机 5%，用于质量监控基线"""


@dataclass(frozen=True)
class AnnotationTask:
    """推给 Label Studio 的一条任务。

    ``prediction`` 是关卡③的输出，作为**预标注**展示给人工——
    人工只需确认或修改，而不是从零标注。这是效率的关键。
    """

    external_id: str
    priority: Priority
    reason: str
    question: str
    answer: str
    tags: list[str]
    confidence: float
    product_ids: list[int]
    source_ref: str | None

    def to_label_studio(self) -> dict[str, Any]:
        """转成 Label Studio 的 import 格式。

        predictions 字段让关卡③的结果作为预标注出现在界面上。
        """
        return {
            "data": {
                "external_id": self.external_id,
                "question": self.question,
                "answer": self.answer,
                "priority": int(self.priority),
                "reason": self.reason,
                "confidence": round(self.confidence, 3),
                "product_ids": self.product_ids,
                "source_ref": self.source_ref,
            },
            "predictions": [
                {
                    "model_version": "gate3-extract",
                    "score": self.confidence,
                    "result": [
                        {
                            "from_name": "answer",
                            "to_name": "dialogue",
                            "type": "textarea",
                            "value": {"text": [self.answer]},
                        },
                        {
                            "from_name": "tags",
                            "to_name": "dialogue",
                            "type": "choices",
                            "value": {"choices": self.tags},
                        },
                    ],
                }
            ],
        }


@dataclass
class Gate4Config:
    review_below_confidence: float = 0.6
    """低于此置信度必须人工看（P0）。"""
    sample_rate: float = 0.05
    """随机抽检比例（P2），质量监控基线。"""
    hot_question_threshold: int = 50
    """同类问题出现次数超过此值，其代表样本进 P1 精修为 golden 答案。"""
    uncertain_words: tuple[str, ...] = (
        "可能", "大概", "应该是", "估计", "不太清楚", "我确认一下",
    )
    auto_approve_above: float = 0.9
    """置信度高于此值且无冲突的条目自动通过，不占用人工。

    这是吞吐量的关键——若全部条目都要人工过一遍，人工就是瓶颈。
    """
    seed: int = 20260401


def _conflicts_with_attrs(
    item: KnowledgeItem, product_attrs: dict[int, dict[str, str]]
) -> str | None:
    """检查答案是否与商品结构化属性冲突。

    客服口头说的和商品实际属性对不上，是最危险的一类错误——
    它会以"知识库权威答案"的身份被客服输出给用户，构成虚假宣传。
    """
    for pid in item.product_ids:
        attrs = product_attrs.get(pid)
        if not attrs:
            continue
        for key, value in attrs.items():
            if key in item.content and value not in item.content:
                # 提到了这个属性名但值对不上
                return f"与商品{pid}的「{key}」属性可能冲突（实际：{value}）"
    return None


def triage(
    items: Iterable[KnowledgeItem],
    config: Gate4Config | None = None,
    *,
    product_attrs: dict[int, dict[str, str]] | None = None,
    question_freq: dict[str, int] | None = None,
) -> tuple[list[AnnotationTask], list[KnowledgeItem], GateStats]:
    """把关卡③的产出分流为「需人工」与「自动通过」。

    Returns:
        (标注任务, 自动通过的条目, 统计)
    """
    import random

    cfg = config or Gate4Config()
    attrs = product_attrs or {}
    freq = question_freq or {}
    rng = random.Random(cfg.seed)

    items = list(items)
    stats = GateStats(gate="④ 人工清洗", input_count=len(items))
    tasks: list[AnnotationTask] = []
    auto_approved: list[KnowledgeItem] = []

    for item in items:
        conf = item.confidence if item.confidence is not None else 0.0
        priority: Priority | None = None
        reason = ""

        conflict = _conflicts_with_attrs(item, attrs)

        if conf < cfg.review_below_confidence:
            priority, reason = Priority.P0, f"置信度低（{conf:.2f}）"
        elif any(w in item.content for w in cfg.uncertain_words):
            priority, reason = Priority.P0, "答案含不确定表述，需改写为确定表述"
        elif conflict:
            priority, reason = Priority.P1, conflict
        elif freq.get(item.title or "", 0) >= cfg.hot_question_threshold:
            priority, reason = Priority.P1, "高频问题，精修为 golden 答案"
        elif conf >= cfg.auto_approve_above:
            if rng.random() < cfg.sample_rate:
                priority, reason = Priority.P2, "随机抽检"
            else:
                item.review_status = ReviewStatus.APPROVED
                auto_approved.append(item)
                stats.modify("自动通过")
                continue
        else:
            priority, reason = Priority.P2, "置信度中等，抽检确认"

        tasks.append(
            AnnotationTask(
                external_id=f"{item.source_ref or 'unknown'}#{item.dedup_key()}",
                priority=priority,
                reason=reason,
                question=item.title or "",
                answer=item.content,
                tags=item.tags,
                confidence=conf,
                product_ids=item.product_ids,
                source_ref=item.source_ref,
            )
        )
        stats.modify(f"推送人工:{priority.name}")

    tasks.sort(key=lambda t: (t.priority, -t.confidence))
    stats.output_count = len(auto_approved) + len(tasks)
    return tasks, auto_approved, stats


# ---------------------------------------------------------------- Label Studio


class LabelStudioClient(Protocol):
    def import_tasks(self, project_id: int, tasks: list[dict[str, Any]]) -> int: ...
    def export_annotations(self, project_id: int) -> list[dict[str, Any]]: ...


class HttpLabelStudioClient:
    """Label Studio REST 客户端。"""

    def __init__(self, base_url: str, api_token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Token {api_token}"}
        self.timeout = timeout

    def import_tasks(self, project_id: int, tasks: list[dict[str, Any]]) -> int:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/api/projects/{project_id}/import",
            headers=self.headers,
            json=tasks,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return int(resp.json().get("task_count", len(tasks)))

    def export_annotations(self, project_id: int) -> list[dict[str, Any]]:
        import httpx

        resp = httpx.get(
            f"{self.base_url}/api/projects/{project_id}/export",
            headers=self.headers,
            params={"exportType": "JSON"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def apply_annotation(item: KnowledgeItem, annotation: dict[str, Any]) -> KnowledgeItem:
    """把人工标注结果回写到知识条目。

    人工的判断优先于模型：审核通过后置信度提到 1.0，
    并把 embedding_status 交由调用方置为 pending 以触发增量向量化。
    """
    action = annotation.get("action", "approve")

    if action == "reject":
        item.review_status = ReviewStatus.REJECTED
        return item

    revised_answer = (annotation.get("answer") or "").strip()
    if revised_answer and revised_answer != item.content:
        item.content = revised_answer
        item.review_status = ReviewStatus.REVISED
    else:
        item.review_status = ReviewStatus.APPROVED

    if tags := annotation.get("tags"):
        item.tags = list(tags)
    if (rid := annotation.get("reviewer_id")) is not None:
        pass  # reviewer_id 由 repository 层写库，领域模型不持有

    item.confidence = 1.0
    return item
