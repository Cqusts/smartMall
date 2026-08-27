"""把一轮对话变成事件流。

**不为流式复制一份编排。** 底下跑的仍然是 ``run_turn``——两条路径
分叉的话，"流式能答、非流式不能"这种 bug 会长期没人发现。做法是给
Deps 挂一个事件回调，节点在关键处发状态，生成节点逐块发文本。

中间状态提示不是装饰。RAG 链路 P95 约三秒（意图分类 + 检索 + 生成），
纯等待会让用户以为卡住了；显示"正在查找资料…"能显著改善体感。

事件类型：

* ``status``     —— 阶段变化，前端渲染成状态条
* ``delta``      —— 生成中的文本块。**这是草稿**，见下
* ``done``       —— 最终结果。文本以这里的为准
* ``error``      —— 兜底，前端照样要显示一句人话

``delta`` 与 ``done`` 的关系是这一层最需要说清的取舍：流式要在合规检查
**之前**把字推出去，而广告法违规内容一旦到了用户眼前，撤回不等于拦截。
所以 delta 按草稿处理，done 里的文本才是过了检查的定稿；被改写或拦截时
前端整段替换。做不到"违规内容一个字都不出现"，但做到了"用户最终看到
并留存的内容一定过了检查"。要做到前者只能放弃流式，那会把三秒的等待
变成纯白屏。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from typing import Any, Iterator

from .graph import safe_run_turn
from .nodes import Deps
from .state import AgentState

_SENTINEL = object()


def turn_result(state: AgentState) -> dict[str, Any]:
    """最终结果。CLI、HTTP、WebSocket 共用同一份形状。"""
    return {
        "type": "done",
        "answer": state.answer,
        "session_id": state.session.session_id,
        "trace_id": state.trace.trace_id,
        "intent": state.intent.value,
        "citations": [
            {"item_id": c.item_id, "title": c.title, "content": c.content,
             "score": round(c.dense_score, 3)}
            for c in state.citations
        ],
        # 挂在这条答案上的图/视频。**URL 由程序给**，模型碰不到它——
        # 让模型写地址的话它会编出看着像样但不存在的文件名，
        # 而用户是拿图当事实看的
        "assets": [
            {"asset_id": a.asset_id, "kind": a.kind, "url": a.url,
             "usage": a.usage, "ai_generated": a.ai_generated,
             "model": a.model, "source": a.source}
            for a in state.assets
        ],
        "handover": state.handover,
        "handover_reason": (
            state.handover_reason.value if state.handover_reason else ""
        ),
        "handover_ticket_id": state.handover_ticket_id,
        # 这一轮往知识运维派出去的补写任务号。**闭环最值得看的就是这一瞬间**：
        # 用户看到"我帮您转人工"的同时，面板上显示"已派发补写任务 #12"——
        # 不透出来的话，跨 Agent 那条链在演示时完全是隐形的
        "dispatched_task_id": state.dispatched_task_id,
        "postcheck_flags": state.postcheck_flags,
        # 调试面板用。把它放进正式响应而不是藏在日志里，是因为
        # "为什么这么答"对演示和调参都是最有价值的信息
        "debug": {
            "hit_count": state.trace.retrieval_hit_count,
            "max_score": round(state.trace.retrieval_max_score, 3),
            "rewritten_query": state.trace.rewritten_query,
            "hits": [
                {"item_id": h.item_id, "title": h.title,
                 "dense": round(h.dense_score, 3),
                 # 覆盖率而不是 bm25>0——后者近乎恒真，面板上会把
                 # "根本没匹配上"显示成有词汇支撑
                 "lexical": round(h.lexical_overlap, 3)}
                for h in state.hits
            ],
            "tools_called": state.trace.tools_called,
            "latency_ms": state.trace.latency_ms,
            "image_kind": state.trace.image_kind,
            "image_observations": state.image_observations,
            # 脱敏命中数要露在面板上。**不显示就永远不知道那道防线在
            # 不在工作**——它平时应该是 0，非 0 说明提示词没拦住，
            # 而那正是需要有人看一眼的时候
            "image_pii_hits": state.trace.image_pii_hits,
        },
    }


def shopping_result(state: Any) -> dict[str, Any]:
    """导购一轮的最终结果。

    形状和 :func:`turn_result` **刻意保持兼容**（answer / session_id /
    trace_id / debug 都在），前端那套渲染与执行轨迹面板一行都不用改。
    不同的只有 debug 里装什么——客服看的是"这句话有没有依据"，
    导购看的是**收敛到哪一步了**：攒了哪些条件、搜出几件、放宽了什么。
    """
    return {
        "type": "done",
        "answer": state.answer,
        "session_id": state.session.session_id,
        "trace_id": state.trace.trace_id,
        "intent": "shopping",
        "citations": [],
        # 导购不挂素材：它给的是商品卡片，图已经在卡片里了
        "assets": [],
        "handover": False,
        "handover_reason": "",
        "handover_ticket_id": None,
        "postcheck_flags": [],
        "outcome": state.outcome,
        "recommended": [
            {"id": int(c["id"]), "name": c.get("name") or "",
             "price": c.get("price_from"), "image": c.get("main_image") or ""}
            for c in state.candidates
            if int(c.get("id", 0)) in set(state.recommended)
        ],
        "debug": {
            "need": state.need.as_dict(),
            "need_text": state.need.describe(),
            "candidate_count": len(state.candidates),
            "relaxed": state.relaxed,
            "asked": state.asked,
            "tools_called": state.trace.tools_called,
            "latency_ms": state.trace.latency_ms,
            # 工具挂了和"确实没货"在页面上长得一样，只有这里能分辨
            "error": state.trace.error,
        },
    }


def _stream(run: Any, build: Any, deps: Deps) -> Iterator[dict[str, Any]]:
    """跑一轮并把事件搬出来。**两个 Agent 共用这一份线程与队列。**

    复制一份的话，早晚出现"客服流式修好了、导购还是老样子"——
    这条链路上真正容易出错的是线程与哨兵，不是业务。
    """
    events: queue.Queue = queue.Queue()
    result: dict[str, Any] = {}

    streaming_deps = replace(deps, on_event=events.put)

    def _run() -> None:
        try:
            result["state"] = run(streaming_deps)
        except Exception as exc:  # noqa: BLE001  safe_run_turn 已兜底，这里是双保险
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            events.put(_SENTINEL)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    while True:
        item = events.get()
        if item is _SENTINEL:
            break
        yield item

    worker.join(timeout=1.0)

    if "state" in result:
        yield build(result["state"])
    else:
        # 绝不把 traceback 推给用户，但要留一句人话
        yield {
            "type": "error",
            "answer": "系统开小差了，正在为您转接人工客服～",
            "detail": result.get("error", ""),
        }


def stream_turn(
    message: str, state: AgentState, deps: Deps
) -> Iterator[dict[str, Any]]:
    """跑一轮客服对话并逐个产出事件。

    编排是同步的（节点里有阻塞的 HTTP 调用），所以放进线程跑，
    事件经队列传出来。这样调用方拿到的是一个普通生成器，
    在 CLI 和 WebSocket 里都好用。
    """
    return _stream(lambda d: safe_run_turn(message, state, d), turn_result, deps)


def stream_shopping_turn(
    message: str, state: Any, deps: Deps
) -> Iterator[dict[str, Any]]:
    """跑一轮导购对话。事件格式与客服完全一致，前端不必分两套。"""
    from .shopping.graph import safe_run_turn as shop_turn

    return _stream(lambda d: shop_turn(message, state, d), shopping_result, deps)
