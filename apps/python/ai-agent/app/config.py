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

    # ---- 会话存储（机制见 app/sessions.py）----
    session_backend: Literal["memory", "redis"] = "memory"
    """会话放哪。``memory`` 只在本进程（重启丢、多实例各记各的）；``redis`` 在内存工作副本之上
    加一层持久化，重启/换实例后接着聊，跨实例用分布式锁串行同一会话。Redis 故障时自动退回 memory。"""
    session_ttl: float = 86400.0
    """会话多久没有新一轮就过期（秒）。内存与 Redis 同一个值。"""
    session_max: int = 50_000
    """进程内最多缓存多少个会话，超出淘汰最久没动的（正在跑的不淘汰）。
    实测 20 轮短消息的会话约 3KB，默认 5 万个约 0.14GB；回答很长或历史很长时按比例涨，按机器内存调。
    配了 Redis 时被淘汰的会话不会丢，下次来会从 Redis 读回。"""
    session_lock_ttl: float = 120.0
    """分布式锁的过期时间（秒）。**必须大于一轮对话的最长耗时**（含 LLM 超时），
    否则一轮没跑完锁就没了，另一个实例会插进来。进程崩溃后别的实例最多等这么久才能接手。"""
    session_lock_wait: float = 3.0
    """抢不到会话锁（别的实例正在跑这个会话）时最多等多久，超过就拒绝成 session_busy。"""
    session_redis_timeout: float = 0.5
    """会话持久层访问 Redis 的超时（秒）。"""


settings = Settings()
