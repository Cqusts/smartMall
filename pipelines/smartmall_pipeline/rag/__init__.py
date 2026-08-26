"""检索：向量化、BM25、轻量向量存储。

这里的实现面向**无 Milvus 的开发环境**——它与 apps/python/ai-rag 的
MilvusStore 保持同样的检索语义（硬性过滤、RRF 融合、BM25 参数），
因此换后端只改配置，检索行为不变。

索引构建本来就是数据中台的职责（对应 dag_kb_incremental_index），
所以放在 pipeline 包里；ai-rag 服务负责的是在线检索。
"""

from .bm25 import Bm25Index, LexicalStats, idf, tokenize
from .embedding import (
    DIM,
    DashScopeEmbedding,
    EmbeddingError,
    EmbeddingProvider,
    LocalBgeEmbedding,
    build_provider,
)
from .store import CHUNK_ID_STRIDE, LocalHit, LocalVectorStore, chunk_pk, embed_query

__all__ = [
    "Bm25Index", "LexicalStats", "idf", "tokenize",
    "DIM", "EmbeddingProvider", "EmbeddingError", "build_provider",
    "DashScopeEmbedding", "LocalBgeEmbedding",
    "LocalVectorStore", "LocalHit", "embed_query",
    "chunk_pk", "CHUNK_ID_STRIDE",
]
