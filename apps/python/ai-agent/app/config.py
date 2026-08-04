"""ai-agent 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-agent"
    port: int = 9002
    rag_base_url: str = "http://localhost:9001"


settings = Settings()
