"""评测指标。

**为什么不只报准确率。** 准确率 0.85 有两种完全不同的情况：
七类各错一点，和某一类全军覆没而其余全对。前者是模型能力问题，
后者是提示词漏了一类的定义——处置完全不同，而单看一个数字分不出来。

所以这里必然要有混淆矩阵与逐类 P/R/F1。它们不是"更全面"，
是**换一类问题才答得上**。

这一层是纯函数，不碰模型也不碰数据库，因此可以直接单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..agent.textwidth import pad as _pad


@dataclass
class ClassMetrics:
    label: str
    support: int = 0
    """真实属于该类的样本数。支撑度小的类，指标本身就不可信。"""
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        """判成这一类的里面，有多少判对了。低说明**误收**。"""
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        """真属于这一类的里面，有多少被找出来了。低说明**漏判**。"""
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class ClassificationReport:
    total: int = 0
    correct: int = 0
    per_class: dict[str, ClassMetrics] = field(default_factory=dict)
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)
    """``{(真实, 预测): 次数}``。看它才知道**错到哪去了**——
    "sizing 被判成 product_knowledge"和"被判成 chitchat"
    是两个完全不同的问题。"""
    errors: list[dict] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def macro_f1(self) -> float:
        """各类 F1 的算术平均，**不按支撑度加权**。

        加权平均会被大类淹没：占一半样本的类做得好，就能把某个
        小类的全军覆没盖过去。而小类往往正是最需要看的那个
        （比如 sensitive——漏判一条就是该转人工没转）。
        """
        scored = [m for m in self.per_class.values() if m.support]
        if not scored:
            return 0.0
        # 只对**有样本**的类求平均。没样本的类不是"考了 0 分"，
        # 是"根本没考"——把两者混为一谈会让部分运行（--limit、
        # 单类调试）的分数毫无意义，而报告里仍然会列出它们，
        # 「这一类没测到」本身就是要暴露的信息。
        return sum(m.f1 for m in scored) / len(scored)

    @property
    def worst_class(self) -> ClassMetrics | None:
        """F1 最低的类。门禁只看总分会放过"某一类全错"。"""
        real = [m for m in self.per_class.values() if m.support]
        return min(real, key=lambda m: m.f1) if real else None

    def top_confusions(self, n: int = 5) -> list[tuple[str, str, int]]:
        pairs = [
            (t, p, c) for (t, p), c in self.confusion.items() if t != p and c
        ]
        return sorted(pairs, key=lambda x: -x[2])[:n]


def classify_report(
    pairs: Sequence[tuple[str, str]], labels: Sequence[str] | None = None
) -> ClassificationReport:
    """从 ``(真实, 预测)`` 列表算出完整报告。"""
    rep = ClassificationReport(total=len(pairs))
    names = list(labels) if labels else sorted(
        {t for t, _ in pairs} | {p for _, p in pairs}
    )
    for name in names:
        rep.per_class[name] = ClassMetrics(label=name)

    for truth, pred in pairs:
        rep.per_class.setdefault(truth, ClassMetrics(label=truth))
        rep.per_class.setdefault(pred, ClassMetrics(label=pred))
        rep.per_class[truth].support += 1
        rep.confusion[(truth, pred)] = rep.confusion.get((truth, pred), 0) + 1

        if truth == pred:
            rep.correct += 1
            rep.per_class[truth].tp += 1
        else:
            rep.per_class[truth].fn += 1
            rep.per_class[pred].fp += 1
    return rep


def render_report(rep: ClassificationReport, *, title: str = "分类评测") -> str:
    lines = [
        f"  {title}",
        "  " + "─" * 66,
        f"  样本 {rep.total} · 准确率 {rep.accuracy:.3f} · macro-F1 {rep.macro_f1:.3f}",
        "",
        f"  {_pad('类别', 24)} {_pad('支撑', 4, right=True)}"
        f" {_pad('精确', 6, right=True)} {_pad('召回', 6, right=True)}"
        f" {_pad('F1', 6, right=True)}",
    ]
    for name in sorted(rep.per_class, key=lambda k: -rep.per_class[k].support):
        m = rep.per_class[name]
        if not m.support and not m.fp:
            continue
        lines.append(
            f"  {_pad(name, 24)} {m.support:>4} {m.precision:>6.3f}"
            f" {m.recall:>6.3f} {m.f1:>6.3f}"
        )

    conf = rep.top_confusions()
    if conf:
        lines += ["", "  主要混淆（真实 → 预测）："]
        # 这几行才是能直接指导改提示词的东西
        lines += [f"    {t} → {p}  ×{c}" for t, p, c in conf]
    return "\n".join(lines)


@dataclass
class GateResult:
    name: str
    value: float
    threshold: float
    passed: bool
    note: str = ""


def check_gates(
    rep: ClassificationReport,
    *,
    accuracy_min: float,
    macro_f1_min: float | None = None,
    per_class_f1_min: float | None = None,
) -> list[GateResult]:
    """门禁。

    **总分之外必须卡逐类下限**：七类平均 0.87 而 sensitive 类 F1 只有
    0.3，意味着该转人工的没转——这种情况总分门禁完全看不出来，
    但它比平均分低几个点严重得多。
    """
    gates = [GateResult("准确率", rep.accuracy, accuracy_min,
                        rep.accuracy >= accuracy_min)]
    if macro_f1_min is not None:
        gates.append(GateResult("macro-F1", rep.macro_f1, macro_f1_min,
                                rep.macro_f1 >= macro_f1_min))
    if per_class_f1_min is not None:
        worst = rep.worst_class
        gates.append(GateResult(
            "最差类 F1", worst.f1 if worst else 0.0, per_class_f1_min,
            bool(worst and worst.f1 >= per_class_f1_min),
            note=worst.label if worst else "",
        ))
    return gates


def render_gates(gates: Sequence[GateResult]) -> str:
    lines = ["", "  门禁"]
    for g in gates:
        mark = "✓" if g.passed else "✗"
        tail = f"（{g.note}）" if g.note else ""
        lines.append(
            f"    {mark} {_pad(g.name, 12)} {g.value:.3f}"
            f" / 需 ≥ {g.threshold:.2f}{tail}"
        )
    return "\n".join(lines)
