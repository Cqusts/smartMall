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
from typing import Any

from . import guard, prompts, vision
from .llm import LlmClient, LlmError
from .retriever import RetrievalError, Retriever
from .tools import (
    ToolBox, ToolError, extract_order_no, render_order, render_size_chart,
    render_skus,
)
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
    clarify_below: float = 0.55
    """介于两者之间：可能沾边但拿不准，且**必须有词汇支撑**才澄清
    （见 graph.has_lexical_support）。

    这两个阈值是初值，不是定论。实测的四个样本：答对的那次 0.760，
    答不上来的三次 0.413 / 0.446 / 0.492——分界线明显在 0.5 以上，
    所以从 0.50 提到 0.55。等 agent_trace 攒够几十条、配上点赞点踩，
    就能用数据校准而不是拍脑袋（``smartmall-agent traces`` 正是为此）。"""

    lexical_support_min: float = 0.15
    """词汇覆盖率下限，低于它视为"知识库里根本没有"（见
    ``graph.has_lexical_support``）。

    **这个 0.15 只在一个 6 条的玩具语料上验过**：库里没有的问题覆盖率
    ≤ 0.105，库里有的 ≥ 0.163。真实语料上这条线在哪，得拿
    ``smartmall-agent eval --suite negative`` 的错例去校——调它之前
    先看错例，别拍脑袋。"""

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
    store: Any = None
    """埋点与工单落库。为 None 时不落库——这是刻意的默认值：
    Agent 在没有数据库的环境里（测试、离线调试）必须照样能跑。"""
    tools: ToolBox | None = None
    """业务数据工具集。为 None 时实时类问题转人工——
    宁可转人工，也不能拿知识库里三个月前的"目前有货"糊弄过去。"""
    vision: Any = None
    """看图模型。为 None 时不接受图片——**失败关闭**：
    这条路径带着脱敏责任，不可用时正确的行为是不收图，不是不脱敏照收。"""
    kb: Any = None
    """知识库读写通道（知识运维 Agent 用）。为 None 时只起草不落库——
    **草稿照样产出**：看得见机器能写成什么样，是决定要不要接上这条链路
    的前提，而那不需要先有写权限。"""
    copy_store: Any = None
    """营销文案落库通道（运营 Agent 用）。为 None 时只生成不落库。"""
    media: Any = None
    """图/视频生成模型（运营 Agent 用）。为 None 时**只出提示词不生成**——
    提示词是这条链路上唯一能被规则检查的东西（图生成完就查不了了），
    看得见它就能判断该不该接模型，而那一步不该先花钱。"""
    asset_store: Any = None
    """素材落库通道。为 None 时只生成不落库。"""
    asset_dir: Any = None
    """生成结果的落地目录。**模型返回的 URL 24 小时后失效**，不下载到本地的话
    今天演示完、明天打开就是一片裂图（见 marketing/media.download）。"""
    on_event: Any = None
    """流式事件回调 ``(dict) -> None``。为 None 时整条链路完全同步，
    行为与从前一致——**流式不另起一套编排**，否则两条路径必然分叉。"""

    def emit_event(self, **event: Any) -> None:
        if self.on_event is not None:
            self.on_event(event)


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


def _tools_text(state: AgentState) -> str:
    """工具数据单独成块，并标明优先级高于知识库。

    混在一起写的话，模型会把三个月前对话里的"目前有货"和刚查到的
    "库存 0"平等对待然后挑一个说——而实时数据存在的全部意义
    就是覆盖过期的知识。
    """
    if not state.tool_results:
        return ""
    return prompts.TOOLS_BLOCK.format(
        tools="\n".join(state.tool_results.values())
    )


def _knowledge_text(hits: list[Citation]) -> str:
    if not hits:
        return "（没有检索到相关知识）"
    return "\n".join(
        f"[#{h.item_id}] {h.title}\n{h.content}" for h in hits
    )


# ---------------------------------------------------------------- 节点


# ---------------------------------------------------------------- 步骤埋点

#: 节点在页面上的名字。用中文而不是函数名——这个面板是给人看的，
#: 演示时对方不该需要先读一遍源码。
NODE_LABELS = {
    "ingest": "接收消息",
    "vision": "看图理解",
    "guard": "输入安全检查",
    "intent": "识别意图",
    "retrieve": "检索知识库",
    "rewrite": "改写查询重试",
    "clarify": "请求澄清",
    "chitchat": "寒暄应答",
    "tools": "调用业务工具",
    "generate": "生成答案",
    "postcheck": "出口合规检查",
    "handover": "转接人工",
    "emit": "收尾落库",
}


