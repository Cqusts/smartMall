"""状态机组装。

用 LangGraph 把 nodes.py 里的函数连成图。**编排逻辑本身不依赖
LangGraph**——``run_turn`` 是一个普通函数，图只是它的一层封装。
这样做的理由和数据中台那边一样：完整链路要能在 pytest 里跑通，
不需要起图运行时。装了 langgraph 就能拿到可视化与检查点，
没装也不影响功能。

路由决策集中在 ``_route_*`` 几个纯函数里，图和普通函数共用同一套，
不会出现"图里走了一条路、直跑走了另一条"的分裂。
"""

from __future__ import annotations

from . import nodes
from .nodes import AgentConfig, Deps
from .state import AgentState, HandoverReason, Intent

# ---------------------------------------------------------------- 路由


def route_after_guard(state: AgentState) -> str:
    if state.blocked:
        return "emit"
    if state.handover:
        return "handover"
    return "intent"


def route_after_intent(state: AgentState) -> str:
    if state.handover:
        return "handover"
    if state.intent is Intent.CHITCHAT:
        return "chitchat"
    if state.intent.needs_realtime:
        # M2 还没接 MCP 工具。实时类问题宁可转人工，也不能拿
        # 知识库里的历史对话回答——那会说出过期的价格和库存。
        state.to_handover(HandoverReason.NO_KNOWLEDGE)
        return "handover"
    return "retrieve"


def route_after_retrieve(state: AgentState, cfg: AgentConfig) -> str:
    if state.handover:
        return "handover"
    score = state.max_score
    if score < cfg.handover_below:
        # 命中太差先试一次改写；改写过还是差，说明知识库真的没有。
        # 转人工的**原因**要在这里定下来——交接摘要和知识盲点统计都靠它，
        # 留给下游猜的话会全部记成"内部异常"，飞轮就断了。
        if state.rewritten:
            state.to_handover(HandoverReason.NO_KNOWLEDGE)
            return "handover"
        return "rewrite"
    if score < cfg.clarify_below:
        return "clarify"
    return "generate"


def route_after_generate(state: AgentState) -> str:
    return "handover" if state.handover else "postcheck"


def route_after_postcheck(state: AgentState) -> str:
    return "handover" if state.handover else "emit"


# ---------------------------------------------------------------- 直跑


def run_turn(message: str, state: AgentState, deps: Deps) -> AgentState:
    """跑完一轮对话。

    不依赖 LangGraph，因此可以在任何地方直接调用与测试。
    """
    state.message = message
    cfg = deps.config

    state = nodes.ingest(state, deps)
    state = nodes.guard_input(state, deps)

    step = route_after_guard(state)
    if step == "emit":
        return nodes.emit(state, deps)
    if step == "handover":
        return nodes.emit(nodes.handover(state, deps), deps)

    state = nodes.classify_intent(state, deps)
    step = route_after_intent(state)
    if step == "handover":
        return nodes.emit(nodes.handover(state, deps), deps)
    if step == "chitchat":
        return nodes.emit(nodes.chitchat(state, deps), deps)

    # 检索 →（必要时改写重试一次）→ 打分分流
    state = nodes.retrieve(state, deps)
    step = route_after_retrieve(state, cfg)
    if step == "rewrite":
        state = nodes.rewrite_query(state, deps)
        state = nodes.retrieve(state, deps)
        step = route_after_retrieve(state, cfg)

    if step == "handover":
        return nodes.emit(nodes.handover(state, deps), deps)
    if step == "clarify":
        state = nodes.clarify(state, deps)
        if state.handover:
            return nodes.emit(nodes.handover(state, deps), deps)
        return nodes.emit(state, deps)

    state = nodes.generate(state, deps)
    if route_after_generate(state) == "handover":
        return nodes.emit(nodes.handover(state, deps), deps)

    state = nodes.post_check(state, deps)
    if route_after_postcheck(state) == "handover":
        return nodes.emit(nodes.handover(state, deps), deps)

    return nodes.emit(state, deps)


def safe_run_turn(message: str, state: AgentState, deps: Deps) -> AgentState:
    """带兜底的入口。

    任何未预料的异常都要落到转人工，而不是把 traceback 丢给用户。
    这是 docs/05 第 9 节那条原则的最后一道防线。
    """
    try:
        return run_turn(message, state, deps)
    except Exception as exc:  # noqa: BLE001
        state.to_handover(HandoverReason.INTERNAL_ERROR)
        state.trace.error = f"{type(exc).__name__}: {exc}"
        try:
            state = nodes.handover(state, deps)
        except Exception:  # noqa: BLE001  连交接摘要都生成不了
            state.answer = "系统开小差了，正在为您转接人工客服～"
            state.trace.handover = True
        return nodes.emit(state, deps)


# ---------------------------------------------------------------- LangGraph


def build_graph(deps: Deps):
    """组装 LangGraph。装了才可用，缺依赖时给出可操作的提示。

    图与 :func:`run_turn` 共用同一批节点函数和同一批路由函数，
    因此两条路径的行为一致——这是刻意的，否则"测试里通过、
    线上走另一条分支"就成了必然。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "需要 langgraph：pip install 'langgraph>=0.2'\n"
            "  只想跑通对话的话，用 run_turn / safe_run_turn 即可，"
            "它们不依赖 langgraph。"
        ) from exc

    cfg = deps.config
    g = StateGraph(AgentState)

    for name, fn in (
        ("ingest", nodes.ingest),
        ("guard", nodes.guard_input),
        ("intent", nodes.classify_intent),
        ("retrieve", nodes.retrieve),
        ("rewrite", nodes.rewrite_query),
        ("clarify", nodes.clarify),
        ("chitchat", nodes.chitchat),
        ("generate", nodes.generate),
        ("postcheck", nodes.post_check),
        ("handover", nodes.handover),
        ("emit", nodes.emit),
    ):
        g.add_node(name, (lambda f: lambda s: f(s, deps))(fn))

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "guard")
    g.add_conditional_edges("guard", route_after_guard,
                            {"emit": "emit", "handover": "handover",
                             "intent": "intent"})
    g.add_conditional_edges("intent", route_after_intent,
                            {"handover": "handover", "chitchat": "chitchat",
                             "retrieve": "retrieve"})
    g.add_conditional_edges("retrieve", lambda s: route_after_retrieve(s, cfg),
                            {"handover": "handover", "rewrite": "rewrite",
                             "clarify": "clarify", "generate": "generate"})
    g.add_edge("rewrite", "retrieve")
    g.add_conditional_edges("generate", route_after_generate,
                            {"handover": "handover", "postcheck": "postcheck"})
    g.add_conditional_edges("postcheck", route_after_postcheck,
                            {"handover": "handover", "emit": "emit"})
    g.add_edge("chitchat", "emit")
    g.add_edge("clarify", "emit")
    g.add_edge("handover", "emit")
    g.add_edge("emit", END)
    return g.compile()
