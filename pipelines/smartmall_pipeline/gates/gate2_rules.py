"""关卡② 业务规则清洗。

电商对话特有的噪声，通用算子处理不了。

最关键的是**参数占位化**——它决定知识能否泛化：

    "您的订单 20250801123 明天到"        → 只对这一单有效，无复用价值
    "您的订单 <ORDER_NO> 预计 <DATE> 送达" → 可复用的话术模板

不做这一步，知识库里会塞满一次性的、永远不会被第二个用户命中的"知识"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Dialogue, GateStats, Turn

# ---------------------------------------------------------------- 系统话术

SYSTEM_PHRASE_PATTERNS = [
    re.compile(r"正在(为您)?转接(人工)?(客服)?"),
    re.compile(r"客服(小妹|小哥|专员)?正在忙"),
    re.compile(r"^(您好|你好)[,，]?(很高兴为您服务|请问有什么(可以)?帮(您|你))"),
    re.compile(r"感谢您的(咨询|耐心等待)[,，]?(祝您|欢迎)"),
    re.compile(r"本次服务已结束"),
    re.compile(r"^亲[,，]?还在吗"),
    re.compile(r"请对本次服务(进行)?评价"),
]

# 纯寒暄：**整句由寒暄词与填充字符组成**时才算无效轮次。
#
# 必须允许多个寒暄词连排——"在的亲～"、"好的谢谢"、"嗯嗯好的" 都是常见形态，
# 只匹配单个词会漏掉大半。反过来，"好的，这件多少钱" 里 "好的" 之后
# 还有实质内容，整串匹配不上，因此会被保留。
_PLEASANTRY_TOKEN = (
    r"(?:在吗|在不在|在的|在|你好|您好|哈喽|hi|hello|嗯+|哦+|噢+|好的|好|行|OK|ok|"
    r"谢谢|感谢|多谢|辛苦了?|麻烦了?|收到|明白|知道了|亲|亲亲|稍等|等等)"
)
_PLEASANTRY_FILLER = r"[\s~～。.!！?？,，、:：呢吧啊呀哦嗯的了啦哈]*"

PLEASANTRY_RE = re.compile(
    rf"^{_PLEASANTRY_FILLER}(?:{_PLEASANTRY_TOKEN}{_PLEASANTRY_FILLER})+$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- 参数占位化

@dataclass(frozen=True)
class ParamRule:
    name: str
    pattern: re.Pattern[str]
    placeholder: str


PARAM_RULES: list[ParamRule] = [
    # 金额：￥99 / 99元 / 99.90块
    ParamRule(
        "amount",
        re.compile(r"(?:￥|¥|\$)\s?\d+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?\s?(?:元|块钱|块)"),
        "<AMOUNT>",
    ),
    # 日期：2026-03-01 / 3月1号 / 3月1日
    ParamRule(
        "date",
        re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]"),
        "<DATE>",
    ),
    # 时间：14:30 / 下午3点
    ParamRule("time", re.compile(r"\d{1,2}[:：]\d{2}"), "<TIME>"),
    # 短订单号：6-11 位数字（12 位以上已在关卡① PII 阶段处理）
    ParamRule("order_no", re.compile(r"(?<![\d<])\d{6,11}(?![\d>])"), "<ORDER_NO>"),
    # 快递单号：SF/YT/ZT 等字母前缀 + 数字
    ParamRule("tracking_no", re.compile(r"\b[A-Z]{2}\d{10,15}\b"), "<TRACKING_NO>"),
]

# 保留的时效性数量词：这些不该被占位化，它们是知识本身
# （"48小时内发货" 占位成 "<AMOUNT>内发货" 就毁了）
DURATION_RE = re.compile(r"\d+\s?(?:小时|天|个工作日|分钟|周|个月)")


@dataclass
class Gate2Config:
    strip_system_turns: bool = True
    strip_pleasantries: bool = True
    parameterize: bool = True
    require_qa_pair: bool = True
    """必须至少有一组「用户提问 + 客服回答」，否则整段丢弃。"""
    min_effective_turns: int = 2
    system_patterns: list[re.Pattern[str]] = field(
        default_factory=lambda: list(SYSTEM_PHRASE_PATTERNS)
    )


def is_system_phrase(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def is_pleasantry(text: str) -> bool:
    return bool(PLEASANTRY_RE.match(text.strip()))


def parameterize(text: str) -> tuple[str, dict[str, int]]:
    """把具体参数替换为占位符，保留时效性数量词。

    实现要点：先把 "48小时" 这类时长片段挖出来占位，跑完参数规则再填回去。
    否则 ``\\d+`` 规则会把 48 当成订单号或金额吃掉。
    """
    hits: dict[str, int] = {}

    # 保护时长片段
    protected: list[str] = []

    def _protect(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = DURATION_RE.sub(_protect, text)

    for rule in PARAM_RULES:
        text, n = rule.pattern.subn(rule.placeholder, text)
        if n:
            hits[rule.name] = hits.get(rule.name, 0) + n

    # 还原
    def _restore(m: re.Match[str]) -> str:
        return protected[int(m.group(1))]

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text, hits


def _fix_roles(turns: list[Turn]) -> tuple[list[Turn], int]:
    """角色纠正。

    部分数据集的角色标注错乱。用一个保守的启发式：连续两轮同角色，
    且后一轮明显是应答口吻（以"亲/您好/好的"开头且上一轮是提问），则翻转。
    保守是有意的——错误的纠正比不纠正更糟。
    """
    fixed = 0
    for i in range(1, len(turns)):
        prev, cur = turns[i - 1], turns[i]
        if prev.role != cur.role or cur.role == "system":
            continue
        if prev.content.rstrip().endswith(("?", "？")) and re.match(
            r"^(亲|您好|你好|好的|可以的|支持的|是的)", cur.content
        ):
            cur.role = "agent" if cur.role == "user" else "user"  # type: ignore[assignment]
            fixed += 1
    return turns, fixed


def run(
    dialogues: list[Dialogue],
    config: Gate2Config | None = None,
) -> tuple[list[Dialogue], GateStats]:
    """执行关卡②。"""
    cfg = config or Gate2Config()
    stats = GateStats(gate="② 规则清洗", input_count=len(dialogues))
    kept: list[Dialogue] = []

    for dlg in dialogues:
        turns: list[Turn] = []

        for turn in dlg.turns:
            if cfg.strip_system_turns:
                if turn.role == "system":
                    stats.modify("剥离system轮次")
                    continue
                if is_system_phrase(turn.content, cfg.system_patterns):
                    stats.modify("剥离系统话术")
                    continue

            if cfg.strip_pleasantries and is_pleasantry(turn.content):
                stats.modify("剥离无效寒暄")
                continue

            if turn.is_empty:
                continue

            if cfg.parameterize:
                new_text, hits = parameterize(turn.content)
                if hits:
                    if turn.raw_content is None:
                        turn.raw_content = turn.content
                    turn.content = new_text
                    for kind, n in hits.items():
                        stats.modify(f"参数占位:{kind}", n)

            turns.append(turn)

        turns, n_fixed = _fix_roles(turns)
        if n_fixed:
            stats.modify("角色纠正", n_fixed)

        # 重排 turn_index，保持连续
        for i, t in enumerate(turns):
            t.turn_index = i

        if len(turns) < cfg.min_effective_turns:
            stats.drop("有效轮次不足")
            continue

        if cfg.require_qa_pair:
            has_user = any(t.role == "user" for t in turns)
            has_agent = any(t.role == "agent" for t in turns)
            if not (has_user and has_agent):
                stats.drop("缺少问答配对")
                continue

        dlg.turns = turns
        kept.append(dlg)

    stats.output_count = len(kept)
    return kept, stats
