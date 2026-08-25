"""跨 Agent 闭环。

**这一份里最重要的是「不派」那一组。** 写一个"转人工就派任务"的规则
只要一行，而且看起来很对——直到它给一句自伤倾向的求助派了一条
"待补写的知识"。一个只有通过分支的判据等于没有判据。

第二重要的是「派完了不回滚」：知识已经真的写进库了，因为派下一环失败
就把上一环标成失败，下次重试会重新补写一遍，库里多一条重复知识。
"""

from __future__ import annotations

import pytest

from app.agent.knowledge.state import SpotState
from app.agent.marketing.state import MarketingState
from app.agent.nodes import Deps
from app.agent.retriever import StubRetriever
from app.agent.state import AgentState, HandoverReason, Intent, SessionContext
from app.agent.tasks import dispatch, runner
from app.agent.tasks.state import Agent, Kind, Status, dedupe_key, normalize
from app.agent.tasks.store import StubTaskStore


# ---------------------------------------------------------------- 闸门


class TestGate:
    """什么情况下**不**派活。这一组是整条闭环里唯一需要判断力的地方。"""

    @pytest.mark.parametrize("reason", [
        HandoverReason.NO_KNOWLEDGE,
        HandoverReason.LOW_CONFIDENCE,
        HandoverReason.POSTCHECK_FAILED,
    ])
    def test_知识盲点要派(self, reason):
        assert dispatch.judge_handover(reason, "这件羊毛衫会起球吗").ok

    def test_自伤求助绝不派(self):
        """**这是这套系统能做的最糟糕的事，没有之一**：
        一个人在求助，而我们把它变成了一条"待补写的知识"。"""
        v = dispatch.judge_handover(HandoverReason.SELF_HARM, "我不想活了")
        assert not v.ok

    def test_用户主动要人工不派(self):
        """用户就是想找人，不是我们不知道。"""
        assert not dispatch.judge_handover(
            HandoverReason.USER_REQUESTED, "我要转人工").ok

    def test_议价投诉退款不派(self):
        """要人拍板，不是知识缺失。补一条「退款政策」进去，
        下次 AI 就会拿它硬答。"""
        assert not dispatch.judge_handover(
            HandoverReason.SENSITIVE_INTENT, "能不能便宜点").ok

    @pytest.mark.parametrize("reason", [
        HandoverReason.TOOL_FAILURE, HandoverReason.INTERNAL_ERROR,
    ])
    def test_故障不是盲点(self, reason):
        """服务挂了，补知识治不了。"""
        assert not dispatch.judge_handover(reason, "我的订单到哪了").ok

    def test_每一种转人工原因都表过态(self):
        """新增一种原因时必须在两张表里做一次选择。

        **落进补集会被默默地派出去**——而"默默"正是问题：加一种
        「用户辱骂」之后，系统会开始给它补知识，没有任何人会发现。
        """
        covered = set(dispatch._DISPATCH) | dispatch._NEVER
        missing = [r.name for r in HandoverReason if r not in covered]
        assert not missing, f"这些原因没表态：{missing}"

    def test_没表态的默认不派(self):
        """就算真漏了一种，也得是不派。"""
        class Fake:
            value = "新来的原因"
        assert not dispatch.judge_handover(Fake(), "一个正常的问题").ok

    def test_太短的问题不派(self):
        """"?" "在吗" 补不出任何知识，而它们会把队列灌满，
        然后队列就没人看了。"""
        for q in ("?", "在吗", "。。。", "  "):
            assert not dispatch.judge_handover(
                HandoverReason.NO_KNOWLEDGE, q).ok

    def test_被问得多的排前面(self):
        one = dispatch.judge_handover(HandoverReason.NO_KNOWLEDGE, "会起球吗", 1)
        many = dispatch.judge_handover(HandoverReason.NO_KNOWLEDGE, "会起球吗", 8)
        assert many.priority > one.priority

    def test_合规拦截的优先级最低(self):
        """补一条知识不一定能解决，派一条低优先级的让人自己判断。"""
        a = dispatch.judge_handover(HandoverReason.NO_KNOWLEDGE, "会起球吗")
        b = dispatch.judge_handover(HandoverReason.POSTCHECK_FAILED, "会起球吗")
        assert b.priority < a.priority

    def test_不派也说得出原因(self):
        """只说"没排上"的话，"为什么这个盲点没排上"只能靠读代码。"""
        v = dispatch.judge_handover(HandoverReason.TOOL_FAILURE, "订单到哪了")
        assert v.reason and "盲点" in v.reason


