"""ai-rag 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-rag"
    port: int = 9001
    embedding_model_path: str = "/data/models/bge-m3"
    reranker_model_path: str = "/data/models/bge-reranker-v2-m3"
    kb_collection: str = "kb_chunk"


settings = Settings()
