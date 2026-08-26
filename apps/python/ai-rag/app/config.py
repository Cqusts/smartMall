"""ai-rag 服务配置。"""

from ai_common import ServiceSettings

PYMILVUS_RESERVED_ENV = (
    "MILVUS_URI",
    "MILVUS_CONN_ALIAS",
    "MILVUS_CONN_TIMEOUT",
    "MILVUS_DEFAULT_CONNECTION",
)
"""pymilvus 自己会读的环境变量，本项目的配置项**不能**用这些名字。

``MILVUS_URI`` 尤其危险：pymilvus 按 URL 校验它，设成 Milvus Lite 需要的
文件路径会在 import 阶段抛 ``Illegal uri``——报错点在 pymilvus 内部，
跟本项目的代码看不出任何关系，很难查。
"""


class Settings(ServiceSettings):
    service_name: str = "ai-rag"
    port: int = 9001
    embedding_model_path: str = "/data/models/bge-m3"
    reranker_model_path: str = "/data/models/bge-reranker-v2-m3"
    kb_collection: str = "kb_chunk"

    kb_milvus_uri: str = ""
    """留空则由 ``milvus_host`` / ``milvus_port`` 拼成服务端地址。

    **填一个本地文件路径就切到 Milvus Lite**（嵌入式，纯 Python，
    Windows 也能跑，不需要 Docker 或 Linux）。例如::

        KB_MILVUS_URI=D:\\smartMall\\data\\kb.db

    业务代码不用改——这是开发机与服务端之间唯一需要动的开关。

    **为什么不叫 MILVUS_URI。** 那个名字被 pymilvus 自己占了
    （``pymilvus/orm/connections.py`` 读 ``getenv("MILVUS_URI")``），
    而且它按 URL 校验：设成一个文件路径，pymilvus 在 import 阶段就抛
    ``Illegal uri``，跟本项目的代码八竿子打不着。见
    :data:`PYMILVUS_RESERVED_ENV`。
    """

    milvus_analyzer: str = "jieba"
    """中文分词器。Lite 用 ``jieba``，服务端用 ``chinese``——
    两边的合法取值不一样，见 ``milvus_store.MilvusConfig.analyzer``。"""

    kb_version: str = ""
    """知识库版本隔离。空字符串表示不隔离（开发期），生产必须指定。"""

    @property
    def milvus_is_embedded(self) -> bool:
        """URI 不是 http/grpc 就是本地文件路径，即 Milvus Lite。"""
        return bool(self.kb_milvus_uri) and "://" not in self.kb_milvus_uri

    @property
    def milvus_target(self) -> str:
        if self.kb_milvus_uri:
            return self.kb_milvus_uri
        return f"http://{self.milvus_host}:{self.milvus_port}"


settings = Settings()
