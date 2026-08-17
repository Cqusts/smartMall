"""导购 Agent 的节点。

与客服 Agent 共用 :class:`Deps`、工具层、输入安全检查——**同一套安全规则
必须覆盖两个 Agent**。注入、违禁、自伤这些和用户在哪个入口无关，
为导购再写一份规则的结果一定是两份规则渐行渐远。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .. import guard
from ..llm import LlmError
from ..nodes import Deps
from . import prompts
from .state import ShoppingNeed, ShoppingState

#: 候选多于这个数就追问收窄——十几件商品铺开，用户挑不过来
TOO_MANY = 4

#: 最多追问几轮。**用户不是来做问卷的。**
#: 问到第三轮还没东西看，他就走了；到上限就按现有条件给结果。
MAX_ASKS = 2

#: 收窄时给的候选选项，从搜索结果里现取——不要写死一份颜色表，
#: 那样会问出「有米白吗」而库里根本没有米白
_OPTION_KEYS = {"color": "颜色", "size": "尺码"}


def _need_from(data: dict[str, Any]) -> ShoppingNeed:
    def num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return ShoppingNeed(
        category=str(data.get("category") or "").strip(),
        price_min=num(data.get("price_min")),
        price_max=num(data.get("price_max")),
        colors=[str(c) for c in (data.get("colors") or []) if c],
        sizes=[str(s) for s in (data.get("sizes") or []) if s],
        scene=str(data.get("scene") or "").strip(),
        notes=[str(n) for n in (data.get("notes") or []) if n],
    )


def ingest(state: ShoppingState, deps: Deps) -> ShoppingState:
    state.begin_turn()
    state.trace.session_id = state.session.session_id
    state.trace.user_id = state.session.user_id
    state.trace.input_text = state.message
    state.session.append("user", state.message)
    return state


def guard_input(state: ShoppingState, deps: Deps) -> ShoppingState:
    """复用客服那一套输入检查。

    注入、违禁、自伤和用户从哪个入口进来无关。为导购再写一份的结果
    一定是两份规则渐行渐远，而先漏的那份不会有人发现。
    """
    result = guard.check_input(state.message)
    if result.reason == "自伤风险":
        state.answer = result.reply
        state.blocked = True
        state.outcome = "blocked"
        return state
    if not result.ok:
        state.answer = result.reply
        state.blocked = True
        state.outcome = "blocked"
    return state


def extract_need(state: ShoppingState, deps: Deps) -> ShoppingState:
    """把这一轮的话抽成结构化条件，并入已知需求。"""
    try:
        data = deps.llm.complete_json(
            model=deps.config.intent_model,
            system=prompts.EXTRACT_SYSTEM,
            user=prompts.EXTRACT_USER.format(
                known=state.need.describe(), message=state.message),
        )
    except LlmError:
        # 抽不出来就用已有条件继续搜。**不能当成"用户没提要求"**——
        # 那会把上一轮攒的条件当成全部，搜出一堆不相干的东西
        return state
    state.need.merge(_need_from(data))
    return state


def search(state: ShoppingState, deps: Deps) -> ShoppingState:
    """按当前条件搜商品。搜不到就逐个放宽，最多放宽两次。

    **放宽的顺序是有讲究的**：先放颜色（换个颜色多半也能接受），
    再放价格（超预算是硬约束，最后才动）。尺码不放——
    穿不上的衣服推荐了也是白推。
    """
    if deps.tools is None:
        state.answer = "商品数据暂时查不到，稍后再试试？"
        state.outcome = "no_match"
        return state

    n = state.need
    base = {"category": n.category or None, "price_min": n.price_min,
            "price_max": n.price_max, "colors": list(n.colors),
            "sizes": list(n.sizes)}

    # **放宽是累积的，报出来的也必须是累积的。** 放到第二格时颜色
    # 早就一起丢了，只报"预算"会让用户以为颜色还是他要的那个——
    # 他点进去看到雾霾蓝变成了焦糖，比一开始就说没货更难接受
    ladder: list[tuple[str, dict]] = []
    if n.colors:
        ladder.append(("颜色", {"colors": []}))
    if n.price_min is not None or n.price_max is not None:
        ladder.append(("预算", {"price_min": None, "price_max": None}))

    attempts: list[tuple[list[str], dict]] = [([], dict(base))]
    current, dropped = dict(base), []
    for label, patch in ladder:
        current = {**current, **patch}
        dropped = dropped + [label]
        attempts.append((list(dropped), dict(current)))

    for relaxed, kwargs in attempts:
        t0 = time.time()
        try:
            found = deps.tools.search_products(**kwargs, limit=20)
        except Exception as exc:  # noqa: BLE001
            # 查不到 ≠ 没有货。工具坏了就说查不到，别让用户以为库里没有
            state.trace.error = f"{type(exc).__name__}: {exc}"
            state.answer = "商品数据暂时查不到，稍后再试试？"
            state.outcome = "no_match"
            return state
        state.trace.tools_called.append({
            "name": "search_products", "latency_ms": int((time.time() - t0) * 1000),
            "hit": bool(found),
        })
        if found:
            state.candidates = found
            state.relaxed = relaxed
            return state

    state.candidates = []
    return state


def narrow(state: ShoppingState, deps: Deps) -> ShoppingState:
    """候选太多，问一个能收窄的问题。"""
    dim = next((d for d in state.need.missing()
                if d in ("color", "size", "price", "category")), "color")
    options = _options_for(dim, state.candidates)

    try:
        q = deps.llm.complete(
            model=deps.config.clarify_model,
            system=prompts.NARROW_SYSTEM,
            user=prompts.NARROW_USER.format(
                need=state.need.describe(), count=len(state.candidates),
                dimension=_OPTION_KEYS.get(dim, dim), options=options),
        ).strip()
    except LlmError:
        q = f"这类有 {len(state.candidates)} 款，您有偏好的{_OPTION_KEYS.get(dim, dim)}吗？"

    state.question = q
    state.answer = q
    state.asked += 1
    state.outcome = "ask"
    return state


def _options_for(dim: str, candidates: list[dict]) -> str:
    """从**搜索结果里**现取选项。

    写死一份颜色表的话，会问出"有米白吗"而库里根本没有米白——
    用户答了"要米白"，下一轮搜出零条，是系统自己把自己逼到死角。
    """
    if dim in ("color", "size"):
        seen: list[str] = []
        for item in candidates:
            for sku in item.get("skus") or []:
                spec = sku.get("spec")
                text = spec if isinstance(spec, str) else str(spec)
                for part in str(text).replace('"', "").replace("{", "") \
                        .replace("}", "").split(","):
                    val = part.split(":")[-1].strip()
                    if val and val not in seen and len(val) <= 6:
                        seen.append(val)
        return "、".join(seen[:8]) or "（结果里没有可选规格）"
    if dim == "price":
        prices = [i.get("price_from", 0) for i in candidates if i.get("price_from")]
        if prices:
            return f"{min(prices):.0f} 到 {max(prices):.0f} 元"
        return "（无价格信息）"
    cats = []
    for i in candidates:
        c = i.get("category") or i.get("short_name")
        if c and c not in cats:
            cats.append(str(c))
    return "、".join(cats[:8]) or "（无类目信息）"


def _items_text(candidates: list[dict]) -> str:
    lines = []
    for it in candidates[:6]:
        specs = "；".join(
            f"{s.get('spec')} {s.get('price')}元 库存{s.get('stock')}"
            for s in (it.get("skus") or [])[:4]
        )
        lines.append(f"#{it['id']} {it.get('name')}（{it.get('category') or ''}）"
                     f" 起价 {it.get('price_from')} 元\n    规格：{specs}")
    return "\n".join(lines)


#: 推荐语里的商品编号标记。和客服那边的引用标记同一个形状，
#: 因为要解决的是同一个问题：**以正文为准，不问模型要一份列表。**
PRODUCT_MARK_RE = re.compile(r"\[#(\d+)\]")


def _picked_from(text: str, candidates: list[dict]) -> tuple[str, list[int]]:
    """从推荐语里回填真正被推荐的商品，并把标记从正文抹掉。

    **卡片必须跟着正文走。** 直接拿 ``candidates[:3]`` 当推荐结果，
    会出现正文只夸了夹克、下面却挂着一张羽绒服卡片的情况——用户看到的是
    系统在推一件它自己都没提的东西。这和客服那边"引用按正文里的标记回填、
    不问模型要引用列表"是同一条：**模型自报的清单和它实际说的话经常对不上。**

    编号不在候选里就丢掉——模型编了个 id 出来，卡片这一关也不放行。
    """
    ids = [int(m) for m in PRODUCT_MARK_RE.findall(text)]
    valid = {int(c["id"]) for c in candidates}
    picked, seen = [], set()
    for i in ids:
        if i in valid and i not in seen:
            seen.add(i)
            picked.append(i)
    # 编号是给页面用的，读起来是噪音。抹掉之后要收干净空格：
    # "夹克 [#9002]，459 元" 直接删标记会留下"夹克 ，"，
    # 中文标点前多一个空格，一眼就能看出是拼接出来的
    clean = PRODUCT_MARK_RE.sub("", text)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r" +(?=[，。、；：！？）】」』])", "", clean)
    clean = re.sub(r"(?<=[（【「『]) +", "", clean)
    return clean.strip(), picked


def recommend(state: ShoppingState, deps: Deps) -> ShoppingState:
    """基于搜到的商品推荐。**一件都不能编。**"""
    scene_hint = (f"- 结合他说的使用场景「{state.need.scene}」讲卖点"
                  if state.need.scene else "")
    fallback = False
    try:
        text = deps.llm.complete(
            model=deps.config.answer_model,
            system=prompts.RECOMMEND_SYSTEM,
            user=prompts.RECOMMEND_USER.format(
                need=state.need.describe(), items=_items_text(state.candidates),
                scene_hint=scene_hint),
        ).strip()
    except LlmError:
        # 模型挂了也要给东西看：退化成列表，比一句"稍后再试"有用
        fallback = True
        text = "为您找到这几款：\n" + "\n".join(
            f"· {i.get('name')}　{i.get('price_from')} 元"
            for i in state.candidates[:3]
        )

    text, picked = _picked_from(text, state.candidates)
    if not picked:
        # 模型没写标记（或走了降级列表）。此时正文列的就是前几件，
        # 卡片跟着给——但**只给正文里真列出来的那几件**
        picked = [int(i["id"]) for i in state.candidates[:3 if fallback else 1]]

    if state.relaxed:
        # 放宽过条件必须说，否则用户以为这些就是符合他要求的
        text += f"\n（注：按您原来的{('、'.join(state.relaxed))}要求没搜到，" \
                f"这几款是放宽后的结果）"

    state.answer = text
    state.recommended = picked
    state.outcome = "recommend"
    return state


def no_match(state: ShoppingState, deps: Deps) -> ShoppingState:
    """一件都没搜到。**如实说，不推荐不符合的。**

    这是这个 Agent 的核心风险点，和客服那边"知识库没有就转人工"
    是同一条线：编一个不存在的商品，或者推荐一个明显不满足条件的，
    比说"没有"糟糕得多——用户点进去才发现，那时候他不会再信任何推荐。
    """
    note = f"（已经尝试放宽{('、'.join(state.relaxed))}，仍然没有）" \
        if state.relaxed else ""
    try:
        state.answer = deps.llm.complete(
            model=deps.config.answer_model,
            system=prompts.NO_MATCH_SYSTEM,
            user=prompts.NO_MATCH_USER.format(
                need=state.need.describe(), relaxed_note=note),
        ).strip()
    except LlmError:
        state.answer = (f"按「{state.need.describe()}」这些条件没找到合适的，"
                        "要不放宽一下预算或颜色？")
    state.outcome = "no_match"
    return state


def step_detail(name: str, state: ShoppingState) -> dict[str, Any]:
    """这一步到底发生了什么。导购版。

    和客服那边同一个道理：**光有节点名说明不了问题**。「筛选商品」跑完了，
    搜出几件？放宽了没有？这两个数才是判断它做得对不对的依据——
    候选 0 却给出了推荐，光看节点名是看不出来的。
    """
    if name == "extract":
        return {"需求": state.need.describe()}
    if name == "search":
        return {"候选": len(state.candidates),
                "放宽": "、".join(state.relaxed) or "无"}
    if name == "narrow":
        return {"第几轮追问": state.asked, "问": state.question[:40]}
    if name == "recommend":
        return {"推荐商品": state.recommended}
    if name == "no_match":
        return {"结论": "这些条件下没有合适的商品"}
    if name == "guard":
        return {"拦截": "已拦截"} if state.outcome == "blocked" else {"通过": "是"}
    return {}


def emit(state: ShoppingState, deps: Deps) -> ShoppingState:
    state.session.append("assistant", state.answer)
    state.trace.answer = state.answer
    state.trace.intent = "shopping"
    if deps.store is not None:
        try:
            deps.store.save(state.trace)
        except Exception:  # noqa: BLE001  埋点失败不该影响对话
            pass
    return state
