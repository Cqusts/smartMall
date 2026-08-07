"""图的各个节点。

每个节点都是 ``(state, deps) -> state`` 的普通函数。这样做的好处是
任何一个节点都能单独构造输入、单独断言输出——不需要跑完整条图，
更不需要起 LangGraph。图只负责把它们连起来。

**贯穿全篇的一条原则**：任何异常的最终兜底都是转人工，
绝不返回错误堆栈或空白（docs/05 第 9 节）。用户看到 traceback
比看到"稍等，我帮您转人工"糟糕一百倍。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import guard, prompts
from .llm import LlmClient, LlmError
from .retriever import RetrievalError, Retriever
from .state import AgentState, Citation, HandoverReason, Intent


@dataclass
class AgentConfig:
    intent_model: str = "chat-light"
    """意图分类走便宜模型。它对每条消息都要跑一次，是量最大的调用。"""
    answer_model: str = "chat-default"
    clarify_model: str = "chat-light"
    chitchat_model: str = "chat-light"
    rewrite_model: str = "chat-light"
    summary_model: str = "chat-light"

    top_k: int = 5
    handover_below: float = 0.30
    """低于此分数说明知识库里根本没有相关内容，直接转人工。
    硬答的结果是一本正经地胡说。"""
    clarify_below: float = 0.50
    """介于两者之间：有点相关但不确定，问一句比猜一句强。"""

    max_history_turns: int = 6
    summarize_after_turns: int = 10
    max_answer_length: int = 200
    negative_streak_limit: int = 3
    """连续这么多轮表达不满就转人工。用户已经在生气了。"""


@dataclass
class Deps:
    """节点的外部依赖。全部通过参数注入，节点自身不建连接。"""

    llm: LlmClient
    retriever: Retriever
    config: AgentConfig = field(default_factory=AgentConfig)


# ---------------------------------------------------------------- 辅助


def _history_text(state: AgentState, n: int) -> str:
    sess = state.session
    lines = []
    if sess.summary:
        lines.append(f"（此前摘要）{sess.summary}")
    for t in sess.recent(n):
        who = "用户" if t.role == "user" else "客服"
        lines.append(f"{who}：{t.content}")
    return "\n".join(lines) or "（无）"


def _knowledge_text(hits: list[Citation]) -> str:
    if not hits:
        return "（没有检索到相关知识）"
    return "\n".join(
        f"[#{h.item_id}] {h.title}\n{h.content}" for h in hits
    )


# ---------------------------------------------------------------- 节点


def ingest(state: AgentState, deps: Deps) -> AgentState:
    """接入消息，写入会话历史。"""
    state.query = state.message
    state.trace.session_id = state.session.session_id
    state.trace.user_id = state.session.user_id
    state.trace.input_text = state.message
    state.session.append("user", state.message)
    return state


def guard_input(state: AgentState, deps: Deps) -> AgentState:
    """输入安全检查。纯规则，不调模型。"""
    result = guard.check_input(state.message)

    if result.wants_handover:
        state.to_handover(HandoverReason.USER_REQUESTED)
        return state

    if not result.ok:
        state.blocked = True
        state.answer = result.reply
        state.trace.postcheck_flags.append(f"输入拦截:{result.reason}")
        return state

    # 单次不满不转人工——可能只是语气重。连续三轮就是真的谈崩了。
    if result.is_negative:
        state.session.negative_streak += 1
        if state.session.negative_streak >= deps.config.negative_streak_limit:
            state.to_handover(HandoverReason.SENSITIVE_INTENT)
    else:
        state.session.negative_streak = 0
    return state


def classify_intent(state: AgentState, deps: Deps) -> AgentState:
    """意图分类。

    分类失败不该让整通对话失败——退回 product_knowledge 走检索，
    那是最通用的分支，最坏情况只是多花一次检索。
    """
    t0 = time.time()
    try:
        verdict = deps.llm.complete_json(
            model=deps.config.intent_model,
            system=prompts.INTENT_SYSTEM,
            user=prompts.INTENT_USER.format(
                history=_history_text(state, 4), message=state.message
            ),
        )
        state.intent = Intent(verdict.get("intent", "product_knowledge"))
    except (LlmError, ValueError):
        state.intent = Intent.PRODUCT_KNOWLEDGE
        state.trace.postcheck_flags.append("意图分类失败→按知识类处理")

    state.trace.intent = state.intent.value
    state.trace.latency_ms["intent"] = int((time.time() - t0) * 1000)

    if state.intent is Intent.SENSITIVE:
        state.to_handover(HandoverReason.SENSITIVE_INTENT)
    return state


def retrieve(state: AgentState, deps: Deps) -> AgentState:
    """知识检索。

    检索**失败**与检索到**0 条**是两回事，处置也不同：0 条是确定结论，
    可以进澄清或转人工的正常流程；失败是不知道，只能转人工——
    对用户说"没找到相关信息"而真相是服务宕了，那是撒谎。
    """
    t0 = time.time()
    try:
        state.hits = deps.retriever.search(
            state.query,
            top_k=deps.config.top_k,
            product_ids=(
                [state.session.current_product_id]
                if state.session.current_product_id else None
            ),
            category_id=state.session.category_id,
        )
    except RetrievalError as exc:
        state.to_handover(HandoverReason.TOOL_FAILURE)
        state.trace.error = str(exc)
        return state

    state.trace.latency_ms["retrieval"] = int((time.time() - t0) * 1000)
    state.trace.retrieval_hit_count = len(state.hits)
    state.trace.retrieval_max_score = state.max_score
    state.trace.retrieval_item_ids = [h.item_id for h in state.hits]
    return state


def rewrite_query(state: AgentState, deps: Deps) -> AgentState:
    """查询改写。只在首次命中不足时触发，且**只重试一次**。

    不设上限的话，一个知识库里根本没有的问题会把改写-检索循环跑满，
    延迟和成本都翻倍，结果仍然是转人工。
    """
    try:
        out = deps.llm.complete_json(
            model=deps.config.rewrite_model,
            system=prompts.REWRITE_SYSTEM,
            user=prompts.REWRITE_USER.format(
                history=_history_text(state, 4),
                product=state.session.current_product_id or "未指定",
                query=state.query,
            ),
        )
        new_query = (out.get("query") or "").strip()
        if new_query:
            state.query = new_query
            state.trace.rewritten_query = new_query
    except LlmError:
        pass  # 改写失败就用原查询再试一次，不值得为此中断
    state.rewritten = True
    return state


def clarify(state: AgentState, deps: Deps) -> AgentState:
    """生成一个具体的澄清问题。"""
    try:
        state.clarify_question = deps.llm.complete(
            model=deps.config.clarify_model,
            system=prompts.CLARIFY_SYSTEM,
            user=prompts.CLARIFY_USER.format(
                message=state.message, knowledge=_knowledge_text(state.hits)
            ),
        ).strip()
    except LlmError:
        state.to_handover(HandoverReason.TOOL_FAILURE)
        return state

    state.answer = state.clarify_question
    return state


def chitchat(state: AgentState, deps: Deps) -> AgentState:
    """寒暄走最小模型，不检索。

    约三成消息是纯寒暄，走完整 RAG 链路是纯粹的浪费——
    一次向量化 + 一次检索 + 一次大模型调用，只为回一句"在的呢"。
    """
    try:
        state.answer = deps.llm.complete(
            model=deps.config.chitchat_model,
            system=prompts.CHITCHAT_SYSTEM,
            user=state.message,
        ).strip()
    except LlmError:
        state.answer = "在的，请问有什么可以帮您？"
    return state


def generate(state: AgentState, deps: Deps) -> AgentState:
    """基于检索到的知识生成答案。"""
    t0 = time.time()
    try:
        state.answer = deps.llm.complete(
            model=deps.config.answer_model,
            system=prompts.ANSWER_SYSTEM,
            user=prompts.ANSWER_USER.format(
                knowledge=_knowledge_text(state.hits),
                history=_history_text(state, deps.config.max_history_turns),
                message=state.message,
            ),
        ).strip()
    except LlmError as exc:
        state.to_handover(HandoverReason.TOOL_FAILURE)
        state.trace.error = str(exc)
        return state

    state.trace.latency_ms["generation"] = int((time.time() - t0) * 1000)
    state.trace.model = deps.config.answer_model
    # 引用由程序按答案里出现的标记回填，不问模型要——
    # 模型报的引用列表和正文里的标记经常对不上
    used = {h.item_id for h in state.hits if h.marker in state.answer}
    state.citations = [h for h in state.hits if h.item_id in used]
    return state


def post_check(state: AgentState, deps: Deps) -> AgentState:
    """输出合规检查。纯规则。"""
    result = guard.check_output(
        state.answer, max_length=deps.config.max_answer_length
    )
    state.answer = result.text
    state.postcheck_flags.extend(result.flags)
    state.trace.postcheck_flags.extend(result.flags)
    if result.blocked:
        state.to_handover(HandoverReason.POSTCHECK_FAILED)
    return state


def handover(state: AgentState, deps: Deps) -> AgentState:
    """转人工：生成结构化交接摘要。

    摘要的唯一目的是**让用户不用重复说一遍**——这是 AI 客服体验
    最大的抱怨点。生成失败也不能挡住转人工本身，退化成模板即可。
    """
    reason = state.handover_reason or HandoverReason.INTERNAL_ERROR
    # 这个节点跑到了，就是真的转人工了。置位必须在这里而不是只靠调用方——
    # 路由函数决定转人工却忘了置位的话，前端拿到的仍是一条普通回复，
    # 转人工工单不会建，用户就卡在 AI 这一侧了。
    state.handover = True
    state.handover_reason = reason
    state.session.handover_count += 1

    fallback = {
        "summary": f"用户问：{state.message}",
        "conversation_digest": "",
        "user_sentiment": "unknown",
        "suggested_action": "请人工跟进",
    }
    try:
        summary = deps.llm.complete_json(
            model=deps.config.summary_model,
            system=prompts.HANDOVER_SYSTEM,
            user=prompts.HANDOVER_USER.format(
                history=_history_text(state, 10),
                reason=reason.value,
                question=state.message,
            ),
        )
    except LlmError:
        summary = fallback

    summary.setdefault("summary", fallback["summary"])
    summary["handover_reason"] = reason.value
    summary["unanswered_question"] = state.message
    summary["current_product_id"] = state.session.current_product_id
    summary["user_intent"] = state.intent.value
    # 每一次转人工都暴露一个知识盲点——这条是飞轮的入口
    summary.setdefault(
        "suggested_action",
        "人工回答后，把该问答补进知识库（source=handover）",
    )
    state.handover_summary = summary

    state.answer = (
        "这个问题我不太确定，帮您转接人工客服，稍等一下～"
        if reason is not HandoverReason.USER_REQUESTED
        else "好的，正在为您转接人工客服，请稍等～"
    )
    state.trace.handover = True
    state.trace.handover_reason = reason.value
    return state


def emit(state: AgentState, deps: Deps) -> AgentState:
    """收尾：写回会话历史，补齐 Trace。"""
    if state.answer:
        state.session.append("assistant", state.answer)
    state.trace.answer = state.answer
    state.trace.citations = [c.item_id for c in state.citations]
    state.trace.latency_ms["total"] = sum(
        v for k, v in state.trace.latency_ms.items() if k != "total"
    )
    return state
