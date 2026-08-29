"""跑检索评测：``python -m app.eval_cli``。

单独一个入口而不是塞进 ai-agent 的 ``smartmall-agent eval``：那边三个评测集
都是分类任务（意图/拒答/安全），输入是 ``Deps``、指标是准确率与 F1；检索是
排序任务，输入要一个语料加一个索引、指标是 Recall@k 与 MRR。硬塞进同一个
``RUNNERS`` 字典，得给它编一份意义不明的 accuracy——**一个填出来的指标比
没有指标更坏**，因为它会被当真。

用 Milvus Lite（给个本地目录当 uri 就是嵌入式），不需要 Docker。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from smartmall_pipeline.rag.embedding import build_provider
from smartmall_pipeline.rag.milvus import MilvusConfig

from .eval_retrieval import (
    index_corpus, load_jsonl, render, render_gate, render_sweep,
    run_gate, run_retrieval,
)
from .milvus_store import MilvusStore
from .retrieval import RetrievalConfig
from .search import SearchService


def _holdout(store, provider, samples, sweep, args) -> str:
    """在一半上选权重，报另一半的分数。

    **这一步不是形式主义。** 扫 8 个权重再挑最好的，等于做了 8 次比较；
    115 条样本上 R@5 差两条就是 1.7 个百分点，挑出来的"最优"很可能只是
    噪声。选参与报分用同一批数据，得到的提升数字不能对外说。
    """
    from .eval_retrieval import split_stratified

    train, test = split_stratified(samples)
    _, tr = run_retrieval(store, provider, train, recall_k=args.recall_k,
                          rrf_k=args.rrf_k, sweep=sweep)
    best_w = max(tr, key=lambda w: (tr[w].recall[5], tr[w].mrr))

    base, te = run_retrieval(store, provider, test, recall_k=args.recall_k,
                             rrf_k=args.rrf_k, sweep=[best_w, 1.0])
    picked, equal, dense = te[best_w], te[1.0], base["dense"]

    n = len(test)
    def line(label, r):
        return (f"    {label:<14}R@5 {r.recall[5]:.3f}"
                f"（{round(r.recall[5] * n)}/{n}）　MRR {r.mrr:.3f}")

    out = [
        "",
        f"  留出验证（在 {len(train)} 条上选权重，报另外 {n} 条的分数）：",
        f"    训练半区选出的权重 = {best_w:.2f}",
        line("纯向量", dense),
        line("等权融合", equal),
        line(f"加权融合 w={best_w:.2f}", picked),
    ]
    d = picked.recall[5] - dense.recall[5]
    if d > 0:
        out.append(f"    → 加权融合比纯向量高 {d:+.3f}"
                   f"（{round(d * n)} 条查询），样本量 {n}，这个差别不显著")
    elif d == 0:
        out.append("    → 加权融合与纯向量打平：这个评测集上混合检索没有净收益")
    else:
        out.append(f"    → **加权融合仍不如纯向量**（{d:+.3f}），"
                   "不要在简历上写混合检索提升了召回")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="eval-retrieval", description="检索召回评测：单路 vs 混合")
    p.add_argument("--embedding", default="onnx",
                   help="向量化后端：onnx（默认，91MB 本地模型，无需 API key）"
                        " / dashscope / local")
    p.add_argument("--db", default="",
                   help="Milvus Lite 的库文件（要以 .db 结尾），"
                        "默认建在临时目录并在结束后删除")
    p.add_argument("--recall-k", type=int, default=50, help="每路召回条数")
    p.add_argument("--rrf-k", type=int, default=60, help="RRF 的 k")
    p.add_argument("--sparse-weight", type=float, default=1.0,
                   help="BM25 那一路在 RRF 里的权重，向量固定 1.0")
    p.add_argument("--sweep", default="",
                   help="扫一组稀疏权重，逗号分隔，如 0.1,0.25,0.5,1.0。"
                        "复用同一批召回结果，只重排序，几乎不额外耗时")
    p.add_argument("--holdout", action="store_true",
                   help="留出验证：在一半样本上选权重，报另一半的分数。"
                        "选参与报分用同一批数据得到的提升不能对外说")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条查询，试水用")
    p.add_argument("--show-misses", type=int, default=8,
                   help="打印几条 R@5 没命中的错例")
    p.add_argument("--gate", action="store_true",
                   help="连兜底闸门一起量：答得了的问题里误转了多少、"
                        "答不了的问题里漏放了多少")
    args = p.parse_args(argv)

    corpus = load_jsonl("retrieval-corpus.jsonl")
    samples = load_jsonl("retrieval.jsonl")
    if args.limit:
        samples = samples[:args.limit]

    ids = {c["id"] for c in corpus}
    # 金标指向不存在的条目 = 那条查询永远算作没召回，会静静地把三档
    # 一起拉低。**必须在跑之前挡住**，跑完再发现就已经信了一轮错数字
    dangling = sorted({g for s in samples for g in s["gold"] if g not in ids})
    if dangling:
        print(f"✗ 评测集里有金标指向不存在的语料：{dangling}", file=sys.stderr)
        return 2

    provider = build_provider(args.embedding)

    # Milvus Lite 的 uri 必须是一个 **.db 文件**，给目录会被判为非法 uri
    # （"needs start with [unix, http, https, tcp] or a local file endswith [.db]"）。
    # 报错信息本身够清楚，但它抛在 ensure_collection 里，看起来像建表失败
    tmp = not args.db
    workdir = tempfile.mkdtemp(prefix="eval-milvus-") if tmp else ""
    db = Path(args.db) if args.db else Path(workdir) / "kb.db"
    try:
        store = MilvusStore(MilvusConfig(
            uri=str(db), collection="kb_chunk_eval",
            analyzer="jieba", dense_dim=provider.dim,
        ))
        store.ensure_collection(drop_existing=True)

        def tick(i, n, what="建索引"):
            print(f"\r  {what} {i}/{n}", end="", flush=True)

        index_corpus(store, provider, corpus, progress=tick)
        print(f"\r  已索引 {store.count()} 条切片" + " " * 20)

        sweep = [float(w) for w in args.sweep.split(",") if w.strip()]
        reports, swept = run_retrieval(
            store, provider, samples,
            recall_k=args.recall_k, rrf_k=args.rrf_k,
            sparse_weight=args.sparse_weight, sweep=sweep,
            progress=lambda i, n: tick(i, n, "检索"),
        )
        print("\r" + " " * 30 + "\r", end="")
        print(render(reports, provider_name=provider.name,
                     corpus_size=len(corpus), sample_size=len(samples)))
        if swept:
            print(render_sweep(swept, reports["dense"]))

        if args.holdout and sweep:
            print(_holdout(store, provider, samples, sweep, args))

        if args.gate:
            negatives = load_jsonl("retrieval-negative.jsonl")
            service = SearchService(
                store=store, provider=provider,
                config=RetrievalConfig(kb_version=""),
            )
            # IDF 表要先建起来，否则 lexical_overlap 恒为 0，
            # 中间地带那道词汇支撑判据等于永远不通过——兜底率会虚高
            service.refresh_stats()
            gate = run_gate(store, provider, samples, negatives,
                            search=service,
                            progress=lambda i, n: tick(i, n, "过闸门"))
            print("\r" + " " * 30 + "\r", end="")
            print(render_gate(gate))
            if args.show_misses:
                print("\n  判错的（漏放优先看）：")
                for m in sorted(gate.misfires,
                                key=lambda m: m["kind"] != "漏放")[:args.show_misses]:
                    print(f"    [{m['kind']}]「{m['text']}」"
                          f" score={m['score']} overlap={m['overlap']}")

        if args.show_misses:
            print("\n  融合这一路 R@5 没命中的（改哪里看这里，不是看总分）：")
            for m in reports["fused"].misses[:args.show_misses]:
                rank = "未召回" if m["rank"] is None else f"排第 {m['rank']}"
                print(f"    [{m['hard']}]「{m['text']}」→ 金标 {m['gold']}，{rank}")
        print()
        return 0
    finally:
        if tmp:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
