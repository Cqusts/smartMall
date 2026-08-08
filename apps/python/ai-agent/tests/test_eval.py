"""评测器本身也要被测。

评测是判断系统好坏的尺子，尺子刻错了比没有尺子更糟——它会给出一个
看起来精确的错误结论，而且没人会怀疑它。

指标层是纯函数，可以直接断言；评测集本身也要检查（类别是否齐全、
有没有混进重复样本、困难例占比够不够）。
"""

from __future__ import annotations

import pytest

from app.agent.llm import FakeLlmClient
from app.agent.nodes import Deps
from app.agent.retriever import StubRetriever
from app.agent.state import Citation, Intent
from app.eval import runner as R
from app.agent.textwidth import display_width
from app.eval.metrics import (
    check_gates, classify_report, render_gates, render_report,
)


def _col(line: str, needle: str) -> int:
    """``needle`` 在这一行的**显示列**。

    不能用 ``line.index()``——那是字符下标。"转人工"补齐后是 3 字 18 空格，
    "硬答"是 2 字 20 空格，字符下标差 1，而屏幕上两者是对齐的。
    这条测试的第一版就是这么误报的。
    """
    return display_width(line[: line.index(needle)])


# ---------------------------------------------------------------- 指标


class TestClassificationMetrics:
    def test_perfect(self):
        rep = classify_report([("a", "a"), ("b", "b")])
        assert rep.accuracy == 1.0 and rep.macro_f1 == 1.0

    def test_accuracy_hides_a_collapsed_class(self):
        """**这条是逐类指标存在的理由。**

        九条 a 全对、一条 b 全错，准确率 0.9 看着不错；
        但 b 类召回是 0——如果 b 是 sensitive（该转人工的），
        这意味着每一条都漏了。总分完全看不出来。
        """
        pairs = [("a", "a")] * 9 + [("b", "a")]
        rep = classify_report(pairs)

        assert rep.accuracy == 0.9
        assert rep.per_class["b"].recall == 0.0
        assert rep.macro_f1 < 0.6, "macro-F1 要能反映出这种坍塌"
        assert rep.worst_class.label == "b"

    def test_macro_f1_is_not_weighted_by_support(self):
        """加权平均会被大类淹没，而小类往往才是关键的那个。"""
        pairs = [("big", "big")] * 50 + [("small", "big")] * 2
        rep = classify_report(pairs)
        assert rep.accuracy > 0.95
        assert rep.macro_f1 < 0.7

    def test_confusion_says_where_it_went_wrong(self):
        """「sizing 被判成 product_knowledge」和「被判成 chitchat」
        是两个完全不同的问题，要能分辨。"""
        pairs = [("sizing", "product_knowledge")] * 3 + [("sizing", "chitchat")]
        rep = classify_report(pairs)
        top = rep.top_confusions()
        assert top[0] == ("sizing", "product_knowledge", 3)

    def test_precision_versus_recall(self):
        # 两条 b 被误判成 a：a 的精确率被拉低（误收），b 的召回归零（漏判）
        rep = classify_report([("a", "a"), ("b", "a"), ("b", "a")])
        assert rep.per_class["a"].recall == 1.0
        assert rep.per_class["a"].precision < 0.5
        assert rep.per_class["b"].recall == 0.0

    def test_labels_with_no_samples_are_kept(self):
        """评测集里一条都没有的类要保留在报告里——
        「这一类根本没测到」本身就是要暴露的信息。"""
        rep = classify_report([("a", "a")], labels=["a", "b", "c"])
        assert set(rep.per_class) == {"a", "b", "c"}
        assert rep.per_class["b"].support == 0

    def test_empty(self):
        rep = classify_report([])
        assert rep.accuracy == 0.0 and rep.macro_f1 == 0.0

    def test_render_is_readable(self):
        out = render_report(classify_report([("a", "a"), ("b", "a")]))
        assert "准确率" in out and "macro-F1" in out and "混淆" in out

    def test_columns_line_up_with_chinese_labels(self):
        """``f"{'转人工':<24}"`` 按**字符数**补齐，终端按**列宽**排版，
        汉字占两列——于是每一行都按自己标签的长度错开。

        评测报告的表格几乎全是中文类名（拦截/转人工/正常），不过这一层
        就没有一行是对齐的。
        """
        rep = classify_report([("转人工", "转人工"), ("硬答", "硬答")])
        rows = [ln for ln in render_report(rep).splitlines()
                if "转人工" in ln or "硬答" in ln]
        assert len(rows) == 2
        assert len({_col(ln, "1.000") for ln in rows}) == 1, f"数字列没对齐：{rows}"

    def test_gate_lines_line_up_too(self):
        rep = classify_report([("a", "a")] * 10)
        out = render_gates(check_gates(rep, accuracy_min=0.85, macro_f1_min=0.80))
        rows = [ln for ln in out.splitlines() if "/ 需" in ln]
        assert len({_col(ln, "/ 需") for ln in rows}) == 1, rows


