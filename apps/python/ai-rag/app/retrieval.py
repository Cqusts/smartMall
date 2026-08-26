"""检索链路：过滤条件构造、RRF 融合、阈值裁剪。

这里只有纯逻辑，不依赖 Milvus——因此可以完整地单元测试。
Milvus 的具体调用在 milvus_store.py。

三件事：

1. **硬性过滤**（:func:`build_filter_expr`）—— 任何查询都必须带上，
   封装成函数不允许调用方绕过。审核状态、时效、版本三条是红线。
2. **RRF 融合**（:func:`reciprocal_rank_fusion`）—— dense 的余弦相似度与
   BM25 分数量纲完全不同，归一化再加权需要针对数据集调参且不稳；
   RRF 只用排名不用分数，无需调参。
3. **阈值裁剪**（:func:`apply_threshold`）—— 检索不到就不回答。
   电商客服说错话的代价是真实的退货与投诉，宁可转人工也不要幻觉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Sequence

from smartmall_pipeline.rag.store import SEARCHABLE_REVIEW_STATUS


class RetrievalOutcome(StrEnum):
    """检索结果的处置方式。"""

    ANSWER = "answer"
    """命中良好，正常生成答案"""
    ANSWER_HEDGED = "answer_hedged"
    """命中不足，生成答案但降低确定性表述并提示可转人工"""
    CLARIFY = "clarify"
    """问题模糊，追问澄清"""
    HANDOVER = "handover"
    """检索无命中，不生成答案，转人工"""


@dataclass(frozen=True)
class Hit:
    """一条召回结果。"""

    chunk_id: int
    item_id: int
    text: str
    score: float
    biz_type: str = "qa"
    modality: str = "text"
    asset_ids: tuple[int, ...] = ()
    product_ids: tuple[int, ...] = ()
    source: str = "dense"
    """dense | sparse | fused | rerank，便于在 Trace 里分析两路的贡献。"""


@dataclass
class RetrievalConfig:
    recall_top_k: int = 50
    """单路召回数量。"""
    fuse_top_k: int = 30
    rerank_top_k: int = 5
    rrf_k: int = 60
    """RRF 平滑常数。60 是社区经验值，无需针对数据集调。"""

    answer_threshold: float = 0.5
    """rerank 最高分 ≥ 此值才正常作答。"""
    hedge_threshold: float = 0.3
    """介于两个阈值之间：作答但降低确定性。低于则拒答。"""

    kb_version: str = ""
    """空字符串表示不做版本隔离（开发期）。生产必须指定。"""


# ---------------------------------------------------------------- 过滤条件


def _escape(value: str) -> str:
    return value.replace('"', '\\"')


def build_filter_expr(
    *,
    kb_version: str = "",
    product_ids: Sequence[int] | None = None,
    category_id: int | None = None,
    biz_types: Sequence[str] | None = None,
    modalities: Sequence[str] | None = None,
    now_ts: int | None = None,
) -> str:
    """构造 Milvus 标量过滤表达式。

    前三条是**硬性过滤，任何查询都必须带上**，调用方无法绕过：

    * ``review_status in [...]`` —— 未审核的知识绝不进入检索结果。
      取值见 ``SEARCHABLE_REVIEW_STATUS``，与本地实现共用一份
    * ``valid_to_ts == 0 or valid_to_ts > now`` —— 过期知识自动排除
    * ``kb_version == ...`` —— 版本隔离，支持秒级回滚

    后面几个是按查询上下文可选的收窄条件。
    """
    now_ts = now_ts if now_ts is not None else int(time.time())

    # 状态列表与 LocalVectorStore.load() 共用一份定义——各写一份的话，
    # 人工改写过的知识（revised）会出现"本地能查到、Milvus 查不到"，
    # 而且不报错。见 SEARCHABLE_REVIEW_STATUS 的注释。
    statuses = ", ".join(f'"{_escape(s)}"' for s in SEARCHABLE_REVIEW_STATUS)
    clauses = [
        f"review_status in [{statuses}]",
        f"(valid_to_ts == 0 or valid_to_ts > {now_ts})",
    ]

    if kb_version:
        clauses.append(f'kb_version == "{_escape(kb_version)}"')

    if product_ids:
        # ARRAY_CONTAINS_ANY：命中任一关联商品即可
        ids = ", ".join(str(int(p)) for p in product_ids)
        clauses.append(f"array_contains_any(product_ids, [{ids}])")

    if category_id is not None:
        clauses.append(f"category_id == {int(category_id)}")

    if biz_types:
        vals = ", ".join(f'"{_escape(b)}"' for b in biz_types)
        clauses.append(f"biz_type in [{vals}]")

    if modalities:
        vals = ", ".join(f'"{_escape(m)}"' for m in modalities)
        clauses.append(f"modality in [{vals}]")

    return " and ".join(clauses)


# ---------------------------------------------------------------- RRF 融合


def reciprocal_rank_fusion(
    *rankings: Iterable[Hit],
    k: int = 60,
    top_k: int | None = None,
) -> list[Hit]:
    """Reciprocal Rank Fusion。

    score(d) = Σ_r 1 / (k + rank_r(d))

    只用排名不用原始分数，因此天然免疫两路分数量纲不一致的问题。
    同一文档被两路都召回时得分叠加——这正是我们想要的信号：
    既语义相关又关键词匹配的结果应该排最前。
    """
    fused: dict[int, float] = {}
    best: dict[int, Hit] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            # 保留首次出现的 Hit 对象作为元数据载体
            best.setdefault(hit.chunk_id, hit)

    ordered = sorted(fused.items(), key=lambda kv: -kv[1])
    if top_k is not None:
        ordered = ordered[:top_k]

    out: list[Hit] = []
    for chunk_id, score in ordered:
        h = best[chunk_id]
        out.append(
            Hit(
                chunk_id=h.chunk_id,
                item_id=h.item_id,
                text=h.text,
                score=score,
                biz_type=h.biz_type,
                modality=h.modality,
                asset_ids=h.asset_ids,
                product_ids=h.product_ids,
                source="fused",
            )
        )
    return out


def dedup_by_item(hits: Sequence[Hit]) -> list[Hit]:
    """同一 knowledge_item 的多个切片只保留最高分的那个。

    政策文档切成 5 块后可能全部命中，占满 Top-5 把其他知识挤掉。
    """
    seen: dict[int, Hit] = {}
    for h in hits:
        prev = seen.get(h.item_id)
        if prev is None or h.score > prev.score:
            seen[h.item_id] = h
    return sorted(seen.values(), key=lambda h: -h.score)


# ---------------------------------------------------------------- 阈值裁剪


@dataclass
class RetrievalResult:
    hits: list[Hit] = field(default_factory=list)
    outcome: RetrievalOutcome = RetrievalOutcome.HANDOVER
    max_score: float = 0.0
    reason: str = ""

    @property
    def should_answer(self) -> bool:
        return self.outcome in (RetrievalOutcome.ANSWER, RetrievalOutcome.ANSWER_HEDGED)


def apply_threshold(
    hits: Sequence[Hit],
    config: RetrievalConfig | None = None,
    *,
    query_is_vague: bool = False,
) -> RetrievalResult:
    """按 rerank 最高分决定处置方式。

    「检索不到就不回答」是硬规则，不是可调策略。
    """
    cfg = config or RetrievalConfig()

    if not hits:
        return RetrievalResult(
            hits=[], outcome=RetrievalOutcome.HANDOVER, max_score=0.0,
            reason="无召回结果",
        )

    top = hits[0].score

    if top >= cfg.answer_threshold:
        return RetrievalResult(
            hits=list(hits), outcome=RetrievalOutcome.ANSWER, max_score=top,
            reason="命中良好",
        )

    if top >= cfg.hedge_threshold:
        if query_is_vague:
            return RetrievalResult(
                hits=list(hits), outcome=RetrievalOutcome.CLARIFY, max_score=top,
                reason="命中不足且问题模糊，追问澄清",
            )
        return RetrievalResult(
            hits=list(hits), outcome=RetrievalOutcome.ANSWER_HEDGED, max_score=top,
            reason="命中不足，降低确定性表述",
        )

    return RetrievalResult(
        hits=[], outcome=RetrievalOutcome.HANDOVER, max_score=top,
        reason=f"最高分 {top:.3f} 低于拒答阈值 {cfg.hedge_threshold}",
    )


# ---------------------------------------------------------------- 查询改写


VAGUE_PRONOUNS = ("这个", "那个", "它", "上面说的", "刚才那个", "这款", "那款")


def needs_rewrite(query: str, *, max_len: int = 10) -> bool:
    """判断是否需要查询改写。

    不是每轮都改写——改写有成本也有风险（可能改错原意）。
    只在三种情况触发：问题太短、含指代词、或首次检索命中不足。
    """
    q = query.strip()
    if len(q) < max_len:
        return True
    return any(p in q for p in VAGUE_PRONOUNS)
