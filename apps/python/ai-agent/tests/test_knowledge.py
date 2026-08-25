"""知识运维 Agent。

断言的核心是一句话：**没有出处的内容，一个字都不许进知识库。**

这是三个 Agent 里后果最重的一条。客服说错话误导一个用户一次；
知识运维写错一条知识，它会被之后每一次相关检索命中、引用进答案，
而且看起来格外权威——它来自知识库。所以下面用了整整两组用例
（不编内容 / 不自动通过）从两个方向堵它。
"""

from __future__ import annotations

import pytest

from app.agent.knowledge import cluster, graph, grounding, nodes, prompts
from app.agent.knowledge.state import BlindSpot, Evidence, SpotState
from app.agent.knowledge.store import StubKnowledgeStore
from app.agent.nodes import AgentConfig, Deps
from app.agent.retriever import RetrievalError, StubRetriever
from app.agent.state import Citation
from app.agent.tools import StubToolBox


# ---------------------------------------------------------------- 替身


class OpsLlm:
    """起草环节的确定性替身。"""

    def __init__(self, draft: str = "羊毛面料，建议手洗，水温不超过30度。",
                 raise_on: str = "") -> None:
        self.draft = draft
        self.raise_on = raise_on
        self.calls: list[str] = []

    def complete(self, *, model, system, user, temperature=0.3) -> str:
        self.calls.append(user)
        if self.raise_on and self.raise_on in system:
            from app.agent.llm import LlmUnavailableError

            raise LlmUnavailableError("注入的故障")
        return self.draft

    def complete_json(self, *, model, system, user) -> dict:
        return {}

    def stream(self, **kw):  # pragma: no cover
        yield self.complete(**kw)


#: 默认依据必须**盖住默认草稿里的每个数字**（这里是 30）。
#: 不盖住的话默认路径就走成"核查没过"，于是每一条测"正常落库"的用例
#: 都在测一条不落库的链路——而且看起来像是落库功能坏了。
def _hit(item_id=1, score=0.5, overlap=0.4,
         content="这款是 100%羊毛的，水温不超过30度手洗") -> Citation:
    return Citation(item_id=item_id, title="面料", content=content,
                    score=score, dense_score=score, bm25_score=1.0,
                    lexical_overlap=overlap)


_DEFAULT = object()


def _deps(hits=_DEFAULT, llm=None, kb=_DEFAULT, tools=None,
          fail_retrieval=False) -> Deps:
    return Deps(
        llm=llm or OpsLlm(),
        retriever=StubRetriever(
            [_hit()] if hits is _DEFAULT else list(hits), fail=fail_retrieval),
        config=AgentConfig(),
        tools=tools,
        kb=StubKnowledgeStore() if kb is _DEFAULT else kb,
    )


def _spot(q="这件是什么面料", **kw) -> BlindSpot:
    kw.setdefault("ticket_ids", [1])
    return BlindSpot(question=q, **kw)


# ---------------------------------------------------------------- 不编内容


