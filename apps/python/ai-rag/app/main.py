"""ai-rag —— 检索服务：切分 · 向量化 · 混合检索 · 重排

``/search`` 是 Agent 侧 ``HttpRetriever`` 唯一调用的接口，字段契约由它
决定（见 ``agent/retriever.py``），改动要两边一起改。

**失败与空结果必须分开。** 检索不到返回 200 + 空 hits（这是一个确定的
结论：知识库里确实没有）；Milvus 连不上、向量化失败返回 5xx（这是"不
知道"）。混在一起的后果是 Agent 拿"没找到相关信息"回复用户——那是在
把故障说成事实。
"""

from __future__ import annotations

from typing import Any

from ai_common import ApiResponse, ErrorCode, create_app
from ai_common.checks import http_check, kafka_check, tcp_check
from fastapi import Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import settings
from .search import SearchError, SearchService, build_service

app = create_app(settings, description="检索服务：切分 · 向量化 · 混合检索 · 重排")


async def _embedded_milvus_check() -> None:
    """Milvus Lite 的深度检查：collection 建了没有。

    正是 ``ai_common.checks`` 那句 "接入真实客户端后替换为深度检查" 说的
    那件事。pymilvus 是同步的，丢到线程池里跑，别阻塞事件循环。
    """
    import asyncio

    def _probe() -> None:
        store = get_service().store
        if not store.client.has_collection(store.cfg.collection):
            raise RuntimeError(f"collection {store.cfg.collection} 不存在，先跑 index 建索引")

    await asyncio.get_running_loop().run_in_executor(None, _probe)


reg = app.state.health
s = settings
if s.milvus_is_embedded:
    # Milvus Lite 跑在进程内，没有端口。对它做 TCP 探测会**永远失败**，
    # /ready 就永远是红的——一个恒假的检查和一个恒真的一样没用。
    # 改成探真正的东西：collection 在不在。
    reg.register("milvus", _embedded_milvus_check)
else:
    reg.register("milvus", tcp_check(s.milvus_host, s.milvus_port))
reg.register("mysql", tcp_check(s.mysql_host, s.mysql_port), required=False)
reg.register("litellm", http_check(f"{s.litellm_base_url}/health/liveliness"), required=False)


@app.get("/info", tags=["ops"], summary="服务能力声明")
async def info():
    """对应 docs/11-agent-cluster.md 的 Agent Card 概念，供运营后台渲染服务清单。"""
    return {
        "service": settings.service_name,
        "version": settings.version,
        "milestone": "M1-M2",
        "capabilities": ["chunking", "embedding", "hybrid_search"],
        "implemented": True,
        "not_implemented": ["rerank"],
        "backend": settings.milvus_target,
        "embedded": settings.milvus_is_embedded,
    }


# ---------------------------------------------------------------- 检索


class SearchRequest(BaseModel):
    """字段与 ``agent.retriever.HttpRetriever`` 发的 payload 对应。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    product_ids: list[int] | None = None
    category_id: int | None = None
    biz_types: list[str] | None = None


#: 懒加载：import 时就建会去连 Milvus，测试和 --help 都会被拖死
_service: SearchService | None = None


def get_service() -> SearchService:
    global _service
    if _service is None:
        _service = build_service(
            uri=settings.milvus_target,
            collection=settings.kb_collection,
            analyzer=settings.milvus_analyzer,
            kb_version=settings.kb_version,
        )
    return _service


def _fail(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ApiResponse.fail(code, message).model_dump(mode="json", by_alias=True),
    )


@app.post("/search", tags=["retrieval"], summary="混合检索")
async def search(req: SearchRequest = Body(...)) -> Any:
    """dense + BM25 两路召回，RRF 融合，返回带完整判据的命中。

    返回的每条命中都带 ``dense_score`` 与 ``lexical_overlap``——Agent 靠
    这两个分流「作答 / 澄清 / 转人工」，缺一个都会让它的闸门失效
    （见 ``agent/graph.py`` 的 ``has_lexical_support``）。
    """
    try:
        hits = get_service().search(
            req.query,
            top_k=req.top_k,
            product_ids=req.product_ids,
            category_id=req.category_id,
            biz_types=req.biz_types,
        )
    except SearchError as exc:
        # 检索失败 ≠ 检索到 0 条。必须以非 200 出去，让 Agent 走转人工，
        # 而不是拿着空结果对用户说"知识库里没有"
        return _fail(ErrorCode.DEPENDENCY_UNAVAILABLE, str(exc), 503)

    return ApiResponse.ok({"hits": [h.to_json() for h in hits]}).model_dump(
        mode="json", by_alias=True
    )


@app.post("/search/reload", tags=["retrieval"], summary="重建 IDF 表")
async def reload_stats() -> Any:
    """重新索引之后调一次，否则词汇覆盖率还是按旧语料算的。

    IDF 表是启动时扫一遍语料建的（和 ``LocalVectorStore.load()`` 一样是
    一次性快照），新知识入库后不刷新的话，新词的 df 还是 0。
    """
    try:
        n = get_service().refresh_stats()
    except SearchError as exc:
        return _fail(ErrorCode.DEPENDENCY_UNAVAILABLE, str(exc), 503)
    return ApiResponse.ok({"documents": n}).model_dump(mode="json", by_alias=True)
