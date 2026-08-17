"""埋点与工单落库。

两条不能破的规则：

1. **埋点绝不能挡住对话。** 用户等的是回复，"这一轮没记上"的代价
   远小于"这一轮没回复"。所以写入失败只记录、不抛出。
2. **但失败要留痕。** 静默吞掉异常会让人以为埋点在工作，
   等到要用这批数据训练时才发现库里是空的。

跑在真实 SQLite 上——要验的是 SQL 本身，内存桩验不出字段写没写对。
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from app.agent.state import (  # noqa: E402
    AgentState, HandoverReason, Intent, SessionContext, TraceRecord,
)
from app.agent.store import MySqlTraceStore, NullTraceStore  # noqa: E402

_DDL = [
    """CREATE TABLE agent_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
        user_id INTEGER, product_id INTEGER, input_text TEXT NOT NULL,
        rewritten_query TEXT DEFAULT '', intent TEXT DEFAULT '',
        retrieval_hit_count INTEGER DEFAULT 0,
        retrieval_max_score REAL DEFAULT 0, retrieval_item_ids TEXT,
        answer TEXT, citations TEXT, postcheck_flags TEXT,
        handover INTEGER DEFAULT 0, handover_reason TEXT DEFAULT '',
        model TEXT DEFAULT '', latency_ms TEXT, error TEXT DEFAULT '',
        thumb INTEGER, feedback_reason TEXT DEFAULT '', feedback_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE handover_ticket (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL, session_id TEXT NOT NULL, user_id INTEGER,
        reason TEXT NOT NULL, intent TEXT DEFAULT '', product_id INTEGER,
        question TEXT NOT NULL, summary TEXT,
        status TEXT DEFAULT 'open', human_answer TEXT,
        knowledge_item_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, answered_at TEXT)""",
]


@pytest.fixture
def store():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(text(ddl))
    # SQLite 不认 MySQL 的 ON DUPLICATE KEY UPDATE，用 OR REPLACE 等价替代
    s = MySqlTraceStore(engine=engine)
    return s


def _rows(store, table: str) -> list[dict]:
    from sqlalchemy import text

    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            text(f"SELECT * FROM {table}")
        ).mappings()]


def _trace(**kw) -> TraceRecord:
    t = TraceRecord(session_id="s1", input_text="这件是什么面料")
    t.intent = "product_knowledge"
    t.retrieval_hit_count = 3
    t.retrieval_max_score = 0.812
    t.retrieval_item_ids = [12, 7, 3]
    t.answer = "100%羊毛 [#12]"
    t.citations = [12]
    t.latency_ms = {"intent": 120, "retrieval": 60, "total": 900}
    for k, v in kw.items():
        setattr(t, k, v)
    return t


class TestNullStore:
    def test_records_without_a_database(self):
        """没有数据库时也要能跑——测试和离线调试都靠它。"""
        s = NullTraceStore()
        s.save(_trace())
        assert len(s.traces) == 1


class TestTracePersistence:
    def test_saves_the_fields_downstream_needs(self, store):
        store.save(_trace())
        rows = _rows(store, "agent_trace")
        assert len(rows) == 1 and not store.errors

        r = rows[0]
        assert r["intent"] == "product_knowledge"
        assert r["retrieval_hit_count"] == 3
        assert abs(r["retrieval_max_score"] - 0.812) < 1e-6
        # 命中分布用来识别从未被命中的死知识
        assert "12" in r["retrieval_item_ids"]
        assert r["citations"] == "[12]"

    def test_chinese_is_not_escaped(self, store):
        """合规标记是中文。转成 \\uXXXX 会让人肉眼查库时读不懂。"""
        store.save(_trace(postcheck_flags=["绝对化用语:最好"]))
        assert "绝对化用语" in _rows(store, "agent_trace")[0]["postcheck_flags"]

    def test_out_of_range_score_is_clamped_not_dropped(self, store):
        """相似度理论上 ≤1，但换模型或改融合算法可能超出。

        截断比整条写失败强——为了一个越界的分数丢掉整条埋点不划算。
        """
        store.save(_trace(retrieval_max_score=42.0))
        assert not store.errors
        assert _rows(store, "agent_trace")[0]["retrieval_max_score"] <= 9.0

    def test_overlong_fields_are_truncated(self, store):
        """列宽有限。截断比抛异常强，同理。"""
        store.save(_trace(error="X" * 5000, model="M" * 500))
        assert not store.errors
        r = _rows(store, "agent_trace")[0]
        assert len(r["error"]) <= 512 and len(r["model"]) <= 64

    def test_write_failure_does_not_raise(self, store):
        """埋点绝不能挡住对话。"""
        from sqlalchemy import text

        with store.engine.begin() as conn:
            conn.execute(text("DROP TABLE agent_trace"))

        store.save(_trace())          # 不该抛
        assert store.errors, "但失败必须留痕，否则会以为埋点在工作"
        assert "trace" in store.errors[0]


class TestHandoverTicket:
    def _state(self, **kw) -> AgentState:
        st = AgentState(
            message="这件能机洗吗",
            session=SessionContext(user_id=10086, current_product_id=1024),
        )
        st.intent = Intent.PRODUCT_KNOWLEDGE
        st.to_handover(HandoverReason.NO_KNOWLEDGE)
        st.handover_summary = {"summary": "用户问羊毛衫能否机洗，知识库没有"}
        for k, v in kw.items():
            setattr(st, k, v)
        return st

    def test_ticket_carries_the_unanswered_question(self, store):
        """题面就是补写任务的题目——丢了它，工单毫无用处。"""
        ticket_id = store.save_handover(self._state())
        assert ticket_id
        r = _rows(store, "handover_ticket")[0]
        assert r["question"] == "这件能机洗吗"
        assert r["reason"] == HandoverReason.NO_KNOWLEDGE.value
        assert r["product_id"] == 1024
        assert r["status"] == "open"

    def test_summary_is_stored_readable(self, store):
        store.save_handover(self._state())
        assert "机洗" in _rows(store, "handover_ticket")[0]["summary"]

    def test_ticket_links_back_to_the_trace(self, store):
        """没有 trace_id 就没法回答"这次转人工时检索到了什么"。"""
        st = self._state()
        store.save_handover(st)
        assert _rows(store, "handover_ticket")[0]["trace_id"] == st.trace.trace_id

    def test_repeated_question_creates_separate_tickets(self, store):
        """频次就是优先级，所以不去重——聚合是查询时的事。"""
        for _ in range(3):
            store.save_handover(self._state())
        assert len(_rows(store, "handover_ticket")) == 3

    def test_blind_spots_rank_by_frequency(self, store):
        for _ in range(3):
            store.save_handover(self._state())
        store.save_handover(self._state(message="尺码怎么选"))

        spots = store.blind_spots()
        assert spots[0]["question"] == "这件能机洗吗"
        assert spots[0]["times"] == 3


class TestFeedback:
    def test_thumb_is_written_back(self, store):
        t = _trace()
        store.save(t)
        assert store.record_feedback(t.trace_id, -1, "太啰嗦")

        r = _rows(store, "agent_trace")[0]
        assert r["thumb"] == -1
        # 「态度生硬」「太啰嗦」直接对应微调要解决的风格问题
        assert r["feedback_reason"] == "太啰嗦"

    def test_unknown_trace_reports_failure(self, store):
        """默默成功会让人以为反馈记上了，实际一条都没有。"""
        assert store.record_feedback("不存在的id", 1) is False

    def test_thumb_up_is_recorded(self, store):
        t = _trace()
        store.save(t)
        store.record_feedback(t.trace_id, 1)
        assert _rows(store, "agent_trace")[0]["thumb"] == 1


class TestGraphIntegration:
    """图跑完必须落库，且落库失败不能影响回复。"""

    def _deps(self, store):
        from app.agent.llm import FakeLlmClient
        from app.agent.nodes import Deps
        from app.agent.retriever import StubRetriever
        from app.agent.state import Citation

        return Deps(
            llm=FakeLlmClient(),
            retriever=StubRetriever([Citation(
                item_id=1, title="面料", content="100%羊毛", dense_score=0.8
            )]),
            store=store,
        )

    def test_every_turn_is_traced(self, store):
        from app.agent.graph import safe_run_turn

        deps = self._deps(store)
        state = AgentState()
        for msg in ("这件是什么面料", "那会起球吗"):
            state.trace = TraceRecord()
            safe_run_turn(msg, state, deps)

        assert len(_rows(store, "agent_trace")) == 2

    def test_handover_creates_a_ticket_and_surfaces_its_id(self, store):
        from app.agent.graph import safe_run_turn

        deps = self._deps(store)
        deps.retriever.hits = []  # type: ignore[attr-defined]
        state = safe_run_turn("量子力学", AgentState(), deps)

        assert state.handover
        assert state.handover_ticket_id, "工单号要回到状态里，前端和 CLI 都要显示"
        assert len(_rows(store, "handover_ticket")) == 1

    def test_broken_store_does_not_break_the_reply(self, store):
        """落库挂了，用户照样要收到答案。"""
        from sqlalchemy import text

        from app.agent.graph import safe_run_turn

        with store.engine.begin() as conn:
            conn.execute(text("DROP TABLE agent_trace"))

        state = safe_run_turn("这件是什么面料", AgentState(), self._deps(store))
        assert state.answer and not state.handover
        assert store.errors

    def test_no_store_means_no_crash(self):
        """默认不落库。Agent 在没有数据库的环境里必须照样能跑。"""
        from app.agent.graph import safe_run_turn

        deps = self._deps(None)
        deps.store = None
        assert safe_run_turn("这件是什么面料", AgentState(), deps).answer


class TestProductDemand:
    """「用户对这件商品问得最多的是什么」。

    这是运营 Agent 区别于"套模板写文案"的唯一依据。埋点里原先没有
    product_id，于是这个问题只能靠转人工工单来猜——而工单只记录了
    **答不上来**的那些，是有偏的样本："会起球吗"答得上来就永远不会
    出现在里面。
    """

    def test_product_id_is_persisted(self, store):
        """不落库的话 product_demand 永远返回空，
        而且是**静默**的空——运营 Agent 会安静地退化成套模板。"""
        trace = TraceRecord(trace_id="t-pid", session_id="s1",
                            product_id=9001, input_text="会起球吗")
        store.save(trace)
        rows = _rows(store, "agent_trace")
        assert rows and rows[0]["product_id"] == 9001

    def test_demand_is_grouped_by_question(self, store):
        for i, q in enumerate(["会起球吗", "会起球吗", "厚吗"]):
            store.save(TraceRecord(trace_id=f"d{i}", session_id="s",
                                   product_id=9001, input_text=q))
        got = store.product_demand(9001)
        assert [r["question"] for r in got] == ["会起球吗", "厚吗"]
        assert got[0]["times"] == 2

    def test_other_products_do_not_leak_in(self, store):
        store.save(TraceRecord(trace_id="a", session_id="s",
                               product_id=9001, input_text="会起球吗"))
        store.save(TraceRecord(trace_id="b", session_id="s",
                               product_id=9002, input_text="防水吗"))
        assert [r["question"] for r in store.product_demand(9001)] == ["会起球吗"]

    def test_unanswered_count_comes_through(self, store):
        """答不上来的问题是**更强**的需求信号——用户想知道、
        我们连答案都没有，那更该在文案里主动讲清楚。"""
        t = TraceRecord(trace_id="u1", session_id="s", product_id=9001,
                        input_text="能机洗吗")
        t.handover = True
        store.save(t)
        store.save(TraceRecord(trace_id="u2", session_id="s", product_id=9001,
                               input_text="能机洗吗"))
        got = store.product_demand(9001)
        assert got[0]["times"] == 2 and int(got[0]["unanswered"]) == 1
