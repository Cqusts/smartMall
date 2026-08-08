"""输入安全检查与输出合规检查。

两端都是**纯规则**，不调模型：

* 规则确定、可测、零延迟、零成本；
* 更重要的是可审计——"为什么拦了这条"必须能指到具体一条规则，
  "模型觉得不合适"在合规场景下不是一个可接受的答复。

模型判断留给灰区（情绪识别、意图分类），硬红线一律走词表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 输入侧

#: prompt 注入。AI 客服是对公网开放的，这类尝试每天都有。
_INJECTION = re.compile(
    r"(?:忽略|无视|forget|ignore)\s*(?:上面|之前|previous|above|all)?\s*"
    r"(?:的)?\s*(?:所有)?\s*(?:指令|规则|设定|instruction|prompt|rule)"
    r"|你现在是|扮演一个|重复(?:你的)?(?:系统)?(?:提示|prompt)"
    r"|system\s*prompt|jailbreak|开发者模式|developer\s*mode",
    re.I,
)

#: 明显与电商客服无关且有风险的话题。命中即回绝，不进图。
_OFF_LIMITS = re.compile(
    r"(?:政治|颠覆|反动|涉黄|赌博|毒品|枪支|炸药)",
)

#: 自伤自杀信号。**必须与 _OFF_LIMITS 分开。**
#:
#: 原先"自杀|自残"混在上面那条里，于是有人说"我想自杀"，客服回
#: "这个话题超出我的服务范围啦。有商品或订单方面的问题吗？"——
#: 在最要紧的时刻用一句轻快的话把人打发走。合规上没错，是人的问题。
#:
#: 电商客服不做心理疏导，能做的是两件：不敷衍，以及交给真人。
_SELF_HARM = re.compile(
    r"自杀|自残|轻生|不想活|活不下去|结束生命|了结自己|活着好累",
)

#: 危机干预话术。给热线而不是给建议——客服没有能力也没有资格做后者。
#: 号码按地区核对后再改：这里用的是全国心理援助热线。
SELF_HARM_REPLY = (
    "看到你这么说，我有点担心你。这方面我帮不上什么忙，但希望你别一个人扛着——"
    "全国心理援助热线 12356 是 24 小时的，随时可以打。"
    "我这边也帮你叫一位同事过来。"
)

_HANDOVER_REQUEST = re.compile(
    r"(?:转|找|要|叫|接)?\s*(?:人工|真人|客服小姐姐|客服mm)"
    r"|人呢|有人吗(?:\?|？)?$|不要机器人|别机器人",
)

#: 连续不满的信号。单次不算，连续 3 轮才转人工（见 SessionContext.negative_streak）。
_NEGATIVE = re.compile(
    r"(?:什么破|垃圾|骗人|太差|气死|无语|答非所问|你听不懂|说了多少遍|再说一遍)"
    r"|(?:没|不)(?:用|行|对)(?:啊|吧|呀)?$|投诉你",
)


@dataclass
class GuardResult:
    ok: bool = True
    reason: str = ""
    reply: str = ""
    """不通过时直接返回给用户的话术。绝不返回错误堆栈或空白。"""
    wants_handover: bool = False
    is_negative: bool = False


def check_input(text: str) -> GuardResult:
    """输入侧安全检查。

    注意顺序：先看用户是不是在要人工——那是正当诉求，不该被
    后面的规则误伤（"你们的机器人太垃圾了，转人工"同时命中负面
    和转人工，应当按转人工处理）。
    """
    raw = (text or "").strip()
    if not raw:
        return GuardResult(
            ok=False, reason="空消息", reply="您好，请问有什么可以帮您的？"
        )

    if len(raw) > 1000:
        return GuardResult(
            ok=False,
            reason="超长输入",
            reply="您的问题有点长，方便拆成几句说吗？这样我能答得更准确。",
        )

    # 自伤信号排在最前面：它可能同时命中转人工、负面情绪、注入等规则，
    # 而这一条的处置不能被任何别的规则抢走
    if _SELF_HARM.search(raw):
        return GuardResult(
            ok=False,
            reason="自伤风险",
            reply=SELF_HARM_REPLY,
            wants_handover=True,
        )

    result = GuardResult()
    if _HANDOVER_REQUEST.search(raw):
        result.wants_handover = True
        return result

    if _INJECTION.search(raw):
        # 不解释为什么被拦——解释本身会教会对方怎么绕过
        return GuardResult(
            ok=False,
            reason="疑似 prompt 注入",
            reply="不好意思，这个问题我帮不上忙。您可以问我商品、尺码、"
                  "物流或售后相关的问题。",
        )

    if _OFF_LIMITS.search(raw):
        return GuardResult(
            ok=False,
            reason="超出服务范围",
            reply="这个话题超出我的服务范围啦。有商品或订单方面的问题吗？",
        )

    result.is_negative = bool(_NEGATIVE.search(raw))
    return result


# ---------------------------------------------------------------- 输出侧

#: 广告法绝对化用语。这是硬红线，电商场景下罚款是真实的。
ABSOLUTE_WORDS = (
    "最好", "最佳", "最优", "最强", "最便宜", "最低价", "第一品牌", "全网第一",
    "顶级", "极致", "唯一", "独家首创", "百分百", "100%满意", "绝对",
    "永久", "万能", "国家级", "世界级",
)

#: 医疗功效表述。服装类目说这些是明确违规。
MEDICAL_WORDS = ("治疗", "根治", "疗效", "药用", "抗癌", "杀菌率")

#: 承诺性表述。不拦截，改写成柔性说法——
#: "保证明天到" 改成 "通常明天能到"，语义没丢，风险没了。
PROMISE_REWRITES = {
    "保证": "通常",
    "一定能": "一般能",
    "肯定能": "一般能",
    "必然": "通常",
    "绝不会": "一般不会",
}

_URL = re.compile(r"https?://([^\s/]+)")

#: 允许出现在答案里的域名。模型编 URL 是高频幻觉，
#: 而一个假链接对用户的伤害远大于少一个链接。
URL_WHITELIST = ("smartmall.local", "taobao.com", "tmall.com")

_DIGIT = re.compile(r"\d")
_CITATION = re.compile(r"\[#\d+\]")

#: 模型自认答不了的说法。**说了就得当真。**
#:
#: 实测：问"可以货到付款吗"，答"这个我不太确定，建议您咨询一下人工客服哈"——
#: 而 ``state.handover`` 是 False，工单没建，前端按普通回复渲染。用户被告知
#: 去找人工，却没有任何人工会收到这通对话，这比直接说"不知道"更糟：
#: 它让用户以为已经转过去了。
#:
#: 这些词只在**答案**里查，不在用户输入里查——用户说"转人工"是另一条路径。
DEFERRAL_PHRASES = (
    "不太确定", "不确定", "无法确认", "没法确认", "不清楚",
    "建议您咨询", "建议你咨询", "建议您联系", "建议你联系",
    "咨询人工", "联系人工", "转接人工", "转人工",
    "帮不了", "无法回答", "回答不了", "我不知道",
)


@dataclass
class PostCheckResult:
    text: str
    flags: list[str] = field(default_factory=list)
    blocked: bool = False
    """必须拦截并转人工（医疗功效、绝对化用语改不掉）。"""
    rewritten: bool = False
    deferred: bool = False
    """答案里模型自己承认答不了。要真的转人工，而不是把这句话发出去
    就算完——否则用户被指去找人工，而人工那边什么都没收到。"""


def check_output(text: str, *, max_length: int = 200) -> PostCheckResult:
    """输出侧合规检查。

    分三档处置，对应 docs/05 第 5 节：

    * **拦截** —— 医疗功效、绝对化用语。改不了，转人工。
    * **改写** —— 承诺性表述、编造的链接。能修就修，不打断对话。
    * **只记录** —— 无引用的事实陈述。拦截会大量误伤（很多正常回复
      本来就不需要引用），改为打标记，用于统计幻觉率趋势。

    还有一档不属于"合规"但必须在这里抓：**模型自己说答不了**。
    它说了就得当真——否则那句"建议您咨询人工客服"只是一段文本，
    工单不会建，人工不会收到，用户以为已经转过去了。
    """
    result = PostCheckResult(text=text)
    if not text:
        result.blocked = True
        result.flags.append("空回复")
        return result

    for p in DEFERRAL_PHRASES:
        if p in text:
            result.flags.append(f"模型自认答不了:{p}")
            result.deferred = True
            break

    for w in MEDICAL_WORDS:
        if w in text:
            result.flags.append(f"医疗功效:{w}")
            result.blocked = True

    for w in ABSOLUTE_WORDS:
        if w in text:
            result.flags.append(f"绝对化用语:{w}")
            result.blocked = True

    for bad, good in PROMISE_REWRITES.items():
        if bad in result.text:
            result.text = result.text.replace(bad, good)
            result.flags.append(f"承诺性表述→{good}")
            result.rewritten = True

    for host in _URL.findall(result.text):
        if not any(host.endswith(w) for w in URL_WHITELIST):
            result.text = _URL.sub(
                lambda m: "" if m.group(1) == host else m.group(0), result.text
            )
            result.flags.append(f"移除非白名单链接:{host}")
            result.rewritten = True

    # 含具体数字却没有引用标记——可能是编的参数
    if _DIGIT.search(result.text) and not _CITATION.search(result.text):
        result.flags.append("无引用的数字陈述")

    if len(result.text) > max_length:
        result.flags.append(f"超长回复:{len(result.text)}字")

    return result


def split_long(text: str, max_length: int = 200) -> list[str]:
    """把超长回复拆成多条。

    按句号断，而不是按字数硬切——切在句子中间比长一点更难读。
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    buf = ""
    for seg in re.split(r"(?<=[。！？\n])", text):
        if not seg:
            continue
        if len(buf) + len(seg) > max_length and buf:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        parts.append(buf)
    return parts
