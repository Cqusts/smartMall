"""ai-train 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-train"
    port: int = 9005
    base_model_path: str = "/data/models/qwen3-8b"
    lora_output_dir: str = "/data/models/lora"


settings = Settings()
