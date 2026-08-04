"""ai-clip 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-clip"
    port: int = 9004
    srs_api_base: str = "http://localhost:1985"
    funasr_model_path: str = "/data/models/funasr"
    live_record_dir: str = "/data/live"


settings = Settings()
