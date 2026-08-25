"""ai-agent —— LangGraph 编排：客服 Agent · 运营 Agent · 考核 Agent

M2 已落地客服 Agent（见 app/agent/）；运营与考核 Agent 待后续里程碑。
"""

from __future__ import annotations

from ai_common import create_app
from ai_common.checks import http_check, kafka_check, tcp_check

from .config import settings
from .routers import chat, media, ws

app = create_app(settings, description="LangGraph 编排：客服 Agent · 运营 Agent · 考核 Agent")

# 商品列表现在是 579 个商品、437KB 的 JSON。gzip 之后 35KB —— 实测的两个数。
# 加在这里而不是 ai-common 里：只有这个服务往外发大 JSON，别的服务
# 响应都是几百字节，压缩反而是纯开销。
#
# minimum_size 取 1KB：比这更小的响应，压缩头本身就占掉可观比例
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024)

# 依赖检查：M0 用连通性探测，接入真实客户端后替换为深度检查
reg = app.state.health
s = settings
app.include_router(chat.router)
app.include_router(media.router)
app.include_router(ws.router)

reg.register("litellm", http_check(f"{s.litellm_base_url}/health/liveliness"))
reg.register("ai-rag", http_check(f"{s.rag_base_url}/health"))
reg.register("redis", tcp_check(s.redis_host, s.redis_port))
reg.register("kafka", kafka_check(s.kafka_bootstrap_servers), required=False)


@app.get("/info", tags=["ops"], summary="服务能力声明")
async def info():
    """对应 docs/11-agent-cluster.md 的 Agent Card 概念，供运营后台渲染服务清单。"""
    return {
        "service": settings.service_name,
        "version": settings.version,
        "milestone": "M2",
        "capabilities": ["customer_service", "shopping_guide", "knowledge_ops",
                         "marketing", "sales_kpi_judge"],
        "implemented": ["customer_service", "shopping_guide", "knowledge_ops",
                        "marketing"],
    }
