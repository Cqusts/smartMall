"""评测。

测试证明"代码按我写的那样跑"；评测证明"这套系统在真实输入上管用"。
对 AI 系统来说后者是更重要的主张，而它只能靠标注数据支撑。

评测集手写、与代码同仓（``evals/datasets/``）——用被评测的模型造样本
是循环论证，它造得出的题正是它答得对的题。
"""

from .metrics import ClassificationReport, check_gates, classify_report
from .runner import RUNNERS, EvalOutcome, load, run_intent, run_negative, run_safety

__all__ = [
    "ClassificationReport", "EvalOutcome", "RUNNERS", "check_gates",
    "classify_report", "load", "run_intent", "run_negative", "run_safety",
]