class TestNeverInventsKnowledge:
    """**这一组是整个 Agent 最重要的部分。**

    写错一条知识比说错一句话严重得多——它会被之后每一次相关检索命中。
    """

    def test_no_evidence_means_no_draft(self):
        """找不到依据就不起草。

        "你们仓库在哪个城市"这种问题，商品表里没有、知识库里也没有，
        机器没有任何办法知道答案。老实说要人来写，比编一个强。
        """
        llm = OpsLlm()
        s = graph.safe_run_spot(_spot("你们仓库在哪个城市"),
                                _deps(hits=[], llm=llm))
        assert s.outcome == "needs_human"
        assert llm.calls == [], "没有依据时不该调用起草提示词"

    def test_fabricated_number_is_caught(self):
        """依据里没有的数字 = 编的。

        模型顺手补一句"支持七天无理由退货"，而店铺政策可能根本不是七天。
        这条草稿绝不能进库。
        """
        llm = OpsLlm(draft="羊毛面料，支持七天无理由退货。")
        kb = StubKnowledgeStore()
        s = graph.safe_run_spot(_spot(), _deps(llm=llm, kb=kb))
        assert s.outcome == "needs_human"
        assert any("数字没有出处" in f for f in s.flags)
        assert kb.staged == [], "核查没过还写了库"

    def test_grounded_number_passes(self):
        """判据必须有真的会通过的分支，否则它等于把整条链关掉。"""
        llm = OpsLlm(draft="羊毛面料，建议手洗，水温不超过30度。")
        hits = [_hit(content="材质 100%羊毛，水温不超过30度手洗")]
        s = graph.safe_run_spot(_spot(), _deps(hits=hits, llm=llm))
        assert s.outcome == "drafted", s.flags

    def test_model_saying_it_cannot_is_taken_at_face_value(self):
        """模型说"材料不足"就当真。

        它是预期内的正常输出，不是失败——把它当失败处理，
        会诱使人去放宽提示词，而放宽的代价就是编造。
        """
        s = graph.safe_run_spot(_spot(), _deps(llm=OpsLlm(draft="材料不足")))
        assert s.outcome == "needs_human"

    def test_deferral_is_matched_by_phrase_not_equality(self):
        """**判据只认全等的话**，模型说「抱歉，依据中没有相关信息」
        就会被当成一条正经知识存进去。"""
        llm = OpsLlm(draft="抱歉，依据中没有关于这个问题的信息，无法确定。")
        s = graph.safe_run_spot(_spot(), _deps(llm=llm))
        assert s.outcome == "needs_human"

    def test_advertising_law_applies_to_knowledge_too(self):
        """知识条目最终会变成客服说出口的话，
        不能因为它躺在库里就豁免出口检查。"""
        llm = OpsLlm(draft="这是全网第一的羊毛衫。")
        kb = StubKnowledgeStore()
        s = graph.safe_run_spot(_spot(), _deps(llm=llm, kb=kb))
        assert s.outcome == "needs_human"
        assert kb.staged == []


# ---------------------------------------------------------------- 不自动通过


class TestNeverSelfApproves:
    """机器不许给自己盖章。

    approved 的条目直接进检索、被引用、被用户当成店铺的正式答复。
    让机器自己盖章，等于前面那套核查全白做——它只需要写出一句
    核查规则挑不出毛病的话，而"挑不出毛病"和"是对的"是两回事。
    """

    def test_staged_draft_is_pending(self):
        from smartmall_pipeline.handover import Ticket, to_knowledge_item
        from smartmall_pipeline.models import ReviewStatus

        item = to_knowledge_item(Ticket(id=1, question="面料", reason="x"),
                                 "羊毛的")
        assert item.review_status is ReviewStatus.PENDING

    def test_machine_drafts_rank_below_human_answers(self):
        """审核队列按置信度排，机器写的该排在人工写的后面。"""
        kb = StubKnowledgeStore()
        graph.safe_run_spot(_spot(), _deps(kb=kb))
        assert kb.staged, "这条该落库的"

    def test_evidence_is_carried_to_the_reviewer(self):
        """审核一条带出处的草稿是几秒钟的事；
        没出处就得自己重查一遍，那还不如一开始就人工写。"""
        kb = StubKnowledgeStore()
        graph.safe_run_spot(_spot(), _deps(kb=kb))
        assert kb.staged[0]["evidence"], "草稿落库时没带依据"

    def test_no_writer_still_produces_a_draft(self):
        """没有写权限也要能看到机器写成什么样——
        那是决定要不要接上这条链路的前提。"""
        s = graph.safe_run_spot(_spot(), _deps(kb=None))
        assert s.draft, "没落库也该有草稿可看"
        assert s.outcome == "draft_only"

    def test_draft_only_is_not_needs_human(self):
        """**试跑不能报告成"这批全都要人工写"**——真实结论恰恰相反。

        混成一个结论的话，第一次跑（默认试跑）会得出完全错误的印象，
        而那正是人决定要不要接这条链路的那一次。
        """
        report = graph.run_batch([_spot()], _deps(kb=None))
        d = report.as_dict()
        assert d["draft_only"] == 1 and d["needs_human"] == 0


