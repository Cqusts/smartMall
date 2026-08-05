"""四道关卡的编排。

把 gates 里四个纯函数串起来并汇总漏斗报表。
刻意做成不依赖 Airflow 的普通函数——DAG 只是它的一层薄封装，
因此完整流水线可以在 pytest 里跑通，不需要起 Airflow。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .gates import gate1_machine, gate2_rules, gate3_model, gate4_human
from .models import Dialogue, FunnelReport, KnowledgeItem, SftSample


@dataclass
class PipelineConfig:
    gate1: gate1_machine.Gate1Config = field(default_factory=gate1_machine.Gate1Config)
    gate2: gate2_rules.Gate2Config = field(default_factory=gate2_rules.Gate2Config)
    gate3: gate3_model.Gate3Config = field(default_factory=gate3_model.Gate3Config)
    gate4: gate4_human.Gate4Config = field(default_factory=gate4_human.Gate4Config)


@dataclass
class PipelineOutput:
    report: FunnelReport
    knowledge_items: list[KnowledgeItem] = field(default_factory=list)
    """自动通过的条目，可直接进入向量化。"""
    annotation_tasks: list[gate4_human.AnnotationTask] = field(default_factory=list)
    """需人工处理的任务，推送 Label Studio。"""
    sft_samples: list[SftSample] = field(default_factory=list)
    ods_outcomes: dict[int, tuple[str, str | None]] = field(default_factory=dict)
    """``{ods_id: (outcome, 被淘汰的关卡)}``。

    没有它，"哪些 ODS 记录已处理"就无从判断——被关卡①②淘汰的对话
    根本到不了 DWD，靠 JOIN DWD 会把它们永远当成待处理，每次清洗
    重新处理一遍，产出成倍的重复知识。
    """

    @property
    def pending_human(self) -> int:
        return len(self.annotation_tasks)


def run_pipeline(
    dialogues: Sequence[Dialogue],
    llm: gate3_model.LlmClient,
    *,
    batch_id: str,
    config: PipelineConfig | None = None,
    product_attrs: dict[int, dict[str, str]] | None = None,
    question_freq: dict[str, int] | None = None,
    product_category: dict[int, int] | None = None,
) -> PipelineOutput:
    """跑完四道关卡。

    每一关的输入是上一关的输出，统计逐关累计成漏斗报表——
    对应验收标准「各关卡的淘汰量有统计报表」。
    """
    cfg = config or PipelineConfig()
    report = FunnelReport(batch_id=batch_id)

    def _ids(dlgs) -> set[int]:
        return {d.ods_id for d in dlgs if d.ods_id is not None}

    incoming = list(dialogues)
    outcomes: dict[int, tuple[str, str | None]] = {}

    after1, s1 = gate1_machine.run(incoming, cfg.gate1)
    report.add(s1)
    for ods_id in _ids(incoming) - _ids(after1):
        outcomes[ods_id] = ("dropped", s1.gate)

    after2, s2 = gate2_rules.run(after1, cfg.gate2)
    report.add(s2)
    for ods_id in _ids(after1) - _ids(after2):
        outcomes[ods_id] = ("dropped", s2.gate)

    items, samples, s3 = gate3_model.run(
        after2, llm, cfg.gate3, product_category=product_category
    )
    report.add(s3)
    # 关卡③按会话判定，产出条目的会话算通过；未产出的算淘汰
    produced = {
        int(i.source_ref.split(":", 1)[1])
        for i in items
        if i.source_ref and i.source_ref.startswith("ods:")
    }
    for ods_id in _ids(after2):
        outcomes[ods_id] = (
            ("passed", None) if ods_id in produced else ("dropped", s3.gate)
        )

    tasks, auto_approved, s4 = gate4_human.triage(
        items, cfg.gate4, product_attrs=product_attrs, question_freq=question_freq
    )
    report.add(s4)

    report.knowledge_items = len(auto_approved)
    report.sft_samples = len(samples)

    return PipelineOutput(
        report=report,
        knowledge_items=auto_approved,
        annotation_tasks=tasks,
        sft_samples=samples,
        ods_outcomes=outcomes,
    )
