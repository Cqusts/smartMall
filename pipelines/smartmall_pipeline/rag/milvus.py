"""Milvus 2.5 索引层：建表与写入。

**为什么在 pipeline 包里。** 索引构建是数据中台的职责（对应
dag_kb_incremental_index），在线检索才是 ai-rag 的职责——``LocalVectorStore``
就在隔壁，这里保持同一分工。这样 ``cli index`` 能直接写 Milvus，
不需要让 pipeline 反过来依赖 apps/（本仓库的依赖方向一直是 apps → pipeline）。

检索侧（``recall`` / ``hybrid_search`` / ``Hit`` 映射）在
``apps/python/ai-rag/app/milvus_store.py``，它继承这里的 :class:`MilvusIndex`。
**schema 只有这一份**——两处各写一份的话，写进去的字段和查出来的字段
会慢慢对不上，而且不报错。

用 Milvus 2.5 的**原生 BM25**：把 text 字段声明 ``enable_analyzer=True``，
挂一个 ``FunctionType.BM25`` 的 Function 自动产出稀疏向量。
这样 dense + sparse 混合检索一套搞定，**省掉独立部署 Elasticsearch**
（省约 2G 内存和一套运维）。

API 依据 Milvus 2.5.x 官方文档：

* schema: ``add_field(..., enable_analyzer=True)`` + ``SPARSE_FLOAT_VECTOR``
* function: ``Function(name, input_field_names, output_field_names, function_type=FunctionType.BM25)``
* 稀疏索引: ``SPARSE_INVERTED_INDEX`` + ``metric_type="BM25"``

**两种部署形态，同一份代码。** ``uri`` 给 ``http://host:19530`` 是服务端；
给一个本地路径就是 Milvus Lite——纯 Python 包，Windows 上 ``pip install``
就能跑，不需要 Docker 也不需要 Linux。差异只有一处：``analyzer`` 的取值
（见 :class:`MilvusConfig`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from .store import chunk_pk

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
    """服务端地址；**换成一个本地文件路径就是 Milvus Lite**（嵌入式，
    纯 Python 包，Windows 也能跑，不需要 Docker 或 Linux）。
    业务代码一行不用改，这正是 milvus-lite 的用法。"""

    collection: str = COLLECTION
    dense_dim: int = DENSE_DIM
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef: int = 64

    analyzer: str = "jieba"
    """中文分词器。**两种部署形态的取值不一样，必须显式配。**

    实测（milvus-lite 3.2.1）：Lite 只认 ``standard`` / ``jieba``，
    传 ``chinese`` 直接报 ``unknown tokenizer type``。而服务端的内置
    analyzer 是 ``standard`` / ``english`` / ``chinese`` / ``arabic`` /
    ``thai``，**没有** ``jieba``。所以写死任何一个，另一边就起不来。

    默认给 ``jieba`` 是因为本地开发跑的是 Lite；部署到服务端时改成
    ``chinese``。注意这不只是名字之差——两者分词结果不同，BM25 的
    召回也会有差异，调阈值时别拿两边的数混着看。
    """


def build_schema(client: Any, cfg: MilvusConfig):
    """构建 collection schema。

    ``image_vec`` 是升级预留位：当前多模态走「VLM 转述 + 文本索引」，
    不参与检索；将来若要加图像向量召回作为补充通道，字段已在（见 docs/04）。
    """
    from pymilvus import DataType, Function, FunctionType

    # auto_id 必须是 False。主键自动生成就没法定位要覆盖哪一行，
    # client.upsert() 会直接报 "Insert missed an field chunk_id"——
    # 而"重新索引一条知识"正是 upsert 的主要用途。主键改用
    # chunk_pk(item_id, chunk_seq) 合成，与本地实现同一个规则。
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)

    schema.add_field(F_CHUNK_ID, DataType.INT64, is_primary=True, auto_id=False)
    schema.add_field(F_ITEM_ID, DataType.INT64)
    schema.add_field(F_CHUNK_SEQ, DataType.INT16)

    # enable_analyzer=True 是 BM25 的前提：Milvus 需要对该字段做分词。
    # analyzer 的取值 Lite 与服务端不同，见 MilvusConfig.analyzer
    schema.add_field(
        F_TEXT, DataType.VARCHAR, max_length=4096,
        enable_analyzer=True, analyzer_params={"type": cfg.analyzer},
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


class MilvusIndex:
    """Milvus 客户端 + 建表 + 写入。检索侧见 ai-rag 的 ``MilvusStore``。"""

    def __init__(self, config: MilvusConfig | None = None) -> None:
        self.cfg = config or MilvusConfig()
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise RuntimeError(
                    "需要 pymilvus：\n"
                    "  pip install pymilvus 'milvus-lite[chinese]'\n"
                    "本地开发把 uri 指向一个文件路径即可（Milvus Lite，"
                    "Windows 也能跑，不需要 Docker）"
                ) from exc

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

        ``chunk_id`` 缺省时按 :func:`chunk_pk` 自动合成。让调用方自己算
        主键迟早会有人算错，而算错的表现是静默覆盖别人的切片。
        """
        if not rows:
            return 0
        data = []
        for row in rows:
            row = dict(row)
            row.pop(F_SPARSE, None)
            if F_CHUNK_ID not in row:
                row[F_CHUNK_ID] = chunk_pk(int(row[F_ITEM_ID]), int(row[F_CHUNK_SEQ]))
            data.append(row)
        res = self.client.upsert(collection_name=self.cfg.collection, data=data)
        return int(res.get("upsert_count", len(data)))

    def delete_by_item(self, item_ids: Sequence[int]) -> None:
        """按 knowledge_item 删除其全部切片。知识下线或重新切分时用。"""
        if not item_ids:
            return
        ids = ", ".join(str(int(i)) for i in item_ids)
        self.client.delete(
            collection_name=self.cfg.collection, filter=f"{F_ITEM_ID} in [{ids}]"
        )

    def count(self) -> int:
        """collection 里有多少条。建完索引后核对用。"""
        if not self.client.has_collection(self.cfg.collection):
            return 0
        rows = self.client.query(
            collection_name=self.cfg.collection,
            filter="",
            output_fields=["count(*)"],
        )
        return int(rows[0]["count(*)"]) if rows else 0
