"""端到端：合成数据 → 四道关卡 → 知识条目 → 发版 → 覆盖度分析。

这是 M1 的验收测试。它不依赖 MySQL / Milvus / LLM API，
因为四道关卡是纯函数、LLM 客户端可注入——整条流水线的正确性
可以在 CI 里完整验证。
"""

from __future__ import annotations

from smartmall_pipeline import coverage, publish
from smartmall_pipeline.gates import gate3_model, pii
from smartmall_pipeline.ingest import synthetic
from smartmall_pipeline.models import KnowledgeType, ReviewStatus
from smartmall_pipeline.orchestrator import run_pipeline


def test_full_pipeline_produces_publishable_knowledge():
    """完整链路跑通，并在每个环节留下可断言的痕迹。"""
    dialogues = synthetic.generate_batch(300, seed=2026)
    llm = gate3_model.FakeLlmClient(items_per_dialogue=2)

    out = run_pipeline(dialogues, llm, batch_id="e2e-001")
    report = out.report

    # ---- 漏斗形状：逐关递减，每关都真的淘汰了东西 ----
    assert len(report.stages) == 4
    assert report.stages[0].gate.startswith("①")
    assert report.stages[-1].gate.startswith("④")
    for i in range(1, 3):
        assert report.stages[i].input_count == report.stages[i - 1].output_count, (
            f"第{i}关的输入必须等于上一关的输出"
        )
    assert report.stages[0].drop_count > 0, "关卡①未淘汰任何数据"
    assert report.stages[1].modified, "关卡②未做任何修改"

    # ---- 出口 ----
    assert out.knowledge_items, "没有产出任何自动通过的知识"
    assert out.sft_samples, "没有产出微调样本"

    # ---- 合规红线：出口不得残留 PII ----
    for item in out.knowledge_items:
        assert not pii.contains_pii(item.content)
        assert not pii.contains_pii(item.title or "")

    # ---- 溯源：任意一条知识都能追回原始记录 ----
    for item in out.knowledge_items:
        assert item.source_ref, "缺少溯源标识"
        assert item.source_ref.startswith(("ods:", "session:"))

    print("\n" + report.render())


def test_auto_approved_items_are_publishable():
    """自动通过的条目应能通过发版门禁。"""
    dialogues = synthetic.generate_batch(300, seed=7)
    llm = gate3_model.FakeLlmClient(items_per_dialogue=1)
    out = run_pipeline(dialogues, llm, batch_id="e2e-002")

    # 关卡④自动通过的条目状态必须是 approved
    assert all(i.review_status is ReviewStatus.APPROVED for i in out.knowledge_items)

    publishable = publish.select_publishable(out.knowledge_items)
    assert len(publishable) == len(out.knowledge_items)


def test_human_queue_is_a_small_fraction():
    """主动学习的意义：人工只看拿不准的那一小部分。

    若人工队列占比过高，关卡④就没有起到分流作用，人工会成为吞吐瓶颈。
    """
    dialogues = synthetic.generate_batch(300, seed=11)
    llm = gate3_model.FakeLlmClient(items_per_dialogue=1, confidence=0.95)
    out = run_pipeline(dialogues, llm, batch_id="e2e-003")

    total = len(out.knowledge_items) + out.pending_human
    assert total > 0
    assert out.pending_human / total < 0.3, (
        f"人工队列占比 {out.pending_human / total:.1%} 过高，主动学习未生效"
    )


def test_low_confidence_routes_everything_to_human():
    """反过来：模型普遍拿不准时，应当全部交给人工，而不是硬着头皮自动通过。"""
    dialogues = synthetic.generate_batch(100, seed=13)
    llm = gate3_model.FakeLlmClient(items_per_dialogue=1, confidence=0.35)
    out = run_pipeline(dialogues, llm, batch_id="e2e-004")

    assert out.knowledge_items == []
    assert out.pending_human > 0
    assert all(t.priority.name == "P0" for t in out.annotation_tasks)


def test_coverage_matrix_finds_gaps():
    """覆盖度矩阵应能指出知识空白，并生成补写任务。"""
    dialogues = synthetic.generate_batch(200, seed=17)
    llm = gate3_model.FakeLlmClient(items_per_dialogue=1)
    out = run_pipeline(dialogues, llm, batch_id="e2e-005")

    # 合成数据只标了 spec 一种知识类型，其余类型应全部为空白
    for item in out.knowledge_items:
        item.category_id = 1024

    cats = {1024: "针织衫", 1025: "羽绒服"}
    demand = {(1025, KnowledgeType.SIZING): 88}
    matrix = coverage.build_matrix(out.knowledge_items, cats, demand=demand)

    assert matrix.empty_cells, "未识别出任何空白格"
    tasks = coverage.generate_write_tasks(matrix)
    assert tasks
    # 空白 + 有真实需求 = 最高优先级
    assert tasks[0].category_id == 1025
    assert tasks[0].knowledge_type is KnowledgeType.SIZING

    print("\n" + matrix.render())


def test_pipeline_is_deterministic():
    """同一批数据两次跑必须得到相同结果。

    否则同一份 ODS 会产出两个不同的知识库版本，数据资产就不可复现了。
    """
    llm_a = gate3_model.FakeLlmClient(items_per_dialogue=1)
    llm_b = gate3_model.FakeLlmClient(items_per_dialogue=1)

    out_a = run_pipeline(synthetic.generate_batch(150, seed=5), llm_a, batch_id="d1")
    out_b = run_pipeline(synthetic.generate_batch(150, seed=5), llm_b, batch_id="d2")

    assert len(out_a.knowledge_items) == len(out_b.knowledge_items)
    assert out_a.pending_human == out_b.pending_human
    assert [s.output_count for s in out_a.report.stages] == [
        s.output_count for s in out_b.report.stages
    ]
