"""客服 Agent。

分层：

* :mod:`state`     —— 状态与 Trace 定义，节点间唯一的通信媒介
* :mod:`guard`     —— 输入安全与输出合规，纯规则、可审计
* :mod:`nodes`     —— 各节点，形如 ``(state, deps) -> state`` 的普通函数
* :mod:`graph`     —— 组装。``run_turn`` 不依赖 LangGraph，图只是薄封装
* :mod:`retriever` —— 检索适配：本地 MySQL / ai-rag，同一个协议
* :mod:`llm`       —— 模型调用，错误分三类（可重试/配置错/业务）
"""

from .graph import build_graph, run_turn, safe_run_turn
from .nodes import AgentConfig, Deps
from .state import AgentState, Citation, HandoverReason, Intent, SessionContext

__all__ = [
    "AgentConfig", "AgentState", "Citation", "Deps", "HandoverReason",
    "Intent", "SessionContext", "build_graph", "run_turn", "safe_run_turn",
]
