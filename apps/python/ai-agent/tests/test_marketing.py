"""运营 Agent。

**这一份的合规用例比另外三个 Agent 都重，因为产出是对外发布的。**

客服说错一句话是一次失言；营销文案印在详情页上，一句"全网最低价"
就是可被投诉、可被处罚的广告违法，而责任主体是店铺不是模型。
所以下面钉三件事：不编成分、不出极限词、不自动发布。
"""

from __future__ import annotations

import pytest

from app.agent.marketing import compliance, graph, nodes
from app.agent.marketing.state import CopyBrief, CopyDraft, MarketingState
from app.agent.marketing.store import StubCopyStore
from app.agent.nodes import AgentConfig, Deps
from app.agent.retriever import StubRetriever
from app.agent.tools import StubToolBox


# ---------------------------------------------------------------- 替身


_GOOD = {
    "title": "米白针织衫 100%羊毛 抗起球",
    "main_images": ["100%羊毛", "抗起球不炸毛", "320g 厚度"],
    "points": ["100%羊毛，柔软亲肤", "抗起球，洗过不炸毛"],
    "detail": "这件针织衫用的是 100%羊毛，克重 320g，秋冬单穿或内搭都合适。"
              "很多人担心羊毛衫起球，这件在纱线上做了处理，日常穿着不易起球。",
    "script": "秋天想买件针织衫又怕起球？这件 100%羊毛，320g 克重，日常穿不炸毛。",
}


class CopyLlm:
    """按 system 提示词区分卖点提炼与文案生成。"""

    def __init__(self, copy: dict | None = None, points: list | None = None,
                 raise_on: str = "") -> None:
        self.copy = _GOOD if copy is None else copy
        self.points = points if points is not None else [
            {"text": "抗起球，洗过不炸毛", "source": "demand"},
            {"text": "100%羊毛，320g 克重", "source": "attr"},
        ]
        self.raise_on = raise_on
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete_json(self, *, model, system, user) -> dict:
        self.systems.append(system)
        self.prompts.append(user)
        if self.raise_on and self.raise_on in system:
            from app.agent.llm import LlmUnavailableError

            raise LlmUnavailableError("注入的故障")
        if "提炼商品卖点" in system:
            return {"points": self.points}
        return dict(self.copy)

    def complete(self, *, model, system, user, temperature=0.3) -> str:
        return ""

    def stream(self, **kw):  # pragma: no cover
        yield ""

    def called(self, keyword: str) -> bool:
        return any(keyword in s for s in self.systems)


class DemandStore:
    def __init__(self, rows=None, fail: bool = False) -> None:
        self.rows = rows if rows is not None else [
            {"question": "会起球吗", "times": 9, "unanswered": 2},
            {"question": "厚不厚", "times": 4, "unanswered": 0},
        ]
        self.fail = fail

    def product_demand(self, product_id, limit=12):
        if self.fail:
            raise RuntimeError("注入的埋点库故障")
        return self.rows


def _box(attrs=None, **kw) -> StubToolBox:
    return StubToolBox(
        products={9001: {"id": 9001, "name": "米白针织衫", "category": "针织衫",
                         "attrs": attrs if attrs is not None
                         else {"材质": "100%羊毛", "克重": "320g"}}},
        skus={9001: [{"spec": "M", "price": 299.0, "origin_price": 399.0,
                      "stock": 12, "in_stock": True}]},
        **kw,
    )


_DEFAULT = object()


def _deps(llm=None, tools=_DEFAULT, store=_DEFAULT, copy_store=_DEFAULT) -> Deps:
    return Deps(
        llm=llm or CopyLlm(),
        retriever=StubRetriever([]),
        config=AgentConfig(),
        tools=_box() if tools is _DEFAULT else tools,
        store=DemandStore() if store is _DEFAULT else store,
        copy_store=StubCopyStore() if copy_store is _DEFAULT else copy_store,
    )


def _run(deps=None, **brief) -> MarketingState:
    brief.setdefault("product_id", 9001)
    return graph.safe_run_copy(CopyBrief(**brief), deps or _deps())


# ---------------------------------------------------------------- 不编成分


