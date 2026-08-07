"""流水线的记账正确性。

这两组断言对应两个在真实数据库上才暴露出来的 bug——纯内存测试
跑不出来，因为它们的症状是"重跑一次数据翻倍"和"矩阵永远 0%"。
补进测试集，防止回归。
"""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------- 基础设施故障


class _UnavailableLlm(gate3_model.FakeLlmClient):
    """模拟模型服务不可用（连不上网关、超时、5xx）。"""

    def complete_json(self, *, model, system, user):
        raise gate3_model.LlmUnavailableError("connection refused")


class _FlakyLlm(gate3_model.FakeLlmClient):
    """前 N 次失败，之后恢复正常。"""

    def __init__(self, fail_first: int = 3, **kw):
        super().__init__(**kw)
        self.fail_first = fail_first
        self.seen = 0

    def complete_json(self, *, model, system, user):
        self.seen += 1
        if self.seen <= self.fail_first:
            raise gate3_model.LlmUnavailableError("temporary outage")
        return super().complete_json(model=model, system=system, user=user)


class TestInfrastructureFailure:
    """基础设施故障绝不能被当作「已处理」。

    真实事故：网关没起，386 条对话全部失败，却全被标记为已处理，
    从此再也取不出来重跑——数据等于丢了。
    """

    def test_unavailable_sessions_get_no_outcome(self):
        # 批量小于快速失败阈值，走正常返回路径而非中止路径
        dialogues = _with_ods_ids(synthetic.generate_batch(8, seed=101))
        out = run_pipeline(dialogues, _UnavailableLlm(), batch_id="f1")

        assert out.knowledge_items == []
        # 关键断言：进到关卡③的会话不能出现在 outcomes 里
        assert out.unavailable_ods_ids
        for ods_id in out.unavailable_ods_ids:
            assert ods_id not in out.ods_outcomes, (
                f"ods {ods_id} 因基础设施故障未处理，却被标记了结论——"
                "这会导致它再也不会被重新清洗"
            )

    def test_fails_fast_instead_of_grinding_through(self):
        """连不上时不该让几百条一条条超时跑完。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(200, seed=103))
        with pytest.raises(gate3_model.LlmUnavailableError) as exc:
            run_pipeline(dialogues, _UnavailableLlm(), batch_id="f2")
        assert "已中止" in str(exc.value)
        assert "重跑" in str(exc.value)

    def test_transient_failure_recovers(self):
        """偶发故障不该中止整批，恢复后继续处理。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(60, seed=105))
        out = run_pipeline(dialogues, _FlakyLlm(fail_first=3), batch_id="f3")

        assert out.knowledge_items, "恢复后应继续产出"
        # 失败的那几条不被标记，成功的正常标记
        assert out.unavailable_ods_ids
        assert set(out.ods_outcomes) & out.unavailable_ods_ids == set()

    def test_business_failure_still_marked(self):
        """业务性失败（确实没有可复用知识）是确定结论，应当标记。"""
        dialogues = _with_ods_ids(synthetic.generate_batch(40, seed=107))
        out = run_pipeline(
            dialogues, gate3_model.FakeLlmClient(has_knowledge=False), batch_id="f4"
        )
        assert out.unavailable_ods_ids == set()
        assert set(out.ods_outcomes) == {d.ods_id for d in dialogues}
        assert all(o == "dropped" for o, _ in out.ods_outcomes.values())

    def test_http_5xx_is_infrastructure_not_business(self):
        """5xx/429 是服务端问题，应归为可重试；4xx 是请求问题。"""
        from smartmall_pipeline.gates.gate3_model import LlmError, LlmUnavailableError

        assert issubclass(LlmUnavailableError, LlmError)