class TestFollowupGate:
    """知识补完了，要不要顺手让运营重写文案。"""

    def test_写进去了才派(self):
        assert dispatch.judge_followup("drafted", 9001).ok

    def test_库里本来就有就不派(self):
        """什么都没变，文案自然也不用动。"""
        assert not dispatch.judge_followup("already_covered", 9001).ok

    def test_试跑不产生真任务(self):
        """draft_only 是试跑。试跑派出一条真任务，等于试跑有副作用。"""
        assert not dispatch.judge_followup("draft_only", 9001).ok

    def test_全店政策与商品文案无关(self):
        """"怎么退货"跟哪件商品的文案都没关系。给它派一条
        "更新文案"，运营点开会一脸茫然。"""
        assert not dispatch.judge_followup("drafted", None).ok


# ---------------------------------------------------------------- 去重


class TestDedupe:
    def test_同一句话只排一条(self):
        store = StubTaskStore()
        for _ in range(5):
            store.enqueue(**dispatch.knowledge_task(
                "这件会起球吗", reason="x", priority=1))
        assert len(store.rows) == 1
        assert store.rows[0].times == 5

    def test_标点空格不算不同的问题(self):
        assert (dedupe_key(Kind.WRITE_KNOWLEDGE, "怎么退货？")
                == dedupe_key(Kind.WRITE_KNOWLEDGE, " 怎么退货 "))

    def test_不同商品是不同的盲点(self):
        """"这件会起球吗"问的是哪件，决定了要补的知识完全不同。"""
        assert (dedupe_key(Kind.WRITE_KNOWLEDGE, "会起球吗", 9001)
                != dedupe_key(Kind.WRITE_KNOWLEDGE, "会起球吗", 9002))

    def test_做完之后同一个问题能再排(self):
        """知识会过期、会被下线。半年后同一个问题又答不上来，
        本该重新排一次——UNIQUE(dedupe_key) 会让它一辈子只排一次。"""
        store = StubTaskStore()
        first = store.enqueue(**dispatch.knowledge_task(
            "怎么退货", reason="x", priority=1))
        store.finish(first.id, Status.DONE)

        again = store.enqueue(**dispatch.knowledge_task(
            "怎么退货", reason="x", priority=1))
        assert again.id != first.id
        assert len(store.rows) == 2

    def test_归一化不吃掉汉字(self):
        assert normalize("怎么, 退货？！") == "怎么退货"


# ---------------------------------------------------------------- 认领


class TestClaim:
    def test_抢到的只有一个(self):
        """先 SELECT 出 pending 再 UPDATE 的话，两个 worker 会同时读到
        pending 然后都去做——一条盲点被补写两次。"""
        store = StubTaskStore()
        t = store.enqueue(**dispatch.knowledge_task("会起球吗", reason="x",
                                                    priority=1))
        assert store.claim(t.id) is True
        assert store.claim(t.id) is False

    def test_认领一次算一次尝试(self):
        store = StubTaskStore()
        t = store.enqueue(**dispatch.knowledge_task("会起球吗", reason="x",
                                                    priority=1))
        store.claim(t.id)
        assert t.attempts == 1

    def test_失败且还有次数就退回队列(self):
        store = StubTaskStore()
        t = store.enqueue(**dispatch.knowledge_task("会起球吗", reason="x",
                                                    priority=1))
        store.claim(t.id)
        store.finish(t.id, Status.FAILED, error="临时故障")
        assert t.status == Status.PENDING
        assert store.pull()

    def test_次数用光才真的失败(self):
        store = StubTaskStore()
        t = store.enqueue(**dispatch.knowledge_task("会起球吗", reason="x",
                                                    priority=1))
        for _ in range(t.max_attempts):
            store.claim(t.id)
            store.finish(t.id, Status.FAILED, error="一直挂")
        assert t.status == Status.FAILED
        assert not store.pull(), "耗光次数的任务不该还在队列里转"

    def test_按优先级出队(self):
        store = StubTaskStore()
        low = store.enqueue(**dispatch.knowledge_task("甲问题啊", reason="x",
                                                      priority=1))
        high = store.enqueue(**dispatch.knowledge_task("乙问题啊", reason="x",
                                                       priority=99))
        assert [t.id for t in store.pull()] == [high.id, low.id]


# ---------------------------------------------------------------- 客服派活


def _cs_state(reason: HandoverReason, msg="这件羊毛衫会起球吗",
              product_id=9001) -> AgentState:
    st = AgentState(session=SessionContext(user_id=10086,
                                           current_product_id=product_id))
    st.message = msg
    st.intent = Intent.PRODUCT_KNOWLEDGE
    st.to_handover(reason)
    st.handover_ticket_id = 77
    return st


