"""知识检索适配层。

Agent 不该知道知识存在 Milvus 还是 MySQL 里。定义一个窄协议，
背后可以是：

* :class:`LocalRetriever` —— 直连 MySQL，走数据中台那套 dense+BM25+RRF。
  只要有 MySQL 就能跑，不需要 Docker，是本地开发与演示的路径。
* :class:`HttpRetriever` —— 调 ai-rag（Milvus + rerank）。生产路径。

两者返回同一种 :class:`Citation`，所以图里的节点一行都不用改。
"""

from __future__ import annotations

import threading
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
                lexical_overlap=h.lexical_overlap,
                # 检索层一路带着它，到这里断掉的话答案就永远挂不上图
                asset_ids=tuple(h.asset_ids),
            )
            for h in hits
            if h.dense_score >= self.min_similarity
        ]


class HttpRetriever:
    """调 ai-rag 服务。生产路径，带 rerank。

    **复用同一个 httpx.Client。**原先每次检索 ``httpx.post(...)``，等于每个用户问题
    都新建一条到 ai-rag 的 TCP 连接——对话链路上最频繁的一次内部调用，白白多付一次握手。
    httpx.Client 线程安全，对话线程池里的各个线程共用它。

    连接超时单独设短（3 秒）：ai-rag 没起来时应该很快失败并转人工，
    而不是让用户干等整个读超时。
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = None
        self._client_lock = threading.Lock()

    def _http(self):
        client = self._client
        if client is None:
            with self._client_lock:
                if self._client is None:
                    import httpx

                    self._client = httpx.Client(
                        timeout=httpx.Timeout(self.timeout, connect=min(3.0, self.timeout)),
                        limits=httpx.Limits(max_connections=100,
                                            max_keepalive_connections=32),
                    )
                client = self._client
        return client

    def close(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

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

        url = f"{self.base_url}/search"
        try:
            try:
                resp = self._http().post(url, json=payload)
            except (httpx.RemoteProtocolError, httpx.ReadError):
                # 复用的长连接被对端悄悄关了；检索是只读的，换条新连接重试一次安全。
                # 超时不重试——那会把用户的等待翻倍
                resp = self._http().post(url, json=payload)
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
                # ai-rag 老版本不返这个字段。缺了就退回 bm25>0 的弱判据，
                # 见 graph.has_lexical_support——那条路会明确记一笔
                lexical_overlap=h.get("lexical_overlap", -1.0),
                asset_ids=tuple(h.get("asset_ids") or ()),
            )
            for h in (resp.json().get("data") or {}).get("hits", [])
        ]


class NullRetriever:
    """不检索的检索器。给根本不走 RAG 的 Agent 用（导购只查结构化商品）。

    **为什么不是直接传 None。** 导购不检索是它现在的形状，不是一条保证。
    真传了 None，哪天有人在导购链路里加一句检索，跑到那行才 AttributeError；
    而返回空列表更糟——那等于对上层说"知识库里确实没有"，
    正是 :class:`RetrievalError` 那段注释里说的那种撒谎。

    所以这里的行为是**被调用就报错**：装配时就说清楚这条链路没有检索能力，
    谁要用谁自己去装一个。
    """

    def search(self, query, *, top_k=5, product_ids=None, category_id=None):
        raise RetrievalError(
            "这条链路没有装检索器（导购只查结构化商品数据）。"
            "需要检索的话，装配时传 with_retriever=True。"
        )


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
