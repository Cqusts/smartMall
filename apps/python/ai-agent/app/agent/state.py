"""客服 Agent 的状态定义。

LangGraph 的每个节点都读写同一个 ``AgentState``。把它定义成一个显式的
数据结构（而不是随手传 dict），是因为这个状态最终要**整份**写进 Trace——
而 Trace 不是日志，是训练数据的采集管道（docs/05 第 7 节）。字段设计
按训练数据的标准来：每一个字段都要能回答"下游谁会用它"。

节点之间只通过这个状态通信，不共享其他可变对象。这样任何一个节点都能
单独构造输入、单独断言输出，不需要跑完整条图。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class Intent(str, Enum):
    """七类意图。分类结果决定后续走哪条分支。"""

    PRODUCT_KNOWLEDGE = "product_knowledge"
    SIZING = "sizing"
    REALTIME_STOCK_PRICE = "realtime_stock_price"
    ORDER_LOGISTICS = "order_logistics"
    AFTERSALE = "aftersale"
    SENSITIVE = "sensitive"
    CHITCHAT = "chitchat"

    @property
    def needs_retrieval(self) -> bool:
        return self in (
            Intent.PRODUCT_KNOWLEDGE,
            Intent.SIZING,
            Intent.AFTERSALE,
        )

    @property
    def needs_realtime(self) -> bool:
        """需要实时数据——知识库里的答案会是错的。

        库存和物流每分钟都在变，检索到的历史对话说"有货"毫无意义。
        这类意图必须走工具，不能走 RAG。
        """
        return self in (Intent.REALTIME_STOCK_PRICE, Intent.ORDER_LOGISTICS)


class HandoverReason(str, Enum):
    NO_KNOWLEDGE = "知识库无相关内容"
    LOW_CONFIDENCE = "检索置信度过低"
    SENSITIVE_INTENT = "涉及议价/投诉/退款"
    USER_REQUESTED = "用户主动要求转人工"
    POSTCHECK_FAILED = "输出合规检查未通过"
    TOOL_FAILURE = "依赖服务不可用"
    INTERNAL_ERROR = "内部异常"
    SELF_HARM = "用户表达自伤倾向"
    """单独一类，不并进"超出服务范围"。人工队列要能优先看到它，
    盲点统计也不该把它当成"知识库缺内容"去补知识。"""


@dataclass
class Citation:
    """答案里的一条引用。

    ``item_id`` 必须能回溯到 knowledge_item——没有它就没法做溯源，
    也没法统计"哪些知识从未被命中"（那些是可以下线的死知识）。
    """

    item_id: int
    title: str
    content: str
    score: float = 0.0
    dense_score: float = 0.0
    bm25_score: float = 0.0
    lexical_overlap: float = 0.0
    """查询里有区分度的词匹配上了多少，0~1（IDF 加权）。

    别用 ``bm25_score > 0`` 代替它：bigram 分词下，"你们支持花呗分期吗"
    和"支持的快递：默认发顺丰"共享一个``支持``就有分，而知识库里
    关于花呗一个字都没有。"""

    @property
    def marker(self) -> str:
        return f"[#{self.item_id}]"


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    content: str
    ts: float = field(default_factory=time.time)


@dataclass
class SessionContext:
    """跨轮次保留的会话上下文。

    ``current_product_id`` 是这里最重要的字段——它决定检索的过滤范围。
    用户从商品页进会话时带入；聊到一半换商品要能识别并更新，
    否则会拿 A 商品的知识回答 B 商品的问题，而这种错误看起来很像
    "模型幻觉"，实际是上下文管理的锅。
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    user_id: int | None = None
    current_product_id: int | None = None
    category_id: int | None = None
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    summary_until_turn: int = 0
    handover_count: int = 0
    negative_streak: int = 0
    """连续表达不满的轮数。连续 3 轮触发转人工——
    用户已经在生气了，再让 AI 试第四次只会更糟。"""

    def append(self, role: str, content: str) -> None:
        self.turns.append(Turn(role=role, content=content))  # type: ignore[arg-type]

    def recent(self, n: int = 6) -> list[Turn]:
        return self.turns[-n:]


@dataclass
class TraceRecord:
    """一次对话的全量埋点。

    下游用途见 docs/05 第 7 节：检索命中分布用来识别死知识，
    max_score 配合点赞点踩用来标定阈值，postcheck_flags 用来监控幻觉率，
    点踩记录直接当 DPO 负例。
    """

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = ""
    user_id: int | None = None
    ts: float = field(default_factory=time.time)

    input_text: str = ""
    rewritten_query: str = ""
    intent: str = ""

    retrieval_hit_count: int = 0
    retrieval_max_score: float = 0.0
    retrieval_item_ids: list[int] = field(default_factory=list)
    retrieval_lexical_overlap: float = 0.0
    """最高的词汇覆盖率。和 max_score 一起看才能判断"到底有没有"——
    分数中等而覆盖率接近 0，说明这点相似度纯粹是向量空间的基线。"""
    notes: list[str] = field(default_factory=list)
    """降级、退回弱判据这类"跑通了但不是正常路径"的记录。
    不记下来的话，线上悄悄退化成旧行为是看不出来的。"""

    tools_called: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    citations: list[int] = field(default_factory=list)
    postcheck_flags: list[str] = field(default_factory=list)
    handover: bool = False
    handover_reason: str = ""

    model: str = ""
    latency_ms: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass
class AgentState:
    """一次请求在图中流转的全部状态。"""

    # ---- 输入 ----
    message: str = ""
    session: SessionContext = field(default_factory=SessionContext)

    # ---- 中间产物 ----
    intent: Intent = Intent.PRODUCT_KNOWLEDGE
    query: str = ""
    """实际用于检索的查询。可能被 Rewrite 节点改写过。"""
    rewritten: bool = False
    hits: list[Citation] = field(default_factory=list)
    tool_results: dict[str, Any] = field(default_factory=dict)
    needs_knowledge: bool = False
    """寒暄分支判定这条消息其实需要事实才能回答，要退回主链路走检索。

    寒暄是七类里的兜底类，分不进另外六类的都落这儿；而它不检索、
    无引用。没有这个回退，兜底类就成了"随便编"的通道。"""

    # ---- 输出 ----
    answer: str = ""
    citations: list[Citation] = field(default_factory=list)
    clarify_question: str = ""
    handover: bool = False
    handover_reason: HandoverReason | None = None
    handover_summary: dict[str, Any] = field(default_factory=dict)
    handover_ticket_id: int | None = None
    """落库后的工单号。人工按它接手，也是补写任务的句柄。"""
    postcheck_flags: list[str] = field(default_factory=list)
    blocked: bool = False
    """输入安全检查未通过，直接回绝，不进入后续任何节点。"""

    trace: TraceRecord = field(default_factory=TraceRecord)

    @property
    def max_score(self) -> float:
        return max((h.dense_score for h in self.hits), default=0.0)

    def to_handover(self, reason: HandoverReason) -> None:
        self.handover = True
        self.handover_reason = reason
