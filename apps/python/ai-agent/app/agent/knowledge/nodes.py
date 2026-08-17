"""知识运维 Agent 的节点。

与客服、导购共用 :class:`Deps`、检索层、工具层、出口合规检查。
不同的是产出去向：那两个 Agent 说完就算了，这个 Agent 往知识库里写，
而**写进去的东西会被之后每一次检索命中**。
"""

from __future__ import annotations

import time
from typing import Any

from ..llm import LlmError
from ..nodes import Deps
from ..retriever import RetrievalError
from . import grounding, prompts
from .state import BlindSpot, Evidence, SpotState

#: 复查时判定「库里已经有了」的分数线。
#:
#: 比客服的 ``clarify_below``(0.55) 高一档：这里的代价不对称。判错成
#: "已经有了"，盲点被悄悄跳过，没人会发现；判错成"还没有"，最多多起草
#: 一条草稿，人工审核时看一眼就发现重复了。**宁可多写一条，不要漏掉一个。**
COVERED_ABOVE = 0.72

#: 光看分数不够。分数中等而词汇覆盖率接近 0，说明这点相似度纯粹是
#: 向量空间的基线——同样的教训在客服那边踩过（见 graph.has_lexical_support）
COVERED_LEXICAL_MIN = 0.30


def load(state: SpotState, deps: Deps) -> SpotState:
    """开始处理一个盲点。"""
    state.trace.input_text = state.spot.question
    state.trace.intent = "knowledge_ops"
    return state


def recheck(state: SpotState, deps: Deps) -> SpotState:
    """复查：这个盲点现在还是盲点吗？

    工单是过去某一刻建的。这之后可能有人补过知识、可能别的工单回流过。
    不复查就直接起草，会往库里写一条**重复的**知识——而且重复的那条
    多半同样检索不到（本来就是检索不到才转的人工），等于白写一条，
    还把知识库搞脏了。

    命中够强 → ``already_covered``。这时候要报告的不是"补上了"，
    而是「库里有，是当时没召回」——**那是检索的问题，再补知识没用**。
    这两种情况的处置完全不同，混为一谈会让人一直往库里加东西而
    问题一直在。
    """
    try:
        hits = deps.retriever.search(state.spot.question, top_k=5)
    except RetrievalError as exc:
        # 检索失败 ≠ 库里没有。不知道就不能往下走——
        # 往下走的结果是给一个可能已经有答案的问题再写一条
        state.trace.error = f"RetrievalError: {exc}"
        state.outcome = "skipped"
        state.flags.append("检索不可用，本轮跳过")
        return state

    state.existing = list(hits)
    state.trace.retrieval_hit_count = len(hits)
    if hits:
        best = max(hits, key=lambda h: h.dense_score)
        state.trace.retrieval_max_score = best.dense_score
        state.trace.retrieval_lexical_overlap = best.lexical_overlap
        if (best.dense_score >= COVERED_ABOVE
                and best.lexical_overlap >= COVERED_LEXICAL_MIN):
            state.outcome = "already_covered"
            state.item_id = best.item_id
    return state


