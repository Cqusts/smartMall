"""ai-agent 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-agent"
    port: int = 9002
    rag_base_url: str = "http://localhost:9001"
    # 下单走 mall-product。演示页面与订单服务不同源，让浏览器直接跨域调 8081
    # 要么开 CORS 要么让用户改 host，都不如在这边转发一次省事
    order_base_url: str = "http://localhost:8081"


settings = Settings()
