"""ai-rag —— 检索服务：切分 · 向量化 · 混合检索 · 重排

M0 阶段只提供骨架与探针，业务能力在 M1-M2 落地。
"""

from __future__ import annotations

from ai_common import create_app
from ai_common.checks import http_check, kafka_check, tcp_check

from .config import settings

app = create_app(settings, description="检索服务：切分 · 向量化 · 混合检索 · 重排")

# 依赖检查：M0 用连通性探测，接入真实客户端后替换为深度检查
reg = app.state.health
s = settings
reg.register("milvus", tcp_check(s.milvus_host, s.milvus_port))
reg.register("mysql", tcp_check(s.mysql_host, s.mysql_port))
reg.register("litellm", http_check(f"{s.litellm_base_url}/health/liveliness"), required=False)


@app.get("/info", tags=["ops"], summary="服务能力声明")
async def info():
    """对应 docs/11-agent-cluster.md 的 Agent Card 概念，供运营后台渲染服务清单。"""
    return {
        "service": settings.service_name,
        "version": settings.version,
        "milestone": "M1-M2",
        "capabilities": ["chunking", "embedding", "hybrid_search", "rerank"],
        "implemented": False,
    }