def gather(state: SpotState, deps: Deps) -> SpotState:
    """找依据。**找不到就不起草。**

    两个来源，都是已有的只读通道：

    * 检索到的知识条目——分不够高（够高就走 already_covered 了），
      但常常沾边，够写出一条更直接的条目
    * 商品结构化数据——工单带 ``product_id`` 时。材质、规格、尺码表
      这些是硬事实，比任何一段对话都可靠

    这个节点空手而归是**正常且必要**的结果："你们仓库在哪个城市"这种
    问题，商品表里没有、知识库里也没有，机器没有任何办法知道答案。
    这时候老实说要人来写，比编一个强。
    """
    ev: list[Evidence] = []

    for h in state.existing[:3]:
        text = (h.content or "").strip()
        if text:
            ev.append(Evidence(kind="knowledge", ref=f"item:{h.item_id}",
                               text=f"{h.title or ''} {text}".strip()))

    pid = state.spot.product_id
    if pid is not None and deps.tools is not None:
        t0 = time.time()
        try:
            detail = deps.tools.get_product_detail(pid)
            skus = deps.tools.get_sku_stock_price(pid)
            chart = deps.tools.get_size_chart(pid)
        except Exception as exc:  # noqa: BLE001
            # 工具坏了 ≠ 这个商品没有资料。记下来，用手上有的继续，
            # 但要让审核的人知道这条草稿是在材料不全的情况下写的
            state.trace.error = f"{type(exc).__name__}: {exc}"
            state.flags.append("商品数据查不到，依据可能不全")
            detail = skus = chart = None
        else:
            state.trace.tools_called.append({
                "name": "get_product_detail",
                "latency_ms": int((time.time() - t0) * 1000),
                "hit": detail is not None,
            })

        if detail:
            attrs = "，".join(f"{k}：{v}" for k, v in
                             (detail.get("attrs") or {}).items())
            ev.append(Evidence(
                kind="product", ref=f"product:{pid}",
                text=f"{detail.get('name')}（{detail.get('category') or ''}）"
                     + (f" {attrs}" if attrs else "")))
        if skus:
            from ..tools import render_skus

            ev.append(Evidence(kind="sku", ref=f"product:{pid}#sku",
                               text=render_skus(skus)))
        if chart:
            from ..tools import render_size_chart

            ev.append(Evidence(kind="size_chart", ref=f"product:{pid}#size",
                               text=render_size_chart(chart)))

    state.evidence = ev
    if not ev:
        state.outcome = "needs_human"
        state.flags.append("找不到任何依据")
    return state


def draft(state: SpotState, deps: Deps) -> SpotState:
    """起草。

    模型说「材料不足」是**预期内的正常结果**，不是失败。
    把它当失败处理会诱使人去放宽提示词，而放宽的代价是编造。
    """
    hint = ""
    if state.spot.variants:
        hint = prompts.VARIANTS_HINT.format(
            variants="\n".join(f"- {v}" for v in state.spot.variants[:5]))

    try:
        text = deps.llm.complete(
            model=deps.config.answer_model,
            system=prompts.DRAFT_SYSTEM,
            user=prompts.DRAFT_USER.format(
                question=state.spot.question, variants_hint=hint,
                evidence="\n".join(e.render() for e in state.evidence)),
        ).strip()
    except LlmError as exc:
        # 模型挂了 ≠ 这条写不出来。下次重跑还有机会，
        # 不能把它标成"需要人写"——那等于把一个基础设施故障
        # 转成了一条永久的人工任务
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append("模型不可用，本轮跳过")
        return state

    if any(m in text for m in prompts.INSUFFICIENT_MARKS) or len(text) < 8:
        state.outcome = "needs_human"
        state.flags.append("模型判定材料不足")
        return state

    state.draft = text
    return state


def ground_check(state: SpotState, deps: Deps) -> SpotState:
    """依据核查。**提示词是请求，不是约束。**

    提示词里已经写了"一个数字都不能编"，这一步是去核对它有没有照做。
    详见 :mod:`.grounding`。

    没过就降级成 ``needs_human``，并把草稿和原因一起留下——
    人看到"机器写到这里，卡在这个数字上"，比看到一句"机器写不了"
    有用得多，往往改一个数就能用。
    """
    result = grounding.check(
        state.draft, state.evidence, question=state.spot.question)
    state.draft = result.text
    state.flags.extend(result.flags)
    state.trace.postcheck_flags = list(result.flags)
    if not result.ok:
        state.outcome = "needs_human"
    return state


#: 本轮内容查重的相似度下限。
#:
#: 量过（``tests/test_knowledge.py`` 钉住了这批样本）：
#:
#:   逐字相同                                    1.00
#:   同一句话换个语序                            0.87
#:   同一件事换个说法                            0.55
#:   ——分界线——
#:   都在讲这件商品，但一个说面料一个说洗涤      0.04
#:   一个讲退货一个讲洗涤                        0.00
#:
#: 0.50 落在 0.55 和 0.04 中间，两边都留了很宽的余量。
DUPLICATE_ABOVE = 0.50


