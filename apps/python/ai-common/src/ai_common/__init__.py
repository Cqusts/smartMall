"""smartMall Python AI 服务公共库。

提供六个 AI 服务共享的基础设施：应用工厂、统一响应、健康检查、
结构化日志、Kafka 消息契约。业务逻辑不放这里。
"""

from .api import ApiResponse, ErrorCode
from .app import create_app
from .health import HealthRegistry
from .logging import get_logger, setup_logging
from .settings import ServiceSettings, get_settings

__version__ = "0.1.0"

__all__ = [
    "ApiResponse",
    "ErrorCode",
    "create_app",
    "HealthRegistry",
    "get_logger",
    "setup_logging",
    "ServiceSettings",
    "get_settings",
]
