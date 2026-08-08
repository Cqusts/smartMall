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
    # 实时类问题必须查工具：知识库里那句"目前有货"是三个月前的。
    # 尺码也走工具——尺码表是二维表，向量检索对这类查询天然很差；
    # 但尺码之后仍要走检索，知识库里的"偏大偏小"经验是工具给不了的。
    if state.intent.needs_realtime or state.intent is Intent.SIZING:
        return "tools"
    return "retrieve"


def route_after_tools(state: AgentState) -> str:
    if state.handover:
        return "handover"
    # 工具节点已经写好了回复（缺订单号、缺商品），直接出去问用户
    if state.clarify_question:
        return "emit"
    if state.intent.needs_realtime:
        # 拿到数据就生成；没拿到说明查不着，工具节点已给出话术
        return "generate" if state.tool_results else "emit"
    return "retrieve"  # 尺码：尺码表 + 知识库经验一起用


def has_lexical_support(state: AgentState) -> bool:
    """命中里是否有**词汇**支撑，而不只是向量的基线相似度。

    中文短文本 embedding 有一个恼人的性质：两句毫不相干的话也能拿到
    0.3~0.4 的余弦相似度。所以"分数中等"本身根本不能说明检索到了
    相关内容——实测里"你们支持花呗分期吗"命中的全是「七天无理由退换」，
    分数 0.413；"能开专票吗"命中「能不能发顺丰」，分数 0.492。

    BM25 是一路**独立**的证据：它只在查询词真的出现在文档里时才给分。
    dense 分中等而 BM25 全为 0，说明这点相似度纯粹来自向量空间的基线，
    没有任何词汇支撑。

    这个区分决定了中间地带该走哪条路：

    * 有词汇支撑 → 确实检索到了沾边的东西，只是拿不准是哪一条，
      **澄清有意义**（"您问的是米白那款还是藏青那款？"）。
    * 没有词汇支撑 → 知识库里根本没有，**澄清是最糟的选择**：
      装作有，反问一个假装在缩小范围的问题，用户答完第二轮还是没有
      知识，只会更恼火。这时候老实转人工。

    只要有一条命中带词汇支撑就算数——混合检索里纯 dense 召回的条目
    BM25 天然为 0，要求全部命中都有支撑会把正常情况也判死。
    """
    return any(h.bm25_score > 0 for h in state.hits)


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
        # 中间地带：分数不高不低。此时**分数说明不了问题**，
        # 要看有没有词汇支撑才能判断是"沾边但拿不准"还是"根本没有"。
        if not has_lexical_support(state):
            state.to_handover(HandoverReason.NO_KNOWLEDGE)
            return "handover"
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

    if step == "tools":
        state = nodes.call_tools(state, deps)
        step = route_after_tools(state)
        if step == "handover":
            return nodes.emit(nodes.handover(state, deps), deps)
        if step == "emit":
            return nodes.emit(state, deps)
        if step == "generate":
            state = nodes.generate(state, deps)
            if route_after_generate(state) == "handover":
                return nodes.emit(nodes.handover(state, deps), deps)
            state = nodes.post_check(state, deps)
            if route_after_postcheck(state) == "handover":
                return nodes.emit(nodes.handover(state, deps), deps)
            return nodes.emit(state, deps)

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
        ("tools", nodes.call_tools),
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
                             "tools": "tools", "retrieve": "retrieve"})
    g.add_conditional_edges("tools", route_after_tools,
                            {"handover": "handover", "emit": "emit",
                             "generate": "generate", "retrieve": "retrieve"})
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
