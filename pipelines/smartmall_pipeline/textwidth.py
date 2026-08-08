"""终端表格的列宽计算。

``f"{'女装':<16}"`` 补的是**字符数**，终端排的是**列数**，汉字占两列——
于是每一行都按自己标签的长度错开，标签越短的行越靠右。这个项目的
输出几乎全是中文列名（漏斗关卡、覆盖度类目、转人工状态），不过这一层
就没有一张表是对齐的。

截断同理：``name[:14]`` 砍到 14 个字符，如果是汉字那就是 28 列，
直接把整列撑爆。

只依赖标准库 ``unicodedata``，是纯函数，所以能直接单测。
"""

from __future__ import annotations

import unicodedata

__all__ = ["display_width", "pad", "truncate"]


def display_width(s: str) -> int:
    """字符串在等宽终端里占的列数。

    ``W``（宽）与 ``F``（全角）占两列，其余按一列算。组合字符（``Mn``，
    比如声调符号）不占位——把它算成一列会让带注音的文本整体偏移。
    """
    return sum(
        0 if unicodedata.combining(c) else
        2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in s
    )


def pad(s: str, width: int, *, right: bool = False) -> str:
    """补空格到 ``width`` **列**。已经超宽就原样返回——

    截断是调用方的决定：把它塞进 pad 里，会让"补齐"这个动作偷偷丢数据。
    """
    fill = " " * max(0, width - display_width(s))
    return fill + s if right else s + fill


def truncate(s: str, width: int, *, mark: str = "…") -> str:
    """按**列数**截断，超出时以 ``mark`` 结尾。

    ``mark`` 自身也占列——不预留它的宽度，截出来的串会正好比列宽多一列，
    这种一列的溢出最难看出来，因为只有恰好截断的那几行会歪。
    """
    if display_width(s) <= width:
        return s
    budget = width - display_width(mark)
    if budget <= 0:
        return mark[:width] if width else ""

    out, used = [], 0
    for c in s:
        w = display_width(c)
        if used + w > budget:
            break
        out.append(c)
        used += w
    return "".join(out) + mark
