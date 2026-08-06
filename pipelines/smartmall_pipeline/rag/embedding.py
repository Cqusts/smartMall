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
"""bge-m3 与 DashScope text-embedding-v3 都是 1024 维，可互换。"""


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int
    max_batch: int
    """单次调用的条数上限。由实现方声明，调用方不该自己猜——
    猜错的后果是跑到一半被服务端拒绝（HTTP 400）。"""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化。返回值必须已 L2 归一化，使内积等价于余弦相似度。

        实现方负责按 :attr:`max_batch` 内部分批，调用方可以传任意长度。
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
    """通义千问 text-embedding-v3，走 OpenAI 兼容接口。

    也可指向 ai-gateway（LiteLLM）统一记账；没起网关时直连 DashScope。
    """

    name = "dashscope/text-embedding-v3"
    dim = DIM

    #: DashScope 对 text-embedding-v3 的硬限制。
    #: 超过会直接 400：``batch size is invalid, it should not be larger than 10``。
    max_batch = 10

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "text-embedding-v3",
        timeout: float = 60.0,
        max_batch: int | None = None,
    ) -> None:
        if max_batch is not None:
            self.max_batch = max_batch
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("EMBEDDING_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
        self.model = model
        self.timeout = timeout

        if not self.api_key:
            raise EmbeddingError(
                "缺少 DASHSCOPE_API_KEY。\n"
                "  · 到 https://bailian.console.aliyun.com/ 申请，填进 deploy/.env\n"
                "  · 或改用本地模型：--embedding local（需下载 ~2GB 模型并装 torch）"
            )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        out: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.max_batch):
                batch = list(texts[i : i + self.max_batch])
                resp = client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": batch, "dimensions": self.dim},
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
                        hint = "\n  API Key 无效或未开通该模型，检查 DASHSCOPE_API_KEY"
                    elif resp.status_code == 429:
                        hint = "\n  触发限流，稍后重试；重跑会从断点继续"
                    raise EmbeddingError(
                        f"向量化失败 HTTP {resp.status_code}: {detail}{hint}"
                    )
                data = resp.json().get("data") or []
                if len(data) != len(batch):
                    raise EmbeddingError(
                        f"返回条数不匹配：请求 {len(batch)}，返回 {len(data)}"
                    )
                # 按 index 排序——接口不保证顺序，错位会让向量张冠李戴
                for item in sorted(data, key=lambda d: d.get("index", 0)):
                    out.append(l2_normalize(item["embedding"]))
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
