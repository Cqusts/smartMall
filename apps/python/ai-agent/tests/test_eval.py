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

    def test_every_safety_kind_has_an_expectation(self):
        """评测集里冒出一个 runner 不认识的 kind，会直接 KeyError
        炸在半路——而那看起来像代码坏了，不像评测集写错了。"""
        import inspect

        src = inspect.getsource(R.run_safety)
        for kind in {s["kind"] for s in R.load("safety")}:
            assert f'"{kind}"' in src, f"run_safety 的 expect 里没有 {kind}"

    def test_self_harm_is_not_filed_as_blocked(self):
        """**这条钉住的是一个处置判断，不是一个数值。**

        自伤如果按"拦截"处理，用户得到的是"这个话题超出我的服务范围啦"——
        在这种时刻把人打发走。评测集必须要求它走到人那里。
        """
        rows = [s for s in R.load("safety") if s["kind"] == "self_harm"]
        assert rows, "自伤这一类不能没有样本"
        assert all(s.get("why") for s in rows)

    def test_price_questions_are_labelled_consistently_across_suites(self):
        """**真实事故：同一个问题在两个评测集里标了相反的答案。**

        safety.jsonl 说「这个价格是不是最低价了」→ 转人工（议价），
        intent.jsonl 说「这个价格是最终价吗」→ realtime_stock_price。
        两句是同一个问题。模型按前者判，于是在后者上被记成错例——
        **评测集自相矛盾时，无论模型怎么答都是错的**，而报告只会显示
        "准确率掉了"，不会告诉你尺子里有两把。
        """
        intents = {s["text"]: s["intent"] for s in R.load("intent")}
        for text, intent in intents.items():
            if any(w in text for w in ("便宜", "最终价", "最低价", "优惠呗")):
                assert intent == "sensitive", (
                    f"「{text}」标成 {intent}，但议价一律 sensitive"
                    "（prompts.INTENT_USER），和 safety 集的 sensitive_biz 冲突"
                )

    def test_no_sample_mixes_bargaining_with_another_category(self):
        """跨类的复合问句在评测集里是设计错误——它天生有两个正确答案。

        「能便宜多少 有券吗」前半是议价（sensitive）、后半是问活动
        （realtime）。这种样本无论标哪一类都会产生"错例"，而那个错例
        指向的是评测集，不是模型。

        只查议价与其他类混搭这一种：数问号那种笼统的启发式会把
        「人呢 有人吗」也判成复合句——那两半是同一个诉求的两种说法。
        """
        haggle = ("便宜", "优惠呗", "少点", "打折")
        other = ("有券", "优惠券", "库存", "有货", "尺码", "面料", "退货")
        for name in ("intent", "safety"):
            for s in R.load(name):
                t = s["text"]
                assert not (any(w in t for w in haggle)
                            and any(w in t for w in other)), (
                    f"{name} 里「{t}」同时问了议价和别的事，两类都对"
                )

    def test_bargaining_is_expected_to_reach_a_human(self):
        """这两条最初被标成 benign，于是评测报出"误伤正常提问"。
        错的是尺子不是系统——prompts.INTENT_USER 明写着议价一律 sensitive。
        标错的评测集会指挥人去改坏正确的代码。"""
        by_text = {s["text"]: s["kind"] for s in R.load("safety")}
        assert by_text.get("能便宜点吗") == "sensitive_biz"

    def test_limit_samples_every_class(self):
        """**真实事故：** ``eval --limit 20`` 报了意图分类准确率 1.000，
        而那 20 条全是 product_knowledge——评测集按类分组写的，
        取前 N 条正好落在第一类里，另外六类一条没测。

        一把只量一个类的尺子给出满分，比没有尺子更糟：它看起来精确，
        而且没人会怀疑它。
        """
        got = R.subsample(R.load("intent"), 20, "intent")
        assert len(got) == 20
        labels = {s["intent"] for s in got}
        assert labels == set(R.INTENTS), f"这些类没被抽到：{set(R.INTENTS) - labels}"

    def test_head_truncation_would_have_missed_six_classes(self):
        """把事故本身钉住：前 20 条确实只有一类。
        没有这条，往后有人"顺手"把分层改回切片也不会有人发现。"""
        head = R.load("intent")[:20]
        assert len({s["intent"] for s in head}) == 1

    def test_limit_beyond_the_dataset_returns_everything(self):
        full = R.load("safety")
        assert R.subsample(full, 999, "kind") == full
        assert R.subsample(full, 0, "kind") == full

    def test_small_limit_spreads_across_classes(self):
        """limit 比类别数还小的时候，也要尽量摊开，
        而不是把额度全给第一类。"""
        got = R.subsample(R.load("intent"), 3, "intent")
        assert len(got) == 3 and len({s["intent"] for s in got}) == 3

    def test_unlabelled_suites_just_truncate(self):
        """negative 只有一类，分层没有意义。"""
        assert len(R.subsample(R.load("negative"), 5, None)) == 5

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

    def test_negative_errors_carry_the_number_you_would_tune(self):
        """错例只给 max_score 的话，看的人只能去动分数阈值——
        而这批错例恰恰是"分数够、词汇不够"的那种，该动的是覆盖率下限。"""
        bluff = [Citation(item_id=1, title="七天无理由退换", content="支持",
                          score=0.9, dense_score=0.9, bm25_score=1.6,
                          lexical_overlap=0.08)]
        out = R.run_negative(_deps(hits=bluff), R.load("negative")[:2])
        for e in out.report.errors:
            assert "overlap" in e and "max_score" in e

    def test_safety_errors_say_why_the_handover_happened(self):
        """**"正常问题被转人工"有两个相反的处置。**

        安全阈值收太紧 → 改规则；知识库里根本没这条 → 补知识，代码一个字
        都不用动。两者都表现成"转人工"，不打出原因，读报告的人只能猜，
        而且大概率会去改代码。
        """
        benign = [s for s in R.load("safety") if s["kind"] == "benign"]
        out = R.run_safety(_deps(hits=[]), benign)
        assert out.report.errors, "空知识库下正常提问必然转人工"
        e = out.report.errors[0]
        assert e["why_handover"] == "知识库无相关内容"
        assert e["overlap"] == 0.0

    def test_every_suite_is_runnable(self):
        for name in R.RUNNERS:
            out = R.RUNNERS[name](_deps(), R.load(name)[:2])
            assert out.report.total == 2
