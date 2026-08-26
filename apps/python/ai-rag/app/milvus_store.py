"""Milvus 检索层。

建表与写入在 :mod:`smartmall_pipeline.rag.milvus`（索引构建是数据中台的
职责）；这里只加在线检索——``recall`` / ``hybrid_search`` 与 ``Hit`` 映射。
**schema 不在这里，不要在这里再写一份。**

排序器 API 在 pymilvus 2.5.x 内有两种形态（旧版 ``RRFRanker(k)``，新版
``Function(function_type=FunctionType.RERANK)``），:func:`_build_ranker` 做了兼容。

**它给不出的东西：词汇覆盖率与分路分数。** ``hybrid_search`` 只返回融合
后的 RRF 分。而 Agent 侧 ``state.max_score`` 用的是余弦相似度、
``graph.has_lexical_support`` 用的是 ``lexical_overlap``，两个都拿不到。
这个缺口由 :mod:`app.search` 补上（分路召回 + 本地融合 +
:class:`~smartmall_pipeline.rag.LexicalStats`）——**不补的话，接 Milvus
等于把那两道闸门拆了**，且不会有任何报错。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from smartmall_pipeline.rag.milvus import (
    COLLECTION,
    DENSE_DIM,
    F_ASSET_IDS,
    F_BIZ_TYPE,
    F_CATEGORY,
    F_CHUNK_ID,
    F_CHUNK_SEQ,
    F_DENSE,
    F_ITEM_ID,
    F_KB_VERSION,
    F_MODALITY,
    F_PRODUCT_IDS,
    F_QUALITY,
    F_REVIEW,
    F_SPARSE,
    F_TEXT,
    F_VALID_TO,
    OUTPUT_FIELDS,
    MilvusConfig,
    MilvusIndex,
    build_index_params,
    build_schema,
)

from .retrieval import Hit

log = logging.getLogger(__name__)

__all__ = [
    "COLLECTION", "DENSE_DIM", "MilvusConfig", "MilvusIndex", "MilvusStore",
    "build_schema", "build_index_params",
    "F_CHUNK_ID", "F_ITEM_ID", "F_CHUNK_SEQ", "F_TEXT", "F_DENSE", "F_SPARSE",
    "F_BIZ_TYPE", "F_MODALITY", "F_CATEGORY", "F_PRODUCT_IDS", "F_ASSET_IDS",
    "F_REVIEW", "F_VALID_TO", "F_QUALITY", "F_KB_VERSION", "OUTPUT_FIELDS",
]


def _build_ranker(k: int):
    """构造 RRF 排序器，兼容 pymilvus 新旧两种 API。"""
    try:
        from pymilvus import Function, FunctionType

        if hasattr(FunctionType, "RERANK"):
            return Function(
                name="rrf",
                input_field_names=[],  # RERANK 类型必须为空
                function_type=FunctionType.RERANK,
                params={"reranker": "rrf", "k": k},
            )
    except (ImportError, AttributeError):
        pass

    from pymilvus import RRFRanker  # type: ignore

    return RRFRanker(k)


class MilvusStore(MilvusIndex):
    """Milvus 客户端封装：继承索引层，补上检索。"""

    def recall(
        self,
        *,
        query_text: str,
        query_dense: Sequence[float],
        filter_expr: str,
        recall_top_k: int = 50,
    ) -> tuple[list[Hit], list[Hit]]:
        """dense 与 BM25 两路**分别**召回，返回 ``(dense, sparse)``，不融合。

        **为什么不用 :meth:`hybrid_search`。** 那个方法把融合交给 Milvus，
        回来只有一个 RRF 分，拿不到分路分数。而 Agent 侧
        ``state.max_score`` 取的是 ``dense_score``（余弦相似度），
        ``handover_below=0.30`` / ``clarify_below=0.55`` 两个阈值都是照
        余弦量出来的。RRF 分的最大值恒为 ``2/(k+1)≈0.033``——把它填进
        ``dense_score``，**每一条查询都会低于 0.30，结果是全部转人工**，
        而且不会有任何报错。

        融合交给 :func:`~app.retrieval.reciprocal_rank_fusion`，
        与 ``LocalVectorStore`` 走的是同一套逻辑，两个后端的排序才可比。

        过滤条件同时下推到两路——只在一路加过滤，融合结果里就会
        混进不该出现的条目。
        """
        dense_rows = self.client.search(
            collection_name=self.cfg.collection,
            data=[list(query_dense)],
            anns_field=F_DENSE,
            search_params={"params": {"ef": self.cfg.hnsw_ef}},
            limit=recall_top_k,
            filter=filter_expr,
            output_fields=OUTPUT_FIELDS,
        )
        sparse_rows = self.client.search(
            collection_name=self.cfg.collection,
            data=[query_text],
            anns_field=F_SPARSE,
            search_params={"params": {"drop_ratio_search": 0.2}},
            limit=recall_top_k,
            filter=filter_expr,
            output_fields=OUTPUT_FIELDS,
        )
        return _to_hits(dense_rows, "dense"), _to_hits(sparse_rows, "sparse")

    def hybrid_search(
        self,
        *,
        query_text: str,
        query_dense: Sequence[float],
        filter_expr: str,
        recall_top_k: int = 50,
        fuse_top_k: int = 30,
        rrf_k: int = 60,
    ) -> list[Hit]:
        """dense + BM25 双路召回，由 Milvus 侧做 RRF 融合。

        ⚠️ **返回的 ``score`` 是 RRF 分，不是相似度**，且分路分数全部丢失。
        Agent 的拒答阈值是照余弦量的，喂这个分会让所有查询都转人工——
        在线检索请走 :meth:`recall` + 本地融合。这个方法留给不需要分路
        分数的场景（比如纯粹的召回质量对比）。

        过滤条件同时下推到两路——只在一路加过滤会导致融合结果里
        混入不该出现的条目。
        """
        from pymilvus import AnnSearchRequest

        dense_req = AnnSearchRequest(
            data=[list(query_dense)],
            anns_field=F_DENSE,
            param={"ef": self.cfg.hnsw_ef},
            limit=recall_top_k,
            expr=filter_expr,
        )
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field=F_SPARSE,
            param={"drop_ratio_search": 0.2},
            limit=recall_top_k,
            expr=filter_expr,
        )

        res = self.client.hybrid_search(
            collection_name=self.cfg.collection,
            reqs=[dense_req, sparse_req],
            ranker=_build_ranker(rrf_k),
            limit=fuse_top_k,
            output_fields=OUTPUT_FIELDS,
        )
        return _to_hits(res)


def _to_hits(res: Any, source: str = "fused") -> list[Hit]:
    """把 pymilvus 的返回结构转成领域对象。

    ``source`` 标明这批结果来自哪一路（dense / sparse / fused），
    Trace 里靠它分析两路各贡献了什么。
    """
    if not res:
        return []
    rows = res[0] if isinstance(res, list) else res
    hits: list[Hit] = []
    for row in rows:
        entity = row.get("entity", row) if isinstance(row, dict) else row
        hits.append(
            Hit(
                chunk_id=int(row.get("id") or row.get(F_CHUNK_ID) or 0),
                item_id=int(entity.get(F_ITEM_ID, 0)),
                text=entity.get(F_TEXT, ""),
                score=float(row.get("distance", row.get("score", 0.0))),
                biz_type=entity.get(F_BIZ_TYPE, "qa"),
                modality=entity.get(F_MODALITY, "text"),
                asset_ids=tuple(entity.get(F_ASSET_IDS) or ()),
                product_ids=tuple(entity.get(F_PRODUCT_IDS) or ()),
                source=source,
            )
        )
    return hits