# ---------------------------------------------------------------- 复查


class TestRecheck:
    def test_strong_hit_means_already_covered(self):
        """工单是过去某一刻建的，之后可能有人补过知识。

        不复查就起草，会写一条重复知识——而且多半同样检索不到
        （本来就是检索不到才转的人工），等于白写还把库搞脏。
        """
        llm = OpsLlm()
        s = graph.safe_run_spot(
            _spot(), _deps(hits=[_hit(score=0.85, overlap=0.6)], llm=llm))
        assert s.outcome == "already_covered"
        assert llm.calls == [], "已经有知识了还去起草"

    def test_high_score_without_lexical_support_is_not_covered(self):
        """分数中等偏高而词汇覆盖率接近 0，说明这点相似度纯粹是
        向量空间的基线——客服那边踩过同一个坑。"""
        s = graph.safe_run_spot(
            _spot(), _deps(hits=[_hit(score=0.85, overlap=0.02)]))
        assert s.outcome != "already_covered"

    def test_weak_hits_still_become_evidence(self):
        """分不够高不代表没用：沾边的条目正好当起草材料。"""
        s = graph.safe_run_spot(_spot(), _deps(hits=[_hit(score=0.5)]))
        assert s.evidence and s.evidence[0].kind == "knowledge"

    def test_retrieval_failure_skips_instead_of_drafting(self):
        """**检索失败 ≠ 库里没有。** 不知道就不能往下走——
        往下走的结果是给一个可能已经有答案的问题再写一条。"""
        kb = StubKnowledgeStore()
        s = graph.safe_run_spot(_spot(), _deps(fail_retrieval=True, kb=kb))
        assert s.outcome == "skipped"
        assert kb.staged == []

    def test_llm_failure_skips_rather_than_blaming_the_human(self):
        """模型挂了 ≠ 这条写不出来。标成 needs_human 等于把一个
        基础设施故障转成了一条永久的人工任务。"""
        s = graph.safe_run_spot(
            _spot(), _deps(llm=OpsLlm(raise_on="知识库的编辑")))
        assert s.outcome == "skipped"


# ---------------------------------------------------------------- 依据收集


class TestGather:
    def test_product_data_becomes_evidence(self):
        box = StubToolBox(
            products={9001: {"name": "米白针织衫", "category": "针织衫",
                             "attrs": {"材质": "100%羊毛"}}},
            skus={9001: [{"spec": "M", "price": 299.0, "origin_price": None,
                          "stock": 3, "in_stock": True}]},
        )
        s = graph.safe_run_spot(
            _spot(product_id=9001), _deps(hits=[], tools=box))
        kinds = {e.kind for e in s.evidence}
        assert "product" in kinds and "sku" in kinds

    def test_tool_failure_does_not_stop_the_draft_but_is_flagged(self):
        """工具坏了 ≠ 这个商品没资料。用手上有的继续，
        但要让审核的人知道这条草稿是在材料不全的情况下写的。"""
        box = StubToolBox(fail=True)
        s = graph.safe_run_spot(_spot(product_id=9001), _deps(tools=box))
        assert any("依据可能不全" in f for f in s.flags)
        assert s.evidence, "知识库那条依据还在，不该被工具故障带走"

    def test_evidence_carries_a_reference(self):
        """没有出处的"依据"不是依据。"""
        s = graph.safe_run_spot(_spot(), _deps())
        assert all(e.ref for e in s.evidence)


# ---------------------------------------------------------------- 聚类


