"""转人工回流：把工单变回知识。

这是数据飞轮的后半圈。前半圈是「对话 → 清洗 → 知识 → 检索」，
后半圈是「答不上来 → 工单 → 人工回答 → 知识 → 下次能答」。

**为什么转人工工单比覆盖度矩阵更值钱**：矩阵是推断出来的——
"这个类目的尺码知识是空的，也许该补"；工单是**已经发生**的——
有个真实用户真的问了这个问题，而我们真的没答上来。同样的补写成本，
先补工单里的，收益高得多。

同一个问题反复转人工，说明它既是真需求又确实没有知识，
补写顺序直接按频次排。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .models import BizType, KnowledgeItem, KnowledgeType, ReviewStatus


@dataclass
class Ticket:
    """一张转人工工单。"""

    id: int
    question: str
    reason: str
    intent: str = ""
    product_id: int | None = None
    status: str = "open"
    human_answer: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    times: int = 1
    """同题面的工单数。频次就是优先级。"""


@dataclass
class BlindSpot:
    """按题面聚合后的知识盲点。"""

    question: str
    times: int
    reason: str
    ticket_id: int
    status: str = "open"

    @property
    def priority(self) -> str:
        # 被问三次以上还没有知识，属于每天都在损失体验的那一类
        if self.times >= 3:
            return "P0"
        return "P1" if self.times == 2 else "P2"


class HandoverImportError(RuntimeError):
    pass


def to_knowledge_item(
    ticket: Ticket,
    answer: str,
    *,
    product_category: dict[int, int] | None = None,
    knowledge_type: KnowledgeType | None = None,
) -> KnowledgeItem:
    """把「问题 + 人工答案」变成一条待审核知识。

    几个刻意的选择：

    * ``review_status`` 一律 **pending**。人工客服的回答是为了解决
      眼前这一个用户，不等于它适合作为通用知识上线——可能带着这单
      特有的让步（"这次给您补个运费"）。必须再过一道审核。
    * ``source='handover'`` 而不是混进 synthetic。发版门禁要能按来源
      算占比，把回流知识和合成数据混为一谈会让门禁失去意义。
    * ``category_id`` 从商品映射填。不填的话覆盖度矩阵漏算它们，
      而"补上了哪块空白"正是回流最想回答的问题。
    """
    answer = (answer or "").strip()
    if not answer:
        raise HandoverImportError(f"工单 #{ticket.id} 没有人工答案，无法回流")
    question = (ticket.question or "").strip()
    if not question:
        raise HandoverImportError(f"工单 #{ticket.id} 没有题面")

    cat = None
    if ticket.product_id is not None and product_category:
        cat = product_category.get(ticket.product_id)

    return KnowledgeItem(
        biz_type=BizType.QA,
        title=question,
        content=answer,
        product_ids=[ticket.product_id] if ticket.product_id else [],
        category_id=cat,
        source="handover",
        source_ref=f"ticket:{ticket.id}",
        # 人工写的答案质量高于模型抽取，但仍要审核，所以给个偏高但不满的分
        quality_score=0.9,
        confidence=0.85,
        knowledge_type=knowledge_type or _guess_type(question, ticket.intent),
        review_status=ReviewStatus.PENDING,
    )


#: 意图与知识类型基本一一对应，能对上就直接用，省一次模型调用
_INTENT_TO_TYPE = {
    "product_knowledge": KnowledgeType.SPEC,
    "sizing": KnowledgeType.SIZING,
    "aftersale": KnowledgeType.AFTERSALE,
    "order_logistics": KnowledgeType.LOGISTICS,
    "realtime_stock_price": KnowledgeType.OTHER,
}

#: **顺序即优先级**——一句话常常同时命中多类。
#: "退货运费谁承担" 同时有"退货"和"运费"，它是售后政策而不是物流问题，
#: 所以售后必须排在物流前面。把最具体的类别放前面，最泛的放后面。
_TYPE_HINTS = (
    (("尺码", "码数", "什么码", "几码", "大码", "小码",
      "身高", "体重", "多大", "偏大", "偏小"), KnowledgeType.SIZING),
    # 用"能退/可以退"而不是光一个"退"：后者会误伤"退色"（褪色的常见写法），
    # 把一个养护问题归成售后
    (("退货", "退换", "换货", "售后", "退款", "保修", "七天",
      "能退", "可以退", "能换", "可以换"), KnowledgeType.AFTERSALE),
    (("活动", "优惠", "满减", "领券", "优惠券", "折扣", "促销"),
     KnowledgeType.PROMOTION),
    (("洗", "保养", "养护", "熨", "晾", "起球", "褪色", "缩水"),
     KnowledgeType.USAGE),
    (("物流", "快递", "发货", "到货", "几天", "多久到", "运费", "包邮"),
     KnowledgeType.LOGISTICS),
    (("材质", "面料", "克重", "成分", "工艺", "填充", "含量"),
     KnowledgeType.SPEC),
)


def _guess_type(question: str, intent: str) -> KnowledgeType:
    """推断知识类型。

    先用意图（Agent 已经分过一次，不必重复判断），意图给不出结论时
    退回关键词。都对不上就 OTHER——宁可标成 other，也不要瞎猜一个
    具体类型，那会让覆盖度矩阵显示出假的覆盖。
    """
    hit = _INTENT_TO_TYPE.get(intent)
    if hit is not None and hit is not KnowledgeType.OTHER:
        return hit
    for words, ktype in _TYPE_HINTS:
        if any(w in question for w in words):
            return ktype
    return KnowledgeType.OTHER


def rank_blind_spots(spots: Sequence[BlindSpot]) -> list[BlindSpot]:
    """按补写价值排序：先看被问了几次，同频次下先来先补。"""
    return sorted(spots, key=lambda s: (-s.times, s.ticket_id))


def render_blind_spots(spots: Sequence[BlindSpot]) -> str:
    if not spots:
        return "  暂无转人工记录——要么知识库覆盖得好，要么还没人用过。"
    lines = [f"  {'优先级':<6} {'次数':>4}  {'状态':<10} 问题", "  " + "─" * 62]
    for s in rank_blind_spots(spots):
        lines.append(
            f"  {s.priority:<6} {s.times:>4}  {s.status:<10} "
            f"#{s.ticket_id} {s.question[:40]}"
        )
    return "\n".join(lines)
