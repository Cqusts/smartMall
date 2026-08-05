"""Milvus 2.5 存储层。

用 Milvus 2.5 的**原生 BM25**：把 text 字段声明 ``enable_analyzer=True``，
挂一个 ``FunctionType.BM25`` 的 Function 自动产出稀疏向量。
这样 dense + sparse 混合检索一套搞定，**省掉独立部署 Elasticsearch**
（省约 2G 内存和一套运维）。

API 依据 Milvus 2.5.x 官方文档：

* schema: ``add_field(..., enable_analyzer=True)`` + ``SPARSE_FLOAT_VECTOR``
* function: ``Function(name, input_field_names, output_field_names, function_type=FunctionType.BM25)``
* 稀疏索引: ``SPARSE_INVERTED_INDEX`` + ``metric_type="BM25"``
* 混合检索: ``AnnSearchRequest`` × 2 → ``client.hybrid_search(reqs=..., ranker=...)``

排序器 API 在 2.5.x 内有两种形态（旧版 ``RRFRanker(k)``，新版
``Function(function_type=FunctionType.RERANK)``），:func:`_build_ranker` 做了兼容。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from .retrieval import Hit

log = logging.getLogger(__name__)

COLLECTION = "kb_chunk"
DENSE_DIM = 1024  # bge-m3

# 字段名集中定义，避免散落在字符串里写错
F_CHUNK_ID = "chunk_id"
F_ITEM_ID = "item_id"
F_CHUNK_SEQ = "chunk_seq"
F_TEXT = "text"
F_DENSE = "dense_vec"
F_SPARSE = "sparse_vec"
F_BIZ_TYPE = "biz_type"
F_MODALITY = "modality"
F_CATEGORY = "category_id"
F_PRODUCT_IDS = "product_ids"
F_ASSET_IDS = "asset_ids"
F_REVIEW = "review_status"
F_VALID_TO = "valid_to_ts"
F_QUALITY = "quality_score"
F_KB_VERSION = "kb_version"

OUTPUT_FIELDS = [
    F_ITEM_ID, F_CHUNK_SEQ, F_TEXT, F_BIZ_TYPE, F_MODALITY,
    F_ASSET_IDS, F_PRODUCT_IDS, F_QUALITY,
]


@dataclass
class MilvusConfig:
    uri: str = "http://localhost:19530"
    collection: str = COLLECTION
    dense_dim: int = DENSE_DIM
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef: int = 64


def build_schema(client: Any, cfg: MilvusConfig):
    """构建 collection schema。

    ``image_vec`` 是升级预留位：当前多模态走「VLM 转述 + 文本索引」，
    不参与检索；将来若要加图像向量召回作为补充通道，字段已在（见 docs/04）。
    """
    from pymilvus import DataType, Function, FunctionType

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)

    schema.add_field(F_CHUNK_ID, DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(F_ITEM_ID, DataType.INT64)
    schema.add_field(F_CHUNK_SEQ, DataType.INT16)

    # enable_analyzer=True 是 BM25 的前提：Milvus 需要对该字段做分词
    schema.add_field(
        F_TEXT, DataType.VARCHAR, max_length=4096,
        enable_analyzer=True, analyzer_params={"type": "chinese"},
    )

    schema.add_field(F_DENSE, DataType.FLOAT_VECTOR, dim=cfg.dense_dim)
    # 稀疏向量由 BM25 Function 自动生成，写入时不要手动提供
    schema.add_field(F_SPARSE, DataType.SPARSE_FLOAT_VECTOR)

    schema.add_field(F_BIZ_TYPE, DataType.VARCHAR, max_length=32)
    schema.add_field(F_MODALITY, DataType.VARCHAR, max_length=16)
    schema.add_field(F_CATEGORY, DataType.INT64)
    schema.add_field(
        F_PRODUCT_IDS, DataType.ARRAY, element_type=DataType.INT64, max_capacity=32
    )
    schema.add_field(
        F_ASSET_IDS, DataType.ARRAY, element_type=DataType.INT64, max_capacity=16
    )
    schema.add_field(F_REVIEW, DataType.VARCHAR, max_length=16)
    schema.add_field(F_VALID_TO, DataType.INT64)
    schema.add_field(F_QUALITY, DataType.FLOAT)
    schema.add_field(F_KB_VERSION, DataType.VARCHAR, max_length=32)

    schema.add_function(
        Function(
            name="text_bm25",
            input_field_names=[F_TEXT],
            output_field_names=[F_SPARSE],
            function_type=FunctionType.BM25,
        )
    )
    return schema


def build_index_params(client: Any, cfg: MilvusConfig):
    index_params = client.prepare_index_params()

    # 十万级数据量下 HNSW 优于 IVF。bge-m3 输出已归一化，用内积等价于余弦
    index_params.add_index(
        field_name=F_DENSE,
        index_type="HNSW",
        metric_type="IP",
        params={"M": cfg.hnsw_m, "efConstruction": cfg.hnsw_ef_construction},
    )
    index_params.add_index(
        field_name=F_SPARSE,
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={
            "inverted_index_algo": "DAAT_MAXSCORE",
            "bm25_k1": cfg.bm25_k1,
            "bm25_b": cfg.bm25_b,
        },
    )
    # 标量索引加速过滤——审核状态与时效是每次查询都要过的条件
    for field in (F_CATEGORY, F_REVIEW, F_VALID_TO, F_KB_VERSION, F_BIZ_TYPE):
        index_params.add_index(field_name=field, index_type="INVERTED")

    return index_params


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


class MilvusStore:
    """Milvus 客户端封装。"""

    def __init__(self, config: MilvusConfig | None = None) -> None:
        self.cfg = config or MilvusConfig()
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from pymilvus import MilvusClient

            self._client = MilvusClient(uri=self.cfg.uri)
        return self._client

    # ---------------------------------------------------------------- DDL

    def ensure_collection(self, *, drop_existing: bool = False) -> None:
        if drop_existing and self.client.has_collection(self.cfg.collection):
            log.warning("删除已有 collection %s", self.cfg.collection)
            self.client.drop_collection(self.cfg.collection)

        if self.client.has_collection(self.cfg.collection):
            return

        self.client.create_collection(
            collection_name=self.cfg.collection,
            schema=build_schema(self.client, self.cfg),
            index_params=build_index_params(self.client, self.cfg),
        )
        log.info("已创建 collection %s", self.cfg.collection)

    # ---------------------------------------------------------------- 写入

    def upsert_chunks(self, rows: Sequence[dict[str, Any]]) -> int:
        """写入切片。

        注意：**不要**提供 sparse_vec 字段——它由 BM25 Function 从 text
        自动生成，手动提供会报错。
        """
        if not rows:
            return 0
        for row in rows:
            row.pop(F_SPARSE, None)
        res = self.client.upsert(collection_name=self.cfg.collection, data=list(rows))
        return int(res.get("upsert_count", len(rows)))

    def delete_by_item(self, item_ids: Sequence[int]) -> None:
        """按 knowledge_item 删除其全部切片。知识下线或重新切分时用。"""
        if not item_ids:
            return
        ids = ", ".join(str(int(i)) for i in item_ids)
        self.client.delete(
            collection_name=self.cfg.collection, filter=f"{F_ITEM_ID} in [{ids}]"
        )

    # ---------------------------------------------------------------- 检索

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


def _to_hits(res: Any) -> list[Hit]:
    """把 pymilvus 的返回结构转成领域对象。"""
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
                source="fused",
            )
        )
    return hits
