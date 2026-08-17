"""运营 Agent 的编排。

链路是直的，但**每一步都可能停**——和知识运维一样，这条链的正确行为
里包含「什么都不产出」：属性表是空的、模型编了成分、文案里有极限词，
这些情况下不写比写了强。

只做文案。图和视频这两条子链路（docs/06 第 3、4 节）需要 ComfyUI +
Wan2.2 + 24G 显卡，本机跑不起来——**没有写成"接口先留着"的空壳**，
那种代码看着像做完了，实际一行都没验证过。
"""

from __future__ import annotations

from ..nodes import Deps, run_node
from . import nodes
from .state import CopyBrief, MarketingState

NODE_LABELS = nodes.NODE_LABELS

_TERMINAL = ("needs_human", "skipped")


def run_copy(brief: CopyBrief, deps: Deps) -> MarketingState:
    state = MarketingState(brief=brief)

    def step(name, fn):
        nonlocal state
        state = run_node(name, fn, state, deps,
                         labels=NODE_LABELS, detail=nodes.step_detail)
        return state.outcome in _TERMINAL

    if step("load", nodes.load):
        return state
    # 需求信号拿不到不算失败：新品还没有对话数据，照样能按属性写
    step("demand", nodes.collect_demand)
    step("evidence", nodes.build_evidence)
    if step("points", nodes.extract_points):
        return state
    if step("write", nodes.write_copy):
        return state
    if step("check", nodes.check_compliance):
        return state
    step("stage", nodes.stage)
    return state


def safe_run_copy(brief: CopyBrief, deps: Deps) -> MarketingState:
    try:
        return run_copy(brief, deps)
    except Exception as exc:  # noqa: BLE001
        state = MarketingState(brief=brief, outcome="skipped")
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.flags.append(f"处理异常：{type(exc).__name__}")
        return state