class TestClustering:
    """近似问法要聚成一个盲点，否则补写顺序整个排错。"""

    SAME = [
        ("怎么退货", "退货怎么弄"),
        ("这件是什么面料", "面料是什么"),
        ("会起球吗", "这个起球吗"),
        ("怎么退货", "请问怎么退货呢"),
        ("这个包邮吗", "包邮吗"),
        ("多久能到", "大概多久能到货"),
    ]
    DIFFERENT = [
        ("七天无理由怎么退", "怎么退货"),
        ("什么面料", "什么材质"),
        ("支持花呗吗", "支持分期吗"),
        ("多久发货", "什么时候发货"),
        ("怎么退货", "怎么洗"),
        ("这件是什么面料", "这件怎么洗"),
        ("会起球吗", "会缩水吗"),
        ("这个包邮吗", "这个有货吗"),
        ("160cm穿什么码", "这件是什么面料"),
    ]

    @pytest.mark.parametrize("a,b", SAME)
    def test_same_gap_clusters(self, a, b):
        assert cluster.similarity(a, b) >= cluster.SIMILAR_ABOVE, \
            f"{a} / {b} = {cluster.similarity(a, b):.2f}"

    @pytest.mark.parametrize("a,b", DIFFERENT)
    def test_different_gaps_stay_apart(self, a, b):
        """**多聚比少聚糟。** 两个不同的问题被合成一条，
        补写时必然漏掉一个，而且没人会发现。"""
        assert cluster.similarity(a, b) < cluster.SIMILAR_ABOVE, \
            f"{a} / {b} = {cluster.similarity(a, b):.2f}"

    def test_synonym_rephrasings_are_a_known_miss(self):
        """词袋对同义词无能为力。**钉住它是为了不让人当成 bug 去"修"**——
        真要解决只能上向量，那是另一个量级的依赖。"""
        assert cluster.similarity("160cm穿什么码", "身高160穿什么尺码") \
            < cluster.SIMILAR_ABOVE

    def test_merging_sums_the_counts(self):
        spots = [BlindSpot("怎么退货", times=1, ticket_ids=[1]),
                 BlindSpot("退货怎么弄", times=1, ticket_ids=[2])]
        out = cluster.cluster_spots(spots)
        assert len(out) == 1 and out[0].times == 2
        assert sorted(out[0].ticket_ids) == [1, 2]

    def test_merging_lifts_the_priority(self):
        """散开时两个 P2，合起来是一个 P1——补写顺序整个不同。"""
        spots = [BlindSpot("怎么退货", times=1, ticket_ids=[1]),
                 BlindSpot("退货怎么弄", times=1, ticket_ids=[2])]
        assert all(s.priority == "P2" for s in spots)
        assert cluster.cluster_spots(spots)[0].priority == "P1"

    def test_variants_are_kept(self):
        """只覆盖一种说法的知识，会让另外几种问法继续搜不到，
        盲点根本没消掉。"""
        spots = [BlindSpot("怎么退货", times=2, ticket_ids=[1]),
                 BlindSpot("退货怎么弄", times=1, ticket_ids=[2])]
        out = cluster.cluster_spots(spots)
        assert out[0].question == "怎么退货"
        assert "退货怎么弄" in out[0].variants

    def test_variants_reach_the_draft_prompt(self):
        llm = OpsLlm()
        spot = BlindSpot("怎么退货", times=2, ticket_ids=[1],
                         variants=["退货怎么弄"])
        graph.safe_run_spot(spot, _deps(llm=llm))
        assert "退货怎么弄" in llm.calls[0]


# ---------------------------------------------------------------- 数值核查


