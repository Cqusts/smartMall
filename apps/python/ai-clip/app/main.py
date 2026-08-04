"""ai-clip —— 直播切片：FunASR 转写 · 语义分段 · 商品对齐 · FFmpeg 切片

M0 阶段只提供骨架与探针，业务能力在 M5 落地。
"""

from __future__ import annotations

from ai_common import create_app
from ai_common.checks import http_check, kafka_check, tcp_check

from .config import settings

app = create_app(settings, description="直播切片：FunASR 转写 · 语义分段 · 商品对齐 · FFmpeg 切片")

# 依赖检查：M0 用连通性探测，接入真实客户端后替换为深度检查
reg = app.state.health
s = settings
reg.register("kafka", kafka_check(s.kafka_bootstrap_servers))
reg.register("asset", http_check(f"{s.asset_base_url}/health"))


@app.get("/info", tags=["ops"], summary="服务能力声明")
async def info():
    """对应 docs/11-agent-cluster.md 的 Agent Card 概念，供运营后台渲染服务清单。"""
    return {
        "service": settings.service_name,
        "version": settings.version,
        "milestone": "M5",
        "capabilities": ["asr", "semantic_segment", "product_align", "clip"],
        "implemented": False,
    }
