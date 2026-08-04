"""服务配置基类。

所有配置来自环境变量，与 ``deploy/.env.example`` 一一对应。
服务自己的专属配置通过继承 :class:`ServiceSettings` 扩展。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """所有 Python AI 服务共享的配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 服务自身 ----
    service_name: str = "ai-service"
    version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 9000
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    """生产环境输出 JSON 日志便于采集；本地开发保持人类可读。"""

    # ---- 模型网关 ----
    litellm_base_url: str = "http://localhost:9000"
    litellm_api_key: str = ""

    # ---- 中间件 ----
    kafka_bootstrap_servers: str = "localhost:9092"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "smartmall"
    mysql_password: str = "smartmall"
    mysql_database: str = "smartmall"

    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # ---- 对象存储（S3 兼容）----
    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "smartmall-assets"

    # ---- 可观测 ----
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_enabled: bool = False

    # ---- 业务服务地址（Java 侧）----
    product_base_url: str = "http://localhost:8081"
    asset_base_url: str = "http://localhost:8082"
    dataplat_base_url: str = "http://localhost:8083"
    kpi_base_url: str = "http://localhost:8084"

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