class TestNumberExtraction:
    @pytest.mark.parametrize("text,expect", [
        ("支持七天无理由退货", {"7"}),
        ("水温不超过30度", {"30"}),
        ("羊毛含量65%", {"65"}),
        ("两年质保", {"2"}),
        ("三十天内可退", {"30"}),
        ("买一件送一件", {"1"}),
    ])
    def test_real_numeric_claims(self, text, expect):
        assert grounding.numbers_in(text) == expect

    @pytest.mark.parametrize("text", [
        "第一次下水会缩水", "一度很流行", "一天到晚", "一次性用品",
        "再一次确认", "每一件都检查", "十分满意", "独一无二", "一般建议手洗",
    ])
    def test_idioms_are_not_numeric_claims(self, text):
        """**一个见谁都拦的判据和一个谁都不拦的判据一样没用。**

        「一」在中文里绝大多数时候是语法成分不是数值，不挡住这些
        位置，核查会变成见句就拦，然后没人再信它。
        """
        assert grounding.numbers_in(text) == set()

    @pytest.mark.parametrize("text", [
        "秋天想要一件保暖又好打理的针织衫",
        "这是一件羊毛混纺的高领衫",
        "洗一次就起球的衣服不值得买",
    ])
    def test_article_yi_is_not_a_quantity_for_marketing(self, text):
        """``件`` 是服装类目的量词，文案里躲不开。

        **实测撞出来的**：闭环第三环，运营写的一句再普通不过的
        「秋天想要一件保暖又好打理的针织衫」被判成"数字 1 没有出处"，
        整条文案卡在合规上。一个在几乎每条文案上都报警的闸门，
        和一个从不报警的一样没用——它会被整个绕过去。
        """
        assert grounding.numbers_in(text, article_yi=True) == set()

    @pytest.mark.parametrize("text,expect", [
        ("限购两件", {"2"}),
        ("满三件打八折", {"3", "8"}),
        ("十一件起批", {"11"}),      # 这里的「一」属于「十一」，不能摘
        ("一天内发货", {"1"}),        # 天是度量单位不是量词
        ("一折清仓", {"1"}),
        ("一元秒杀", {"1"}),
    ])
    def test_article_yi_does_not_swallow_real_quantities(self, text, expect):
        assert grounding.numbers_in(text, article_yi=True) == expect

    def test_knowledge_side_stays_strict(self):
        """两个 Agent 在这一点上刻意不一致。

        知识运维拦错了无非是转人工写（现状），放过了是一个编出来的数字
        进知识库、之后每次检索都命中它——代价不对称，所以这边照拦。
        """
        assert grounding.numbers_in("买一件送一件") == {"1"}

    def test_chinese_and_arabic_are_compared_after_normalising(self):
        """依据写「7天」而草稿写「七天」，不归一就会判成编造——
        把正确的草稿拦下来，比放过一条错的更让人绕过这套检查。"""
        ev = [Evidence("knowledge", "item:1", "支持7天无理由退货")]
        assert grounding.check("支持七天无理由退货。", ev).ok

    def test_numbers_from_the_question_count_as_sourced(self):
        """用户自己问「160 穿什么码」，答案里回一句 160 不是编的。"""
        ev = [Evidence("size_chart", "product:1#size", "M 码适合中等身材")]
        r = grounding.check("160 建议选 M 码。", ev, question="160cm穿什么码")
        assert r.ok, r.flags

    def test_empty_draft_is_rejected(self):
        assert not grounding.check("", [Evidence("k", "r", "t")]).ok

    def test_no_evidence_is_rejected_even_if_asked_directly(self):
        """编排里已经挡过一次，这里再挡一次——
        没有依据的"草稿"整段都是模型编的。"""
        assert not grounding.check("羊毛的，手洗。", []).ok

    def test_promise_wording_is_rewritten_not_blocked(self):
        """能修就修，但要留痕，让审核的人知道这条被动过。"""
        ev = [Evidence("knowledge", "item:1", "次日达")]
        r = grounding.check("保证明天送到。", ev)
        assert r.ok and "保证" not in r.text and r.flags


# ---------------------------------------------------------------- 批处理


