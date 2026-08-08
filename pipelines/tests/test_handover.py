"""转人工回流：工单 → 知识。

这是数据飞轮的后半圈。前半圈把历史对话变成知识，后半圈把
「答不上来的问题」变成知识——后者更值钱，因为它由真实用户的
真实提问驱动，而不是从存量数据里挖。

回流最容易出的错不是漏，是**太急**：把人工客服为某一个用户写的
答案直接当通用知识上线。那句"这次给您补个运费"会变成对所有人的承诺。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.handover import (
    BlindSpot,
    HandoverImportError,
    Ticket,
    rank_blind_spots,
    render_blind_spots,
    to_knowledge_item,
)
from smartmall_pipeline.models import BizType, KnowledgeType, ReviewStatus


def _ticket(**kw) -> Ticket:
    base = dict(
        id=7,
        question="这件羊毛衫能机洗吗",
        reason="知识库无相关内容",
        intent="product_knowledge",
        product_id=1024,
    )
    base.update(kw)
    return Ticket(**base)  # type: ignore[arg-type]


class TestToKnowledgeItem:
    def test_never_lands_approved(self):
        """人工客服的回答是为了解决眼前这一个用户。

        它可能带着这单特有的让步（"这次给您补个运费"），直接当
        通用知识上线，就是把一次性特例变成对所有人的承诺。
        必须再过一道审核。
        """
        item = to_knowledge_item(_ticket(), "建议手洗，水温不超过30度")
        assert item.review_status is ReviewStatus.PENDING

    def test_source_is_distinguishable(self):
        """回流知识不能混进 synthetic。

        发版门禁要按来源算占比，混为一谈会让"合成数据不超过 50%"
        这条门禁失去意义。
        """
        item = to_knowledge_item(_ticket(), "建议手洗")
        assert item.source == "handover"
        assert item.source_ref == "ticket:7"

    def test_category_comes_from_product_mapping(self):
        """不填类目，覆盖度矩阵就漏算它——
        而"补上了哪块空白"正是回流最想回答的问题。"""
        item = to_knowledge_item(
            _ticket(product_id=1024), "建议手洗", product_category={1024: 9001}
        )
        assert item.category_id == 9001
        assert item.product_ids == [1024]

    def test_no_mapping_means_no_category_not_a_guess(self):
        item = to_knowledge_item(_ticket(), "建议手洗")
        assert item.category_id is None

    def test_question_becomes_the_title(self):
        item = to_knowledge_item(_ticket(), "建议手洗")
        assert item.title == "这件羊毛衫能机洗吗"
        assert item.content == "建议手洗"
        assert item.biz_type is BizType.QA

    def test_blank_answer_is_refused(self):
        """没答案就没有知识。空字符串写进去只会污染检索。"""
        with pytest.raises(HandoverImportError):
            to_knowledge_item(_ticket(), "   ")

    def test_blank_question_is_refused(self):
        with pytest.raises(HandoverImportError):
            to_knowledge_item(_ticket(question=""), "建议手洗")

    def test_answer_is_trimmed(self):
        assert to_knowledge_item(_ticket(), "  建议手洗  ").content == "建议手洗"


class TestKnowledgeTypeInference:
    def test_intent_is_used_first(self):
        """Agent 已经分过一次意图，不必再判一遍。"""
        item = to_knowledge_item(_ticket(intent="sizing"), "建议选 L 码")
        assert item.knowledge_type is KnowledgeType.SIZING

    @pytest.mark.parametrize("question,expected", [
        ("160cm 穿什么码", KnowledgeType.SIZING),
        ("几天能到货", KnowledgeType.LOGISTICS),
        ("怎么退货", KnowledgeType.AFTERSALE),
        ("能机洗吗", KnowledgeType.USAGE),
        ("有满减活动吗", KnowledgeType.PROMOTION),
        ("什么面料的", KnowledgeType.SPEC),
    ])
    def test_falls_back_to_keywords(self, question, expected):
        """意图给不出结论时用关键词兜。"""
        item = to_knowledge_item(
            _ticket(question=question, intent=""), "答案"
        )
        assert item.knowledge_type is expected

    @pytest.mark.parametrize("question,expected", [
        # 顺序即优先级：一句话常常同时命中多类
        ("退货运费谁承担", KnowledgeType.AFTERSALE),   # 退货 + 运费 → 售后政策
        ("洗坏了能退吗", KnowledgeType.AFTERSALE),      # 洗 + 退 → 售后
        ("活动商品几天发货", KnowledgeType.PROMOTION),  # 活动 + 发货 → 活动规则
    ])
    def test_most_specific_category_wins(self, question, expected):
        """归错类比不归类更糟：检索按类目收窄时会把它排除在正确范围外。"""
        item = to_knowledge_item(_ticket(question=question, intent=""), "答案")
        assert item.knowledge_type is expected

    def test_unknown_stays_other_rather_than_guessing(self):
        """瞎猜一个具体类型会让覆盖度矩阵显示出假的覆盖——
        以为那个格子补上了，其实里面是不相干的东西。"""
        item = to_knowledge_item(
            _ticket(question="你们老板是谁", intent=""), "这个不方便透露"
        )
        assert item.knowledge_type is KnowledgeType.OTHER

    def test_realtime_intent_does_not_claim_a_type(self):
        """实时类问题（库存价格）本来就不该沉淀成知识。"""
        item = to_knowledge_item(
            _ticket(question="现在还有货吗", intent="realtime_stock_price"),
            "目前有货",
        )
        assert item.knowledge_type is KnowledgeType.OTHER


class TestBlindSpotRanking:
    def test_frequency_drives_priority(self):
        """同一个问题反复转人工，说明它既是真需求又确实没有知识。"""
        assert BlindSpot("q", 5, "r", 1).priority == "P0"
        assert BlindSpot("q", 2, "r", 1).priority == "P1"
        assert BlindSpot("q", 1, "r", 1).priority == "P2"

    def test_sorted_by_times_then_age(self):
        spots = [
            BlindSpot("少见", 1, "r", 10),
            BlindSpot("很常见", 7, "r", 3),
            BlindSpot("常见", 3, "r", 8),
            BlindSpot("同样常见但更早", 3, "r", 2),
        ]
        got = [s.question for s in rank_blind_spots(spots)]
        assert got == ["很常见", "同样常见但更早", "常见", "少见"]

    def test_render_is_readable_when_empty(self):
        out = render_blind_spots([])
        assert out.strip()
        assert "暂无" in out

    def test_render_lists_highest_priority_first(self):
        out = render_blind_spots([
            BlindSpot("只问过一次", 1, "r", 5),
            BlindSpot("问了很多次", 9, "r", 6),
        ])
        assert out.index("问了很多次") < out.index("只问过一次")
        assert "P0" in out

    def test_fading_is_care_not_aftersale(self):
        """「退色」是褪色的常见写法。

        用光一个"退"字做售后判据会把这个养护问题归错类——
        这是加宽关键词时最容易踩的坑，用测试钉住。
        """
        item = to_knowledge_item(
            _ticket(question="这件洗了会退色吗", intent=""), "不会明显退色"
        )
        assert item.knowledge_type is KnowledgeType.USAGE
