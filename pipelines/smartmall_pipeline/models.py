"""数据中台领域模型。

刻意与数据库解耦：四道清洗关卡全部是对这些模型的纯函数变换，
不碰数据库、不发网络请求（关卡③的 LLM 客户端通过协议注入）。
这样每一关都能单独测试，流水线出问题时能精确定位到哪一关。

数据库读写在 repository.py，Airflow DAG 负责把两者串起来。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["user", "agent", "system"]


class BizType(StrEnum):
    """知识条目的业务类型，决定切分策略与检索权重。"""

    QA = "qa"
    """清洗后的对话问答对"""
    SPEC = "spec"
    """商品结构化属性"""
    SCRIPT = "script"
    """直播口播话术"""
    POLICY = "policy"
    """售后政策、活动规则"""
    FAQ = "faq"
    """人工补写的常见问题"""


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISED = "revised"


class KnowledgeType(StrEnum):
    """关卡③判定出的知识类型，用于覆盖度矩阵的一个维度。"""

    SPEC = "spec"
    LOGISTICS = "logistics"
    AFTERSALE = "aftersale"
    SIZING = "sizing"
    PROMOTION = "promotion"
    USAGE = "usage"
    OTHER = "other"


# ---------------------------------------------------------------- 对话

class Turn(BaseModel):
    """一轮对话。"""

    turn_index: int
    role: Role
    content: str
    raw_content: str | None = None
    """脱敏前原文。落库时加密存储，仅审计角色可解密。"""
    image_urls: list[str] = Field(default_factory=list)
    said_at: datetime | None = None
    reply_cost_ms: int | None = None
    intent: str | None = None
    sentiment: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


class Dialogue(BaseModel):
    """一次完整会话。四道关卡的处理单元。"""

    session_no: str
    source_type: str
    """jddc | jddc2 | ecd | trace | synthetic | manual_import"""
    turns: list[Turn]

    channel: str | None = None
    agent_id: int | None = None
    agent_type: str | None = None
    product_ids: list[int] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    transferred_human: bool = False
    order_created: bool = False

    ods_id: int | None = None
    external_id: str | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def has_image(self) -> bool:
        return any(t.image_urls for t in self.turns)

    @property
    def text(self) -> str:
        """带角色前缀的全文，用于人读与日志展示。"""
        return "\n".join(f"{t.role}: {t.content}" for t in self.turns)

    @property
    def content_text(self) -> str:
        """纯内容全文，不含角色前缀。

        长度过滤、中文占比、MinHash 去重都必须用这个而不是 :attr:`text`——
        ``user:`` / ``agent:`` 前缀是纯 ASCII，会把中文占比压低，
        导致合法的中文短对话被误判为"非中文/乱码"。
        """
        return "\n".join(t.content for t in self.turns)

    def content_hash(self) -> str:
        """内容指纹，用于 ODS 幂等导入。

        只对 role+content 取哈希，不含时间戳与会话号——
        同一段对话被重复导入两次（换了个 session_no）也应该被识别为重复。
        """
        payload = json.dumps(
            [[t.role, t.content] for t in self.turns],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def user_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "user"]

    def agent_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "agent"]


# ---------------------------------------------------------------- 出口模型

class KnowledgeItem(BaseModel):
    """知识条目 —— RAG 知识库的唯一数据源。

    对应 MySQL 的 knowledge_item 表。清洗后的对话 QA、素材 VLM 描述、
    直播口播话术、商品属性、售后政策，全部归一到这一个形状。
    """

    id: int | None = None
    biz_type: BizType
    modality: Modality = Modality.TEXT

    title: str | None = None
    content: str
    summary: str | None = None

    asset_ids: list[int] = Field(default_factory=list)
    product_ids: list[int] = Field(default_factory=list)
    category_id: int | None = None
    tags: list[str] = Field(default_factory=list)

    source: str
    source_ref: str | None = None
    """溯源标识。任意一条知识都必须能追回 ODS 原始记录。"""

    quality_score: float | None = None
    confidence: float | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING

    valid_from: datetime | None = None
    valid_to: datetime | None = None
    """None = 永久有效。促销与活动类必填，否则客服会播报过期活动。"""

    knowledge_type: KnowledgeType = KnowledgeType.OTHER

    def dedup_key(self) -> str:
        """同一问题的去重键。用于发版前的重复率检查。"""
        base = f"{self.biz_type}|{(self.title or '').strip()}|{sorted(self.product_ids)}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


class SftSample(BaseModel):
    """微调样本 —— SFT 与 DPO 的统一形态。

    目标是学风格，不是学知识：messages 中的具体商品参数应已参数化，
    否则模型会把"这件是 100% 羊毛"当成通用知识记住。
    """

    id: int | None = None
    sample_type: Literal["sft", "dpo"]
    messages: list[dict[str, str]]
    chosen: str | None = None
    rejected: str | None = None

    style_tags: list[str] = Field(default_factory=list)
    scene_tag: str | None = None
    quality_score: float | None = None
    is_golden: bool = False
    agent_id: int | None = None

    source: str
    source_ref: str | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING


# ---------------------------------------------------------------- 关卡统计

class GateStats(BaseModel):
    """单道关卡的处理统计。

    对应验收标准「各关卡的淘汰量有统计报表」。
    dropped 按原因分类，是调参的主要依据——比如发现 80% 被
    "语种过滤" 淘汰，说明语种识别阈值设错了。
    """

    gate: str
    input_count: int = 0
    output_count: int = 0
    input_unit: str = "会话"
    output_unit: str = "会话"
    """处理单元。关卡①②进出都是「会话」；关卡③把一段会话拆成多条「条目」，
    进出单位不同——它是换算关卡，不是过滤关卡。

    分成进/出两个字段而不是一个，是因为拿 48 条目除以 20 会话得到的
    "240% 存活率" 根本不是存活率。换算关卡应该报的是产出倍率。
    """
    dropped: dict[str, int] = Field(default_factory=dict)
    drop_units: dict[str, str] = Field(default_factory=dict)
    """每个淘汰原因对应的单位。关卡③既会整段毙掉会话（判定不通过），
    也会逐条丢弃抽取结果（置信度过低），两者不能加在一起报一个数。"""
    modified: dict[str, int] = Field(default_factory=dict)
    """按类型统计"被修改"的次数，如 pii_masked、order_no_parameterized"""

    def drop(self, reason: str, n: int = 1, unit: str | None = None) -> None:
        """记一次淘汰。

        ``unit`` 默认取 ``input_unit``——淘汰的是流进来的东西。
        关卡③里逐条丢弃抽取结果时要显式传 "条目"。
        """
        self.dropped[reason] = self.dropped.get(reason, 0) + n
        self.drop_units[reason] = unit or self.input_unit

    def modify(self, kind: str, n: int = 1) -> None:
        self.modified[kind] = self.modified.get(kind, 0) + n

    @property
    def drop_count(self) -> int:
        """淘汰总数。跨单位时这个数没有意义，展示请用 drop_summary。"""
        return sum(self.dropped.values())

    @property
    def drops_by_unit(self) -> dict[str, int]:
        by_unit: dict[str, int] = {}
        for reason, n in self.dropped.items():
            u = self.drop_units.get(reason, self.input_unit)
            by_unit[u] = by_unit.get(u, 0) + n
        return by_unit

    @property
    def drop_summary(self) -> str:
        by_unit = self.drops_by_unit
        if not by_unit:
            return "淘汰 0"
        if len(by_unit) == 1:
            unit, n = next(iter(by_unit.items()))
            # 与出口同单位时不必赘述，读者自明
            return f"淘汰 {n}" if unit == self.output_unit else f"淘汰 {n} {unit}"
        return "淘汰 " + " + ".join(
            f"{n} {u}" for u, n in sorted(by_unit.items())
        )

    @property
    def keep_rate(self) -> float:
        return self.output_count / self.input_count if self.input_count else 0.0

    def merge(self, other: "GateStats") -> "GateStats":
        """合并同一关卡的多个批次统计。"""
        merged = GateStats(
            gate=self.gate,
            input_count=self.input_count + other.input_count,
            output_count=self.output_count + other.output_count,
            input_unit=self.input_unit,
            output_unit=self.output_unit,
        )
        for src in (self, other):
            for k, v in src.dropped.items():
                merged.drop(k, v, unit=src.drop_units.get(k, src.input_unit))
        for src in (self.modified, other.modified):
            for k, v in src.items():
                merged.modify(k, v)
        return merged


class FunnelReport(BaseModel):
    """四道关卡的漏斗报表。"""

    batch_id: str
    stages: list[GateStats] = Field(default_factory=list)
    knowledge_items: int = 0
    sft_samples: int = 0

    def add(self, stats: GateStats) -> None:
        self.stages.append(stats)

    def render(self) -> str:
        """渲染为可读的漏斗，用于 Airflow 日志与后台展示。"""
        if not self.stages:
            return "(空)"
        lines = [f"清洗漏斗 batch={self.batch_id}", "=" * 62]
        # 累计存活率只在同一单位内有意义。换算关卡（会话→条目）报产出倍率，
        # 并把后续关卡的基数换成它的产出——否则 48 条目会被拿去除以 20 会话。
        base_unit = self.stages[0].input_unit
        base_count = self.stages[0].input_count
        for s in self.stages:
            if s.input_unit != base_unit:
                base_unit, base_count = s.input_unit, s.input_count
            if s.output_unit != s.input_unit:
                ratio = s.output_count / s.input_count if s.input_count else 0
                metric = f"{ratio:.2f} {s.output_unit}/{s.input_unit}"
                base_unit, base_count = s.output_unit, s.output_count
            else:
                pct = s.output_count / base_count * 100 if base_count else 0
                metric = f"{pct:.1f}% 存活"
            lines.append(
                f"{s.gate:<20} {s.input_count:>6} {s.input_unit}"
                f" → {s.output_count:>6} {s.output_unit}"
                f"  ({metric}, {s.drop_summary})"
            )
            for reason, n in sorted(s.dropped.items(), key=lambda kv: -kv[1]):
                lines.append(f"    ✗ {reason:<28} {n:>7}")
            for kind, n in sorted(s.modified.items(), key=lambda kv: -kv[1]):
                lines.append(f"    ~ {kind:<28} {n:>7}")
        lines.append("=" * 62)
        lines.append(f"产出 knowledge_item {self.knowledge_items} 条 / "
                     f"sft_sample {self.sft_samples} 条")
        return "\n".join(lines)

    def to_stats_json(self) -> dict[str, Any]:
        """写入 ds_job.stats 字段。"""
        return {
            "batch_id": self.batch_id,
            "stages": [s.model_dump() for s in self.stages],
            "knowledge_items": self.knowledge_items,
            "sft_samples": self.sft_samples,
        }
