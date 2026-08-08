"""知识条目去重。

关卡①去的是**会话**的重（MinHash 近重复）。但去完之后，几段内容不同的
对话仍可能抽出**同一个问题**——"针织衫怎么洗"这种高频问题，几乎每段
养护类对话里都有。结果是知识库里躺着五条一模一样的条目。

后果比"浪费存储"严重得多：检索 top-5 会被同一条知识的五个副本占满，
多样性归零，模型看到的等于只有一个事实。用户问一个稍微复杂点的问题，
本该由三条不同知识拼出的答案，只剩下一条。

**合并而不是丢弃。** 五段不同的对话都说"建议手洗"，这不是噪音，是
五个独立信源相互印证——应当提高置信度。丢掉四条再按单条的置信度
判断，等于把证据扔了。
"""

from __future__ import annotations

from typing import Sequence

from .models import GateStats, KnowledgeItem

#: 每多一个独立信源印证，置信度加这么多。
#: 取小值是因为它们可能同源（同一批合成数据、同一个主播的话术），
#: 印证强度不如真正独立的来源。
CORROBORATION_BONUS = 0.03

#: 印证加成的**总上限**。
#:
#: 这个上限比单次加成更重要。没有它的话，一个被抽出一百次、
#: 但模型每次都只给 0.35 置信度的条目，会被加成推到自动通过——
#: 而"模型一百次都拿不准"根本不是"确定"的证据，只是同一个不确定
#: 重复了一百遍。把相关证据当独立证据累加，是这类打分最容易犯的错。
#:
#: 有了上限，印证只能锦上添花：0.85 的条目能推到 0.95 自动通过，
#: 0.35 的条目推到 0.45 仍然进人工队列——后者本来就该让人看一眼。
MAX_BONUS = 0.10

#: 置信度绝对上限。永远留一点余量：重复得多只说明大家都这么说，
#: 不代表这个答案就是对的。
MAX_CONFIDENCE = 0.98


def _score(item: KnowledgeItem) -> tuple:
    """挑代表时的排序键：置信度 > 质量分 > 内容更完整。

    内容长度作为末位判据，是因为同一个问题的多个抽取里，
    更长的那条通常包含更多限定条件（"水温不超过30度"比"手洗"有用）。
    """
    return (item.confidence, item.quality_score, len(item.content or ""))


def merge_duplicates(
    items: Sequence[KnowledgeItem],
    *,
    bonus: float = CORROBORATION_BONUS,
) -> tuple[list[KnowledgeItem], GateStats]:
    """按 ``dedup_key`` 合并同题条目。

    保留最优的一条作为代表，把其余条目的标签与关联商品并进来，
    并按印证次数提高置信度。

    Returns:
        (合并后的条目, 统计)。统计里记录了合并掉多少条，
        以及重复率——重复率突然升高通常意味着上游进了重复数据。
    """
    stats = GateStats(
        gate="⑤ 知识去重", input_count=len(items),
        input_unit="条目", output_unit="条目",
    )

    groups: dict[str, list[KnowledgeItem]] = {}
    for item in items:
        groups.setdefault(item.dedup_key(), []).append(item)

    merged: list[KnowledgeItem] = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue

        rep = max(members, key=_score)
        others = [m for m in members if m is not rep]

        # 并集而不是覆盖：不同抽取可能各自标了一部分标签
        tags = list(rep.tags)
        products = list(rep.product_ids)
        for m in others:
            tags.extend(t for t in m.tags if t not in tags)
            products.extend(p for p in m.product_ids if p not in products)
        rep.tags = tags
        rep.product_ids = products

        # 代表条目可能没有类目而其他有——别浪费这个信息
        if rep.category_id is None:
            rep.category_id = next(
                (m.category_id for m in others if m.category_id is not None), None
            )

        boost = min(MAX_BONUS, bonus * len(others))
        rep.confidence = min(MAX_CONFIDENCE, rep.confidence + boost)
        merged.append(rep)
        stats.drop("同题合并", len(others))
        stats.modify("多信源印证→提高置信度")

    stats.output_count = len(merged)
    return merged, stats


def duplicate_ratio(items: Sequence[KnowledgeItem]) -> float:
    """重复率。发版门禁与线上监控都看这个数。"""
    if not items:
        return 0.0
    unique = len({i.dedup_key() for i in items})
    return 1.0 - unique / len(items)