class TestGates:
    def test_per_class_floor_catches_what_the_average_misses(self):
        """七类平均 0.87，但 sensitive 类 F1 只有 0.3——
        该转人工的没转，而总分门禁完全放行。"""
        pairs = [("a", "a")] * 45 + [("b", "b")] * 4 + [("b", "a")] * 6
        rep = classify_report(pairs)

        assert rep.accuracy >= 0.85
        gates = check_gates(rep, accuracy_min=0.85, per_class_f1_min=0.60)
        assert gates[0].passed, "总分是过的"
        assert not gates[-1].passed, "逐类下限必须拦住"

    def test_all_pass_when_healthy(self):
        rep = classify_report([("a", "a")] * 10 + [("b", "b")] * 10)
        gates = check_gates(rep, accuracy_min=0.85, macro_f1_min=0.80,
                            per_class_f1_min=0.60)
        assert all(g.passed for g in gates)


class TestBaselineComparison:
    def test_regression_is_flagged(self):
        """退步比绝对值低更值得警惕——通常意味着刚改的东西弄坏了什么。"""
        rep = classify_report([("a", "a")] * 8 + [("a", "b")] * 2)  # 0.80
        note = R.compare_baseline("intent", rep, {"intent": {"accuracy": 0.90}})
        assert note.startswith("✗") and "退步" in note

    def test_small_dip_is_tolerated(self):
        rep = classify_report([("a", "a")] * 89 + [("a", "b")] * 11)
        note = R.compare_baseline("intent", rep, {"intent": {"accuracy": 0.90}})
        assert not note.startswith("✗")

    def test_no_baseline_is_not_a_failure(self):
        assert R.compare_baseline("intent", classify_report([("a", "a")]), {}) is None


# ---------------------------------------------------------------- 评测集


class TestDatasets:
    def test_intent_covers_every_class(self):
        """漏掉一类就等于没测那一类，而报告不会主动告诉你。"""
        labels = {s["intent"] for s in R.load("intent")}
        assert labels == set(R.INTENTS), f"缺少：{set(R.INTENTS) - labels}"

    def test_every_intent_has_enough_support(self):
        """支撑度太小的类，指标本身就不可信——
        5 条样本错一条就掉 20 个点。"""
        from collections import Counter

        counts = Counter(s["intent"] for s in R.load("intent"))
        thin = {k: v for k, v in counts.items() if v < 10}
        assert not thin, f"这些类样本太少，指标不可信：{thin}"

    def test_no_duplicate_samples(self):
        """重样本会让某几条的对错被放大计入。"""
        for name in ("intent", "negative", "safety"):
            texts = [s["text"] for s in R.load(name)]
            dupes = {t for t in texts if texts.count(t) > 1}
            assert not dupes, f"{name} 有重复样本：{dupes}"

    def test_intent_includes_the_hard_boundary_cases(self):
        """全是简单样本的评测集给出的分数没有意义。

        这几条是类别边界上的：价格问题属于实时而非商品知识，
        退货运费属于售后而非物流——分不清这些的分类器是没用的。
        """
        by_text = {s["text"]: s["intent"] for s in R.load("intent")}
        assert by_text.get("多少钱") == "realtime_stock_price"
        assert by_text.get("退货运费谁承担") == "aftersale"
        assert by_text.get("我这单退款到账了吗") == "order_logistics"

    def test_safety_includes_benign_samples(self):
        """只测拦截率会诱导把阈值调死——全拦掉就是 100%。
        正常样本必须在同一个评测集里。"""
        kinds = {s["kind"] for s in R.load("safety")}
        assert "benign" in kinds and "injection" in kinds

    def test_negative_samples_carry_a_reason(self):
        """每条都要写明"为什么知识库里没有"，
        否则半年后没人敢改这个评测集。"""
        assert all(s.get("why") for s in R.load("negative"))


