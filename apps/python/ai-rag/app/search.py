"""在线检索：把 Milvus 的召回补成 Agent 能用的判据。

Milvus 负责它擅长的（HNSW 近邻、倒排 BM25、标量过滤下推），这里负责
三件它做不到、而 Agent 又必须有的事：

1. **查询向量化** —— Milvus 存向量但不产向量。
2. **硬性过滤** —— 走 :func:`~app.retrieval.build_filter_expr`，
   审核状态、时效、版本三条红线由函数封死，调用方无法绕过。
3. **判据补全** —— ``dense_score`` 与 ``lexical_overlap``。
   这两个是 Agent 决定「作答 / 澄清 / 转人工」的依据，而 Milvus 的
   ``hybrid_search`` 一个都给不出来。详见 :meth:`SearchService.search`。

融合用 :func:`~app.retrieval.reciprocal_rank_fusion`——与
``LocalVectorStore`` 同一套逻辑、同一组参数，所以换后端排序行为可比。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from smartmall_pipeline.rag import LexicalStats
from smartmall_pipeline.rag.store import SEARCHABLE_REVIEW_STATUS

from .milvus_store import F_REVIEW, F_TEXT, MilvusConfig, MilvusStore
from .retrieval import (
    Hit,
    RetrievalConfig,
    build_filter_expr,
    dedup_by_item,
    reciprocal_rank_fusion,
)

log = logging.getLogger(__name__)

#: 建 IDF 表时单次拉取的条数。Milvus 的 query 有返回上限，要分页。
_SCAN_PAGE = 1000


class SearchError(RuntimeError):
    """检索失败。

    与「检索到 0 条」是两回事，这个区分一路传到 Agent：0 条是确定的
    结论（知识库里确实没有），失败是不知道——不知道就不能对用户说
    「没找到相关信息」。见 ``agent.retriever.RetrievalError``。
    """


@dataclass(frozen=True)
class ScoredHit:
    """一条带完整判据的命中。

    比 :class:`~app.retrieval.Hit` 多三个分数，因为 Agent 要靠它们
    分流，而不只是排序。
    """

    item_id: int
    chunk_id: int
    title: str
    content: str
    score: float
    """RRF 融合分。**只反映排名，不反映相关度**——别拿它当拒答判据。"""
    dense_score: float
    """余弦相似度。Agent 的 ``max_score`` 用的是这个。"""
    bm25_score: float
    lexical_overlap: float
    """查询里有区分度的词匹配上了多少，0~1（IDF 加权）。"""
    biz_type: str = "qa"
    modality: str = "text"
    asset_ids: tuple[int, ...] = ()
    product_ids: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """字段名与 ``agent.retriever.HttpRetriever`` 读的键一一对应。"""
        return {
            "item_id": self.item_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "content": self.content,
            "score": round(self.score, 6),
            "dense_score": round(self.dense_score, 6),
            "bm25_score": round(self.bm25_score, 6),
            "lexical_overlap": round(self.lexical_overlap, 6),
            "biz_type": self.biz_type,
            "modality": self.modality,
            "asset_ids": list(self.asset_ids),
            "product_ids": list(self.product_ids),
        }


def _split_title(text: str) -> tuple[str, str]:
    """索引时文本是 ``f"{title}\\n{content}"``（见 cli.cmd_index），按此还原。

    与 ``agent.retriever.LocalRetriever`` 的切法保持一致，
    否则同一条知识在两个后端下显示出来的标题不一样。
    """
    return text.split("\n", 1)[0][:60], text


@dataclass
class SearchService:
    """在线检索。

    ``stats`` 是懒加载的：第一次检索时扫一遍 Milvus 建 IDF 表。
    """

    store: MilvusStore
    provider: Any
    """向量化后端，:func:`smartmall_pipeline.rag.build_provider` 产出。"""
    config: RetrievalConfig = field(default_factory=RetrievalConfig)
    stats: LexicalStats | None = None

    # ------------------------------------------------------------ IDF 表

    def refresh_stats(self) -> int:
        """重扫语料，重建 IDF 表。返回文档数。

        **语料必须取自 Milvus 自己，不能从 MySQL 建。** IDF 要描述的是
        「实际能被检索到的那批文档」；索引落后于 MySQL 时（``pending`` /
        ``stale`` 还没跑完），从 MySQL 建出来的 IDF 描述的是一个并不存在
        的语料，覆盖率就会系统性偏低——而这个偏差没有任何报错，只表现为
        「莫名其妙老是转人工」。
        """
        # 状态列表必须和 build_filter_expr 用同一份定义。少收一个状态，
        # IDF 表的 N 就小一截，**所有**覆盖率都会跟着偏——而检索本身照常
        # 工作，只是闸门的松紧悄悄变了。（这里一开始就写错成只收
        # approved，是集成测试量出 0.386 vs 0.411 才发现的。）
        statuses = ", ".join(f'"{s}"' for s in SEARCHABLE_REVIEW_STATUS)
        texts: list[str] = []
        offset = 0
        while True:
            try:
                rows = self.store.client.query(
                    collection_name=self.store.cfg.collection,
                    filter=f"{F_REVIEW} in [{statuses}]",
                    output_fields=[F_TEXT],
                    limit=_SCAN_PAGE,
                    offset=offset,
                )
            except Exception as exc:  # noqa: BLE001
                raise SearchError(f"扫描语料建 IDF 表失败：{exc}") from exc
            if not rows:
                break
            texts.extend(r.get(F_TEXT, "") for r in rows)
            if len(rows) < _SCAN_PAGE:
                break
            offset += _SCAN_PAGE

        self.stats = LexicalStats.build(texts)
        log.info("IDF 表已重建：%d 篇文档，%d 个词", self.stats.total, len(self.stats.df))
        return self.stats.total

    def _ensure_stats(self) -> LexicalStats:
        if self.stats is None:
            self.refresh_stats()
        assert self.stats is not None
        return self.stats

    # ------------------------------------------------------------ 检索

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        product_ids: Sequence[int] | None = None,
        category_id: int | None = None,
        biz_types: Sequence[str] | None = None,
    ) -> list[ScoredHit]:
        """两路召回 → 本地 RRF 融合 → 按 item 去重 → 补判据。

        **为什么不用 Milvus 的 hybrid_search。** 那条路把融合做在服务端，
        回来只有一个 RRF 分。Agent 的 ``state.max_score`` 取的是余弦
        相似度，两个阈值（0.30 / 0.55）也是照余弦量出来的；RRF 分最大
        只有 ``2/(k+1)≈0.033``，喂进去的结果是**每条查询都转人工**。
        见 :meth:`MilvusStore.recall`。
        """
        if not query.strip():
            return []

        stats = self._ensure_stats()

        try:
            from smartmall_pipeline.rag.store import embed_query

            qvec = embed_query(self.provider, query)
        except Exception as exc:  # noqa: BLE001
            raise SearchError(f"查询向量化失败：{exc}") from exc

        expr = build_filter_expr(
            kb_version=self.config.kb_version,
            product_ids=product_ids,
            category_id=category_id,
            biz_types=biz_types,
        )

        try:
            dense, sparse = self.store.recall(
                query_text=query,
                query_dense=qvec,
                filter_expr=expr,
                recall_top_k=self.config.recall_top_k,
            )
        except Exception as exc:  # noqa: BLE001
            raise SearchError(f"Milvus 检索失败：{exc}") from exc

        fused = reciprocal_rank_fusion(
            dense, sparse, k=self.config.rrf_k, top_k=self.config.fuse_top_k
        )
        ordered = dedup_by_item(fused)[:top_k]

        dense_by_id = {h.chunk_id: h.score for h in dense}
        sparse_by_id = {h.chunk_id: h.score for h in sparse}

        out: list[ScoredHit] = []
        for h in ordered:
            title, content = _split_title(h.text)
            out.append(ScoredHit(
                item_id=h.item_id,
                chunk_id=h.chunk_id,
                title=title,
                content=content,
                score=h.score,
                dense_score=dense_by_id.get(h.chunk_id, 0.0),
                bm25_score=sparse_by_id.get(h.chunk_id, 0.0),
                # 覆盖率对命中自身的文本算，与它是从哪一路召回的无关——
                # 纯 dense 召回的条目不在 sparse 列表里，但照样要知道
                # 它有没有词汇支撑（LocalVectorStore 也是这么做的）
                lexical_overlap=stats.coverage(query, h.text),
                biz_type=h.biz_type,
                modality=h.modality,
                asset_ids=h.asset_ids,
                product_ids=h.product_ids,
            ))
        return out


def build_service(
    *,
    uri: str | None = None,
    collection: str | None = None,
    analyzer: str | None = None,
    embedding: str | None = None,
    kb_version: str = "",
) -> SearchService:
    """按配置装一个 :class:`SearchService`。缺省值取环境变量。"""
    import os

    from smartmall_pipeline.rag import build_provider

    cfg = MilvusConfig(
        uri=uri or os.environ.get("KB_MILVUS_URI", "http://localhost:19530"),
        collection=collection or os.environ.get("MILVUS_COLLECTION", "kb_chunk"),
        analyzer=analyzer or os.environ.get("MILVUS_ANALYZER", "jieba"),
    )
    return SearchService(
        store=MilvusStore(cfg),
        provider=build_provider(embedding or os.environ.get("EMBEDDING_BACKEND", "dashscope")),
        config=RetrievalConfig(kb_version=kb_version),
    )
