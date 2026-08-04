"""ai-media 服务配置。"""

from ai_common import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "ai-media"
    port: int = 9003
    comfyui_base_url: str = "http://localhost:8188"
    wan_model_path: str = "/data/models/wan2.2-ti2v-5b"
    cosyvoice_model_path: str = "/data/models/cosyvoice2"


settings = Settings()