class TestBatch:
    def test_clustering_happens_before_truncation(self):
        """**顺序不能反。** 先截断的话，一个被问了五次但分散成五种问法的
        盲点，很可能五条都排在 limit 之外——而它恰恰最该补。"""
        spots = [BlindSpot("怎么退货", times=1, ticket_ids=[i]) for i in range(3)]
        spots += [BlindSpot("退货怎么弄", times=1, ticket_ids=[9])]
        spots += [BlindSpot("会缩水吗", times=2, ticket_ids=[10])]
        report = graph.run_batch(spots, _deps(), limit=1)
        assert len(report.spots) == 1
        assert report.spots[0].spot.question == "怎么退货"
        assert report.spots[0].spot.times == 4

    def test_one_bad_spot_does_not_take_down_the_batch(self):
        class Boom:
            def search(self, *a, **kw):
                raise ZeroDivisionError("完全没预料到的")

        deps = Deps(llm=OpsLlm(), retriever=Boom(),  # type: ignore[arg-type]
                    config=AgentConfig(), kb=StubKnowledgeStore())
        report = graph.run_batch([_spot("a"), _spot("b")], deps, cluster=False)
        assert len(report.spots) == 2
        assert all(s.outcome == "skipped" for s in report.spots)

    def test_report_counts_each_outcome(self):
        report = graph.run_batch([_spot()], _deps())
        d = report.as_dict()
        assert d["total"] == 1 and d["drafted"] == 1

    def test_staging_failure_is_reported_not_swallowed(self):
        kb = StubKnowledgeStore(fail=True)
        s = graph.safe_run_spot(_spot(), _deps(kb=kb))
        assert s.outcome == "skipped"
        assert "落库失败" in "".join(s.flags)


# ---------------------------------------------------------------- 执行轨迹


class TestTrace:
    def _events(self, spot=None, **kw) -> list[dict]:
        got: list[dict] = []
        deps = _deps(**kw)
        deps.on_event = got.append
        graph.safe_run_spot(spot or _spot(), deps)
        return [e for e in got if e.get("type") == "step"]

    def test_labels_are_in_chinese(self):
        for e in self._events():
            assert e["label"] != e["node"], f"{e['node']} 没有中文标签"

    def test_gather_reports_how_much_evidence_it_found(self):
        """收到几条依据，是判断后面那条草稿可不可信的依据。"""
        detail = next(e["detail"] for e in self._events()
                      if e["node"] == "gather" and e["phase"] == "exit")
        assert detail.get("依据") == 1

    def test_chain_stops_at_the_terminal_node(self):
        """**不写是随时可以做出的决定**，链路不必走到底。"""
        nodes_run = {e["node"] for e in
                     self._events(hits=[_hit(score=0.85, overlap=0.6)])}
        assert "stage" not in nodes_run and "draft" not in nodes_run

    def test_ground_check_result_is_visible(self):
        evs = self._events(llm=OpsLlm(draft="羊毛面料，支持七天无理由退货。"))
        detail = next(e["detail"] for e in evs
                      if e["node"] == "ground" and e["phase"] == "exit")
        assert "数字没有出处" in str(detail)


# ---------------------------------------------------------------- 提示词


