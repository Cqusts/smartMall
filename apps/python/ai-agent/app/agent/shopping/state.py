"""导购会话的状态。

**这是导购 Agent 与客服 Agent 最本质的区别。** 客服是被动的：一问一答，
历史只是上下文。导购是有目标的——它要跨多轮把「想买件秋天穿的外套，
预算五百」收敛成一个具体商品，而**累积下来的需求本身驱动它下一步做什么**。

所以需求必须是结构化的、跨轮累积的，不能每轮重新从对话里猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state import SessionContext, TraceRecord


@dataclass
class ShoppingNeed:
    """累积的购买需求。每轮把新信息并进来，不覆盖已知的。"""

    category: str = ""
    price_min: float | None = None
    price_max: float | None = None
    colors: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    scene: str = ""
    """使用场景：通勤 / 约会 / 运动 / 度假。它不进 SQL 条件，
    但决定推荐时说什么——同一件外套对通勤和约会的卖点不一样。"""
    notes: list[str] = field(default_factory=list)

    #: 能用来收窄的维度，按「问出来最有用」排序。
    #: 类目排第一：不知道要买什么品类时，问颜色毫无意义。
    DIMENSIONS = ("category", "price", "size", "color")

    def merge(self, other: "ShoppingNeed") -> None:
        """并入新一轮抽取到的信息。

        **只补不覆盖**——用户上一轮说了「藏青」，这一轮说「要 M 码」，
        模型这轮多半抽不到颜色。覆盖的话会把已知条件丢掉，
        然后系统又回头问一遍已经问过的问题，这是聊天机器人最招人烦的行为。

        例外是用户明确改口（"算了不要藏青，看看米白的"）——
        那种情况抽取节点会把新值放进来，这里按「新值非空则替换」处理。
        """
        if other.category:
            self.category = other.category
        if other.price_min is not None:
            self.price_min = other.price_min
        if other.price_max is not None:
            self.price_max = other.price_max
        if other.scene:
            self.scene = other.scene
        for c in other.colors:
            if c not in self.colors:
                self.colors.append(c)
        for s in other.sizes:
            if s not in self.sizes:
                self.sizes.append(s)
        for n in other.notes:
            if n not in self.notes:
                self.notes.append(n)

    def known(self) -> list[str]:
        """已经知道的维度。"""
        out = []
        if self.category:
            out.append("category")
        if self.price_min is not None or self.price_max is not None:
            out.append("price")
        if self.sizes:
            out.append("size")
        if self.colors:
            out.append("color")
        return out

    def missing(self) -> list[str]:
        known = set(self.known())
        return [d for d in self.DIMENSIONS if d not in known]

    def describe(self) -> str:
        bits = []
        if self.category:
            bits.append(self.category)
        if self.price_min is not None and self.price_max is not None:
            bits.append(f"{self.price_min:.0f}-{self.price_max:.0f} 元")
        elif self.price_max is not None:
            bits.append(f"{self.price_max:.0f} 元以内")
        elif self.price_min is not None:
            bits.append(f"{self.price_min:.0f} 元以上")
        if self.colors:
            bits.append("/".join(self.colors))
        if self.sizes:
            bits.append("/".join(self.sizes))
        if self.scene:
            bits.append(self.scene)
        return "、".join(bits) or "（还没说具体要求）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "price_min": self.price_min,
            "price_max": self.price_max, "colors": list(self.colors),
            "sizes": list(self.sizes), "scene": self.scene,
        }


@dataclass
class ShoppingState:
    message: str = ""
    session: SessionContext = field(default_factory=SessionContext)
    need: ShoppingNeed = field(default_factory=ShoppingNeed)

    candidates: list[dict[str, Any]] = field(default_factory=list)
    asked: int = 0
    """已经追问过几轮。**必须有上限**——用户不是来做问卷的，
    问到第三轮还没给东西看，他就走了。"""
    relaxed: list[str] = field(default_factory=list)
    """为了搜出结果而放宽过的条件。要告诉用户，
    否则他以为这些就是符合他要求的。"""

    answer: str = ""
    question: str = ""
    recommended: list[int] = field(default_factory=list)
    blocked: bool = False
    outcome: str = ""
    """recommend / ask / no_match / blocked —— 评测按它算准确率。"""

    trace: TraceRecord = field(default_factory=TraceRecord)

    def begin_turn(self) -> None:
        """清掉上一轮的产出，保留跨轮累积的东西。

        **这个方法是状态跨轮复用的代价。** 导购必须记住用户说过的条件
        （见 :class:`ShoppingNeed`），所以 state 不能每轮新建；
        而只要它不新建，每一个单轮字段就都得在这里显式清掉——
        漏一个就串台，而且串得很隐蔽：

        * ``candidates`` 不清 → 这一轮搜了个空，却拿上一轮的商品去推荐，
          用户看到的是一条**凭空出现**的推荐
        * ``outcome`` 不清 → 编排里"上一步已经写过话术就别覆盖"那个判断
          会误命中，用户收到的是上一轮的原话
        * ``relaxed`` 不清 → 明明这轮按原条件就搜到了，却告诉用户
          "这是放宽后的结果"
        * ``trace`` 不清 → 两轮共用一条埋点，tools_called 越积越长，
          后面做转化率分析时按 trace 计数会全错

        保留的只有三样：``session``（对话历史）、``need``（累积的需求）、
        ``asked``（已追问轮数——它就是用来跨轮计数的，清了追问上限就失效）。
        """
        self.candidates = []
        self.relaxed = []
        self.recommended = []
        self.answer = ""
        self.question = ""
        self.blocked = False
        self.outcome = ""
        self.trace = TraceRecord()