def step_detail(name: str, state: AgentState) -> dict[str, Any]:
    """这一步到底发生了什么。

    每个节点报告自己的关键产出——**光有节点名说明不了问题**：
    "检索知识库"跑完了，命中几条？最高分多少？有没有词汇支撑？
    这三个数才是判断它做得对不对的依据，而它们平时全是隐形的。
    """
    if name == "intent":
        return {"意图": state.intent.value}
    if name == "vision":
        d: dict[str, Any] = {"图片类型": state.image_kind or "-"}
        if state.trace.image_pii_hits:
            d["脱敏命中"] = state.trace.image_pii_hits
        return d
    if name == "retrieve":
        return {
            "命中": state.trace.retrieval_hit_count,
            "最高相似度": round(state.trace.retrieval_max_score, 3),
            "词汇覆盖率": round(state.trace.retrieval_lexical_overlap, 3),
        }
    if name == "rewrite":
        return {"改写为": state.trace.rewritten_query}
    if name == "tools":
        return {"调用": [t["name"] for t in state.trace.tools_called]} \
            if state.trace.tools_called else {"调用": "无"}
    if name == "postcheck":
        return {"合规": state.postcheck_flags or "通过"}
    if name == "handover":
        return {"原因": state.handover_reason.value if state.handover_reason else "-"}
    if name == "clarify":
        return {"澄清": state.clarify_question[:40] or "判定澄清无意义"}
    return {}


def run_node(
    name: str, fn: Any, state: Any, deps: Deps,
    *, labels: dict[str, str] | None = None, detail: Any = None,
) -> Any:
    """执行一个节点并埋点。

    **包一层而不是在每个节点里手写 emit_event。** 原先只有 5 个节点发事件，
    而且是散着手写的——加一个节点就会漏一个，漏了还看不出来（页面上少一行
    而已，没人会发现）。包在派发处，谁也漏不掉。

    ``run_turn`` 与 LangGraph 两条路径都走这里，所以页面上看到的流程
    和图里跑的流程必然一致。

    ``labels`` 与 ``detail`` 让别的 Agent（导购、知识运维）复用同一套埋点，
    只换自己的词表。**不给它们换的话会静默退化**：标签落回英文节点名、
    detail 一律是空字典——页面照样有行，只是每行都看不出发生了什么，
    而这正是加这个面板要解决的问题。所以导购那边有一条用例专门断言
    「标签不等于节点名」。
    """
    labels = NODE_LABELS if labels is None else labels
    detail_of = step_detail if detail is None else detail
    t0 = time.time()
    deps.emit_event(type="step", node=name,
                    label=labels.get(name, name), phase="enter")
    state = fn(state, deps)
    deps.emit_event(
        type="step", node=name, label=labels.get(name, name),
        phase="exit", ms=int((time.time() - t0) * 1000),
        detail=detail_of(name, state),
    )
    return state


def ingest(state: AgentState, deps: Deps) -> AgentState:
    """接入消息，写入会话历史。"""
    state.query = state.message
    state.trace.session_id = state.session.session_id
    state.trace.user_id = state.session.user_id
    state.trace.product_id = state.session.current_product_id
    state.trace.input_text = state.message
    state.session.append("user", state.message)
    return state


