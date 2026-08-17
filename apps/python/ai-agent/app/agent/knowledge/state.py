"""知识运维 Agent 的状态。

**这个 Agent 和另外两个最大的不同：它的产出是持久的。**

客服说错一句话，误导一个用户一次；导购推错一件商品，用户点进去发现
不存在。知识运维写错一条知识，它会躺在库里，被之后**每一次**相关检索
命中、被引用进答案——而且看起来格外权威，因为它来自知识库。

所以这里的状态里有两样东西是为"可核查"存在的：:class:`Evidence`
（每条依据都带出处）和 :attr:`SpotState.flags`（核查没过的原因）。
少了它们，人工审核就退化成"看着像那么回事就通过"，而那正是这套
流程唯一的价值所在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state import Citation, TraceRecord


@dataclass
class BlindSpot:
    """一个知识盲点：用户问过、我们没答上来的问题。

    **一个盲点可能由多张工单聚成。** 「怎么退货」和「退货怎么弄」是
    同一个缺口，按题面精确分组会把它算成两个 P2，而合起来它是一个 P1——
    补写顺序整个排错。聚类见 :func:`~.cluster.cluster_spots`。
    """

    question: str
    """代表问法。取被问得最多的那一种——人工补写时照着它写最自然。"""
    times: int = 1
    ticket_ids: list[int] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    """同一个盲点的其他问法。补写时要能看到，
    否则写出来的知识只覆盖了其中一种说法。"""
    reason: str = ""
    intent: str = ""
    product_id: int | None = None

    @property
    def priority(self) -> str:
        if self.times >= 3:
            return "P0"
        return "P1" if self.times == 2 else "P2"


@dataclass
class Evidence:
    """一条依据。**没有依据就不许起草。**

    ``ref`` 不是装饰：人工审核时要能一眼看到"这句话是从哪来的"。
    审核一条带出处的草稿是几秒钟的事，审核一条没出处的等于自己
    重新查一遍——那还不如一开始就人工写。
    """

    kind: str
    """knowledge / product / sku / size_chart"""
    ref: str
    """item:12 / product:9001"""
    text: str

    def render(self) -> str:
        return f"[{self.ref}] {self.text}"


@dataclass
class SpotState:
    """处理一个盲点的过程状态。"""

    spot: BlindSpot
    existing: list[Citation] = field(default_factory=list)
    """复查时库里搜到的条目。分够高 = 这个缺口已经被别人补上了。"""
    evidence: list[Evidence] = field(default_factory=list)
    draft: str = ""
    flags: list[str] = field(default_factory=list)
    """依据核查没过的原因。**要带到人工那里去**——
    只说"机器写不了"，人不知道是没材料还是写出来被判违规。"""
    drafted_in_run: list[tuple[str, int]] = field(default_factory=list)
    """本轮已经落库的 (草稿正文, 条目 id)。

    **recheck 看不到这些**——刚写进去的条目还没进检索索引。
    同一批里两个盲点写出同样的答案时，只有比对正文才拦得住
    （见 nodes.dedup_check）。"""

    item_id: int | None = None
    outcome: str = ""
    """already_covered / drafted / draft_only / duplicate / needs_human / skipped

    ``draft_only`` 是过了全部核查但没写库（试跑或没配写入通道）。
    它和 ``needs_human`` 必须分开——混起来的话，试跑会报告
    "这批全都要人工写"，而真实结论恰恰相反。"""

    trace: TraceRecord = field(default_factory=TraceRecord)

    @property
    def question(self) -> str:
        return self.spot.question


@dataclass
class OpsReport:
    """一次运行的结果。"""

    spots: list[SpotState] = field(default_factory=list)
    error: str = ""

    def count(self, outcome: str) -> int:
        return sum(1 for s in self.spots if s.outcome == outcome)

    def by_outcome(self, outcome: str) -> list[SpotState]:
        return [s for s in self.spots if s.outcome == outcome]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.spots),
            "drafted": self.count("drafted"),
            "draft_only": self.count("draft_only"),
            "already_covered": self.count("already_covered"),
            "duplicate": self.count("duplicate"),
            "needs_human": self.count("needs_human"),
            "skipped": self.count("skipped"),
            "error": self.error,
        }
