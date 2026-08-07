"""知识检索适配层。

Agent 不该知道知识存在 Milvus 还是 MySQL 里。定义一个窄协议，
背后可以是：

* :class:`LocalRetriever` —— 直连 MySQL，走数据中台那套 dense+BM25+RRF。
  只要有 MySQL 就能跑，不需要 Docker，是本地开发与演示的路径。
* :class:`HttpRetriever` —— 调 ai-rag（Milvus + rerank）。生产路径。

两者返回同一种 :class:`Citation`，所以图里的节点一行都不用改。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .state import Citation


class RetrievalError(RuntimeError):
    """检索失败。

    与"检索到 0 条"是**两回事**：0 条是确定的结论（知识库里确实没有），
    失败是不知道——不知道就不能对用户说"没找到相关信息"，那是撒谎。
    正确处置是转人工。
    """


@runtime_checkable
class Retriever(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        product_ids: Sequence[int] | None = None,
        category_id: int | None = None,
    ) -> list[Citation]:
        ...


class LocalRetriever:
    """直连 MySQL 的检索。复用数据中台已经建好的索引。"""

    def __init__(self, min_similarity: float = 0.0) -> None:
        try:
            from smartmall_pipeline.rag import LocalVectorStore, build_provider
            from smartmall_pipeline.repository import DwsRepository
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise RetrievalError(
                "本地检索需要 smartmall-pipeline：\n"
                "  pip install -e pipelines\n"
                "  或改用 HttpRetriever 指向 ai-rag 服务"
            ) from exc

        self._provider = build_provider(_embedding_kind())
        self._store = LocalVectorStore(engine=DwsRepository.from_env().engine)
        self._loaded = self._store.load()
        self.min_similarity = min_similarity

    @property
    def size(self) -> int:
        return self._loaded

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "")

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        product_ids: Sequence[int] | None = None,
        category_id: int | None = None,
    ) -> list[Citation]:
        from smartmall_pipeline.rag.store import embed_query

        try:
            vec = embed_query(self._provider, query)
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(f"查询向量化失败：{exc}") from exc

        hits = self._store.search(
            query, vec, top_k=top_k,
            product_ids=product_ids, category_id=category_id,
        )
        return [
            Citation(
                item_id=h.item_id,
                title=(h.text.split("\n", 1)[0])[:60],
                content=h.text,
                score=h.score,
                dense_score=h.dense_score,
                bm25_score=h.bm25_score,
            )
            for h in hits
            if h.dense_score >= self.min_similarity
        ]


class HttpRetriever:
    """调 ai-rag 服务。生产路径，带 rerank。"""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        product_ids: Sequence[int] | None = None,
        category_id: int | None = None,
    ) -> list[Citation]:
        import httpx

        payload = {"query": query, "top_k": top_k}
        if product_ids:
            payload["product_ids"] = list(product_ids)
        if category_id:
            payload["category_id"] = category_id

        try:
            resp = httpx.post(
                f"{self.base_url}/search", json=payload, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(
                f"无法连接 ai-rag {self.base_url}：{type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise RetrievalError(f"ai-rag 返回 {resp.status_code}: {resp.text[:200]}")

        return [
            Citation(
                item_id=h["item_id"],
                title=h.get("title", ""),
                content=h.get("content", ""),
                score=h.get("score", 0.0),
                dense_score=h.get("rerank_score", h.get("dense_score", 0.0)),
                bm25_score=h.get("bm25_score", 0.0),
            )
            for h in (resp.json().get("data") or {}).get("hits", [])
        ]


class StubRetriever:
    """测试替身。让整张图不碰数据库就能跑通。"""

    def __init__(self, hits: list[Citation] | None = None,
                 fail: bool = False) -> None:
        self.hits = hits or []
        self.fail = fail
        self.queries: list[str] = []

    def search(self, query, *, top_k=5, product_ids=None, category_id=None):
        self.queries.append(query)
        if self.fail:
            raise RetrievalError("注入的检索故障")
        return self.hits[:top_k]


def _embedding_kind() -> str:
    import os

    return os.environ.get("EMBEDDING_BACKEND", "dashscope")
