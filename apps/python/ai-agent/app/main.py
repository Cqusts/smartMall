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



def register_dependencies(reg, s) -> None:
    reg.register("litellm", http_check(f"{s.litellm_base_url}/health/liveliness"))
    # ai-rag 只有走 http 后端时才是必需依赖。local 后端根本不调它，
    # 它挂了不该让 ai-agent 的 /ready 变红、被编排系统摘掉流量
    reg.register("ai-rag", http_check(f"{s.rag_base_url}/health"),
                 required=(s.retriever_backend == "http"))
    reg.register("redis", tcp_check(s.redis_host, s.redis_port))
    reg.register("kafka", kafka_check(s.kafka_bootstrap_servers), required=False)


register_dependencies(reg, s)


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
        # 对话检索走哪条路。运维一眼看出生产是不是真的在用 Milvus
        "retriever": settings.retriever_backend,
        # 对话入口当前负载与累计拒绝数。active 长期顶满 max_active、rejected 在涨，
        # 说明该扩容（或 CHAT_WORKERS 太小）；active 归不了零说明名额泄漏
        "chat_load": chat.get_guard().snapshot(),
        # 会话数、淘汰数、持久层是否在降级。sessions 长期贴着 max_sessions、evicted_cap 在涨，
        # 说明在淘汰还活着的会话（调大 SESSION_MAX 或加机器）
        "sessions": chat._sessions.stats(),
    }
