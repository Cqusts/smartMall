"""导购 Agent。

**和客服 Agent 的区别不在话术，在形状。** 客服是一问一答，链路基本是直的；
导购是个带回退的收敛过程——搜不到就放宽条件重搜，太多就追问再搜，
直到给出结果或者说清没有。那个循环才是它称得上 Agent 的原因。

分层与客服一致，因此两边共用 :class:`~app.agent.nodes.Deps`、工具层、
输入安全检查与埋点格式：

* :mod:`state`   —— :class:`ShoppingNeed` 跨轮累积，是驱动下一步的东西
* :mod:`prompts` —— 抽取 / 追问 / 推荐 / 无结果
* :mod:`nodes`   —— ``(state, deps) -> state`` 的普通函数
* :mod:`graph`   —— 编排。搜完之后的三分支是这个 Agent 的核心决策

**唯一不能妥协的一条**：候选为零时如实说没有，绝不推荐一个不存在的、
或明显不满足条件的商品。这和客服那边"知识库没有就转人工"是同一条线——
编出来的商品用户点进去才发现，那之后他不会再信任何一条推荐。
"""

from .graph import NODE_LABELS, run_turn, safe_run_turn
from .state import ShoppingNeed, ShoppingState

__all__ = [
    "NODE_LABELS", "ShoppingNeed", "ShoppingState", "run_turn", "safe_run_turn",
]
