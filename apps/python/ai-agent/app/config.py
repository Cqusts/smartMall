"""ai-agent 服务配置。"""

from typing import Literal

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-agent"
    port: int = 9002
    rag_base_url: str = "http://localhost:9001"

    retriever_backend: Literal["local", "http"] = "local"
    """对话链路用哪个检索后端。

    * ``local`` —— 直连 MySQL：把全部知识向量读进本进程内存，Python 里暴力算余弦。
      只要有 MySQL 就能跑，是本地开发与演示的路径。**但它有规模上限**：实测单次检索
      728 条 33ms、5000 条 262ms、20000 条 1.2s，而且吃满一个核；向量在 Python 里是
      list[float]，内存是二进制大小的 8 倍；每个 worker 各存一份，新知识要重启才可见。
    * ``http`` —— 调 ai-rag（Milvus HNSW + 原生 BM25）。生产路径。

    **默认 local，docker-compose 里显式设成 http。**原先服务端无论如何都是 local
    （``get_deps`` 根本不传 ``rag_url``），``rag_base_url`` 只被健康检查用到——
    于是 compose 里的 ai-rag 与 Milvus 虽然起着，却从来不在对话的查询链路上。

    写成 ``Literal`` 而不是 ``str``：``RETRIEVER_BACKEND=htp`` 这种拼写错误会在服务
    启动时直接报错，而不是悄悄退回 local、让人以为生产在走 Milvus。
    """
    # 下单走 mall-product。演示页面与订单服务不同源，让浏览器直接跨域调 8081
    # 要么开 CORS 要么让用户改 host，都不如在这边转发一次省事
    order_base_url: str = "http://localhost:8081"

    # ---- 对话入口的过载保护与限流（机制见 app/overload.py、app/ratelimit.py）----
    # 全局同时在跑的轮数 = CHAT_WORKERS（线程数），见 app/concurrency.py
    chat_max_waiting: int = 64
    """等名额的队列长度。队满立刻拒绝。取线程数量级：队列里的请求至少要等一整轮
    （几秒）才轮到，排得比"queue_timeout 内能服务的"更长，多出来的注定超时。"""
    chat_queue_timeout: float = 8.0
    """在队列里最多等多久（秒）。超过就拒绝——用户多半已经走了，再跑就是白花 LLM 的钱。"""
    chat_session_max_pending: int = 2
    """同一会话最多几个在途（含正在跑的）。2 = 一个在跑、一个在等。"""
    chat_retry_after: int = 5
    """过载时告诉客户端多少秒后重试（HTTP ``Retry-After``）。"""
    chat_rate_backend: Literal["memory", "redis", "off"] = "memory"
    """限流计数放哪。``memory`` 每个进程各记各的（N 实例即 N 倍上限）；
    ``redis`` 所有实例共用，Redis 故障时自动退回 memory（fail-open）。"""
    chat_rate_redis_timeout: float = 0.25
    """限流访问 Redis 的超时（秒）。要短：它在每条消息的路径上。"""
    chat_rate_ip_burst: float = 30
    chat_rate_ip_per_min: float = 60
    """每个 IP：突发 30 条，长期 60 条/分钟。宽松，因为一个出口 IP 后面可能是整个办公室。"""
    chat_rate_identity_burst: float = 6
    chat_rate_identity_per_min: float = 12
    """每个用户（无 user_id 时按会话）：突发 6 条，长期 12 条/分钟。"""
    chat_trust_proxy_hops: int = 0
    """前面有几层可信反向代理。0 = 不读 ``X-Forwarded-For``，只用对端地址。
    **站在代理后面必须设**，否则所有用户共用一个 IP 桶（见 ratelimit.client_ip）。"""


settings = Settings()
