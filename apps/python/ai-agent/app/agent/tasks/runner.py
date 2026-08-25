"""把队列里的活跑掉，并把下一环派出去。

**这里是「Agent 集群」和「四个共用底座的 Agent」的分界线。** 在这个文件
之前，知识运维要人手动跑一次才知道有盲点，运营完全不知道知识补过了；
有了它，客服答不上来那一刻起，后面两环自己会接上。

一轮 worker 做四件事，每件都可能失败，而失败的处置各不相同：

    拉活   → 拉不到就是没活，正常收工
    认领   → 抢不到说明别人先拿走了，跳过（不是错误）
    执行   → 挂了要能重试；"机器做不了"不是挂了
    派下一环 → **失败绝不能回滚上一环**，见 _followup
"""

from __future__ import annotations

from typing import Any

from ..nodes import Deps
from . import dispatch
from .state import Agent, AgentTask, Kind, RunReport, Status


# ---------------------------------------------------------------- 执行器


def _why(outcome: str, flags: Any, error: str = "") -> str:
    """一句能直接读的失败原因。

    Agent 把原因散在三个地方：``outcome`` 是结论，``flags`` 是判据没过的
    条目，``trace.error`` 是异常。**只取其中一个都会丢东西**——
    落库失败时 flags 只有一句"落库失败：OperationalError"，真正说明问题的
    那句 SQL 在 trace.error 里。
    """
    bits = [outcome] + [str(f) for f in (flags or [])]
    if error:
        bits.append(error)
    return "；".join(b for b in bits if b)[:480]


def _run_write_knowledge(task: AgentTask, deps: Deps) -> tuple[str, dict]:
    """补写一条知识。返回 (任务终态, 结果)。"""
    from ..knowledge.graph import safe_run_spot
    from ..knowledge.state import BlindSpot

    p = task.payload or {}
    question = str(p.get("question") or "")
    if not question:
        # 派活时就该拦住的，走到这里说明数据坏了。**不重试**——
        # 重试一万次也变不出一个问题来
        return Status.NEEDS_HUMAN, {"reason": "任务里没有问题正文"}

    spot = BlindSpot(
        question=question, times=task.times,
        ticket_ids=[p["ticket_id"]] if p.get("ticket_id") else [],
        reason=str(p.get("handover_reason") or ""),
        intent=str(p.get("intent") or ""), product_id=task.product_id,
    )
    state = safe_run_spot(spot, deps)
    result = {
        "outcome": state.outcome, "item_id": state.item_id,
        "flags": list(state.flags), "draft": state.draft[:200],
        # **失败原因必须落到任务上**，不能只留在 SpotState 里：队列上一条
        # failed 而 error 是空的，等于告诉运维"它挂了，自己去猜"。
        # 实测踩到——第一次跑通闭环时，第二环失败，队列里一个字都没有
        "reason": _why(state.outcome, state.flags, state.trace.error),
    }

    # 三类结论，三种终态。**混成一个的话，重试循环会一遍遍去跑
    # "库里本来就有"这件已经有答案的事**
    if state.outcome in ("drafted", "draft_only", "already_covered", "duplicate"):
        return Status.DONE, result
    if state.outcome == "needs_human":
        return Status.NEEDS_HUMAN, result
    return Status.FAILED, result          # skipped：临时故障，可以重试


def _run_refresh_copy(task: AgentTask, deps: Deps) -> tuple[str, dict]:
    """给商品重写一版文案。"""
    from ..marketing.graph import safe_run_copy
    from ..marketing.state import CopyBrief

    if not task.product_id:
        return Status.NEEDS_HUMAN, {"reason": "任务里没有商品"}

    state = safe_run_copy(CopyBrief(product_id=task.product_id), deps)
    result = {"outcome": state.outcome, "copy_id": state.copy_id,
              "flags": list(state.flags),
              "reason": _why(state.outcome, state.flags, state.trace.error)}
    if state.outcome in ("staged", "draft_only"):
        return Status.DONE, result
    if state.outcome == "needs_human":
        return Status.NEEDS_HUMAN, result
    return Status.FAILED, result


