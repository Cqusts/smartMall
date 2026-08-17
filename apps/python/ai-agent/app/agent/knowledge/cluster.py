"""把近似问法聚成一个盲点。

`handover_ticket` 按题面精确分组，于是「怎么退货」和「退货怎么弄」
算成两个盲点，各被问一次，各是 P2。合起来它是一个被问两次的 P1。

**这不是锦上添花，是补写顺序对不对的问题。** 真实工单里同一个缺口
有五六种问法很常见，散开之后每一种看起来都不紧急，而它加起来
可能是当天最该补的那一条。

用词袋 Jaccard 而不是向量：这一步要在没有 embedding 服务的环境里
也能跑（补写是离线工作，不该被一个在线依赖挡住），而且判据要能
人工复核——两个问题为什么被判成一类，看一眼共同词就明白。
"""

from __future__ import annotations

from typing import Sequence

from .state import BlindSpot

#: 判成同一个盲点的相似度下限。
#:
#: **这个 0.30 是量出来的。** 拿 17 对真实问法量过（``tests/test_knowledge.py``
#: 里把这批样本钉住了，改阈值会被测出来）：
#:
#:   同一个缺口                        不同缺口
#:   请问怎么退货呢 / 怎么退货   0.50   怎么退货 / 怎么洗           0.25
#:   这件是什么面料 / 面料是什么 0.43   七天无理由怎么退 / 怎么退货 0.25
#:   怎么退货 / 退货怎么弄       0.40   多久发货 / 什么时候发货     0.14
#:   会起球吗 / 这个起球吗       0.40   会起球吗 / 会缩水吗         0.00
#:
#: 真正例最低 0.40、假正例最高 0.25，0.30 落在中间。
#:
#: **它抓不到的：换了同义词的问法。**
#:
#:   160cm穿什么码 / 身高160穿什么尺码   0.22  ← 漏
#:   可以退换吗 / 支持退换货吗           0.12  ← 漏
#:
#: 写在这里是因为它会被误当成 bug。词袋对同义词无能为力，要解决只能上
#: 向量，那是另一个量级的依赖（补写是离线工作，不该被一个在线服务挡住）。
#: 现状是**宁可少聚**——多算几个盲点，人工一眼看得出是一回事；多聚了
#: 是两个不同的问题被合成一条，补写时必然漏掉一个，而且没人会发现。
#:
#: 试过用重合系数（交集 / 较短一方）代替 Jaccard 来救长短不一的情况，
#: 结果更差：「七天无理由怎么退 / 怎么退货」会涨到 0.67 被判成同一类，
#: 而它们是两个问题。短问题被长问题包含**恰恰说明后者更具体**。
SIMILAR_ABOVE = 0.30


def _tokens(text: str) -> set[str]:
    """与检索侧同一套分词。

    自己再写一套的话，"这两个问题算不算像"在聚类和检索里会给出
    不同答案，而排查时没人会想到去比这两处的分词规则。
    """
    from smartmall_pipeline.rag.bm25 import tokenize

    return set(tokenize(text))


def similarity(a: str, b: str) -> float:
    """词袋 Jaccard。"""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cluster_spots(
    spots: Sequence[BlindSpot], *, threshold: float = SIMILAR_ABOVE
) -> list[BlindSpot]:
    """把近似问法合并。

    贪心：按被问次数从多到少扫，每个问题并进第一个够像的簇。
    次数最多的那个问法当代表——**人工补写时照着最常见的说法写**，
    写出来的知识更容易被后来的同类提问检索到。

    合并后 ``times`` 相加，``ticket_ids`` 合并，其余问法进 ``variants``。
    """
    ordered = sorted(spots, key=lambda s: (-s.times, s.question))
    clusters: list[BlindSpot] = []

    for spot in ordered:
        for c in clusters:
            if similarity(c.question, spot.question) >= threshold:
                c.times += spot.times
                c.ticket_ids.extend(spot.ticket_ids)
                # 代表问法不变，但别的问法要留着：只覆盖一种说法的知识
                # 会让另外几种问法继续检索不到，盲点根本没消掉
                if spot.question not in c.variants:
                    c.variants.append(spot.question)
                c.variants.extend(
                    v for v in spot.variants if v not in c.variants)
                if c.product_id is None:
                    c.product_id = spot.product_id
                break
        else:
            clusters.append(BlindSpot(
                question=spot.question, times=spot.times,
                ticket_ids=list(spot.ticket_ids), variants=list(spot.variants),
                reason=spot.reason, intent=spot.intent,
                product_id=spot.product_id,
            ))

    return sorted(clusters, key=lambda s: (-s.times, s.question))