def understand_image(state: AgentState, deps: Deps) -> AgentState:
    """把用户发的图转成文本，接回主链路。

    转出来的文本**替换或补足** ``state.query``，后面的检索、闸门、生成、
    出口检查一概复用——图片这条入口不需要自己再长一遍这些能力。

    三种图三种处置：

    * ``product`` —— 转述当检索词，正常往下走。
    * ``document`` —— 订单截图、面单、聊天记录。这类图上大概率有个人
      信息，而且用户发它通常是要查单或投诉，**知识库回答不了**。
      有订单号就交给工具查，没有就转人工。
    * ``other`` —— 看不清或与购物无关。问一句比猜一句好。
    """
    if not state.image:
        return state
    if deps.vision is None:
        # 没配看图模型就不收图。装作没看见比"看了但没脱敏"安全得多
        state.answer = "我这边暂时看不了图片，麻烦用文字描述一下可以吗？"
        state.blocked = True
        state.trace.postcheck_flags.append("图片拒收:未配置视觉模型")
        return state

    t0 = time.time()
    try:
        result = vision.understand(
            deps.vision, state.image, state.image_mime, state.message
        )
    except vision.ImageRejected as exc:
        # 入口校验没过（格式、大小、脱敏模块缺失）。给用户一句能照做的话，
        # 而不是一个错误码
        state.answer = exc.reply
        state.blocked = True
        state.trace.postcheck_flags.append(f"图片拒收:{exc.reason}")
        return state
    except LlmError as exc:
        # 看图失败 ≠ 图里没内容。和检索、打标同一条原则：
        # 业务结论只能来自成功的响应
        state.to_handover(HandoverReason.TOOL_FAILURE)
        state.trace.error = f"{type(exc).__name__}: {exc}"
        return state

    state.trace.latency_ms["vision"] = int((time.time() - t0) * 1000)
    state.image_kind = state.trace.image_kind = result.kind
    state.image_observations = result.observations
    state.trace.image_pii_hits = result.pii_hits

    if result.kind == "other":
        state.clarify_question = (
            "这张图我没太看清，方便说一下您想问的是哪方面吗？"
        )
        state.answer = state.clarify_question
        return state

    if result.is_document:
        # 单据类：把订单号补进消息里交给下游，**但不在这里定意图**。
        # 定了也没用——classify_intent 在后面跑，会盖掉；而且也不该定：
        # 用户可能是发着订单截图问"这件还有货吗"，订单号只是上下文，
        # 不是问题本身。谁问的问题谁清楚，那是分类器的活。
        if result.order_no:
            state.message = f"{state.message} 订单号{result.order_no}".strip()
            state.query = state.message
            state.trace.input_text = state.message
        else:
            # 一张看不出单号的单据，知识库答不了——硬检索只会命中一堆
            # 不相干的物流知识，然后一本正经地说错
            state.to_handover(HandoverReason.NO_KNOWLEDGE)
        return state

    # 商品图：转述作为检索词。用户自己打的字优先——他写的"起球怎么办"
    # 比模型转述的"米白色针织衫"更接近他真正想问的
    parts = [p for p in (state.message.strip(), result.query) if p]
    state.query = " ".join(parts) or result.observations
    if not state.message.strip():
        state.message = result.query or "（用户发来一张图片）"
        state.trace.input_text = state.message
    return state


def guard_input(state: AgentState, deps: Deps) -> AgentState:
    """输入安全检查。纯规则，不调模型。"""
    result = guard.check_input(state.message)

    if result.reason == "自伤风险":
        # 话术是写死的，不能交给模型生成——这一句上模型的自由发挥是纯风险。
        # 同时转人工：客服不做心理疏导，但必须把人接住而不是打发走。
        state.answer = result.reply
        state.to_handover(HandoverReason.SELF_HARM)
        return state

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
    state.trace.retrieval_lexical_overlap = max(
        (h.lexical_overlap for h in state.hits), default=0.0
    )
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
    """生成一个具体的澄清问题——**或者判定澄清没有意义**。

    ``lexical_support_min`` 那道阈值拦的是"词汇上完全不沾边"；拦不住的是
    "沾边但问的不是同一件事"。实测两条：问"能不能定制刺绣名字"，覆盖率
    过了线（知识库里有"名字""定制"这类词），于是反问"您想定制刺绣名字的是
    哪一款商品呢"——而知识库里没有任何关于定制服务的内容。
    问"能不能延长保修到三年"同理。

    **问题不在哪款商品，在于这项服务根本没有。** 无论用户回答哪一款，
    第二轮同样没有知识可用，只是把人多耗一轮。这个判断是语义的，
    阈值做不到，所以交给模型——它手上同时有问题和检索结果。
    """
    try:
        data = deps.llm.complete_json(
            model=deps.config.clarify_model,
            system=prompts.CLARIFY_SYSTEM,
            user=prompts.CLARIFY_USER.format(
                message=state.message, knowledge=_knowledge_text(state.hits)
            ),
        )
    except LlmError:
        state.to_handover(HandoverReason.TOOL_FAILURE)
        return state

    question = str(data.get("question") or "").strip()
    if not data.get("useful") or not question:
        # 澄清没意义就转人工，原因记 NO_KNOWLEDGE——这条正是知识盲点，
        # 人工答完要把它补进知识库
        state.to_handover(HandoverReason.NO_KNOWLEDGE)
        return state

    state.clarify_question = question
    state.answer = question
    return state