class TestNeverInventsMaterials:
    """**模型编造材质是最常见也最危险的错误。**

    属性写聚酯纤维而文案写"精选羊毛"，用户收到货一摸就知道——
    然后是差评、退货、投诉。这不是文笔问题，是虚假宣传。
    """

    def test_material_not_in_attrs_blocks_the_copy(self):
        llm = CopyLlm(copy={**_GOOD, "detail": "精选进口羊绒，柔软亲肤。"})
        store = StubCopyStore()
        s = _run(_deps(llm=llm, copy_store=store))
        assert s.outcome == "needs_human"
        assert any("羊绒" in f for f in s.flags)
        assert store.staged == [], "合规没过还落库了"

    def test_material_in_attrs_is_fine(self):
        """判据必须有真的会通过的分支，否则等于把整条链关掉。"""
        s = _run()
        assert s.outcome == "staged", s.flags

    def test_generic_fabric_words_are_not_conflicts(self):
        """"精选面料"没有主张任何具体的东西，拦它是纯误伤。"""
        assert compliance.attribute_conflicts(
            "采用优质面料，做工精细", {"材质": "100%羊毛"}) == []

    def test_product_name_is_exempt(self):
        """店铺自己把商品叫「羊毛针织衫」，文案跟着说羊毛不是编造。

        这条豁免在 VLM 那条链路上踩出来过——当时三条误报全在这儿。
        """
        assert compliance.attribute_conflicts(
            "这件羊毛针织衫很百搭", {"克重": "320g"},
            product_name="羊毛针织衫") == []

    def test_fabricated_number_blocks_the_copy(self):
        """克重、含量、折扣，属性表里没有的一个都不许出现。"""
        llm = CopyLlm(copy={**_GOOD, "detail": "含绒量高达 95%，蓬松保暖。"})
        s = _run(_deps(llm=llm))
        assert s.outcome == "needs_human"
        assert any("数字没有出处" in f for f in s.flags)

    def test_empty_attrs_stops_before_writing(self):
        """属性表空着还硬写，模型只能瞎编。

        而且这里缺的是**数据**不是能力——补上属性就能跑，
        所以要说清楚是哪一种，别让人以为是 Agent 坏了。
        """
        llm = CopyLlm()
        s = _run(_deps(llm=llm, tools=_box(attrs={})))
        assert s.outcome == "needs_human"
        assert any("属性表是空的" in f for f in s.flags)
        assert not llm.called("电商文案"), "属性都没有还去写文案"


# ---------------------------------------------------------------- 极限词


class TestAdvertisingLaw:
    """《广告法》第九条。**这一层比客服的出口检查严。**"""

    @pytest.mark.parametrize("bad", [
        "最好", "最佳", "国家级", "全网第一", "顶级", "唯一", "百分百",
        "极致", "首选", "销量第一", "史无前例",
    ])
    def test_superlatives_are_caught(self, bad):
        assert not compliance.check(f"这件是{bad}的选择", attrs={}).ok

    @pytest.mark.parametrize("ok_text", [
        "最近上新", "最多可选 3 件", "最后一天", "秋冬最新款",
        "这件最大码是 XL", "柔软亲肤，版型挺括",
    ])
    def test_benign_wording_passes(self, ok_text):
        """**一个见谁都拦的判据和一个谁都不拦的判据一样没用。**

        「最近」「最后」「最多」是事实陈述不是自我评价，
        拦了它们这道闸门就会被绕过去。
        """
        assert compliance.check(ok_text, attrs={}).ok, ok_text

    def test_unlisted_superlatives_are_flagged_but_do_not_block(self):
        """名单永远列不全（"最出片""最能打"每年都有新词），所以有条兜底模式。

        **但它只提示不拦截**，理由见下一条。文案一律要人工过一遍，
        标记就够让人看见了。
        """
        r = compliance.check("全场最出片的一件", attrs={})
        assert r.ok, "兜底模式不该拦截"
        assert any("疑似极限词" in n for n in r.notes)

    def test_ordinary_prose_is_not_blocked_by_the_fallback_pattern(self):
        """**实测逼出来的这条。**

        「很多人买羊毛衫最担心的就是起球」被判成极限词「最担」——
        而这句话根本没在夸商品，它在讲用户的顾虑。这种误伤要是会拦截，
        闸门就会在日常文案上条条报警，然后被人整个绕过去。
        """
        attrs = {"材质": "100%羊毛"}
        for line in ("很多人买羊毛衫最担心的就是起球",
                     "这是大家最关心的问题",
                     "秋天最适合的一件"):
            assert compliance.check(line, attrs=attrs).ok, line

    def test_listed_superlatives_still_block(self):
        """兜底降级成提示了，列出来的那批**必须还是拦截**——
        否则这次改动等于把整道闸门关掉。"""
        assert not compliance.check("最好的选择", attrs={}).ok
        assert not compliance.check("全网第一", attrs={}).ok

    @pytest.mark.parametrize("bad", ["治疗", "消炎", "排毒", "减肥"])
    def test_efficacy_claims_are_caught(self, bad):
        assert not compliance.check(f"长期穿着有助于{bad}", attrs={}).ok

    def test_unbacked_promises_are_caught(self):
        """印在页面上的承诺是**要约**，店铺得兑现。"""
        assert not compliance.check("假一赔十，保证正品", attrs={}).ok

    def test_every_field_is_checked_not_just_the_long_copy(self):
        """一句"全网最低价"藏在主图角标里照样发出去了，
        而主图恰恰是被看见最多的那个位置。"""
        llm = CopyLlm(copy={**_GOOD,
                            "main_images": ["100%羊毛", "全网最低价", "抗起球"]})
        store = StubCopyStore()
        s = _run(_deps(llm=llm, copy_store=store))
        assert s.outcome == "needs_human"
        assert store.staged == []

    def test_violations_are_blocked_not_rewritten(self):
        """把"最好"自动改成"很好"看着无害，但改完就没人再看一眼了——
        而广告法的责任在店铺不在模型。"""
        r = compliance.check("最好的羊毛衫", attrs={})
        assert not r.ok and r.blocked
        assert "最好的羊毛衫" == "最好的羊毛衫", "check 不应改写入参"

    def test_missing_vocabulary_fails_closed(self):
        """**这道闸门跑不了的时候，正确的行为是别放行。**"""
        import builtins

        real = builtins.__import__

        def boom(name, *a, **kw):
            if name == "smartmall_pipeline.vision":
                raise ImportError("装不上")
            return real(name, *a, **kw)

        builtins.__import__ = boom
        try:
            r = compliance.check("精选羊绒", attrs={"材质": "羊毛"})
        finally:
            builtins.__import__ = real
        assert not r.ok
        assert any("不可用" in f for f in r.blocked)


