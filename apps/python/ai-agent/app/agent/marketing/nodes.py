"""运营 Agent 的节点。

与另外三个 Agent 共用 :class:`Deps`、工具层、埋点格式；合规检查用自己
那一套（``compliance.py``），因为**发布出去的文字比聊天说出口的话
要求严得多**。
"""

from __future__ import annotations

import time
from typing import Any

from ..knowledge.state import Evidence
from ..llm import LlmError
from ..nodes import Deps
from ..tools import render_skus
from . import compliance, prompts
from .state import CopyDraft, DemandSignal, MarketingState, SellingPoint


def load(state: MarketingState, deps: Deps) -> MarketingState:
    """读商品。**读不到就什么都不做。**

    没有属性表就写文案，等于让模型凭商品名想象一件商品——
    而那正是虚假宣传的定义。
    """
    state.trace.intent = "marketing"
    state.trace.product_id = state.brief.product_id

    if deps.tools is None:
        state.outcome = "skipped"
        state.flags.append("没有商品数据通道")
        return state

    t0 = time.time()
    try:
        detail = deps.tools.get_product_detail(state.brief.product_id)
        skus = deps.tools.get_sku_stock_price(state.brief.product_id)
    except Exception as exc:  # noqa: BLE001
        # 查不到 ≠ 这件商品没有属性。工具坏了就停，别拿空属性去写
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append("商品数据查不到，本轮跳过")
        return state

    state.trace.tools_called.append({
        "name": "get_product_detail",
        "latency_ms": int((time.time() - t0) * 1000),
        "hit": detail is not None,
    })

    if not detail:
        state.outcome = "skipped"
        state.flags.append(f"商品 #{state.brief.product_id} 不存在或已下架")
        return state

    state.product = detail
    state.skus = list(skus or [])
    if not state.attrs:
        # 属性表空着还硬写，模型只能瞎编。这条比"跳过"更值得说清楚：
        # 缺的是**数据**，补上属性就能跑
        state.outcome = "needs_human"
        state.flags.append("商品属性表是空的，没有可写的事实依据")
    return state


def collect_demand(state: MarketingState, deps: Deps) -> MarketingState:
    """用户对这件商品实际问了什么。

    **这是这个 Agent 唯一不能被"套模板写文案"替代的地方。**
    运营拍脑袋想的卖点是"高级感"，用户反复问的是"会不会起球"——
    后者才是他下单前真正犹豫的点。

    拿不到不算失败：新品还没有对话数据，照样能按属性写，只是少了
    这一层。所以这里不设终止分支，只在埋点里记下有没有拿到。
    """
    if deps.store is None:
        return state
    try:
        rows = deps.store.product_demand(state.brief.product_id)
    except Exception as exc:  # noqa: BLE001
        # 需求信号是增强项不是前提。拿不到就按属性写，但要留痕，
        # 否则"文案质量下降"这件事查不出根因
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.flags.append("需求信号查不到，本轮只按属性写")
        return state

    state.demand = [
        DemandSignal(question=str(r.get("question") or ""),
                     times=int(r.get("times") or 1),
                     unanswered=int(r.get("unanswered") or 0))
        for r in (rows or []) if r.get("question")
    ]
    return state


def _attrs_text(state: MarketingState) -> str:
    return "\n".join(f"- {k}：{v}" for k, v in state.attrs.items()) or "（无）"


def _demand_text(state: MarketingState) -> str:
    if not state.demand:
        return "（这件商品还没有客服对话数据）"
    return "\n".join(
        f"- 「{d.question}」被问 {d.times} 次"
        + ("（其中有转人工，说明我们自己也没讲清楚）" if d.unanswered else "")
        for d in state.demand[:8]
    )


def build_evidence(state: MarketingState, deps: Deps) -> MarketingState:
    """把能当依据的东西收拢成一份，供后面核查数字用。"""
    ev = [Evidence(kind="product", ref=f"product:{state.brief.product_id}",
                   text=f"{state.product_name} {_attrs_text(state)}")]
    if state.skus:
        ev.append(Evidence(kind="sku",
                           ref=f"product:{state.brief.product_id}#sku",
                           text=render_skus(state.skus)))
    state.evidence = ev
    return state


def extract_points(state: MarketingState, deps: Deps) -> MarketingState:
    """提炼卖点。"""
    try:
        data = deps.llm.complete_json(
            model=deps.config.answer_model,
            system=prompts.POINTS_SYSTEM,
            user=prompts.POINTS_USER.format(
                attrs=_attrs_text(state),
                skus=render_skus(state.skus),
                demand=_demand_text(state)),
        )
    except LlmError as exc:
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append("模型不可用，本轮跳过")
        return state

    state.points = [
        SellingPoint(text=str(p.get("text") or "").strip(),
                     source=str(p.get("source") or "attr"))
        for p in (data.get("points") or []) if p.get("text")
    ]
    if not state.points:
        state.outcome = "needs_human"
        state.flags.append("没提炼出卖点")
    return state