def chitchat(state: AgentState, deps: Deps) -> AgentState:
    """寒暄走最小模型，不检索。

    约三成消息是纯寒暄，走完整 RAG 链路是纯粹的浪费——
    一次向量化 + 一次检索 + 一次大模型调用，只为回一句"在的呢"。

    **但寒暄同时是七类里的兜底类。** 分不进另外六类的消息最后都落这儿，
    而这一支不检索、无引用、原本还不过出口合规检查——等于把**最自由的
    分支**当成了兜底。实测代价：问"你们公司注册地在哪"，模型答
    "我们公司注册在杭州呢"；问"有没有实习岗位"，答"我们目前确实有实习
    岗位在招哦"。两条都是凭空编的本店事实，检索一次都没发生。

    所以这里让模型先分流：真·社交话语才直接回，凡是需要事实才能回答的
    一律退回主链路重走检索——兜底类必须是**最保守**的分支，不是最自由的。
    """
    try:
        data = deps.llm.complete_json(
            model=deps.config.chitchat_model,
            system=prompts.CHITCHAT_SYSTEM,
            user=state.message,
        )
    except LlmError:
        # 判不出来就当它需要事实：编一条本店信息的代价比多转一次人工大得多
        state.needs_knowledge = True
        return state

    if str(data.get("kind")) != "social":
        state.needs_knowledge = True
        return state

    reply = str(data.get("reply") or "").strip()
    state.answer = reply or "在的，请问有什么可以帮您？"
    return state


def call_tools(state: AgentState, deps: Deps) -> AgentState:
    """按意图调用业务工具。

    这一步存在的理由：库存、价格、物流每分钟都在变，知识库里那句
    "目前有货"是三个月前某段对话里说的。拿它回答"还有货吗"，
    用户下单才发现缺货——比说"我帮您转人工"糟糕得多。

    工具失败与查不到是两回事：查不到是确定结论（这个商品确实没尺码表），
    失败是不知道，只能转人工。这条判据贯穿全项目。
    """
    if deps.tools is None:
        # 没接工具就老实承认答不了，绝不退回 RAG 拿过期数据顶上
        state.to_handover(HandoverReason.TOOL_FAILURE)
        state.trace.error = "未配置业务工具集"
        return state

    t0 = time.time()
    product_id = state.session.current_product_id

    try:
        if state.intent is Intent.ORDER_LOGISTICS:
            _tool_order(state, deps)
        elif state.intent is Intent.REALTIME_STOCK_PRICE:
            _tool_stock_price(state, deps, product_id)
        elif state.intent is Intent.SIZING:
            _tool_size_chart(state, deps, product_id)
    except ToolError as exc:
        state.to_handover(HandoverReason.TOOL_FAILURE)
        state.trace.error = str(exc)
        return state

    state.trace.latency_ms["tools"] = int((time.time() - t0) * 1000)
    return state


def _record(state: AgentState, name: str, t0: float, hit: bool) -> None:
    state.trace.tools_called.append({
        "name": name, "latency_ms": int((time.time() - t0) * 1000), "hit": hit,
    })


def _tool_order(state: AgentState, deps: Deps) -> None:
    """查订单。没给订单号就问一句，别转人工——那是可以自己解决的。"""
    order_no = extract_order_no(state.message)
    if not order_no:
        state.answer = "麻烦提供一下订单号，我帮您查一下物流～"
        state.clarify_question = state.answer
        return

    t0 = time.time()
    order = deps.tools.get_order_status(order_no, state.session.user_id)
    _record(state, "get_order_status", t0, order is not None)

    if order is None:
        # 查不到与越权返回同一句话——区别对待会泄露订单是否存在，
        # 攻击者可以靠枚举订单号来确认哪些是真的
        state.answer = (
            f"没有查到订单 {order_no} 呢，麻烦确认一下订单号，"
            "或者确认是否用当前账号下的单～"
        )
        return
    state.tool_results["order"] = render_order(order)


def _tool_stock_price(state: AgentState, deps: Deps, product_id: int | None) -> None:
    if product_id is None:
        state.answer = "请问您问的是哪款商品呢？方便说下商品名或者从商品页进来～"
        state.clarify_question = state.answer
        return

    t0 = time.time()
    skus = deps.tools.get_sku_stock_price(product_id)
    _record(state, "get_sku_stock_price", t0, bool(skus))
    if skus:
        state.tool_results["sku"] = render_skus(skus)