# ---------------------------------------------------------------- 不自动发布


class TestNeverSelfPublishes:
    def test_staged_copy_is_pending_and_marked_ai(self):
        """``review_status`` 与 ``ai_generated`` 都不做成参数——
        **能传参数就意味着某天会有人传 approved。**"""
        import inspect

        from app.agent.marketing.store import MySqlCopyStore

        src = inspect.getsource(MySqlCopyStore.stage_copy)
        assert "'pending'" in src and "review_status" not in src.split("def ")[0]

    def test_no_writer_still_produces_a_draft(self):
        """看得见机器写成什么样，是决定要不要接这条链路的前提。"""
        s = _run(_deps(copy_store=None))
        assert s.outcome == "draft_only" and s.draft.title

    def test_draft_only_is_not_needs_human(self):
        s = _run(_deps(copy_store=None))
        assert s.outcome != "needs_human"

    def test_evidence_and_demand_are_carried_to_the_reviewer(self):
        """被投诉时要拿得出"这条文案凭什么这么写"。"""
        store = StubCopyStore()
        _run(_deps(copy_store=store))
        assert store.staged[0]["evidence"]
        assert store.staged[0]["demand"]

    def test_staging_failure_is_reported_not_swallowed(self):
        s = _run(_deps(copy_store=StubCopyStore(fail=True)))
        assert s.outcome == "skipped"
        assert any("落库失败" in f for f in s.flags)


# ---------------------------------------------------------------- 需求信号


class TestDemandSignals:
    """**这是这个 Agent 唯一不能被"套模板写文案"替代的地方。**"""

    def test_user_questions_reach_the_selling_point_prompt(self):
        llm = CopyLlm()
        _run(_deps(llm=llm))
        prompt = next(p for p, s in zip(llm.prompts, llm.systems)
                      if "提炼商品卖点" in s)
        assert "会起球吗" in prompt
        assert "9 次" in prompt

    def test_unanswered_questions_are_marked_as_stronger_signal(self):
        """答不上来的问题是更强的需求信号——用户想知道、
        我们连答案都没有，那更该在文案里主动讲清楚。"""
        llm = CopyLlm()
        _run(_deps(llm=llm))
        prompt = next(p for p, s in zip(llm.prompts, llm.systems)
                      if "提炼商品卖点" in s)
        assert "转人工" in prompt

    def test_no_demand_data_still_writes_from_attributes(self):
        """新品还没有对话数据，照样能按属性写——
        需求信号是增强项，不是前提。"""
        s = _run(_deps(store=DemandStore(rows=[])))
        assert s.outcome == "staged", s.flags

    def test_demand_store_failure_degrades_but_is_flagged(self):
        """拿不到就按属性写，但要留痕，
        否则"文案质量下降"这件事查不出根因。"""
        s = _run(_deps(store=DemandStore(fail=True)))
        assert s.outcome == "staged"
        assert any("需求信号查不到" in f for f in s.flags)

    def test_point_sources_are_recorded(self):
        """卖点说不出从哪来，就没法回答"这条文案凭什么这么写"。"""
        s = _run()
        assert {p.source for p in s.points} <= {"attr", "demand", "sku"}
        assert any(p.source == "demand" for p in s.points)


# ---------------------------------------------------------------- 一致性