class TestDispatchFromHandover:
    def _deps(self, **kw) -> Deps:
        return Deps(llm=None, retriever=StubRetriever([]),
                    tasks=StubTaskStore(), **kw)

    def test_盲点转人工会派活(self):
        deps = self._deps()
        task = runner.dispatch_handover(
            _cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        assert task is not None
        assert task.kind == Kind.WRITE_KNOWLEDGE
        assert task.source_agent == Agent.CUSTOMER_SERVICE
        assert task.target_agent == Agent.KNOWLEDGE_OPS
        assert task.payload["question"] == "这件羊毛衫会起球吗"
        assert task.payload["ticket_id"] == 77
        assert task.product_id == 9001

    def test_自伤求助不派(self):
        deps = self._deps()
        assert runner.dispatch_handover(
            _cs_state(HandoverReason.SELF_HARM, "我不想活了"), deps) is None
        assert deps.tasks.rows == []

    def test_没接任务表就不派(self):
        deps = Deps(llm=None, retriever=StubRetriever([]), tasks=None)
        assert runner.dispatch_handover(
            _cs_state(HandoverReason.NO_KNOWLEDGE), deps) is None

    def test_派活失败不能影响回复(self):
        """这是在客服的回复路径上。派活失败少补一条知识，
        抛出去用户就看不到回复了。"""
        deps = self._deps()
        deps.tasks.fail = True
        assert runner.dispatch_handover(
            _cs_state(HandoverReason.NO_KNOWLEDGE), deps) is None

    def test_emit_节点真的接上了(self):
        """判据写对了但没人调用，等于没写。"""
        from app.agent.nodes import emit

        deps = self._deps()
        state = emit(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)
        assert state.dispatched_task_id is not None
        assert len(deps.tasks.rows) == 1

    def test_不该派的时候emit也不派(self):
        from app.agent.nodes import emit

        deps = self._deps()
        state = emit(_cs_state(HandoverReason.USER_REQUESTED, "我要转人工"), deps)
        assert state.dispatched_task_id is None
        assert deps.tasks.rows == []


# ---------------------------------------------------------------- 执行


class FakeSpotRun:
    """替掉 safe_run_spot。按 outcome 编排结果。"""

    def __init__(self, outcome="drafted", item_id=501):
        self.outcome, self.item_id = outcome, item_id
        self.seen: list = []

    def __call__(self, spot, deps, **kw):
        self.seen.append(spot)
        st = SpotState(spot=spot, outcome=self.outcome)
        st.item_id = self.item_id if self.outcome == "drafted" else None
        st.draft = "羊毛衫做过抗起球处理，日常穿着不易起球。"
        return st


class FakeCopyRun:
    def __init__(self, outcome="staged"):
        self.outcome = outcome
        self.seen: list = []

    def __call__(self, brief, deps):
        self.seen.append(brief)
        st = MarketingState(brief=brief, outcome=self.outcome)
        st.copy_id = 601 if self.outcome == "staged" else None
        return st


@pytest.fixture
def wired(monkeypatch):
    """一套接好的依赖 + 两个假 Agent。"""
    spot, copy = FakeSpotRun(), FakeCopyRun()
    monkeypatch.setattr("app.agent.knowledge.graph.safe_run_spot", spot)
    monkeypatch.setattr("app.agent.marketing.graph.safe_run_copy", copy)
    deps = Deps(llm=None, retriever=StubRetriever([]), tasks=StubTaskStore())
    return deps, spot, copy


class TestRunner:
    def test_闭环走完三环(self, wired):
        """**这条是整个增量的验收**：客服转人工 → 补写知识 → 更新文案，
        三环自己接上，中间没有人点过任何按钮。"""
        deps, spot, copy = wired
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        first = runner.run_pending(deps)
        assert first.done == 1
        assert first.dispatched == 1, "补完知识没有通知运营，链断在第二环"
        assert spot.seen and spot.seen[0].question == "这件羊毛衫会起球吗"

        second = runner.run_pending(deps)
        assert second.done == 1
        assert copy.seen and copy.seen[0].product_id == 9001

        chain = deps.tasks.chain(deps.tasks.rows[0].id)
        assert len(chain) == 2, "两环不在同一条链上，事后画不出闭环"
        assert chain[1].parent_id == chain[0].id

    def test_没活可干时是正常收工(self, wired):
        deps, _, _ = wired
        r = runner.run_pending(deps)
        assert r.claimed == 0 and not r.notes

    def test_库里已有就不再派文案(self, wired):
        deps, spot, copy = wired
        spot.outcome = "already_covered"
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        r = runner.run_pending(deps)
        assert r.done == 1 and r.dispatched == 0
        assert copy.seen == []

    def test_要人写的不是失败(self, wired):
        """failed 会被一遍遍重试，而这件事机器永远做不成。"""
        deps, spot, _ = wired
        spot.outcome = "needs_human"
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        r = runner.run_pending(deps)
        assert r.needs_human == 1 and r.failed == 0
        assert not deps.tasks.pull(), "要人处理的任务不该还在队列里转"

    def test_临时故障会重试(self, wired):
        deps, spot, _ = wired
        spot.outcome = "skipped"
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        r = runner.run_pending(deps)
        assert r.failed == 1
        assert deps.tasks.pull(), "临时故障该退回队列等下一轮"

    def test_执行器炸了不带走整轮(self, wired):
        deps, _, copy = wired

        def boom(spot, deps, **kw):
            raise RuntimeError("注入的故障")

        import app.agent.knowledge.graph as kg
        kg.safe_run_spot = boom
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)
        # 第二条活：另一个商品的盲点
        runner.dispatch_handover(
            _cs_state(HandoverReason.NO_KNOWLEDGE, "这条裤子会掉色吗", 9008), deps)

        r = runner.run_pending(deps)
        assert r.claimed == 2, "第一条炸了，第二条没跑"
        assert r.failed == 2

    def test_派下一环失败不回滚上一环(self, wired, monkeypatch):
        """知识已经真的写进库了。因为派下一环失败就把它标成失败，
        下次重试会重新补写一遍，库里多一条重复知识。"""
        deps, spot, _ = wired
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)

        real = deps.tasks.enqueue
        calls = {"n": 0}

        def flaky(**kw):
            calls["n"] += 1
            if kw.get("kind") == Kind.REFRESH_COPY:
                raise RuntimeError("注入的派活故障")
            return real(**kw)

        deps.tasks.enqueue = flaky
        r = runner.run_pending(deps)

        assert r.done == 1, "上一环该是完成的"
        assert r.dispatched == 0
        assert any("不回滚" in n for n in r.notes)
        assert deps.tasks.rows[0].status == Status.DONE

    def test_不认识的任务类型不重试(self, wired):
        """它每次都会同样地认不出来，重试只会把 attempts 耗光，
        然后变成一条看不出原因的 failed。"""
        deps, _, _ = wired
        deps.tasks.enqueue(kind="不存在的类型", dedupe_key="k1", payload={})

        r = runner.run_pending(deps)
        assert r.needs_human == 1
        assert not deps.tasks.pull()

    def test_没接任务表时如实说(self, wired):
        deps, _, _ = wired
        deps.tasks = None
        r = runner.run_pending(deps)
        assert r.claimed == 0 and r.notes


