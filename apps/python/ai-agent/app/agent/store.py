"""埋点与工单落库。

**Trace 不是日志，是训练数据的采集管道。** 日志写完就是给人看的，
写丢一条无所谓；Trace 是下游七八个用途的原料——标定检索阈值、
识别死知识、监控幻觉率、攒 DPO 偏好对——写丢一条就是少一个样本。

但它也不能挡住对话。落库失败时宁可丢埋点也要把答案发给用户，
所以所有写入都吞异常，只在内部计数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .state import AgentState, TraceRecord


@runtime_checkable
class TraceStore(Protocol):
    def save(self, trace: TraceRecord) -> None: ...

    def save_handover(self, state: AgentState) -> int | None:
        """记一张转人工工单，返回工单号。"""
        ...

    def record_feedback(self, trace_id: str, thumb: int, reason: str = "") -> bool:
        ...


class NullTraceStore:
    """不落库。测试与离线调试用。"""

    def __init__(self) -> None:
        self.traces: list[TraceRecord] = []
        self.handovers: list[AgentState] = []
        self.feedback: list[tuple[str, int, str]] = []

    def save(self, trace: TraceRecord) -> None:
        self.traces.append(trace)

    def save_handover(self, state: AgentState) -> int | None:
        self.handovers.append(state)
        return len(self.handovers)

    def record_feedback(self, trace_id: str, thumb: int, reason: str = "") -> bool:
        self.feedback.append((trace_id, thumb, reason))
        return True


@dataclass
class MySqlTraceStore:
    """写 MySQL。与数据中台同一个库——埋点最终要和清洗流水线合流。"""

    engine: Any
    errors: list[str] = field(default_factory=list)
    """落库失败不抛出，但要留痕。静默失败会让人以为埋点在工作。"""

    @classmethod
    def from_env(cls) -> "MySqlTraceStore":
        try:
            from smartmall_pipeline.repository import DwsRepository
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "埋点落库需要 smartmall-pipeline：pip install -e pipelines"
            ) from exc
        return cls(engine=DwsRepository.from_env().engine)

    # ---------------------------------------------------------------- 写入

    def save(self, trace: TraceRecord) -> None:
        from sqlalchemy import text

        sql = text(
            "INSERT INTO agent_trace "
            "(trace_id, session_id, user_id, product_id, input_text,"
            " rewritten_query, intent,"
            " retrieval_hit_count, retrieval_max_score, retrieval_item_ids,"
            " answer, citations, postcheck_flags, handover, handover_reason,"
            " model, latency_ms, error) "
            "VALUES (:trace_id, :session_id, :user_id, :product_id,"
            " :input_text, :rewritten_query, :intent,"
            " :hit_count, :max_score, :item_ids,"
            " :answer, :citations, :flags, :handover, :handover_reason,"
            " :model, :latency, :error)"
            # 刻意不写 upsert：trace_id 每轮新生成，撞车说明别处有 bug，
            # 静默覆盖会把那个 bug 藏起来。让唯一索引报错、记进 errors 更好。
            # 顺带一个好处：这条 SQL 不带方言，测试能跑在 SQLite 上，
            # 而字段有没有写对，只有真正执行过 SQL 才知道。
        )
        params = {
            "trace_id": trace.trace_id,
            "session_id": trace.session_id,
            "user_id": trace.user_id,
            "product_id": trace.product_id,
            "input_text": trace.input_text,
            "rewritten_query": trace.rewritten_query[:512],
            "intent": trace.intent,
            "hit_count": trace.retrieval_hit_count,
            # DECIMAL(6,4) 存不下 1 以上的值；相似度理论上 ≤1，但
            # 换了模型或改了融合算法可能超出，截断比整条写失败强
            "max_score": min(max(trace.retrieval_max_score, -9.0), 9.0),
            "item_ids": json.dumps(trace.retrieval_item_ids),
            "answer": trace.answer,
            "citations": json.dumps(trace.citations),
            "flags": json.dumps(trace.postcheck_flags, ensure_ascii=False),
            "handover": 1 if trace.handover else 0,
            "handover_reason": trace.handover_reason[:64],
            "model": trace.model[:64],
            "latency": json.dumps(trace.latency_ms),
            "error": trace.error[:512],
        }
        self._exec(sql, params, what="trace")

    def save_handover(self, state: AgentState) -> int | None:
        """记一张工单。

        这是飞轮的入口：每一次转人工都是一个**已经暴露**的知识盲点，
        比覆盖度矩阵推断出来的空格子更值钱——它带着用户的原话，
        证明这个问题真的有人问。
        """
        from sqlalchemy import text

        reason = state.handover_reason.value if state.handover_reason else ""
        sql = text(
            "INSERT INTO handover_ticket "
            "(trace_id, session_id, user_id, reason, intent, product_id,"
            " question, summary) "
            "VALUES (:trace_id, :session_id, :user_id, :reason, :intent,"
            " :product_id, :question, :summary)"
        )
        params = {
            "trace_id": state.trace.trace_id,
            "session_id": state.session.session_id,
            "user_id": state.session.user_id,
            "reason": reason[:64],
            "intent": state.intent.value,
            "product_id": state.session.current_product_id,
            "question": state.message[:512],
            "summary": json.dumps(state.handover_summary, ensure_ascii=False),
        }
        return self._exec(sql, params, what="handover", want_id=True)

    def record_feedback(self, trace_id: str, thumb: int, reason: str = "") -> bool:
        """回写点赞点踩。

        点踩的回复直接作为 DPO 负例；原因里的"态度生硬""太啰嗦"
        正对应微调要解决的风格问题，价值最高。
        """
        from sqlalchemy import text

        sql = text(
            "UPDATE agent_trace SET thumb = :thumb, feedback_reason = :reason,"
            # CURRENT_TIMESTAMP 而不是 NOW()：前者是标准 SQL，MySQL 与 SQLite
            # 都认，于是这条语句能在测试里被真正执行一遍
            " feedback_at = CURRENT_TIMESTAMP WHERE trace_id = :trace_id"
        )
        n = self._exec(
            sql,
            {"thumb": thumb, "reason": reason[:64], "trace_id": trace_id},
            what="feedback",
            want_rowcount=True,
        )
        return bool(n)

    # ---------------------------------------------------------------- 读取

    def recent(self, limit: int = 20, *, handover_only: bool = False) -> list[dict]:
        from sqlalchemy import text

        where = "WHERE handover = 1 " if handover_only else ""
        with self.engine.connect() as conn:
            return [
                dict(r) for r in conn.execute(text(
                    "SELECT trace_id, created_at, intent, input_text, answer,"
                    " retrieval_hit_count, retrieval_max_score, handover,"
                    " handover_reason, thumb "
                    f"FROM agent_trace {where}ORDER BY id DESC LIMIT :n"
                ), {"n": limit}).mappings()
            ]

    def blind_spots(self, limit: int = 20) -> list[dict]:
        """按题面聚合的知识盲点。

        同一个问题反复转人工，说明它既是真需求又没有知识——
        补写顺序直接按这个排，比拍脑袋准得多。
        """
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return [
                dict(r) for r in conn.execute(text(
                    "SELECT question, COUNT(*) AS times, MAX(reason) AS reason,"
                    " MIN(id) AS ticket_id, MAX(status) AS status "
                    "FROM handover_ticket GROUP BY question "
                    "ORDER BY times DESC, ticket_id ASC LIMIT :n"
                ), {"n": limit}).mappings()
            ]

    def product_demand(self, product_id: int, limit: int = 12) -> list[dict]:
        """用户对这件商品实际问得最多的是什么。

        **这是运营 Agent 区别于"套模板写文案"的唯一依据。** 运营拍脑袋
        想的卖点是"高级感"，而用户反复问的是"会不会起球"——后者才该上
        主图。这条查询就是数据飞轮在运营侧的那一环：客服数据反哺文案生产。

        按题面聚合，同时带上「答上来了没有」：答不上来的问题**同样是需求
        信号**，而且更强——用户想知道、我们连答案都没有，那更该在文案里
        主动讲清楚。
        """
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return [
                dict(r) for r in conn.execute(text(
                    "SELECT input_text AS question, COUNT(*) AS times,"
                    " MAX(intent) AS intent, SUM(handover) AS unanswered "
                    "FROM agent_trace WHERE product_id = :pid"
                    " AND input_text <> '' "
                    "GROUP BY input_text "
                    "ORDER BY times DESC, question ASC LIMIT :n"
                ), {"pid": product_id, "n": limit}).mappings()
            ]

    # ---------------------------------------------------------------- 内部

    def _exec(self, sql, params, *, what: str,
              want_id: bool = False, want_rowcount: bool = False):
        """执行写入，失败只记录不抛出。

        埋点绝不能挡住对话——用户等着答案，而"这一轮没记上"
        的代价远小于"这一轮没回复"。
        """
        try:
            with self.engine.begin() as conn:
                result = conn.execute(sql, params)
                if want_id:
                    return result.lastrowid
                if want_rowcount:
                    return result.rowcount
                return None
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{what}: {type(exc).__name__}: {exc}")
            return None
