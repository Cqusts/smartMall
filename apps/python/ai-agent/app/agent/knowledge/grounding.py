"""依据核查：这段草稿里的话，有出处吗？

**这是知识运维 Agent 唯一不能省的一步。**

提示词里写了「只能用依据里的内容」，但提示词是请求，不是约束——
这个项目里已经三次栽在同一件事上。模型会顺手补一句听起来天经地义的
「支持七天无理由退货」，而店铺的政策可能根本不是七天。

写错一条知识比说错一句话严重得多：说错话误导一个用户一次，写错知识
会被之后**每一次**相关检索命中、引用进答案，而且看起来格外权威——
它来自知识库。

所以这里做规则核查，不问模型「你确定吗」（同一个模型自己判自己，
它当然确定）。判据有两条，都能真的把草稿拦下来：

1. **数字必须有出处。** 电商里幻觉最伤人的正是数字——天数、温度、
   百分比、价格、尺码。而数字恰好是能精确核对的：草稿里出现的每个
   数字，必须在某条依据（或用户原问题）里出现过。
2. **过广告法那道关。** 知识条目最终会变成客服说出口的话，不能因为
   它躺在库里就豁免出口检查。

漏掉的：形容词和限定词编造（"版型偏宽松"）核查不了。这条判据只覆盖
数字，写在这里是为了不让人以为过了核查就等于内容正确——
**它只是把最容易错、也最好查的那类挡掉了，人工审核仍然是必须的。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .state import Evidence

#: 中文数字。只在**后面跟着单位**时才当数字看——
#: 否则「一般」「一样」「第一」里的「一」全会被当成数值，
#: 核查变成见谁拦谁，而一个见谁都拦的判据和一个谁都不拦的判据一样没用。
_CN_DIGITS = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}

#: 单位。挑的是电商知识里真会出现、且写错就有后果的那些。
_UNITS = ("天", "日", "年", "月", "周", "小时", "分钟", "度", "元", "块",
          "件", "次", "码", "厘米", "cm", "米", "斤", "克", "kg", "g",
          "%", "折", "倍", "岁", "人")

_UNIT_RE = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))

#: 阿拉伯数字（带不带单位都算）。写「30度」和写「30」一样要有出处
_ARABIC = re.compile(r"\d+(?:\.\d+)?")

#: 中文数字 + 单位。「七天」「三十天」「两年」
#:
#: 前面挡掉 ``第``（序数：第一次、第二天）和 ``再/又/每``——这些位置上的
#: 数字是语法成分不是数值。不挡的话「第一次下水会缩水」会被判成"编了个 1"，
#: 而这句话里根本没有数值主张。
_CN_NUM = re.compile(
    rf"(?<![第再又每])([零一二两三四五六七八九十百]+)\s*(?:{_UNIT_RE})"
)

#: 「一」当**冠词**用的时候不是数值主张。
#:
#: 中文里 ``一`` + 量词 常常只相当于英文的 a/an：「想要**一件**保暖的针织衫」
#: 说的不是"恰好一件"，而是"一件（某件）"。而 ``件`` 正是服装类目的量词——
#: 这个店铺卖的每一件商品，文案里都躲不开它。
#:
#: **只挡 ``一``，不挡别的数字。** 「三件」「限购两件」是真的数量主张。
#: 前面的 lookbehind 保证「十一件」不受影响（那里的 ``一`` 属于 ``十一``）。
#:
#: 量词只列 ``件`` 与 ``次``：``天/年/元/折/度`` 这些是度量单位不是量词，
#: 「一天」「一元」「一折」全都是实打实的数值，一个都不能放。
#:
#: **默认不启用**，见 :func:`numbers_in` 的 ``article_yi``。
_ARTICLE_YI = re.compile(r"(?<![零一二两三四五六七八九十百])一(?=[件次])")

#: 固定说法里的数字不是数值主张。
#:
#: **这份名单只能短，不能长。** 判据宁可多拦：拦错了无非是这条草稿
#: 转人工写——那本来就是现状；漏掉了是一个编出来的数字进了知识库，
#: 之后每次检索都命中它。名单越长，漏的越多。
#:
#: ``二次销售`` 是实测撞出来的，值得单独说：它几乎出现在每一条退换货
#: 政策里（"不影响二次销售"），而 ``二`` + ``次`` 正好落进"中文数字+单位"。
#: 不排掉的话，**这个 Agent 在退换货这一整类问题上会条条报编造**——
#: 而退换货恰恰是知识盲点最集中的地方。
_IDIOMS = ("一次性", "一度", "一时", "一天到晚", "万一", "二次销售")


def _cn_to_int(s: str) -> str | None:
    """把「七」「三十」「二十五」转成数字。

    只处理一百以内——知识条目里的中文数字基本都是天数、年数、折扣，
    再大的数（价格、库存）没人用中文写。超出范围返回 None 而不是
    硬凑一个值：**宁可漏一个也不要核对错**，核对错会把正确的草稿拦掉。
    """
    if not s:
        return None
    if s == "十":
        return "10"
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, "1" if left == "" else None)
        if tens is None:
            return None
        ones = _CN_DIGITS.get(right, "0" if right == "" else None)
        if ones is None:
            return None
        return str(int(tens) * 10 + int(ones))
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def numbers_in(text: str, *, article_yi: bool = False) -> set[str]:
    """抽出文本里的数值，中文数字归一成阿拉伯数字。

    归一是必须的：依据里写「7天」而草稿写「七天」，不归一就会判成
    编造——把一条本来正确的草稿拦下来，比放过一条错的更让人不信任
    这套检查，因为人会开始绕过它。

    ``article_yi=True`` 时把冠词用法的「一件」「一次」当成非数值。
    **两个 Agent 在这一点上刻意不一致**，因为拦错的代价不一样：

    * 知识运维（默认 False）：拦错了无非是这条草稿转人工写——那本来
      就是现状。放过了是一个编出来的数字进知识库，之后每次检索都命中。
      所以「买一件送一件」照拦。
    * 运营（传 True）：``件`` 是服装类目的量词，「想要一件保暖的针织衫」
      这种句子在文案里躲不开。实测就是它把闭环第三环卡住的——
      **一个在几乎每条文案上都报警的闸门，和一个从不报警的一样没用**，
      它会被整个绕过去。而文案本来就要人工过一遍才能发布。
    """
    text = text or ""
    for phrase in _IDIOMS:
        text = text.replace(phrase, "")
    if article_yi:
        # 冠词用法的「一」先摘掉，剩下的「件」「次」不带数字，自然不匹配
        text = _ARTICLE_YI.sub("", text)

    out: set[str] = set()
    for m in _ARABIC.findall(text):
        out.add(m.lstrip("0") or "0")
    for m in _CN_NUM.findall(text):
        got = _cn_to_int(m)
        if got is not None:
            out.add(got)
    return out


def unsourced_numbers(
    text: str, evidence: Sequence[Evidence], *, question: str = "",
    article_yi: bool = False,
) -> list[str]:
    """草稿里出现、而依据与用户原问题里都没有的数字。

    单独拆出来是给运营 Agent 用的：它有自己那套更严的广告合规检查，
    **只需要借这一条数值判据**。整个 :func:`check` 借过去的话，
    客服那套出口检查会跟着跑一遍，同一个「最好」会被报两次
    （"极限词:最好" + "绝对化用语:最好"），页面上像是出了两个问题。

    ``article_yi`` 见 :func:`numbers_in`——运营那边传 True。
    """
    known: set[str] = set()
    for e in evidence:
        known |= numbers_in(e.text, article_yi=article_yi)
    known |= numbers_in(question, article_yi=article_yi)
    got = numbers_in(text, article_yi=article_yi)
    return sorted(got - known, key=lambda x: int(float(x)))


@dataclass
class GroundingResult:
    ok: bool = True
    text: str = ""
    flags: list[str] = field(default_factory=list)
    unsourced_numbers: list[str] = field(default_factory=list)


def check(
    draft: str, evidence: Sequence[Evidence], *, question: str = ""
) -> GroundingResult:
    """核查草稿。不过就不写库。

    ``question`` 里出现的数字算有出处：用户自己问「160 穿什么码」，
    答案里回一句「160」不是编的。
    """
    result = GroundingResult(text=(draft or "").strip())

    if not result.text:
        result.ok = False
        result.flags.append("草稿是空的")
        return result

    if not evidence:
        # 走到这里说明编排漏了 gather 的判断。宁可在这里再挡一次——
        # 没有依据的"草稿"整段都是模型编的
        result.ok = False
        result.flags.append("没有任何依据")
        return result

    unsourced = unsourced_numbers(result.text, evidence, question=question)
    if unsourced:
        result.ok = False
        result.unsourced_numbers = unsourced
        result.flags.append(f"数字没有出处:{'、'.join(unsourced)}")

    # 知识条目最终会变成客服说出口的话，必须过同一道出口检查
    from .. import guard

    post = guard.check_output(result.text, max_length=10_000)
    if post.blocked:
        result.ok = False
        result.flags.extend(post.flags)
    elif post.rewritten:
        # 能修就修（"保证明天到"→"通常明天到"），但要留痕，
        # 让审核的人知道这条被动过
        result.text = post.text
        result.flags.extend(post.flags)

    return result