def dedup_check(state: SpotState, deps: Deps) -> SpotState:
    """本轮已经写过同样内容的话，就不写第二条。

    **``recheck`` 挡不住这个。** 它查的是检索索引，而这一轮刚写进去的
    条目 ``embedding_status`` 还是 stale、根本没进索引——所以同一批里
    两个盲点写出同样的答案时，recheck 两次都说"库里没有"。

    实测踩到的例子：「怎么洗」和「洗涤方式」，**题面上一个 bigram 都不
    共享**（相似度 0.00），聚类完全救不了；但它们的草稿是逐字相同的。
    所以查重必须查**内容**，不能查题面。

    重复知识的害处不是占地方：检索 top-5 会被同一条知识的几个副本占满，
    多样性归零，模型看到的等于只有一个事实（见 pipeline 的 dedup 模块）。
    """
    from .cluster import similarity

    for text, item_id in state.drafted_in_run:
        if similarity(state.draft, text) >= DUPLICATE_ABOVE:
            state.outcome = "duplicate"
            state.item_id = item_id
            state.flags.append(f"与本轮 #{item_id} 内容重复")
            return state
    return state


def stage(state: SpotState, deps: Deps) -> SpotState:
    """写进知识库，状态 pending。

    **机器起草的知识永远不许自动通过。**

    ``review_status=approved`` 的条目会直接进检索、被引用、被用户当成
    店铺的正式答复。让机器给自己盖章，等于前面那套核查全白做——
    它只需要写出一句核查规则挑不出毛病的话就行了，而"挑不出毛病"
    和"是对的"完全是两回事。

    人工审核是这条链路的**出口**，不是可选的加固。
    """
    if deps.kb is None:
        # **和"需要人来写"是两回事。** 这条草稿过了全部核查，只是没有
        # 写入通道（试跑，或者没配库）。混成一个结论的话，试跑的结果会
        # 显示"这批全都要人工写"，而真实结论恰恰相反
        state.outcome = "draft_only"
        state.flags.append("未接写入通道，只起草不落库")
        return state

    try:
        state.item_id = deps.kb.stage_draft(
            question=state.spot.question,
            answer=state.draft,
            ticket_ids=state.spot.ticket_ids,
            product_id=state.spot.product_id,
            intent=state.spot.intent,
            evidence=[e.render() for e in state.evidence],
        )
    except Exception as exc:  # noqa: BLE001
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append(f"落库失败：{type(exc).__name__}")
        return state

    state.outcome = "drafted" if state.item_id else "skipped"
    if not state.item_id:
        state.flags.append("落库没有返回 ID，可能是重复条目")
    return state


# ---------------------------------------------------------------- 埋点

NODE_LABELS = {
    "load": "读取盲点",
    "recheck": "复查知识库",
    "gather": "收集依据",
    "draft": "起草条目",
    "ground": "依据核查",
    "dedup": "本轮查重",
    "stage": "写入待审",
}


def step_detail(name: str, state: SpotState) -> dict[str, Any]:
    """这一步发生了什么。

    和另外两个 Agent 同一个道理：光有节点名说明不了问题。
    「收集依据」跑完了，收到几条？没收到就该停——这个数才是判断
    后面那条草稿可不可信的依据。
    """
    if name == "recheck":
        return {
            "库里命中": state.trace.retrieval_hit_count,
            "最高相似度": round(state.trace.retrieval_max_score, 3),
            "判定": "已有知识" if state.outcome == "already_covered" else "仍是盲点",
        }
    if name == "gather":
        return {"依据": len(state.evidence),
                "来源": "、".join(sorted({e.kind for e in state.evidence})) or "无"}
    if name == "draft":
        return {"字数": len(state.draft)} if state.draft else {"结果": "材料不足"}
    if name == "ground":
        return {"核查": "通过" if not state.flags else "、".join(state.flags)}
    if name == "dedup":
        return ({"重复于": f"#{state.item_id}"} if state.outcome == "duplicate"
                else {"查重": "本轮没写过"})
    if name == "stage":
        return {"知识条目": f"#{state.item_id}" if state.item_id else "未落库"}
    return {}


def spot_from_ticket_group(
    question: str, times: int, ticket_ids: list[int], **kw
) -> BlindSpot:
    return BlindSpot(question=question, times=times,
                     ticket_ids=list(ticket_ids), **kw)
