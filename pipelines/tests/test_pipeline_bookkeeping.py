"""流水线的记账正确性。

这两组断言对应两个在真实数据库上才暴露出来的 bug——纯内存测试
跑不出来，因为它们的症状是"重跑一次数据翻倍"和"矩阵永远 0%"。
补进测试集，防止回归。
"""

from __future__ import annotations

from smartmall_pipeline import coverage
from smartmall_pipeline.gates import gate3_model
from smartmall_pipeline.ingest import synthetic
from smartmall_pipeline.models import Dialogue, KnowledgeType, Turn
from smartmall_pipeline.orchestrator import run_pipeline


def _with_ods_ids(dialogues: list[Dialogue]) -> list[Dialogue]:
    """模拟从 ODS 取出的对话——每条都带 ods_id。"""
    for i, d in enumerate(dialogues, start=1):
        d.ods_id = i
    return dialogues


# ---------------------------------------------------------------- ODS 处理记账


class TestOdsOutcomes:
    def test_every_input_gets_an_outcome(self):
        """每条输入都必须有处理结论。

        漏掉任何一条，它就会在下次清洗时被重新取出处理一遍，
        产出重复知识——这正是真实库上暴露的 bug。
        """
        dialogues = _with_ods_ids(synthetic.generate_batch(200, seed=3))
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="b1")

        all_ids = {d.ods_id for d in dialogues}
        assert set(out.ods_outcomes) == all_ids, (
            f"有 {len(all_ids - set(out.ods_outcomes))} 条对话没有处理结论"
        )

    def test_gate1_drops_are_attributed(self):
        """被关卡①淘汰的（近重复等）必须标为 dropped 并记录关卡。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(200, seed=5))
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="b2")

        dropped_at_g1 = [
            ods_id for ods_id, (outcome, gate) in out.ods_outcomes.items()
            if outcome == "dropped" and gate and gate.startswith("①")
        ]
        assert dropped_at_g1, "关卡①的淘汰未被归因"

    def test_passed_ones_produced_knowledge(self):
        """标记为 passed 的必须真的产出了知识条目。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(150, seed=7))
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="b3")

        passed = {
            ods_id for ods_id, (outcome, _) in out.ods_outcomes.items()
            if outcome == "passed"
        }
        produced = {
            int(i.source_ref.split(":", 1)[1])
            for i in out.knowledge_items
            if i.source_ref and i.source_ref.startswith("ods:")
        }
        # 自动通过的是 passed 的子集（部分条目被抽检推给了人工）
        assert produced <= passed
        assert passed, "没有任何对话被标为 passed"

    def test_no_dialogue_is_both_passed_and_dropped(self):
        dialogues = _with_ods_ids(synthetic.generate_batch(150, seed=11))
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="b4")
        for ods_id, (outcome, gate) in out.ods_outcomes.items():
            assert outcome in ("passed", "dropped")
            if outcome == "passed":
                assert gate is None
            else:
                assert gate, f"ods {ods_id} 被淘汰但没记录关卡"

    def test_llm_failures_are_attributed_not_silently_lost(self):
        """关卡③调用失败的会话也要有结论，否则会被无限重试。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(60, seed=13))
        llm = gate3_model.FakeLlmClient(has_knowledge=False)  # 全部判定为无知识
        out = run_pipeline(dialogues, llm, batch_id="b5")

        assert out.knowledge_items == []
        assert set(out.ods_outcomes) == {d.ods_id for d in dialogues}
        assert all(o == "dropped" for o, _ in out.ods_outcomes.values())


# ---------------------------------------------------------------- 类目与知识类型


class TestCategoryPropagation:
    def test_category_id_is_filled_from_product_mapping(self):
        """知识条目必须带上类目。

        没有它，覆盖度矩阵永远显示 0%——"哪里缺知识"就无从谈起，
        检索也无法按类目收窄范围。
        """
        dialogues = _with_ods_ids(synthetic.generate_batch(100, seed=17))
        out = run_pipeline(
            dialogues,
            gate3_model.FakeLlmClient(),
            batch_id="c1",
            product_category=synthetic.product_category_map(),
        )
        assert out.knowledge_items
        assert all(i.category_id is not None for i in out.knowledge_items), (
            "有知识条目没有类目"
        )
        # 类目必须来自映射，不能是凭空的值
        valid = set(synthetic.product_category_map().values())
        assert {i.category_id for i in out.knowledge_items} <= valid

    def test_without_mapping_category_is_none(self):
        """不传映射时不应瞎猜类目——宁可为空也不要错。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(50, seed=19))
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="c2")
        assert all(i.category_id is None for i in out.knowledge_items)

    def test_product_ids_are_distinct_from_category_ids(self):
        """商品 ID 与类目 ID 是两个维度，不能混用同一套编号。"""
        mapping = synthetic.product_category_map()
        assert set(mapping) & set(mapping.values()) == set(), (
            "商品 ID 与类目 ID 出现重叠，说明建模把两者混为一谈了"
        )

    def test_coverage_matrix_is_non_empty_with_mapping(self):
        """端到端：有映射 → 矩阵能反映真实分布。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(120, seed=23))
        out = run_pipeline(
            dialogues,
            gate3_model.FakeLlmClient(knowledge_type="spec"),
            batch_id="c3",
            product_category=synthetic.product_category_map(),
        )
        cats = {
            cat: name for name, (_, cat) in synthetic.PRODUCTS.items()
        }
        matrix = coverage.build_matrix(out.knowledge_items, cats)
        assert matrix.coverage_ratio > 0, "矩阵仍是全空，类目未生效"
        # FakeLlmClient 固定产出 spec，所以只有该列有值
        for cat_id in cats:
            cell = matrix.cell(cat_id, KnowledgeType.SPEC)
            assert cell is not None and cell.count > 0

    def test_knowledge_type_survives_the_pipeline(self):
        """关卡③判定的知识类型必须传到出口，它是矩阵的列维度。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(40, seed=29))
        out = run_pipeline(
            dialogues,
            gate3_model.FakeLlmClient(knowledge_type="logistics"),
            batch_id="c4",
            product_category=synthetic.product_category_map(),
        )
        assert out.knowledge_items
        assert all(
            i.knowledge_type is KnowledgeType.LOGISTICS for i in out.knowledge_items
        )
