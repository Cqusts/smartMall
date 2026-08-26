"""轻量向量存储：MySQL 存向量，内存做检索。

**定位**：无 Milvus 的开发环境替代品，不是 Milvus 的竞品。
与 :class:`~app.milvus_store.MilvusStore` 保持同样的检索语义
（同样的硬性过滤、同样的 RRF 融合、同样的 BM25 参数），
所以从这里切到 Milvus 只需换配置，检索行为不变。

**适用范围**：几万条以内。向量全量载入内存做暴力检索——
728 条知识约 3MB，5 万条约 200MB，都在可接受范围。
再大就该上 Milvus 了，届时 HNSW 索引的价值才体现出来。

向量存 MySQL 的 BLOB：float32 紧凑二进制，不用 JSON——
1024 维 JSON 化约 20KB，二进制只要 4KB，差 5 倍。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Sequence

from .bm25 import Bm25Index
from .embedding import DIM, EmbeddingProvider


def pack_vector(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


SEARCHABLE_REVIEW_STATUS = ("approved", "revised")
"""哪些审核状态算「可检索」。

``revised`` 是人工改写过的，恰恰是质量最高的那批，必须能被检索到；
``pending`` / ``rejected`` 绝不进检索结果。

**两个后端共用这一份定义。** 之前 ``LocalVectorStore.load()`` 收
``('approved','revised')`` 而 ``build_filter_expr`` 只收 ``approved``——
同一条人工改写过的知识，本地能查到、换到 Milvus 就查不到，且不报错。
"""

CHUNK_ID_STRIDE = 1000
"""一个 knowledge_item 最多允许的切片数。见 :func:`chunk_pk`。"""


def chunk_pk(item_id: int, chunk_seq: int) -> int:
    """由 ``(item_id, chunk_seq)`` 合成切片主键。

    **两个后端必须用同一个规则。** Milvus 那边 ``chunk_id`` 是主键，
    upsert 靠它定位要覆盖哪一行；本地实现靠它与 Milvus 的结果对齐。
    各写一份的话，同一条知识在两个后端会有两个 id，而不会有任何报错。

    ``chunk_seq`` 越界必须报错而不是回绕：``item_id=7, chunk_seq=1000``
    与 ``item_id=8, chunk_seq=0`` 会算出同一个主键，后写的那条**静默
    覆盖**前一条——检索时只是少了一条知识，查不出是哪一步弄丢的。
    """
    if not 0 <= chunk_seq < CHUNK_ID_STRIDE:
        raise ValueError(
            f"chunk_seq={chunk_seq} 超出 [0, {CHUNK_ID_STRIDE}) —— "
            f"再切下去主键会和 item_id={item_id + 1} 的切片撞上。"
            f"要支持更多切片得同时调大两个后端的 CHUNK_ID_STRIDE。"
        )
    return item_id * CHUNK_ID_STRIDE + chunk_seq


@dataclass
class LocalHit:
    """一条召回结果。字段与 ai-rag 的 Hit 对齐。"""

    item_id: int
    chunk_seq: int
    text: str
    score: float
    """RRF 融合分。**只反映排名，不反映相关度**——最大值恒为
    ``2/(rrf_k+1)``（两路都排第一），与查询是否真的匹配无关。
    因此它不能用来判断「要不要回答」。"""

    dense_score: float = 0.0
    """余弦相似度（向量已归一化）。这才是可用作拒答判据的量。"""
    bm25_score: float = 0.0
    """BM25 分。注意 **> 0 几乎不说明问题**：bigram 分词下一个
    ``支持``就能让毫不相干的查询拿到分。判"有没有真的匹配上"看
    ``lexical_overlap``。"""
    lexical_overlap: float = 0.0
    """查询里**有区分度的词**匹配上了多少，0~1，IDF 加权（见
    :meth:`Bm25Index.coverage`）。这才是"知识库里到底有没有"的判据。"""

    biz_type: str = "qa"
    modality: str = "text"
    knowledge_type: str = "other"
    category_id: int | None = None
    product_ids: tuple[int, ...] = ()
    asset_ids: tuple[int, ...] = ()
    source: str = "dense"

    @property
    def chunk_id(self) -> int:
        """与 Milvus 的 chunk_id 对应。见 :func:`chunk_pk`。"""
        return chunk_pk(self.item_id, self.chunk_seq)


@dataclass
class _Row:
    item_id: int
    chunk_seq: int
    text: str
    vector: list[float]
    biz_type: str
    modality: str
    knowledge_type: str
    category_id: int | None
    product_ids: tuple[int, ...]
    asset_ids: tuple[int, ...]
    valid_to_ts: int
    review_status: str


@dataclass
class LocalVectorStore:
    """MySQL + 内存的向量存储。"""

    engine: object
    rows: list[_Row] = field(default_factory=list)
    bm25: Bm25Index | None = None
    provider_name: str = ""
    loaded_at: float = 0.0

    # ---------------------------------------------------------------- 写入

    def upsert(
        self,
        items: Sequence[tuple[int, int, str, list[float]]],
        provider_name: str,
    ) -> int:
        """写入 ``(item_id, chunk_seq, text, vector)``。"""
        from sqlalchemy import text as sql

        if not items:
            return 0

        stmt = sql(
            "REPLACE INTO kb_embedding "
            "(item_id, chunk_seq, chunk_text, vector, dim, provider) "
            "VALUES (:item_id, :chunk_seq, :chunk_text, :vector, :dim, :provider)"
        )
        rows = [
            {
                "item_id": item_id,
                "chunk_seq": seq,
                "chunk_text": text_,
                "vector": pack_vector(vec),
                "dim": len(vec),
                "provider": provider_name,
            }
            for item_id, seq, text_, vec in items
        ]
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(stmt, rows)
        return len(rows)

    def delete_by_item(self, item_ids: Sequence[int]) -> None:
        from sqlalchemy import text as sql

        if not item_ids:
            return
        ids = ", ".join(str(int(i)) for i in item_ids)
        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(sql(f"DELETE FROM kb_embedding WHERE item_id IN ({ids})"))

    def providers_in_index(self) -> dict[str, int]:
        """索引里各后端各有多少行。

        换后端前必须先问这个问题。混用检测在 :meth:`load` 里才报错，
        那时候已经晚了——用户是在启动 Agent 时被拦住的，
        而制造混用的是上一步的 index。
        """
        from sqlalchemy import text as sql

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            return {
                r[0]: r[1]
                for r in conn.execute(sql(
                    "SELECT provider, COUNT(*) FROM kb_embedding GROUP BY provider"
                ))
            }

    def delete_other_providers(self, provider_name: str) -> int:
        """删掉非当前后端的向量。

        它们本来就不可用——不同后端的向量不可比。留着只会让下一次
        加载直接报"混用"，而不是留下什么可回退的东西。
        """
        from sqlalchemy import text as sql

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            return conn.execute(
                sql("DELETE FROM kb_embedding WHERE provider <> :p"),
                {"p": provider_name},
            ).rowcount or 0

    # ---------------------------------------------------------------- 加载

    def load(self) -> int:
        """把向量与元数据载入内存。

        只载入**已审核**的条目——未审核的知识绝不进入检索结果，
        这条硬性规则在数据加载阶段就生效，调用方无法绕过。
        """
        from sqlalchemy import text as sql

        stmt = sql(
            "SELECT e.item_id, e.chunk_seq, e.chunk_text, e.vector, e.provider, "
            "       k.biz_type, k.modality, k.knowledge_type, k.category_id, "
            "       k.product_ids, k.asset_ids, k.valid_to, k.review_status "
            "FROM kb_embedding e "
            "JOIN knowledge_item k ON k.id = e.item_id "
            "WHERE k.deleted = 0 AND k.review_status IN ("
            + ", ".join(f"'{s}'" for s in SEARCHABLE_REVIEW_STATUS)
            + ")"
        )
        with self.engine.connect() as conn:  # type: ignore[attr-defined]
            raw = conn.execute(stmt).mappings().fetchall()

        import json

        rows: list[_Row] = []
        providers: set[str] = set()
        for r in raw:
            providers.add(r["provider"] or "")
            valid_to = r["valid_to"]
            rows.append(_Row(
                item_id=r["item_id"],
                chunk_seq=r["chunk_seq"],
                text=r["chunk_text"],
                vector=unpack_vector(r["vector"]),
                biz_type=r["biz_type"],
                modality=r["modality"],
                knowledge_type=r["knowledge_type"] or "other",
                category_id=r["category_id"],
                product_ids=tuple(json.loads(r["product_ids"] or "[]")),
                asset_ids=tuple(json.loads(r["asset_ids"] or "[]")),
                valid_to_ts=int(valid_to.timestamp()) if valid_to else 0,
                review_status=r["review_status"],
            ))

        self.rows = rows
        self.provider_name = next(iter(providers)) if len(providers) == 1 else ""
        self.bm25 = Bm25Index.build([(i, r.text) for i, r in enumerate(rows)])
        self.loaded_at = time.time()

        if len(providers) > 1:
            raise RuntimeError(
                f"索引里混用了多个向量化后端 {sorted(providers)}——"
                "不同后端的向量不可比，相似度会失去意义。"
                "请先 `index --rebuild` 用同一个后端重建。"
            )
        return len(rows)

    # ---------------------------------------------------------------- 检索

    def _passes_filter(
        self,
        row: _Row,
        *,
        now_ts: int,
        product_ids: Sequence[int] | None,
        category_id: int | None,
        biz_types: Sequence[str] | None,
    ) -> bool:
        # 时效：过期知识自动排除，防止客服播报已结束的活动
        if row.valid_to_ts and row.valid_to_ts <= now_ts:
            return False
        if product_ids and not set(row.product_ids) & set(product_ids):
            return False
        if category_id is not None and row.category_id != category_id:
            return False
        if biz_types and row.biz_type not in biz_types:
            return False
        return True

    def search(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        top_k: int = 5,
        recall_k: int = 50,
        rrf_k: int = 60,
        product_ids: Sequence[int] | None = None,
        category_id: int | None = None,
        biz_types: Sequence[str] | None = None,
        now_ts: int | None = None,
    ) -> list[LocalHit]:
        """dense + BM25 双路召回，RRF 融合。

        与 Milvus 路径的差异只在召回实现（暴力 vs HNSW/倒排），
        过滤条件与融合算法完全一致。
        """
        if not self.rows:
            return []

        now_ts = now_ts if now_ts is not None else int(time.time())
        allowed = [
            i for i, r in enumerate(self.rows)
            if self._passes_filter(
                r, now_ts=now_ts, product_ids=product_ids,
                category_id=category_id, biz_types=biz_types,
            )
        ]
        if not allowed:
            return []

        # dense
        dense = sorted(
            ((i, dot(query_vector, self.rows[i].vector)) for i in allowed),
            key=lambda kv: -kv[1],
        )[:recall_k]

        # sparse：BM25 结果里剔除被过滤掉的
        allowed_set = set(allowed)
        sparse = [
            (i, s) for i, s in (self.bm25.search(query, top_k=recall_k * 2) if self.bm25 else [])
            if i in allowed_set
        ][:recall_k]

        # RRF：只用排名不用分数，免疫两路量纲差异。
        # 但原始分要留着——RRF 分只反映排名，判断「要不要回答」得看相似度。
        fused: dict[int, float] = {}
        for ranking in (dense, sparse):
            for rank, (idx, _) in enumerate(ranking, start=1):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + rank)

        dense_scores = dict(dense)
        sparse_scores = dict(sparse)
        # 词汇覆盖率对全库算，与 BM25 的 top_k 截断无关：纯 dense 召回的
        # 条目不在 sparse 列表里，但照样需要知道它有没有词汇支撑
        overlap = self.bm25.coverage(query) if self.bm25 else {}
        order = sorted(fused.items(), key=lambda kv: -kv[1])

        # 同一 knowledge_item 的多个切片只留最高分，避免长文档占满结果
        seen: dict[int, LocalHit] = {}
        for idx, score in order:
            r = self.rows[idx]
            if r.item_id in seen:
                continue
            seen[r.item_id] = LocalHit(
                item_id=r.item_id, chunk_seq=r.chunk_seq, text=r.text, score=score,
                dense_score=dense_scores.get(idx, 0.0),
                bm25_score=sparse_scores.get(idx, 0.0),
                lexical_overlap=overlap.get(idx, 0.0),
                biz_type=r.biz_type, modality=r.modality,
                knowledge_type=r.knowledge_type, category_id=r.category_id,
                product_ids=r.product_ids, asset_ids=r.asset_ids, source="fused",
            )
            if len(seen) >= top_k:
                break

        return list(seen.values())


def embed_query(provider: EmbeddingProvider, query: str) -> list[float]:
    """把查询转成向量。

    优先用 provider 的 :meth:`~...EmbeddingProvider.embed_query`——
    Qwen3-Embedding 这类非对称模型对"待检索的问题"和"库里的文档"
    产出不同的向量，用错一侧会白白损失召回。
    不支持的实现（如 bge-m3 dense）退回 ``embed``，行为与从前一致。
    """
    fn = getattr(provider, "embed_query", None)
    vec = [fn(query)] if callable(fn) else provider.embed([query])
    if not vec or len(vec[0]) != DIM:
        raise RuntimeError(f"向量维度异常：期望 {DIM}")
    return vec[0]
