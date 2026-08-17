"""知识运维 Agent 的编排。

**每个节点都有权终止这条链。** 这是和另外两个 Agent 最大的编排差异：
客服和导购无论如何都要给用户一句话，所以链路总要走到底；这里正相反，
**不写**是随时可以、而且经常应该做出的决定。

    复查发现库里已经有了      → 停，别写重复的
    收集依据一条也没找到      → 停，人来写
    模型自己说材料不足        → 停，人来写
    核查发现数字没出处        → 停，人来写
    本轮已经写过同样的内容    → 停，指向已写的那条

真正走到"写入待审"的只是其中一部分，这是设计如此，不是漏斗漏了。
"""

from __future__ import annotations

from typing import Sequence

from ..nodes import Deps, run_node
from . import nodes
from .cluster import cluster_spots
from .state import BlindSpot, OpsReport, SpotState

NODE_LABELS = nodes.NODE_LABELS

#: 终止链路的结论。走到任何一个就不再往下——
#: 尤其是 ``already_covered`` 和 ``duplicate``：继续往下会写一条重复知识
_TERMINAL = ("already_covered", "duplicate", "needs_human", "skipped")


def run_spot(
    spot: BlindSpot, deps: Deps, *, drafted: Sequence[tuple[str, int]] = ()
) -> SpotState:
    """处理一个盲点。

    ``drafted`` 是本轮已经落库的草稿，用来做轮内查重——
    ``recheck`` 看不到它们（刚写的条目还没进检索索引）。
    """
    state = SpotState(spot=spot, drafted_in_run=list(drafted))

    def step(name, fn):
        nonlocal state
        state = run_node(name, fn, state, deps,
                         labels=NODE_LABELS, detail=nodes.step_detail)
        return state.outcome in _TERMINAL

    if step("load", nodes.load):
        return state
    if step("recheck", nodes.recheck):
        return state
    if step("gather", nodes.gather):
        return state
    if step("draft", nodes.draft):
        return state
    if step("ground", nodes.ground_check):
        return state
    if step("dedup", nodes.dedup_check):
        return state
    step("stage", nodes.stage)
    return state


def safe_run_spot(
    spot: BlindSpot, deps: Deps, *, drafted: Sequence[tuple[str, int]] = ()
) -> SpotState:
    """带兜底。一个盲点炸了不能带走整批。"""
    try:
        return run_spot(spot, deps, drafted=drafted)
    except Exception as exc:  # noqa: BLE001
        state = SpotState(spot=spot, outcome="skipped")
        state.trace.error = f"{type(exc).__name__}: {exc}"
        state.flags.append(f"处理异常：{type(exc).__name__}")
        return state


def run_batch(
    spots: Sequence[BlindSpot], deps: Deps, *, limit: int = 0,
    cluster: bool = True,
) -> OpsReport:
    """跑一批盲点。

    先聚类再截断，**顺序不能反**。先截断的话，一个被问了五次但分散成
    五种问法的盲点，很可能五条都排在 limit 之外——而它恰恰是最该补的
    那一条。
    """
    todo = cluster_spots(spots) if cluster else list(spots)
    if limit > 0:
        todo = todo[:limit]

    report = OpsReport()
    drafted: list[tuple[str, int]] = []
    for spot in todo:
        state = safe_run_spot(spot, deps, drafted=drafted)
        report.spots.append(state)
        # 试跑（draft_only）也要进查重表：否则试跑的报告会说"能写 5 条"，
        # 真跑的时候只写出 2 条——两次给出的结论不一样，试跑就没意义了
        if state.outcome in ("drafted", "draft_only") and state.draft:
            drafted.append((state.draft, state.item_id or 0))
    return report
