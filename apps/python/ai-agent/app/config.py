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


settings = Settings()