class TestOneShotGeneration:
    def test_all_forms_come_from_a_single_call(self):
        """**分开调用会让各形态互相矛盾**——标题说"羊毛"、详情说"混纺"，
        而这两行在页面上是并排显示的。"""
        llm = CopyLlm()
        _run(_deps(llm=llm))
        assert sum(1 for s in llm.systems if "电商文案" in s) == 1

    def test_all_forms_are_produced(self):
        s = _run()
        d = s.draft
        assert d.title and d.main_images and d.points and d.detail and d.script

    def test_all_text_covers_every_field(self):
        d = CopyDraft(title="A", main_images=["B"], points=["C"],
                      detail="D", script="E")
        assert set("ABCDE") <= set(d.all_text())


# ---------------------------------------------------------------- 兜底


class TestFallbacks:
    def test_missing_product_stops_early(self):
        """读不到商品就写文案，等于让模型凭商品名想象一件商品——
        而那正是虚假宣传的定义。"""
        llm = CopyLlm()
        s = _run(_deps(llm=llm), product_id=99999)
        assert s.outcome == "skipped"
        assert llm.systems == []

    def test_tool_failure_is_not_treated_as_no_attributes(self):
        """**查不到 ≠ 这件商品没有属性。**"""
        s = _run(_deps(tools=_box(fail=True)))
        assert s.outcome == "skipped"
        assert any("查不到" in f for f in s.flags)

    def test_no_toolbox_at_all(self):
        s = _run(_deps(tools=None))
        assert s.outcome == "skipped"

    def test_llm_failure_skips_rather_than_blaming_the_human(self):
        """模型挂了 ≠ 这条写不出来。标成 needs_human 等于把一个
        基础设施故障转成了一条永久的人工任务。"""
        s = _run(_deps(llm=CopyLlm(raise_on="提炼商品卖点")))
        assert s.outcome == "skipped"

    def test_no_selling_points_stops_before_writing(self):
        llm = CopyLlm(points=[])
        s = _run(_deps(llm=llm))
        assert s.outcome == "needs_human"
        assert not llm.called("电商文案")

    def test_crash_does_not_leak_a_traceback(self):
        class Boom:
            def complete_json(self, **kw):
                raise ValueError("炸了")

        s = _run(_deps(llm=Boom()))
        assert s.outcome == "skipped" and s.flags


# ---------------------------------------------------------------- 执行轨迹


class TestTrace:
    def _events(self, deps=None) -> list[dict]:
        got: list[dict] = []
        deps = deps or _deps()
        deps.on_event = got.append
        graph.safe_run_copy(CopyBrief(product_id=9001), deps)
        return [e for e in got if e.get("type") == "step"]

    def test_labels_are_in_chinese(self):
        for e in self._events():
            assert e["label"] != e["node"], f"{e['node']} 没有中文标签"

    def test_demand_step_reports_what_users_asked(self):
        detail = next(e["detail"] for e in self._events()
                      if e["node"] == "demand" and e["phase"] == "exit")
        assert detail.get("提问题面") == 2
        assert "起球" in str(detail.get("问得最多"))

    def test_points_step_reports_how_many_came_from_users(self):
        """这个数就是这个 Agent 有没有在用飞轮的证据。"""
        detail = next(e["detail"] for e in self._events()
                      if e["node"] == "points" and e["phase"] == "exit")
        assert detail.get("来自用户提问") == 1

    def test_compliance_result_is_visible(self):
        llm = CopyLlm(copy={**_GOOD, "detail": "全网最低价，精选羊绒。"})
        evs = self._events(_deps(llm=llm))
        detail = next(e["detail"] for e in evs
                      if e["node"] == "check" and e["phase"] == "exit")
        assert "极限词" in str(detail) or "成分" in str(detail)


# ---------------------------------------------------------------- 提示词


class TestPrompts:
    def test_write_prompt_forbids_the_four_things(self):
        from app.agent.marketing import prompts

        for rule in ("不许编成分", "不许编数字", "不许用极限词", "不许说功效"):
            assert rule in prompts.WRITE_USER

    def test_points_prompt_prioritises_real_questions(self):
        from app.agent.marketing import prompts

        assert "用户反复问的，优先做成卖点" in prompts.POINTS_USER

    def test_terminal_outcomes_cover_every_early_exit(self):
        """漏一个就会出现"节点判了不写、编排照样往下走"。"""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "app" / "agent" / "marketing" / "nodes.py")
        found = {
            n.value.value
            for n in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
            and any(isinstance(t, ast.Attribute) and t.attr == "outcome"
                    for t in n.targets)
        }
        keep_going = {"staged", "draft_only"}
        assert found - keep_going <= set(graph._TERMINAL), (
            f"没登记进 _TERMINAL：{found - keep_going - set(graph._TERMINAL)}")