def write_copy(state: MarketingState, deps: Deps) -> MarketingState:
    """一次生成全部形态。

    **分开调用会让各形态互相矛盾**——标题说"羊毛"、详情说"混纺"，
    而这两行在页面上是并排显示的。一次调用，模型在同一上下文里
    自己保持一致。
    """
    hint = ""
    if state.brief.audience:
        hint += prompts.AUDIENCE_HINT.format(audience=state.brief.audience)
    if state.brief.style:
        hint += prompts.STYLE_HINT.format(style=state.brief.style)

    try:
        data = deps.llm.complete_json(
            model=deps.config.answer_model,
            system=prompts.WRITE_SYSTEM,
            user=prompts.WRITE_USER.format(
                name=state.product_name,
                category=state.product.get("category") or "",
                attrs=_attrs_text(state),
                skus=render_skus(state.skus),
                points="\n".join(f"- {p.text}" for p in state.points),
                audience_hint=hint, style_hint=""),
        )
    except LlmError as exc:
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append("模型不可用，本轮跳过")
        return state

    def _lines(key: str) -> list[str]:
        got = data.get(key) or []
        if isinstance(got, str):
            got = [got]
        return [str(x).strip() for x in got if str(x).strip()]

    state.draft = CopyDraft(
        title=str(data.get("title") or "").strip(),
        main_images=_lines("main_images"),
        points=_lines("points"),
        detail=str(data.get("detail") or "").strip(),
        script=str(data.get("script") or "").strip(),
    )
    if state.draft.is_empty():
        state.outcome = "needs_human"
        state.flags.append("模型没写出内容")
    return state


def check_compliance(state: MarketingState, deps: Deps) -> MarketingState:
    """广告合规。**查每一个字段，不只查详情。**

    一句"全网最低价"藏在主图角标里照样发出去了，而主图恰恰是被看见
    最多的那个位置。

    不改写、只拦截：把"最好"自动改成"很好"看似无害，但改完之后就
    没人再看一眼了，而广告法的责任在店铺不在模型。
    """
    result = compliance.check(
        state.draft.all_text(), attrs=state.attrs,
        product_name=state.product_name, evidence=state.evidence)
    state.flags.extend(result.flags)
    state.trace.postcheck_flags = list(result.flags)
    if not result.ok:
        state.outcome = "needs_human"
    return state


def stage(state: MarketingState, deps: Deps) -> MarketingState:
    """落库，状态 pending。

    **机器写的营销文案永远不自动发布。** 广告法的责任主体是店铺；
    模型写错一句"国家级工艺"，被处罚的是店。人工过一眼是这条链路的
    出口，不是可选的加固。

    同时要标记 ``ai_generated``——《人工智能生成合成内容标识办法》
    要求生成内容可识别。存成字段而不是往正文里塞"AI生成"三个字：
    展示层怎么标是展示层的事，但"这段是机器写的"这个事实必须留在数据里。
    """
    if deps.copy_store is None:
        state.outcome = "draft_only"
        state.flags.append("未接写入通道，只生成不落库")
        return state

    try:
        state.copy_id = deps.copy_store.stage_copy(
            product_id=state.brief.product_id,
            draft=state.draft,
            evidence=[e.render() for e in state.evidence],
            demand=[{"question": d.question, "times": d.times}
                    for d in state.demand],
            flags=state.flags,
            model=deps.config.answer_model,
        )
    except Exception as exc:  # noqa: BLE001
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.outcome = "skipped"
        state.flags.append(f"落库失败：{type(exc).__name__}")
        return state

    state.outcome = "staged" if state.copy_id else "skipped"
    return state


# ---------------------------------------------------------------- 埋点

NODE_LABELS = {
    "load": "读取商品",
    "demand": "统计用户关注点",
    "evidence": "收拢依据",
    "points": "提炼卖点",
    "write": "生成文案",
    "check": "广告合规检查",
    "stage": "写入待审",
}


def step_detail(name: str, state: MarketingState) -> dict[str, Any]:
    if name == "load":
        return {"商品": state.product_name or "-", "属性条数": len(state.attrs)}
    if name == "demand":
        top = state.demand[0].question[:16] if state.demand else "无数据"
        return {"提问题面": len(state.demand), "问得最多": top}
    if name == "evidence":
        return {"依据": len(state.evidence)}
    if name == "points":
        from_demand = sum(1 for p in state.points if p.source == "demand")
        return {"卖点": len(state.points), "来自用户提问": from_demand}
    if name == "write":
        return {"标题": state.draft.title[:20] or "-",
                "字数": len(state.draft.all_text())}
    if name == "check":
        return {"合规": "、".join(state.flags) if state.flags else "通过"}
    if name == "stage":
        return {"文案": f"#{state.copy_id}" if state.copy_id else "未落库"}
    return {}
