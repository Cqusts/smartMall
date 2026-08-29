"""检索评测：单路 vs 混合，到底差多少。

**这是整个项目里唯一能回答"混合检索值不值"的地方。** 代码里写了双通道
召回 + RRF 融合，单测能证明它按设计跑，但证明不了它比单路好——那是一个
关于效果的主张，只能量。

三档并排跑，同一批查询、同一个索引、同一组参数，只换排序依据：

* ``dense``  —— 只按余弦相似度排
* ``bm25``   —— 只按 Milvus 原生 BM25 排
* ``fused``  —— 两路各召回 recall_k 条，本地 RRF 融合

**只报融合那一档等于没报。** 没有基线的话，读的人无法判断那几十行融合
代码换来了什么；而这恰恰是面试里最先被追问的地方。

---------------------------------------------------------------- 为什么是 Recall@5

评测集 100 条语料。**Recall@50 在这个规模上没有意义**——召回 50 条等于
把半个库都拿回来，三档都接近 1.0，看不出差别。项目文档里定的验收线本来
就是 Recall@5 ≥ 0.85（见 evals/README.md），这里跟它对齐。

同时报 MRR@10：Recall 只问"在不在里面"，MRR 还问"排第几"。融合的价值
常常体现在把对的那条从第 4 位提到第 1 位，而 Recall@5 对此完全无感。

---------------------------------------------------------------- 数字的出处

报告里必须打出向量化后端的名字。换个 embedding 数字就变，不写清楚
就等于给了一个没有出处的百分比——那种数字在简历上是负资产。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from smartmall_pipeline.rag.embedding import EmbeddingProvider

from .milvus_store import MilvusStore
from .retrieval import Hit, dedup_by_item, reciprocal_rank_fusion

CHANNELS = ("dense", "bm25", "fused")

#: 报哪几个 k。@1 是"直接答对"，@5 是文档里的验收线，
#: @10 是留给 rerank 的输入规模。
RECALL_AT = (1, 3, 5, 10)

MRR_AT = 10


def dataset_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        d = parent / "evals" / "datasets"
        if d.is_dir():
            return d
    raise FileNotFoundError("找不到 evals/datasets")


def load_jsonl(name: str) -> list[dict]:
    path = dataset_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"缺少评测集 {path}")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------- 建索引


def index_corpus(
    store: MilvusStore,
    provider: EmbeddingProvider,
    corpus: Sequence[dict],
    *,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """把评测语料灌进 Milvus。

    **标题与正文一起向量化、一起进 BM25 的 text 字段。** 只放正文的话，
    "云朵针织衫的面料成分"这类标题里的商品名就检索不到——而真实的
    knowledge_item 也是标题正文一起入索引的（见 pipeline 的 cmd_index），
    评测条件要和线上一致，不然量的是另一套系统。
    """
    texts = [f"{d['title']}\n{d['content']}" for d in corpus]
    vecs: list[list[float]] = []
    batch = getattr(provider, "max_batch", 16)
    for i in range(0, len(texts), batch):
        vecs.extend(provider.embed(texts[i:i + batch]))
        if progress:
            progress(min(i + batch, len(texts)), len(texts))

    store.upsert_chunks([
        {
            "item_id": int(d["id"]), "chunk_seq": 0, "text": t,
            "dense_vec": v,
            "biz_type": d.get("biz_type", "faq"),
            "modality": "text",
            "category_id": 0,
            "product_ids": list(d.get("product_ids") or []),
            "asset_ids": [],
            # 评测语料全部按"已审核通过"入库：审核闸门有它自己的用例，
            # 混在检索评测里只会让召回率无缘无故变低，看不出是哪一层的问题
            "review_status": "approved", "valid_to_ts": 0,
            "quality_score": 0.9, "kb_version": "",
        }
        for d, t, v in zip(corpus, texts, vecs)
    ])
    return len(corpus)


# ---------------------------------------------------------------- 指标


@dataclass
class ChannelReport:
    """一路检索的成绩。"""

    channel: str
    recall: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    #: 按难度分层的 Recall@5。总分掩盖分布——
    #: 词面难例上 BM25 崩掉、精确匹配上向量崩掉，只看总分两者都看不见
    by_hard: dict[str, float] = field(default_factory=dict)
    misses: list[dict] = field(default_factory=list)


def _rank_of_gold(ranked: Sequence[Hit], gold: set[int]) -> int | None:
    """金标在结果里排第几（1 起）。不在里面返回 None。"""
    for i, h in enumerate(ranked, start=1):
        if h.item_id in gold:
            return i
    return None


def _score(channel: str, ranks: Sequence[int | None],
           samples: Sequence[dict]) -> ChannelReport:
    n = len(ranks)
    rep = ChannelReport(channel=channel)
    for k in RECALL_AT:
        rep.recall[k] = sum(1 for r in ranks if r is not None and r <= k) / n
    rep.mrr = sum(1.0 / r for r in ranks if r is not None and r <= MRR_AT) / n

    buckets: dict[str, list[int | None]] = {}
    for r, s in zip(ranks, samples):
        buckets.setdefault(s.get("hard", "plain"), []).append(r)
    for hard, rs in buckets.items():
        rep.by_hard[hard] = sum(1 for r in rs if r is not None and r <= 5) / len(rs)

    rep.misses = [
        {"text": s["text"], "gold": s["gold"], "hard": s.get("hard", ""),
         "rank": r}
        for r, s in zip(ranks, samples)
        if r is None or r > 5
    ]
    return rep


# ---------------------------------------------------------------- 跑


def run_retrieval(
    store: MilvusStore,
    provider: EmbeddingProvider,
    samples: Sequence[dict],
    *,
    filter_expr: str = 'review_status in ["approved", "revised"]',
    recall_k: int = 50,
    rrf_k: int = 60,
    sparse_weight: float = 1.0,
    sweep: Sequence[float] = (),
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, ChannelReport], dict[float, ChannelReport]]:
    """三档并排跑，外加一次可选的稀疏权重扫描。

    **一次查询算三档，不是跑三遍。** ``recall()`` 一次就把两路都拿回来了，
    分开跑除了慢三倍，还会因为每次的近邻搜索有随机性而让三档不可比——
    比较的前提是除了排序依据以外什么都一样。同理，扫权重时复用同一批
    召回结果：融合只是重新加权排序，不需要重新查一遍。

    返回 ``(三档报告, 权重 → 报告)``。
    """
    ranks: dict[str, list[int | None]] = {c: [] for c in CHANNELS}
    swept: dict[float, list[int | None]] = {w: [] for w in sweep}

    for i, s in enumerate(samples, 1):
        qvec = provider.embed_query(s["text"])
        dense, sparse = store.recall(
            query_text=s["text"], query_dense=qvec,
            filter_expr=filter_expr, recall_top_k=recall_k,
        )
        fused = reciprocal_rank_fusion(
            dense, sparse, k=rrf_k, weights=(1.0, sparse_weight))

        gold = set(s["gold"])
        for channel, hits in (("dense", dense), ("bm25", sparse), ("fused", fused)):
            # 去重按 item：同一条知识切成多块时，多块并列会把别的知识挤下去，
            # 而指标问的是"这条知识找到没有"，不是"找到几个切片"
            ranks[channel].append(_rank_of_gold(dedup_by_item(hits), gold))

        for w in sweep:
            alt = reciprocal_rank_fusion(dense, sparse, k=rrf_k, weights=(1.0, w))
            swept[w].append(_rank_of_gold(dedup_by_item(alt), gold))

        if progress:
            progress(i, len(samples))

    return (
        {c: _score(c, ranks[c], samples) for c in CHANNELS},
        {w: _score(f"w={w}", swept[w], samples) for w in sweep},
    )


# ---------------------------------------------------------------- 报告


_CH_LABEL = {"dense": "纯向量", "bm25": "纯 BM25", "fused": "RRF 融合"}


def render(reports: dict[str, ChannelReport], *, provider_name: str,
           corpus_size: int, sample_size: int) -> str:
    lines = [
        "检索召回（单路 vs 混合）",
        f"  向量化后端 {provider_name}　语料 {corpus_size} 条　查询 {sample_size} 条",
        "",
        f"  {'':<10}" + "".join(f"R@{k:<7}" for k in RECALL_AT) + f"MRR@{MRR_AT}",
    ]
    for c in CHANNELS:
        r = reports[c]
        row = f"  {_CH_LABEL[c]:<9}" + "".join(
            f"{r.recall[k]:<9.3f}" for k in RECALL_AT) + f"{r.mrr:.3f}"
        lines.append(row)

    base, fused = reports["dense"], reports["fused"]
    d5 = fused.recall[5] - base.recall[5]
    lines += [
        "",
        f"  融合相对纯向量：R@5 {base.recall[5]:.3f} → {fused.recall[5]:.3f}"
        f"（{d5:+.3f}）　MRR {base.mrr:.3f} → {fused.mrr:.3f}"
        f"（{fused.mrr - base.mrr:+.3f}）",
    ]

    hards = sorted({h for r in reports.values() for h in r.by_hard})
    if hards:
        lines += ["", "  按难度分的 R@5（总分掩盖分布，这里才看得出两路各自的短板）：",
                  f"    {'':<10}" + "".join(f"{h:<10}" for h in hards)]
        for c in CHANNELS:
            r = reports[c]
            lines.append(f"    {_CH_LABEL[c]:<9}" + "".join(
                f"{r.by_hard.get(h, float('nan')):<10.3f}" for h in hards))
    return "\n".join(lines)


def split_stratified(samples: Sequence[dict], seed: int = 20260829
                     ) -> tuple[list[dict], list[dict]]:
    """按难度分层对半split，用来调参 / 验证。

    **在同一批样本上选权重再报同一批的分数，是在给自己发奖状。** 115 条里
    R@5 从 0.974 到 0.991 只是两条查询的差别，而扫了 8 个权重之后总能挑到
    一个看起来最好的——那个"提升"很可能只是挑出来的噪声。

    分层而不是随机对半：三类难度的占比差很多（plain 56 / lexical 42 /
    exact 17），随机切会让某一半的 exact 只剩四五条，两半根本不可比。
    """
    import random

    rng = random.Random(seed)
    buckets: dict[str, list[dict]] = {}
    for s in samples:
        buckets.setdefault(s.get("hard", "plain"), []).append(s)

    train: list[dict] = []
    test: list[dict] = []
    for hard in sorted(buckets):
        group = list(buckets[hard])
        rng.shuffle(group)
        half = len(group) // 2
        train.extend(group[:half])
        test.extend(group[half:])
    return train, test


def render_sweep(swept: dict[float, ChannelReport],
                 baseline: ChannelReport) -> str:
    """稀疏权重扫描表。

    **权重要扫出来，不能拍。** 拍一个 0.5 然后说"效果更好"，被追问
    "为什么是 0.5 不是 0.3" 就答不上来了；而扫一遍的成本只是把同一批
    召回结果重新排序，几秒钟的事。
    """
    lines = [
        "",
        "  稀疏权重扫描（向量权重固定 1.0，只调 BM25 那一路）：",
        f"    {'权重':<8}" + "".join(f"R@{k:<7}" for k in RECALL_AT) + f"MRR@{MRR_AT}",
        f"    {'0（纯向量）':<6}" + "".join(
            f"{baseline.recall[k]:<9.3f}" for k in RECALL_AT) + f"{baseline.mrr:.3f}",
    ]
    for w in sorted(swept):
        r = swept[w]
        lines.append(f"    {w:<8.2f}" + "".join(
            f"{r.recall[k]:<9.3f}" for k in RECALL_AT) + f"{r.mrr:.3f}")

    best_w = max(swept, key=lambda w: (swept[w].recall[5], swept[w].mrr))
    best = swept[best_w]
    verdict = (
        f"最好的权重 {best_w:.2f}：R@5 {best.recall[5]:.3f}、MRR {best.mrr:.3f}"
    )
    if (best.recall[5], best.mrr) <= (baseline.recall[5], baseline.mrr):
        verdict += "　—— **仍然不如纯向量**，这个评测集上混合检索没有收益"
    lines += ["", "  " + verdict]
    return "\n".join(lines)
