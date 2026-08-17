"""运营 Agent 的状态。

**这个 Agent 的产出是对外发布的。** 客服的话说完就散了，知识条目还有
人工审核兜着，而营销文案会印在详情页上、投进信息流——《广告法》管的
正是这一类，责任在店铺不在模型。

所以状态里有两样东西是为「事后能交代」存在的：:attr:`SellingPoint.source`
（这个卖点凭什么写）和 :attr:`CopyDraft.flags`（合规检查命中了什么）。
被投诉时拿不出依据，等于没做过检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..knowledge.state import Evidence
from ..state import TraceRecord


@dataclass
class CopyBrief:
    """一次文案生产的输入。"""

    product_id: int
    audience: str = ""
    """目标人群。"25-35 岁通勤女性"这种。空着也能跑，
    但写了之后卖点的取舍会明显不同。"""
    style: str = ""
    """风格。"简洁克制" / "热情种草"。"""
    channel: str = "detail"
    """detail 详情页 / feed 信息流 / live 直播口播。"""


@dataclass
class SellingPoint:
    """一条卖点，**必须说得出从哪来**。"""

    text: str
    source: str = "attr"
    """attr 商品属性 / demand 用户高频提问 / sku 规格。

    ``demand`` 是这个 Agent 最值钱的那一类：运营拍脑袋想的是"高级感"，
    用户反复问的是"会不会起球"——后者才该上主图。"""
    ref: str = ""


@dataclass
class DemandSignal:
    """用户对这件商品实际问过什么。"""

    question: str
    times: int = 1
    unanswered: int = 0
    """其中转过人工的次数。**答不上来的问题是更强的需求信号**——
    用户想知道、我们连答案都没有，那更该在文案里主动讲清楚。"""


@dataclass
class CopyDraft:
    """一次生成的全部形态。

    **一次生成而不是分开调用。** 分开调的话各形态之间会不一致——
    标题说"羊毛"、详情说"混纺"，而这种不一致在页面上是并排显示的。
    """

    title: str = ""
    main_images: list[str] = field(default_factory=list)
    """主图角标文案，短句。"""
    points: list[str] = field(default_factory=list)
    detail: str = ""
    script: str = ""
    """短视频 / 直播口播脚本。"""

    def all_text(self) -> str:
        """合规检查要查**每一个字段**。

        只查详情长文案的话，一句"全网最低价"藏在主图角标里就发出去了——
        而主图恰恰是被看见最多的那个位置。
        """
        return "\n".join(filter(None, [
            self.title, *self.main_images, *self.points, self.detail,
            self.script,
        ]))

    def is_empty(self) -> bool:
        return not self.all_text().strip()


@dataclass
class MarketingState:
    brief: CopyBrief
    product: dict[str, Any] = field(default_factory=dict)
    skus: list[dict[str, Any]] = field(default_factory=list)
    demand: list[DemandSignal] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    points: list[SellingPoint] = field(default_factory=list)
    draft: CopyDraft = field(default_factory=CopyDraft)

    flags: list[str] = field(default_factory=list)
    copy_id: int | None = None
    outcome: str = ""
    """staged / draft_only / needs_human / skipped"""

    trace: TraceRecord = field(default_factory=TraceRecord)

    @property
    def attrs(self) -> dict[str, str]:
        return dict(self.product.get("attrs") or {})

    @property
    def product_name(self) -> str:
        return str(self.product.get("name") or "")
