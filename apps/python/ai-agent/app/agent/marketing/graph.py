"""运营 Agent 的编排。

链路是直的，但**每一步都可能停**——和知识运维一样，这条链的正确行为
里包含「什么都不产出」：属性表是空的、模型编了成分、文案里有极限词，
这些情况下不写比写了强。

**这个文件只管文案。** 图与视频是另一条链，在 :mod:`.media_flow`——
形状不同所以不合并：文案一次生成全部形态，而视频是异步的，创建任务
和取结果隔着 1–5 分钟，硬塞进同一条链就得在函数里阻塞轮询。

（原方案里图和视频要 ComfyUI + Wan2.2 + 24G 显卡本机跑，实际改走了
云 API —— 生成商品图不涉及私有数据也不需要 LoRA，没有本地跑的理由。）
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
