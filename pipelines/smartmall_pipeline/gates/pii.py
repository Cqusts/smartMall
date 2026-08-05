"""PII 脱敏。

**这不是数据质量问题，是合规问题**（见 docs/15-risks-compliance.md）。
脱敏后的文本进 `dwd_dialogue_turn.content`，原文加密进 `raw_content`，
仅审计角色可解密。

设计约束：

* **顺序敏感**。银行卡（16-19 位）必须在手机号（11 位）之前匹配，
  否则银行卡号的中间 11 位会被先当成手机号吃掉。
* **宁可多脱不可漏脱**。误脱一个商品编号只是损失一点信息，
  漏脱一个手机号是合规事故。
* **占位符要保留类型**。``<PHONE>`` 而不是 ``***``——
  下游的参数占位化（关卡②）依赖类型信息来判断这句话能否泛化。
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Rule(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    placeholder: str


# 顺序即优先级，不要随意调整
RULES: list[Rule] = [
    # 身份证：18 位，末位可能是 X。放最前面——它的前 17 位是纯数字，
    # 会被银行卡或手机号规则误伤
    Rule("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "<ID_CARD>"),

    # 银行卡：16-19 位连续数字
    Rule("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "<BANK_CARD>"),

    # 手机号：1 开头 11 位
    Rule("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "<PHONE>"),

    # 座机：区号-号码
    Rule("telephone", re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)"), "<TELEPHONE>"),

    # 邮箱
    Rule("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),

    # 详细地址：省/市/区 + 后续门牌
    Rule(
        "address",
        re.compile(
            r"[一-龥]{2,8}(?:省|自治区)?"
            r"[一-龥]{2,10}(?:市|自治州)"
            r"[一-龥]{2,10}(?:区|县|市)"
            r"[一-龥\d]{0,30}?(?:路|街|道|号|栋|室|区|巷|弄)[\d\-号栋室]*"
        ),
        "<ADDRESS>",
    ),

    # 订单号：电商订单号通常 12-24 位纯数字。
    # 放在手机号之后，避免把手机号当订单号
    Rule("order_no", re.compile(r"(?<!\d)\d{12,24}(?!\d)"), "<ORDER_NO>"),
]


class MaskResult(NamedTuple):
    text: str
    hits: dict[str, int]

    @property
    def changed(self) -> bool:
        return bool(self.hits)


def mask(text: str) -> MaskResult:
    """对单条文本做 PII 脱敏。

    Returns:
        脱敏后的文本与各类型命中次数。命中次数进 GateStats.modified，
        用于监控——某天 phone 命中数突然翻倍，通常意味着上游数据源变了。
    """
    hits: dict[str, int] = {}
    for rule in RULES:
        text, n = rule.pattern.subn(rule.placeholder, text)
        if n:
            hits[rule.name] = hits.get(rule.name, 0) + n
    return MaskResult(text, hits)


def contains_pii(text: str) -> bool:
    """快速判断是否残留 PII。用于发版前的合规抽检。"""
    return any(rule.pattern.search(text) for rule in RULES)
