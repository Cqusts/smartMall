"""知识去重。

真实现象：672 条知识里，"针织衫怎么洗？"有五条一模一样的副本，
检索 top-5 全被它们占满，多样性归零——用户问一个稍复杂的问题，
本该由三条不同知识拼出的答案，只剩下一条。

关卡①去的是**会话**的重；这一步去的是**知识**的重。几段内容完全
不同的对话，照样会抽出同一个高频问题。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.dedup import (
    MAX_BONUS,
    MAX_CONFIDENCE,
    duplicate_ratio,
    merge_duplicates,
)
from smartmall_pipeline.models import BizType, KnowledgeItem


def _item(title: str = "针织衫怎么洗", **kw) -> KnowledgeItem:
    base = dict(
        biz_type=BizType.QA,
        title=title,
        content="建议手洗",
        source="synthetic",
        confidence=0.8,
        quality_score=0.8,
        product_ids=[1024],
    )
    base.update(kw)
    return KnowledgeItem(**base)  # type: ignore[arg-type]


class TestMerging:
    def test_identical_questions_collapse(self):
        merged, stats = merge_duplicates([_item() for _ in range(5)])
        assert len(merged) == 1
        assert stats.dropped["同题合并"] == 4
        assert stats.output_count == 1

    def test_different_questions_are_kept(self):
        items = [_item("怎么洗"), _item("会起球吗"), _item("什么面料")]
        merged, _ = merge_duplicates(items)
        assert len(merged) == 3

    def test_same_question_different_product_is_not_a_duplicate(self):
        """同一个问题问不同商品，答案可能完全不同。

        "这件怎么洗"对羊毛衫和对速干衣是两条知识，合并会张冠李戴。
        """
        merged, _ = merge_duplicates([
            _item(product_ids=[1024]), _item(product_ids=[2048])
        ])
        assert len(merged) == 2

    def test_keeps_the_most_confident(self):
        merged, _ = merge_duplicates([
            _item(confidence=0.5, content="手洗"),
            _item(confidence=0.9, content="建议手洗，水温不超过30度"),
            _item(confidence=0.6, content="手洗就行"),
        ])
        assert "水温不超过30度" in merged[0].content

    def test_longer_content_breaks_ties(self):
        """置信度相同时留更完整的那条。

        "水温不超过30度"比"手洗"多一个限定条件，对用户更有用。
        """
        merged, _ = merge_duplicates([
            _item(content="手洗"),
            _item(content="建议手洗，水温不超过30度，不要拧干"),
        ])
        assert len(merged[0].content) > 10

    def test_tags_are_unioned_not_overwritten(self):
        """不同抽取各自标了一部分标签，合并要取并集。"""
        merged, _ = merge_duplicates([
            _item(tags=["养护"], confidence=0.9),
            _item(tags=["材质"], confidence=0.5),
        ])
        assert set(merged[0].tags) == {"养护", "材质"}

    def test_category_is_recovered_from_siblings(self):
        """代表条目没类目而兄弟有，别浪费这个信息——
        没有类目，覆盖度矩阵就漏算它。"""
        merged, _ = merge_duplicates([
            _item(confidence=0.9, category_id=None),
            _item(confidence=0.5, category_id=9001),
        ])
        assert merged[0].category_id == 9001

    def test_empty_input(self):
        merged, stats = merge_duplicates([])
        assert merged == [] and stats.output_count == 0


class TestCorroboration:
    """多信源印证提高置信度——但不能无限提高。"""

    def test_agreement_raises_confidence(self):
        """五段不同对话都说"建议手洗"，这是证据，不是噪音。

        丢掉四条再按单条的置信度判断，等于把证据扔了。
        """
        one, _ = merge_duplicates([_item(confidence=0.8)])
        many, _ = merge_duplicates([_item(confidence=0.8) for _ in range(4)])
        assert many[0].confidence > one[0].confidence

    def test_repetition_cannot_manufacture_certainty(self):
        """**这条最重要。**

        一个被抽出一百次、但模型每次只给 0.35 置信度的条目，不该被
        印证加成推到自动通过。"模型一百次都拿不准"不是"确定"的证据，
        只是同一个不确定重复了一百遍——把相关证据当独立证据累加，
        是这类打分最容易犯的错。
        """
        merged, _ = merge_duplicates([_item(confidence=0.35) for _ in range(100)])
        assert merged[0].confidence <= 0.35 + MAX_BONUS
        assert merged[0].confidence < 0.6, "仍该进人工队列，而不是自动通过"

    def test_bonus_grows_then_stops(self):
        """加成随印证数增长，撞到上限后就不再动。

        4 条只有 3 个印证（3×0.03=0.09，未封顶）；
        5 条就是 4 个印证（0.12 → 封到 0.10），之后再多也一样。
        """
        def conf(n: int) -> float:
            merged, _ = merge_duplicates([_item(confidence=0.5) for _ in range(n)])
            return merged[0].confidence

        assert conf(1) == 0.5
        assert 0.5 < conf(4) < 0.5 + MAX_BONUS
        assert conf(5) == pytest.approx(0.5 + MAX_BONUS)
        assert conf(40) == pytest.approx(conf(5)), "封顶后不该再涨"

    def test_never_reaches_absolute_certainty(self):
        """重复得多只说明大家都这么说，不代表答案是对的。"""
        merged, _ = merge_duplicates([_item(confidence=0.97) for _ in range(50)])
        assert merged[0].confidence <= MAX_CONFIDENCE < 1.0

    def test_single_item_is_untouched(self):
        merged, _ = merge_duplicates([_item(confidence=0.72)])
        assert merged[0].confidence == 0.72


class TestDuplicateRatio:
    def test_all_unique(self):
        assert duplicate_ratio([_item(f"问题{i}") for i in range(5)]) == 0.0

    def test_all_same(self):
        assert duplicate_ratio([_item() for _ in range(5)]) == pytest.approx(0.8)

    def test_empty(self):
        assert duplicate_ratio([]) == 0.0


class TestInPipeline:
    """在真实编排里必须生效——单测过了但没接进流水线，等于没做。"""

    def test_pipeline_output_has_no_duplicates(self):
        from smartmall_pipeline.gates import gate3_model
        from smartmall_pipeline.ingest import synthetic
        from smartmall_pipeline.orchestrator import run_pipeline

        out = run_pipeline(
            synthetic.generate_batch(150, seed=53),
            gate3_model.FakeLlmClient(),
            batch_id="dd-1",
        )
        everything = out.knowledge_items + out.pending_items
        keys = [i.dedup_key() for i in everything]
        assert len(keys) == len(set(keys)), (
            f"出口仍有 {len(keys) - len(set(keys))} 条重复知识"
        )

    def test_dedup_runs_before_human_triage(self):
        """顺序有讲究：置信度是自动通过的判据，而印证会提高置信度。

        放到关卡④后面的话，五条同题条目会各自按单条置信度被判断，
        其中四条还会白白占掉人工的抽检名额。
        """
        from smartmall_pipeline.gates import gate3_model
        from smartmall_pipeline.ingest import synthetic
        from smartmall_pipeline.orchestrator import run_pipeline

        out = run_pipeline(
            synthetic.generate_batch(100, seed=59),
            gate3_model.FakeLlmClient(),
            batch_id="dd-2",
        )
        gates = [s.gate for s in out.report.stages]
        assert gates.index("⑤ 知识去重") < gates.index("④ 人工清洗")

    def test_funnel_reports_the_merge(self):
        """漏斗要能看出去掉了多少重复——重复率突然升高说明上游进了脏数据。"""
        from smartmall_pipeline.gates import gate3_model
        from smartmall_pipeline.ingest import synthetic
        from smartmall_pipeline.orchestrator import run_pipeline

        out = run_pipeline(
            synthetic.generate_batch(120, seed=61),
            gate3_model.FakeLlmClient(items_per_dialogue=2),
            batch_id="dd-3",
        )
        line = next(s for s in out.report.stages if s.gate == "⑤ 知识去重")
        assert line.dropped.get("同题合并", 0) > 0
        assert "同题合并" in out.report.render()
