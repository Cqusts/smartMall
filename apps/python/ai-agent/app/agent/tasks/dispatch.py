"""什么情况下才派活。

**这个文件是整条闭环里唯一需要判断力的地方，别的都是管道。**

写一个「转人工就派任务」的规则只要一行，而且看起来很对——直到你发现
它给「我要转人工」派了一条补写任务、给「服务挂了」派了一条补写任务、
给一句自伤倾向的求助派了一条补写任务。一个只有通过分支的判据等于没有
判据；这里的价值全在**不派**的那几条上。
"""

from __future__ import annotations

from ..state import HandoverReason
from .state import Agent, Kind, dedupe_key, normalize

#: 转人工原因 → 要不要给知识运维派补写任务，以及优先级加成。
#:
#: 派的三种，共同点是「知识库里本该有、但没有」：
_DISPATCH: dict[HandoverReason, int] = {
    HandoverReason.NO_KNOWLEDGE: 20,
    # 检索到了但分不够。可能是知识写得不好，也可能确实没有——两种都值得补
    HandoverReason.LOW_CONFIDENCE: 10,
    # 模型说了不该说的。**优先级最低**：补一条知识不一定能解决，
    # 但通常是它手上没有实在东西可说才开始发挥。派一条低优先级的，
    # 让人看到时自己判断，比直接丢掉强
    HandoverReason.POSTCHECK_FAILED: 0,
}

#: **不派的，每一条都有理由**——这几行才是这个闸门存在的意义：
#:
#: * ``SELF_HARM``       —— 一个人在求助。把它变成一条"待补写的知识"
#:                          是这套系统能做的最糟糕的事，没有之一。
#: * ``USER_REQUESTED``  —— 用户就是想找人，不是我们不知道。
#: * ``SENSITIVE_INTENT``—— 议价、投诉、退款要人拍板，不是知识缺失。
#:                          补一条"退款政策"进去，下次 AI 就会拿它硬答。
#: * ``TOOL_FAILURE``    —— 服务挂了。**故障不是盲点**，补知识治不了。
#: * ``INTERNAL_ERROR``  —— 同上。
#:
#: 写成显式的名单而不是 `_DISPATCH` 的补集，是为了让「新增一种转人工原因」
#: 时必须在这里做一次选择——落进补集会被默默地派出去。
_NEVER = {
    HandoverReason.SELF_HARM,
    HandoverReason.USER_REQUESTED,
    HandoverReason.SENSITIVE_INTENT,
    HandoverReason.TOOL_FAILURE,
    HandoverReason.INTERNAL_ERROR,
}

#: 问题太短就不派。"?" "在吗" "哦" 这种补不出任何知识，
#: 而它们会把队列灌满，然后队列就没人看了。
MIN_QUESTION_CHARS = 4


class Decision:
    """派还是不派，以及为什么。**不派也要说得出原因**——
    否则"为什么这个盲点没排上"只能靠读代码。"""

    __slots__ = ("ok", "reason", "priority")

    def __init__(self, ok: bool, reason: str, priority: int = 0) -> None:
        self.ok, self.reason, self.priority = ok, reason, priority

    def __repr__(self) -> str:  # pragma: no cover
        return f"Decision({self.ok}, {self.reason!r}, {self.priority})"


def judge_handover(reason: HandoverReason | None, question: str,
                   times: int = 1) -> Decision:
    """一次转人工该不该变成一条补写任务。"""
    if reason is None:
        return Decision(False, "没有转人工原因")
    if reason in _NEVER:
        return Decision(False, f"{reason.value}：不是知识盲点")

    bump = _DISPATCH.get(reason)
    if bump is None:
        # 新增的原因没在两张表里出现。**默认不派**——
        # 默认派的话，某天加一种"用户辱骂"就会开始给它补知识
        return Decision(False, f"{reason.value}：未登记，默认不派")

    if len(normalize(question)) < MIN_QUESTION_CHARS:
        return Decision(False, "问题太短，补不出知识")

    return Decision(True, reason.value, priority=bump + min(times, 10))


def knowledge_task(question: str, *, reason: str, priority: int,
                   product_id: int | None = None, intent: str = "",
                   ticket_id: int | None = None,
                   session_id: str = "") -> dict:
    """拼一条给知识运维的任务。"""
    return {
        "kind": Kind.WRITE_KNOWLEDGE,
        "dedupe_key": dedupe_key(Kind.WRITE_KNOWLEDGE, question, product_id),
        "source_agent": Agent.CUSTOMER_SERVICE,
        "target_agent": Agent.KNOWLEDGE_OPS,
        "priority": priority,
        "product_id": product_id,
        "payload": {
            "question": question[:512],
            "handover_reason": reason,
            "intent": intent,
            "ticket_id": ticket_id,
            "session_id": session_id,
        },
    }


def copy_task(product_id: int, *, reason: str, parent_id: int | None = None,
              root_id: int | None = None, priority: int = 0) -> dict:
    """拼一条给运营的任务：这个商品的知识变了，文案该重写一遍。"""
    return {
        "kind": Kind.REFRESH_COPY,
        "dedupe_key": dedupe_key(Kind.REFRESH_COPY, f"product-{product_id}",
                                 product_id),
        "source_agent": Agent.KNOWLEDGE_OPS,
        "target_agent": Agent.MARKETING,
        "priority": priority,
        "product_id": product_id,
        "parent_id": parent_id,
        "root_id": root_id,
        "payload": {"reason": reason, "product_id": product_id},
    }


def judge_followup(outcome: str, product_id: int | None) -> Decision:
    """知识补完了，要不要顺手让运营重写文案。

    两个条件缺一不可：

    * **知识真的写进去了。** ``already_covered``（库里本来就有）说明什么都
      没变，文案自然也不用动；``needs_human`` 是还没写。只有 ``drafted``
      算数——``draft_only`` 是试跑，试跑不该产生真任务。
    * **这个盲点属于某个商品。** "怎么退货"是全店政策，跟哪件商品的文案
      都没关系。给它派一条"更新文案"任务，运营点开会一脸茫然。
    """
    if outcome != "drafted":
        return Decision(False, f"知识没落库（{outcome}），文案无需更新")
    if not product_id:
        return Decision(False, "不是商品级盲点，与文案无关")
    return Decision(True, "商品知识已更新")
