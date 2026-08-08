"""终端表格的列宽计算。

``f"{'转人工':<12}"`` 补的是**字符数**，终端排的是**列数**，汉字占两列——
于是每一行都按自己标签的长度错开。评测报告的类名（拦截/转人工/正常）
和 trace 表的表头全是中文，不过这一层就没有一行是对齐的。

与 ``smartmall_pipeline.textwidth`` 是同一份逻辑的两份实现。没有抽成
公共包：ai-agent 只把 pipeline 当**可选**依赖（``[local]`` extra），
而唯一的公共库 ai-common 拖着 fastapi——为了四行纯函数让 CLI 依赖
一个 Web 框架，比抄一遍更糟。
"""

from __future__ import annotations

import unicodedata

__all__ = ["display_width", "pad", "truncate"]


def display_width(s: str) -> int:
    """字符串在等宽终端里占的列数。

    ``W``（宽）与 ``F``（全角）占两列；组合字符（``Mn``）不占位。
    """
    return sum(
        0 if unicodedata.combining(c) else
        2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in s
    )


def pad(s: str, width: int, *, right: bool = False) -> str:
    """补空格到 ``width`` **列**。已经超宽就原样返回——截断是调用方的
    决定，塞进 pad 里会让"补齐"这个动作偷偷丢数据。"""
    fill = " " * max(0, width - display_width(s))
    return fill + s if right else s + fill


def truncate(s: str, width: int, *, mark: str = "…") -> str:
    """按**列数**截断，超出时以 ``mark`` 结尾。

    ``mark`` 自身也占列——不预留它的宽度，截出来的串正好比列宽多一列。
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
