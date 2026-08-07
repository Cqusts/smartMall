"""向量化。

两种实现，同一个协议：

* :class:`DashScopeEmbedding` —— 走 API，不用下模型、不吃显存，
  728 条知识的成本是几分钱。**默认选择**，尤其适合没有 GPU 的开发机。
* :class:`LocalBgeEmbedding` —— 本地 bge-m3，数据不出域，但要下 ~2GB
  模型且装 torch。生产环境或数据敏感时用。

两者输出都是 1024 维并做 L2 归一化，因此可以互换而不需要重建索引
（前提是同一批数据用同一个实现向量化——混用会让相似度失去意义，
:class:`~smartmall_pipeline.rag.store.LocalVectorStore` 会记录 provider 名做校验）。
"""

from __future__ import annotations

import math
import os
from typing import Protocol, Sequence, runtime_checkable

DIM = 1024
"""bge-m3 与百炼 text-embedding-v3/v4 都支持 1024 维，可互换。

v4 最高支持 2048 维，但换维度要重建整个索引，收益不抵成本——
1024 维已经是检索质量与存储开销的合理平衡点。"""


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int
    max_batch: int
    """单次调用的条数上限。由实现方声明，调用方不该自己猜——
    猜错的后果是跑到一半被服务端拒绝（HTTP 400）。"""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化**文档**。返回值必须已 L2 归一化，使内积等价于余弦相似度。

        实现方负责按 :attr:`max_batch` 内部分批，调用方可以传任意长度。
        """
        ...

    def embed_query(self, text: str) -> list[float]:
        """向量化**查询**。可选方法，用 :func:`embed_one_query` 调用。

        Qwen3-Embedding 这类模型是非对称的：同一句话作为"待检索的问题"
        和作为"被检索的文档"，应当产出不同的向量。实现方支持这种区分时
        就实现本方法；不支持的（如 bge-m3 dense）不必实现，
        :func:`embed_one_query` 会退回 :meth:`embed`。
        """
        ...


class EmbeddingError(RuntimeError):
    pass


def l2_normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return list(vec)
    return [v / norm for v in vec]


# ---------------------------------------------------------------- DashScope


class DashScopeEmbedding:
    """百炼通用文本向量，走 OpenAI 兼容接口。

    也可指向 ai-gateway（LiteLLM）统一记账；没起网关时直连 DashScope。

    百炼的免费额度**按模型独立发放，互不相通**——聊天模型额度用尽
    并不影响向量模型，所以 clean 走别家、index 仍走百炼是可行的组合。
    """

    #: 各模型的单次请求条数上限。超过直接 400
    #: （``batch size is invalid, it should not be larger than 10``）。
    #: 由实现方声明而不是让调用方猜，是因为猜错要跑到一半才发现。
    MODEL_BATCH_LIMITS = {
        "text-embedding-v4": 10,
        "text-embedding-v3": 10,
        "text-embedding-v2": 25,
        "text-embedding-v1": 25,
        "qwen3.7-text-embedding": 20,
    }
    DEFAULT_MODEL = "text-embedding-v4"
    """v4 与 v3 同价，但免费额度翻倍（100 万 vs 50 万 token）、
    多语言更强，且同样支持 1024 维——没有理由继续用 v3。"""

    NATIVE_URL = (
        "https://dashscope.aliyuncs.com"
        "/api/v1/services/embeddings/text-embedding/text-embedding"
    )
    """原生接口。只有它支持 ``text_type``——OpenAI 兼容接口没有这个位置。"""

    dim = DIM

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
        max_batch: int | None = None,
        native: bool | None = None,
        query_instruct: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("EMBEDDING_MODEL") or self.DEFAULT_MODEL
        # 模型名进 name：LocalVectorStore 靠它检测混用。
        # 写死成常量的话，换了模型也照样往同一个索引里塞，
        # 而不同模型的向量放在一起，相似度就没有意义了。
        self.name = f"dashscope/{self.model}"
        self.max_batch = (
            max_batch
            if max_batch is not None
            else self.MODEL_BATCH_LIMITS.get(self.model, 10)
        )
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        routed = base_url or os.environ.get("EMBEDDING_BASE_URL")
        self.base_url = (
            routed or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
        # 指向网关（LiteLLM）时只能走 OpenAI 兼容协议；直连百炼时走原生，
        # 因为 text_type 只有原生接口认。这个默认规则不用记：
        # 配了 base_url 就是在做路由，没配就是直连。
        self.native = native if native is not None else routed is None
        self.query_instruct = query_instruct or os.environ.get(
            "EMBEDDING_QUERY_INSTRUCT"
        )
        self.timeout = timeout

        if not self.api_key:
            raise EmbeddingError(
                "缺少 DASHSCOPE_API_KEY。\n"
                "  · 到 https://bailian.console.aliyun.com/ 申请，填进 deploy/.env\n"
                "  · 或改用本地模型：--embedding local（需下载 ~2GB 模型并装 torch）"
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, text_type="document")

    def embed_query(self, text: str) -> list[float]:
        """按"查询"语义向量化。

        Qwen3-Embedding 系列是非对称模型——"这件能机洗吗"作为待检索的
        问题、和作为库里的一条知识，应当产出不同的向量。区分了才用得上
        这个模型的主要优势。

        只有原生接口支持 ``text_type``；走 OpenAI 兼容模式时它会被忽略，
        退化成与文档同样的向量（仍然可用，只是少了这份增益）。
        """
        return self._embed([text], text_type="query")[0]

    def _payload(self, batch: list[str], text_type: str) -> dict:
        if not self.native:
            # OpenAI 兼容接口没有 text_type 这个位置，只能不带
            return {"model": self.model, "input": batch, "dimensions": self.dim}
        params: dict[str, object] = {
            "dimension": self.dim,      # 原生接口是单数 dimension
            "text_type": text_type,
            "output_type": "dense",
        }
        # instruct 只在 query 侧生效，且必须是英文
        if text_type == "query" and self.query_instruct:
            params["instruct"] = self.query_instruct
        return {"model": self.model, "input": {"texts": batch}, "parameters": params}

    def _vectors(self, body: dict, expected: int) -> list[list[float]]:
        """从两种响应结构里取出向量，按输入顺序还原。

        接口不保证返回顺序，错位会让向量张冠李戴——症状是检索结果
        看起来"有点不对"，但不会报错，极难发现。
        """
        if self.native:
            data = (body.get("output") or {}).get("embeddings") or []
            key = "text_index"
        else:
            data = body.get("data") or []
            key = "index"
        if len(data) != expected:
            raise EmbeddingError(f"返回条数不匹配：请求 {expected}，返回 {len(data)}")
        return [
            l2_normalize(item["embedding"])
            for item in sorted(data, key=lambda d: d.get(key, 0))
        ]

    def _embed(self, texts: Sequence[str], *, text_type: str) -> list[list[float]]:
        import httpx

        url = self.NATIVE_URL if self.native else f"{self.base_url}/embeddings"
        out: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.max_batch):
                batch = list(texts[i : i + self.max_batch])
                resp = client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=self._payload(batch, text_type),
                )
                if resp.status_code != 200:
                    detail = resp.text[:300]
                    hint = ""
                    if "batch size" in detail:
                        hint = (
                            f"\n  当前批次 {len(batch)} 条超过服务端上限，"
                            f"用 --batch-size 调小（该模型上限 {self.max_batch}）"
                        )
                    elif resp.status_code in (401, 403):
                        hint = (
                            "\n  API Key 无效、未开通该模型，或该模型免费额度已用尽。"
                            "\n  百炼的免费额度按模型独立发放：聊天模型用尽不影响向量模型，"
                            "\n  反之亦然。到控制台确认这个模型自己的余额。"
                            "\n  换模型：EMBEDDING_MODEL=text-embedding-v3"
                            "\n  不想付费：--embedding local（本地 bge-m3）"
                        )
                    elif resp.status_code == 429:
                        hint = "\n  触发限流，稍后重试；重跑会从断点继续"
                    raise EmbeddingError(
                        f"向量化失败 HTTP {resp.status_code}: {detail}{hint}"
                    )
                out.extend(self._vectors(resp.json(), len(batch)))
        return out


# ---------------------------------------------------------------- 本地 bge-m3


class LocalBgeEmbedding:
    """本地 bge-m3。数据不出域，但需要 ~2GB 模型与 torch。"""

    name = "local/bge-m3"
    dim = DIM
    max_batch = 64  # 本地无服务端限制，受显存/内存约束

    def __init__(self, model_path: str | None = None, batch_size: int = 16) -> None:
        self.model_path = model_path or os.environ.get(
            "EMBEDDING_MODEL_PATH", "/data/models/bge-m3"
        )
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from FlagEmbedding import BGEM3FlagModel  # type: ignore
            except ImportError as exc:
                raise EmbeddingError(
                    "本地向量化需要 FlagEmbedding：pip install FlagEmbedding\n"
                    "  没有 GPU 的话建议改用 --embedding dashscope（走 API，无需下载模型）"
                ) from exc
            self._model = BGEM3FlagModel(self.model_path, use_fp16=False)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        result = model.encode(
            list(texts), batch_size=self.batch_size, max_length=512
        )["dense_vecs"]
        return [l2_normalize(v.tolist()) for v in result]


# ---------------------------------------------------------------- 工厂


def build_provider(kind: str = "dashscope", **kwargs) -> EmbeddingProvider:
    kind = (kind or "dashscope").lower()
    if kind in ("dashscope", "api"):
        return DashScopeEmbedding(**kwargs)
    if kind in ("local", "bge", "bge-m3"):
        return LocalBgeEmbedding(**kwargs)
    raise EmbeddingError(f"未知的向量化后端: {kind}（可选 dashscope / local）")
