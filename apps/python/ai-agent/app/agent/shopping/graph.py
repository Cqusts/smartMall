"""导购 Agent 的编排。

和客服 Agent 一样：路由是纯函数，编排是普通函数，埋点包在派发处。

**这条链和客服链形状不同，是有理由的。** 客服是一问一答，
链路基本是直的；导购是个**带回退的收敛过程**——搜不到就放宽条件重搜，
太多就追问再搜，直到给出结果或者说清没有。那个循环才是它称得上
Agent 的原因。
"""

from __future__ import annotations

from ..nodes import Deps, run_node
from . import nodes
from .state import ShoppingState

#: 导购节点在页面上的名字。与客服共用同一套 step 事件格式，
#: 所以执行轨迹面板不需要改一行代码就能显示它
NODE_LABELS = {
    "ingest": "接收消息",
    "guard": "输入安全检查",
    "extract": "抽取购买需求",
    "search": "筛选商品",
    "narrow": "追问收窄",
    "recommend": "生成推荐",
    "no_match": "说明无合适商品",
    "emit": "收尾落库",
}


def route_after_guard(state: ShoppingState) -> str:
    return "emit" if state.blocked else "extract"


def route_after_search(state: ShoppingState) -> str:
    """搜完之后的三分支——这是导购 Agent 的核心决策。

    * 一件都没有 → 如实说。**绝不推荐不符合条件的东西补位。**
    * 太多挑不过来，且还能问 → 追问收窄。
    * 其余（含问够了）→ 推荐。

    追问有上限是刻意的：用户不是来做问卷的，问到第三轮还没东西看，
    他就走了。到上限就按现有条件给结果，宁可给得宽一点。
    """
    if not state.candidates:
        return "no_match"
    if len(state.candidates) > nodes.TOO_MANY and state.asked < nodes.MAX_ASKS:
        return "narrow"
    return "recommend"


def run_turn(message: str, state: ShoppingState, deps: Deps) -> ShoppingState:
    """跑完一轮导购对话。

    ``state.need`` 跨轮累积，所以调用方要把同一个 state 传进来——
    每轮新建 state 的话，用户说过的条件全丢，系统会一遍遍问同样的问题。
    """
    state.message = message

    def step(name, fn, st):
        # 传自己的词表和 detail：不传的话会静默退化成客服那一套——
        # 页面上标签变回英文节点名、detail 全是空的，行还在，内容没了
        return run_node(name, fn, st, deps,
                        labels=NODE_LABELS, detail=nodes.step_detail)

    state = step("ingest", nodes.ingest, state)
    state = step("guard", nodes.guard_input, state)
    if route_after_guard(state) == "emit":
        return step("emit", nodes.emit, state)

    state = step("extract", nodes.extract_need, state)
    state = step("search", nodes.search, state)

    branch = route_after_search(state)
    if branch == "no_match":
        # search 节点自己已经写了话术（工具不可用）就别再覆盖
        if state.outcome != "no_match":
            state = step("no_match", nodes.no_match, state)
    elif branch == "narrow":
        state = step("narrow", nodes.narrow, state)
    else:
        state = step("recommend", nodes.recommend, state)

    return step("emit", nodes.emit, state)


def safe_run_turn(message: str, state: ShoppingState, deps: Deps) -> ShoppingState:
    """带兜底的入口。异常也要给用户一句人话，不能是 traceback。"""
    try:
        return run_turn(message, state, deps)
    except Exception as exc:  # noqa: BLE001
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.answer = "挑商品的时候出了点问题，换个说法再试试？"
        state.outcome = "no_match"
        try:
            return nodes.emit(state, deps)
        except Exception:  # noqa: BLE001
            return state