# ---------------------------------------------------------------- 真 SQL

_DDL = """
CREATE TABLE agent_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  source_agent TEXT DEFAULT '', target_agent TEXT DEFAULT '',
  dedupe_key TEXT NOT NULL, open_key TEXT DEFAULT NULL,
  times INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 0,
  payload TEXT, result TEXT, product_id INTEGER,
  parent_id INTEGER, root_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  error TEXT NOT NULL DEFAULT '',
  claimed_at TEXT, finished_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (open_key))
"""


@pytest.fixture
def sql_store():
    """真引擎、**真 SQL，一行都不替换**。

    替身测不出唯一索引与 rowcount 语义，而这两条恰恰是去重和认领的
    全部依据。之前这里给 enqueue 打过一个 SQLite 方言的补丁，那等于
    生产的那段 SQL 一次都没被执行过——所以 store 里的 SQL 改成了
    方言中立的写法，这里就能跑真的。
    """
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine, text

    from app.agent.tasks.store import MySqlTaskStore

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(_DDL))
    return MySqlTaskStore(engine=engine)


class TestRealSql:
    def test_未完成的同类任务只有一条(self, sql_store):
        for _ in range(4):
            sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=3))
        rows = sql_store.recent()
        assert len(rows) == 1 and rows[0].times == 4

    def test_做完之后腾出位置(self, sql_store):
        """终态清掉 open_key，同类任务才能再排下一条。"""
        a = sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=1))
        sql_store.finish(a.id, Status.DONE)
        b = sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=1))
        assert b.id != a.id
        assert len(sql_store.recent()) == 2

    def test_认领只能成功一次(self, sql_store):
        """带条件的 UPDATE，rowcount 决定谁抢到。"""
        t = sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=1))
        assert sql_store.claim(t.id) is True
        assert sql_store.claim(t.id) is False

    def test_失败退回队列且重新占位(self, sql_store):
        """退回 pending 却不占 open_key 的话，会被再派一条出来。"""
        t = sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=1))
        sql_store.claim(t.id)
        sql_store.finish(t.id, Status.FAILED, error="临时故障")

        again = sql_store.recent()[0]
        assert again.status == Status.PENDING
        sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                    priority=1))
        assert len(sql_store.recent()) == 1, "退回队列的任务被重复派了一条"

    def test_耗光次数就不再出队(self, sql_store):
        t = sql_store.enqueue(**dispatch.knowledge_task("会起球吗嘛", reason="x",
                                                        priority=1))
        for _ in range(3):
            sql_store.claim(t.id)
            sql_store.finish(t.id, Status.FAILED, error="一直挂")
        assert sql_store.recent()[0].status == Status.FAILED
        assert sql_store.pull() == []

    def test_链能按root聚起来(self, sql_store):
        a = sql_store.enqueue(**dispatch.knowledge_task(
            "会起球吗嘛", reason="x", priority=1, product_id=9001))
        sql_store.enqueue(**dispatch.copy_task(
            9001, reason="知识已补写", parent_id=a.id, root_id=a.id))
        chain = sql_store.chain(a.id)
        assert [t.kind for t in chain] == [Kind.WRITE_KNOWLEDGE,
                                           Kind.REFRESH_COPY]

    def test_闭环在真store上也走得完(self, sql_store, monkeypatch):
        """替身 store 上走得通，不代表真 SQL 上走得通。

        这一条把 runner 接到真的任务表上跑完整条链——认领用的是
        rowcount、去重用的是唯一索引，两样替身都测不出来。
        """
        spot, copy = FakeSpotRun(), FakeCopyRun()
        monkeypatch.setattr("app.agent.knowledge.graph.safe_run_spot", spot)
        monkeypatch.setattr("app.agent.marketing.graph.safe_run_copy", copy)
        deps = Deps(llm=None, retriever=StubRetriever([]), tasks=sql_store)

        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)
        first = runner.run_pending(deps)
        assert (first.done, first.dispatched) == (1, 1)

        second = runner.run_pending(deps)
        assert second.done == 1
        assert copy.seen and copy.seen[0].product_id == 9001

        rows = sorted(sql_store.recent(), key=lambda t: t.id)
        assert [t.kind for t in rows] == [Kind.WRITE_KNOWLEDGE, Kind.REFRESH_COPY]
        assert all(t.status == Status.DONE for t in rows)
        assert len(sql_store.chain(rows[0].id)) == 2

    def test_失败的任务记得下原因(self, sql_store, monkeypatch):
        """**实测踩到**：第一次跑通闭环时第二环失败，而队列里 error 是空的——
        等于告诉运维"它挂了，自己去猜"。原因散在 outcome / flags /
        trace.error 三处，只取一处都会丢东西。"""
        spot = FakeSpotRun(outcome="skipped")

        def with_flags(s, d, **kw):
            st = spot(s, d, **kw)
            st.flags.append("落库失败：OperationalError")
            st.trace.error = "no such column: biz_type"
            return st

        monkeypatch.setattr("app.agent.knowledge.graph.safe_run_spot", with_flags)
        deps = Deps(llm=None, retriever=StubRetriever([]), tasks=sql_store)
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)
        runner.run_pending(deps)

        err = sql_store.recent()[0].error
        assert "skipped" in err and "落库失败" in err and "biz_type" in err

    def test_成功的任务不占着错误栏(self, sql_store, monkeypatch):
        """把 "drafted" 塞进错误栏的话，列表上每一行都挂着一句话，
        真正失败的那几条就淹了。"""
        monkeypatch.setattr("app.agent.knowledge.graph.safe_run_spot",
                            FakeSpotRun())
        monkeypatch.setattr("app.agent.marketing.graph.safe_run_copy",
                            FakeCopyRun())
        deps = Deps(llm=None, retriever=StubRetriever([]), tasks=sql_store)
        runner.dispatch_handover(_cs_state(HandoverReason.NO_KNOWLEDGE), deps)
        runner.run_pending(deps)

        assert sql_store.recent()[-1].error == ""

    def test_payload是json不是字符串(self, sql_store):
        """MySQL 的 JSON 列回来是 dict，SQLite 上是 str。两种都要兜住，
        不然测试绿、线上炸（或者反过来）。"""
        t = sql_store.enqueue(**dispatch.knowledge_task(
            "会起球吗嘛", reason="没有相关知识", priority=1))
        back = sql_store.recent()[0]
        assert isinstance(back.payload, dict)
        assert back.payload["question"] == "会起球吗嘛"
