"""广告合规检查。

**这一层比客服的出口检查严，是有理由的。** 客服回复是一对一的、即时的；
营销文案是**发布出去**的、一对多的，而《广告法》管的正是后者。同一句
"最好的羊毛衫"，客服说出来是一次失言，印在详情页上是可被投诉、可被
处罚的广告违法。

三类检查，按后果排：

1. **属性冲突** —— 文案说羊绒而属性写羊毛。这是虚假宣传，最危险，
   而且是模型最爱犯的错（它会顺手把"优质"升级成"羊绒"）
2. **极限词** —— 《广告法》第九条。"最佳""国家级""第一"这类
3. **数字没出处** —— 复用知识运维那套核查。文案里的克重、含量、
   折扣，编一个出来就是虚假宣传

三类都不改写、只拦截。**营销文案不能自动改**——把"最好"改成"很好"
看似无害，但改完之后没人再看一眼，而广告法的责任在店铺不在模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

#: 《广告法》第九条那一类。比客服的 ``guard.ABSOLUTE_WORDS`` 长得多——
#: 那份是给聊天用的，这份是给**印在页面上**的文字用的。
SUPERLATIVES = (
    "国家级", "世界级", "最高级", "最佳", "最好", "最优", "最强", "最先进",
    "最低价", "最便宜", "最流行", "最受欢迎", "第一品牌", "全网第一",
    "销量第一", "行业第一", "全国第一", "顶级", "极致", "极品", "唯一",
    "独家首创", "首选", "王牌", "冠军", "领导品牌", "领先品牌",
    "史无前例", "前所未有", "绝无仅有", "百分百", "百分之百", "绝对",
    "永久", "万能", "无敌", "完美", "顶尖", "至尊", "巅峰",
)

#: 医疗与功效。服装类目说这些是明确违规。
EFFICACY = (
    "治疗", "根治", "疗效", "药用", "抗癌", "杀菌率", "消炎", "止痛",
    "促进血液循环", "排毒", "养生", "增高", "减肥",
)

#: 无依据的承诺。客服那边是改写，这里是拦截——
#: 印在页面上的承诺是**要约**，店铺得兑现。
PROMISES = ("假一赔十", "假一赔百", "无效退款", "终身保修", "永久包换",
            "保证正品", "绝对正品")

#: ``最X`` 的兜底。名单永远列不全（"最能打""最出片"每年都有新词）。
#:
#: **但它只提示，不拦截**，这是实测逼出来的：一句再正常不过的
#: 「很多人买羊毛衫**最担心**的就是起球」会被判成极限词「最担」——
#: 而这句话根本没在夸商品，它在讲用户的顾虑。这种误伤要是会拦截，
#: 闸门就会在日常文案上条条报警，然后被人整个绕过去。
#:
#: 所以分成两档：``SUPERLATIVES`` 那份是列出来的、准确的，**拦**；
#: 这条模式是兜底的、宁滥勿缺的，**只标记**。反正文案一律要人工过一遍，
#: 标记就够让人看见了；而拦错的代价是这道闸门失去信用。
_SUPERLATIVE_RE = re.compile(r"最([一-龥])")

#: 明确无害的后接字：**时间与数量**的用法。「最近上新」「最多可选 3 件」
#: 「最后一天」——事实陈述，不是自我评价，连标记都不必。
#:
#: ``最新`` 放行是有意的：「最新款」在电商里几乎等同于"新款"。
_BENIGN_AFTER_ZUI = frozenset("近后初终多少大小早晚新")


def _superlative_hits(text: str) -> tuple[list[str], list[str]]:
    """返回 (拦截项, 提示项)。"""
    blocked = [w for w in SUPERLATIVES if w in text]
    suspect: list[str] = []
    for ch in _SUPERLATIVE_RE.findall(text):
        if ch in _BENIGN_AFTER_ZUI:
            continue
        word = f"最{ch}"
        if any(word in w for w in blocked) or word in suspect:
            continue
        suspect.append(word)
    return blocked, suspect


def attribute_conflicts(
    text: str, attrs: dict[str, str], *, product_name: str = ""
) -> list[str]:
    """文案里的成分，属性表里有没有。

    **模型编造材质是最常见也最危险的错误。** 属性写"聚酯纤维"而文案写
    "精选羊毛"，这不是文笔问题，是虚假宣传——用户收到货一摸就知道，
    然后是差评、退货、投诉。

    只查**具体成分**（羊毛/羊绒/涤纶…），不查泛称（材质/面料/成分）：
    "精选面料"没有主张任何具体的东西，拦它是纯误伤。这个划分与
    VLM 那条链路共用同一份词表（``smartmall_pipeline.vision``），
    两处各写一份的话必然渐行渐远，而先漏的那份不会有人发现。

    ``product_name`` 免检：店铺自己把商品叫「羊毛针织衫」，文案跟着说
    羊毛不是编造——这条豁免在 VLM 那边踩出来过（当时三条误报全在这儿）。
    """
    try:
        from smartmall_pipeline.vision import FIBER_SPECIFIC
    except ImportError:
        # 拿不到词表就**不做这项检查**，并且要让调用方知道。
        # 静默跳过等于悄悄关掉一道合规闸门
        raise ComplianceUnavailable(
            "缺少 smartmall_pipeline，属性冲突检查跑不了"
        ) from None

    known = " ".join(f"{k}{v}" for k, v in (attrs or {}).items())
    return [
        w for w in FIBER_SPECIFIC
        if w in text and w not in known and w not in (product_name or "")
    ]


class ComplianceUnavailable(RuntimeError):
    """检查跑不了。**不等于检查通过**——调用方必须当成拦截处理。"""


@dataclass
class ComplianceResult:
    ok: bool = True
    blocked: list[str] = field(default_factory=list)
    """必须人工处理的问题。营销文案不自动改写。"""
    notes: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        return list(self.blocked) + list(self.notes)


def check(
    text: str, *, attrs: dict[str, str] | None = None, product_name: str = "",
    evidence: Sequence[Any] = (), question: str = "",
) -> ComplianceResult:
    """查一段文案。

    ``evidence`` 传进来就同时做数字出处核查（复用知识运维那套）。
    """
    result = ComplianceResult()
    text = (text or "").strip()
    if not text:
        result.ok = False
        result.blocked.append("文案是空的")
        return result

    blocked, suspect = _superlative_hits(text)
    for w in blocked:
        result.blocked.append(f"极限词:{w}")
    for w in suspect:
        result.notes.append(f"疑似极限词:{w}（人工确认）")
    for w in EFFICACY:
        if w in text:
            result.blocked.append(f"功效宣称:{w}")
    for w in PROMISES:
        if w in text:
            result.blocked.append(f"无依据承诺:{w}")

    try:
        for w in attribute_conflicts(text, attrs or {},
                                     product_name=product_name):
            result.blocked.append(f"属性表里没有的成分:{w}")
    except ComplianceUnavailable as exc:
        # **失败关闭。** 这道闸门跑不了的时候，正确的行为是别放行，
        # 不是当它通过了
        result.blocked.append(f"合规检查不可用:{exc}")

    if evidence:
        # 只借数值判据，不借整个 grounding.check——后者会把客服那套出口
        # 检查也跑一遍，同一个「最好」被报两次（"极限词" + "绝对化用语"），
        # 页面上看着像出了两个问题
        from ..knowledge.grounding import unsourced_numbers

        # article_yi=True：``件`` 是服装类目的量词，「想要一件保暖的针织衫」
        # 这种句子在文案里躲不开。知识运维那边刻意不开——两边拦错的代价
        # 不一样，见 numbers_in 的注释
        bad = unsourced_numbers(text, list(evidence), question=question,
                                article_yi=True)
        if bad:
            result.blocked.append(f"数字没有出处:{'、'.join(bad)}")

    result.ok = not result.blocked
    return result