# ---------------------------------------------------------------- 评测流程


def _deps(intent="product_knowledge", hits=None) -> Deps:
    return Deps(
        llm=FakeLlmClient(intent=intent),
        retriever=StubRetriever(hits if hits is not None else []),
    )


@pytest.fixture
def populated() -> Deps:
    """知识库非空的一套依赖。"""
    hit = Citation(item_id=1, title="常见问题", content="答案",
                   score=0.85, dense_score=0.85, bm25_score=2.0)
    return Deps(llm=FakeLlmClient(), retriever=StubRetriever([hit]))


class TestRunners:
    def test_intent_runner_scores_a_perfect_stub(self):
        """替身固定返回 product_knowledge，所以只喂该类样本应当满分。"""
        samples = [s for s in R.load("intent")
                   if s["intent"] == "product_knowledge"][:5]
        out = R.run_intent(_deps(), samples)
        assert out.report.accuracy == 1.0 and out.passed

    def test_intent_runner_records_errors_not_just_a_score(self):
        """错例是报告里最有用的部分——总分说行不行，错例说改哪里。"""
        samples = [s for s in R.load("intent") if s["intent"] == "sizing"][:3]
        out = R.run_intent(_deps(intent="product_knowledge"), samples)
        assert out.report.accuracy == 0.0
        assert out.report.errors[0]["truth"] == "sizing"
        assert out.report.errors[0]["pred"] == "product_knowledge"

    def test_negative_runner_wants_handover(self):
        """没有任何命中时必须转人工。"""
        out = R.run_negative(_deps(hits=[]), R.load("negative")[:3])
        assert out.report.accuracy == 1.0 and out.passed

    def test_negative_runner_fails_when_the_agent_bluffs(self):
        """命中一堆高分但不相关的内容而硬答——这正是实测抓到的失败模式。"""
        bluff = [Citation(item_id=1, title="七天无理由退换", content="支持",
                          score=0.9, dense_score=0.9, bm25_score=1.0)]
        out = R.run_negative(_deps(hits=bluff), R.load("negative")[:3])
        assert out.report.accuracy == 0.0 and not out.passed
        assert out.report.errors[0]["why"], "错例要带上「本该没有」的理由"

    def test_safety_runner_checks_both_directions(self, populated):
        """正常提问必须能正常答——只测拦截率会诱导把阈值调死。

        这一项要求知识库非空：空库时正常提问会因为检索不到而转人工，
        看起来像误伤，实际是数据没准备好。所以这里给一个有命中的检索。
        """
        out = R.run_safety(populated, R.load("safety"))
        assert out.extra["leaked"] == 0, "违禁内容一条都不能漏"
        assert out.report.accuracy > 0.8

    def test_leak_gate_is_absolute(self, populated):
        """漏放是红线：拦截率可以不满分（误伤会拉低准确率），
        但违禁内容一条都不能放过去。"""
        out = R.run_safety(populated, R.load("safety"))
        leak_gate = [g for g in out.gates if g.name == "违禁漏放"]
        assert leak_gate and leak_gate[0].threshold == 0.0

    def test_every_suite_is_runnable(self):
        for name in R.RUNNERS:
            out = R.RUNNERS[name](_deps(), R.load(name)[:2])
            assert out.report.total == 2
