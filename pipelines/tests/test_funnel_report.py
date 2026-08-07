"""漏斗报表的单位正确性。

这份报表是判断"清洗规则调得对不对"的唯一依据——每一关淘汰了多少、
为什么淘汰，全靠它。所以它自己不能骗人。

真实事故：关卡③把 20 段会话拆成 48 条知识，报表拿 48÷20 算出
"240.0% 存活率"，关卡④跟着继承同一个基数也报 240%。存活率超过 100%
是个显然读不通的数字，而它之所以能一直存在，是因为这里一个测试都没有。
"""

from __future__ import annotations

import re

from smartmall_pipeline.gates import gate3_model
from smartmall_pipeline.ingest import synthetic
from smartmall_pipeline.models import FunnelReport, GateStats
from smartmall_pipeline.orchestrator import run_pipeline


def _funnel() -> FunnelReport:
    """还原一次真实运行的形状：会话进、条目出。"""
    r = FunnelReport(batch_id="t1")
    r.add(GateStats(gate="① 机器清洗", input_count=20, output_count=18))
    r.add(GateStats(gate="② 规则清洗", input_count=18, output_count=18))
    g3 = GateStats(
        gate="③ 模型清洗", input_count=18, output_count=48,
        input_unit="会话", output_unit="条目",
    )
    g3.drop("已过期")
    r.add(g3)
    r.add(GateStats(
        gate="④ 人工清洗", input_count=48, output_count=48,
        input_unit="条目", output_unit="条目",
    ))
    return r


def _percentages(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"([\d.]+)% 存活", text)]


class TestSurvivalRate:
    def test_never_exceeds_100_percent(self):
        """存活率超过 100% 说明分子分母不是同一种东西。"""
        for pct in _percentages(_funnel().render()):
            assert pct <= 100.0, f"存活率 {pct}% > 100%，分母用错了单位"

    def test_conversion_stage_reports_ratio_not_percentage(self):
        """会话→条目是换算，不是过滤，报倍率才说得通。"""
        text = _funnel().render()
        line = next(ln for ln in text.splitlines() if "③" in ln)
        assert "条目/会话" in line, f"换算关卡仍在报百分比：{line}"
        assert "% 存活" not in line
        assert "2.67" in line, f"倍率应为 48/18≈2.67：{line}"

    def test_stage_after_conversion_rebases(self):
        """关卡④的分母必须是关卡③的产出（48 条目），不是最初的 20 会话。"""
        line = next(ln for ln in _funnel().render().splitlines() if "④" in ln)
        assert "100.0% 存活" in line, f"基数未随单位切换而重置：{line}"

    def test_both_units_are_shown(self):
        """只标出口单位会让人以为进出是同一种东西。"""
        line = next(ln for ln in _funnel().render().splitlines() if "③" in ln)
        assert "18 会话" in line and "48 条目" in line


class TestDropUnits:
    def test_session_drop_is_labelled_when_it_differs_from_output(self):
        """关卡③淘汰的是会话，出口却是条目，不标单位会被读成"少了 1 条知识"。"""
        line = next(ln for ln in _funnel().render().splitlines() if "③" in ln)
        assert "淘汰 1 会话" in line, line

    def test_same_unit_drop_is_not_over_annotated(self):
        """关卡①进出都是会话，再标一遍单位是噪音。"""
        g = GateStats(gate="①", input_count=10, output_count=7)
        g.drop("近重复", 3)
        assert g.drop_summary == "淘汰 3"

    def test_mixed_units_are_reported_separately(self):
        """整段毙掉的会话和逐条丢弃的条目不能加成一个数。"""
        g = GateStats(
            gate="③", input_count=10, output_count=12,
            input_unit="会话", output_unit="条目",
        )
        g.drop("无可复用知识", 2)              # 整段毙掉 → 会话
        g.drop("抽取置信度过低", 5, unit="条目")  # 逐条丢弃 → 条目
        assert g.drops_by_unit == {"会话": 2, "条目": 5}
        assert "2 会话" in g.drop_summary and "5 条目" in g.drop_summary

    def test_merge_preserves_units(self):
        """跨批次合并不能把单位丢掉，否则汇总报表又变回骗人的。"""
        def _one():
            g = GateStats(
                gate="③", input_count=10, output_count=12,
                input_unit="会话", output_unit="条目",
            )
            g.drop("无可复用知识", 2)
            g.drop("抽取置信度过低", 5, unit="条目")
            return g

        merged = _one().merge(_one())
        assert merged.input_unit == "会话" and merged.output_unit == "条目"
        assert merged.drops_by_unit == {"会话": 4, "条目": 10}


class TestRealPipelineFunnel:
    """端到端跑一遍，报表不能出现读不通的数字。

    上面的用例是手工搭的形状；这个用例保证真实编排产出的形状也对——
    两者对不上时，问题出在关卡自己声明的单位上。
    """

    def test_end_to_end_percentages_are_sane(self):
        dialogues = synthetic.generate_batch(120, seed=31)
        for i, d in enumerate(dialogues, start=1):
            d.ods_id = i
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="e2e")
        text = out.report.render()

        pcts = _percentages(text)
        assert pcts, f"没渲染出任何存活率：\n{text}"
        for pct in pcts:
            assert 0 <= pct <= 100, f"存活率 {pct}% 越界：\n{text}"

    def test_gates_declare_consistent_units(self):
        """相邻两关的单位必须首尾相接，否则基数重置会算错。"""
        dialogues = synthetic.generate_batch(60, seed=37)
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="u1")
        stages = out.report.stages
        for prev, cur in zip(stages, stages[1:]):
            assert cur.input_unit == prev.output_unit, (
                f"{prev.gate} 出口是 {prev.output_unit}，"
                f"{cur.gate} 却声明入口是 {cur.input_unit}"
            )

    def test_counts_chain_across_gates(self):
        """上一关的产出就是下一关的输入，数量必须对得上。"""
        dialogues = synthetic.generate_batch(60, seed=41)
        out = run_pipeline(dialogues, gate3_model.FakeLlmClient(), batch_id="u2")
        stages = out.report.stages
        for prev, cur in zip(stages, stages[1:]):
            assert cur.input_count == prev.output_count, (
                f"{prev.gate} 产出 {prev.output_count}，"
                f"{cur.gate} 却收到 {cur.input_count}"
            )