def _tool_size_chart(state: AgentState, deps: Deps, product_id: int | None) -> None:
    """尺码表。查不到不算失败——继续走检索，知识库里也许有尺码经验。"""
    if product_id is None:
        return
    t0 = time.time()
    chart = deps.tools.get_size_chart(product_id)
    _record(state, "get_size_chart", t0, chart is not None)
    if chart:
        state.tool_results["size_chart"] = render_size_chart(chart)


def generate(state: AgentState, deps: Deps) -> AgentState:
    """基于检索到的知识生成答案。"""
    t0 = time.time()
    user_prompt = prompts.ANSWER_USER.format(
        knowledge=_knowledge_text(state.hits),
        tools=_tools_text(state),
        history=_history_text(state, deps.config.max_history_turns),
        message=state.message,
    )
    try:
        if deps.on_event is not None:
            # 流式：逐块推给前端。但**这些是草稿**——合规检查还没跑。
            # 前端必须等 done 事件里的最终文本再定稿，见 post_check 的注释。
            pieces: list[str] = []
            for piece in deps.llm.stream(
                model=deps.config.answer_model,
                system=prompts.ANSWER_SYSTEM,
                user=user_prompt,
            ):
                pieces.append(piece)
                deps.emit_event(type="delta", text=piece)
            state.answer = "".join(pieces).strip()
        else:
            state.answer = deps.llm.complete(
                model=deps.config.answer_model,
                system=prompts.ANSWER_SYSTEM,
                user=user_prompt,
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
    """输出合规检查。纯规则。

    **流式输出与合规检查天然冲突**：要逐字推给用户就得在检查之前推，
    而广告法违规内容一旦到了用户眼前，撤回不等于拦截。

    这里的取舍是：流式推的内容前端按**草稿**处理，最终文本以 ``done``
    事件为准；被改写或拦截时前端整段替换。做不到"违规内容一个字都不
    出现"，但做到了"用户最终看到并留存的内容一定过了检查"。
    真要做到前者只能放弃流式——那会让 P95 三秒的等待变成纯白屏。
    """
    result = guard.check_output(
        state.answer, max_length=deps.config.max_answer_length
    )
    state.answer = result.text
    state.postcheck_flags.extend(result.flags)
    state.trace.postcheck_flags.extend(result.flags)
    if result.blocked:
        state.to_handover(HandoverReason.POSTCHECK_FAILED)
    elif result.deferred:
        # 模型自己说答不了。原因记 NO_KNOWLEDGE 而不是 POSTCHECK_FAILED：
        # 这不是合规问题，是知识库缺内容——盲点统计要靠这个分类才准，
        # 记错了就永远补不上那块知识。
        state.to_handover(HandoverReason.NO_KNOWLEDGE)
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

    if reason is HandoverReason.SELF_HARM:
        # 话术已经在 guard_input 里定好了，不覆盖。"这个问题我不太确定"
        # 用在这里是荒唐的——用户说的不是一个"问题"。
        pass
    elif reason is HandoverReason.USER_REQUESTED:
        state.answer = "好的，正在为您转接人工客服，请稍等～"
    else:
        state.answer = "这个问题我不太确定，帮您转接人工客服，稍等一下～"
    state.trace.handover = True
    state.trace.handover_reason = reason.value
    return state


def emit(state: AgentState, deps: Deps) -> AgentState:
    """收尾：写回会话历史，补齐并落库 Trace。"""
    if state.answer:
        state.session.append("assistant", state.answer)
    state.trace.answer = state.answer
    state.trace.citations = [c.item_id for c in state.citations]
    state.trace.latency_ms["total"] = sum(
        v for k, v in state.trace.latency_ms.items() if k != "total"
    )

    # 落库放在最后一个节点，且失败不影响返回给用户的答案。
    # Trace 是训练数据的原料，但用户等的是回复——
    # "这一轮没记上"的代价远小于"这一轮没回复"。
    if deps.store is not None:
        deps.store.save(state.trace)
        if state.handover:
            # 每一次转人工都是一个已经暴露的知识盲点。它比覆盖度矩阵
            # 推断出来的空格子更值钱——带着用户的原话，证明真的有人问
            state.handover_ticket_id = deps.store.save_handover(state)
    return state