class TestPrompts:
    def test_draft_prompt_forbids_inventing_numbers(self):
        assert "一个数字都不能编" in prompts.DRAFT_USER

    def test_draft_prompt_offers_an_escape_hatch(self):
        """不给模型一条"写不出来"的出路，它一定会硬写一条出来。"""
        assert "材料不足" in prompts.DRAFT_USER

    def test_terminal_outcomes_cover_every_early_exit(self):
        """节点里出现的每一个"别写了"的结论，都必须在 ``_TERMINAL`` 里。

        **漏一个的后果是节点判了不写、编排照样往下走**，一路走到 stage
        把它写进去——而这条链路上"不写"是随时可以做出的决定，加节点是
        常事。所以这里不写死名单，而是把 nodes.py 扫一遍：
        以后谁加一个新结论忘了登记，这条会红。
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "app" / "agent" / "knowledge" / "nodes.py")
        found = {
            node.value.value
            for node in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(isinstance(t, ast.Attribute) and t.attr == "outcome"
                    for t in node.targets)
        }
        # 只有这两个表示"继续往下"，其余一律该终止
        keep_going = {"drafted", "draft_only"}
        assert found - keep_going <= set(graph._TERMINAL), (
            f"这些结论没登记进 _TERMINAL：{found - keep_going - set(graph._TERMINAL)}"
        )

    def test_covered_threshold_is_stricter_than_the_agent_clarify_line(self):
        """代价不对称：判错成"已经有了"，盲点被悄悄跳过没人发现；
        判错成"还没有"，最多多写一条草稿，审核时一眼看出重复。"""
        assert nodes.COVERED_ABOVE > AgentConfig().clarify_below


# ---------------------------------------------------------------- 轮内查重


class TestWithinRunDedup:
    """同一批里写出两条一样的知识，``recheck`` 挡不住。

    它查的是检索索引，而这一轮刚写进去的条目 embedding_status 还是
    stale、根本没进索引，所以 recheck 两次都会说"库里没有"。
    """

    def test_identical_drafts_are_written_once(self):
        spots = [_spot("怎么洗"), _spot("洗涤方式")]
        kb = StubKnowledgeStore()
        report = graph.run_batch(spots, _deps(kb=kb), cluster=False)
        assert [s.outcome for s in report.spots] == ["drafted", "duplicate"]
        assert len(kb.staged) == 1

    def test_the_duplicate_points_at_the_one_that_was_written(self):
        """报"重复"而不指出重复于哪条，人没法核对。"""
        kb = StubKnowledgeStore()
        report = graph.run_batch(
            [_spot("怎么洗"), _spot("洗涤方式")], _deps(kb=kb), cluster=False)
        assert report.spots[1].item_id == report.spots[0].item_id

    def test_clustering_cannot_catch_this_case(self):
        """**题面上一个 bigram 都不共享**——聚类完全救不了，
        所以查重必须查内容。这条钉住的是"为什么需要两套机制"。"""
        assert cluster.similarity("怎么洗", "洗涤方式") == 0.0

    def test_different_answers_are_both_written(self):
        """查重不能把不同的知识也吞掉。"""
        class TwoAnswers:
            def complete(self, *, model, system, user, temperature=0.3):
                # 认「问题：」那一行，不要认整段提示词——
                # "依据"两个字在提示词第一句里就有，按它切会两次都走同一支
                if "问题：怎么退货" in user:
                    return "无理由退货请在收货后联系客服，不影响二次销售即可。"
                return "羊毛材质，建议手洗，水温不超过30度。"

            def complete_json(self, **kw):
                return {}

        kb = StubKnowledgeStore()
        report = graph.run_batch([_spot("怎么退货"), _spot("怎么洗")],
                                 _deps(llm=TwoAnswers(), kb=kb), cluster=False)
        assert [s.outcome for s in report.spots] == ["drafted", "drafted"]
        assert len(kb.staged) == 2

    def test_dry_run_dedups_the_same_way(self):
        """**试跑和真跑必须得出同一个结论。**

        试跑不查重的话，报告会说"能写 2 条"，真跑只写出 1 条——
        那试跑就没有意义了，而人正是靠它决定要不要接这条链路。
        """
        report = graph.run_batch([_spot("怎么洗"), _spot("洗涤方式")],
                                 _deps(kb=None), cluster=False)
        d = report.as_dict()
        assert d["draft_only"] == 1 and d["duplicate"] == 1

    def test_return_policy_wording_is_not_a_fabricated_number(self):
        """「不影响二次销售」几乎出现在每一条退换货政策里。

        不排掉它，这个 Agent 在**退换货这一整类问题**上会条条报编造——
        而退换货恰恰是知识盲点最集中的地方。
        """
        assert grounding.numbers_in("不影响二次销售即可退换") == set()