EXECUTORS = {
    Kind.WRITE_KNOWLEDGE: _run_write_knowledge,
    Kind.REFRESH_COPY: _run_refresh_copy,
}


# ---------------------------------------------------------------- 下一环


def _followup(task: AgentTask, status: str, result: dict,
              deps: Deps, report: RunReport) -> None:
    """知识补完了，看要不要让运营也动一下。

    **整段包在 try 里，失败只记一笔。** 上一环已经真的把知识写进库了，
    因为派下一环失败就把它标成失败，是拿一件已经做成的事去陪葬——
    而且下次重试会重新补写一遍，库里就多一条重复知识。
    """
    if task.kind != Kind.WRITE_KNOWLEDGE or status != Status.DONE:
        return
    verdict = dispatch.judge_followup(result.get("outcome", ""), task.product_id)
    if not verdict.ok:
        return
    try:
        nxt = deps.tasks.enqueue(**dispatch.copy_task(
            task.product_id, reason=f"知识已补写（任务 #{task.id}）",
            parent_id=task.id, root_id=task.root_id or task.id,
            priority=task.priority))
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"#{task.id} 派下一环失败（知识已写入，不回滚）："
                            f"{type(exc).__name__}")
        return
    if nxt:
        report.dispatched += 1


# ---------------------------------------------------------------- 编排


def run_pending(deps: Deps, *, limit: int = 10,
                kinds: tuple[str, ...] = ()) -> RunReport:
    """跑一轮。"""
    report = RunReport()
    if deps.tasks is None:
        report.notes.append("没接任务表，跑不了")
        return report

    for task in deps.tasks.pull(limit, kinds):
        if task.id is None:
            continue
        # 抢不到不是错误：另一个 worker 先拿走了，或者它刚被取消
        if not deps.tasks.claim(task.id):
            continue
        report.claimed += 1
        task.attempts += 1

        fn = EXECUTORS.get(task.kind)
        if fn is None:
            # 类型拼错了。**不重试**——它每次都会同样地认不出来，
            # 而重试只会把 attempts 耗光然后变成一条看不出原因的 failed
            deps.tasks.finish(task.id, Status.NEEDS_HUMAN,
                              error=f"没有 {task.kind} 的执行器")
            report.needs_human += 1
            report.notes.append(f"#{task.id} 未知任务类型 {task.kind}")
            continue

        try:
            status, result = fn(task, deps)
        except Exception as exc:  # noqa: BLE001
            # 执行器炸了不能带走整轮：后面还排着别的活
            status, result = Status.FAILED, {}
            deps.tasks.finish(task.id, status,
                              error=f"{type(exc).__name__}: {exc}")
            report.failed += 1
            report.notes.append(f"#{task.id} 执行异常：{type(exc).__name__}")
            continue

        # 成功的任务不记 error——把"drafted"塞进错误栏，列表上每一行都
        # 挂着一句话，真正失败的那几条就淹了
        deps.tasks.finish(
            task.id, status, result=result,
            error="" if status == Status.DONE else str(result.get("reason") or ""))
        if status == Status.DONE:
            report.done += 1
        elif status == Status.NEEDS_HUMAN:
            report.needs_human += 1
        else:
            report.failed += 1

        _followup(task, status, result, deps, report)

    return report


# ---------------------------------------------------------------- 派活入口


def dispatch_handover(state: Any, deps: Deps) -> AgentTask | None:
    """客服转人工那一刻的派活。

    **整段吞异常。** 这是在客服的回复路径上：派活失败的代价是少补一条
    知识，而抛出去的代价是用户看不到回复。和埋点落库同一条原则——
    用户等的是回复。
    """
    if deps.tasks is None:
        return None
    try:
        verdict = dispatch.judge_handover(
            state.handover_reason, state.message, times=1)
        if not verdict.ok:
            return None
        return deps.tasks.enqueue(**dispatch.knowledge_task(
            state.message, reason=verdict.reason, priority=verdict.priority,
            product_id=state.session.current_product_id,
            intent=state.intent.value, ticket_id=state.handover_ticket_id,
            session_id=state.session.session_id))
    except Exception:  # noqa: BLE001
        return None
