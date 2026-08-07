"""模型调用。

错误分类沿用数据中台关卡③的那套判据，因为它是踩出来的：

    **业务结论只能来自 200 响应。**

任何 HTTP 层的失败都不是"模型的回答"。在清洗流水线里，混淆这一点
会把基础设施故障当成"这段对话没有可复用知识"，数据就永久丢了；
在客服 Agent 里，混淆这一点会把"网关挂了"当成"知识库没有答案"，
用户收到一句一本正经的"抱歉没有找到相关信息"——而真相是服务宕了。
两种场景的正确处置完全不同：前者该重跑，后者该转人工。

这里没有复用 pipeline 的实现，是因为 Agent 需要流式输出而清洗不需要；
共享的是判据，不是代码。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterator, Protocol, runtime_checkable


class LlmError(RuntimeError):
    """模型调用失败的基类。"""


class LlmUnavailableError(LlmError):
    """连不上、超时、限流、5xx —— 重试可能成功。对用户表现为转人工。"""


class LlmConfigError(LlmError):
    """4xx —— 请求本身有问题，重试无用。对每个用户都会同样失败，
    属于运维事故而非单次对话的问题。"""


@runtime_checkable
class LlmClient(Protocol):
    def complete(self, *, model: str, system: str, user: str,
                 temperature: float = 0.3) -> str:
        ...

    def complete_json(self, *, model: str, system: str,
                      user: str) -> dict[str, Any]:
        ...

    def stream(self, *, model: str, system: str, user: str,
               temperature: float = 0.3) -> Iterator[str]:
        """逐块产出文本。RAG 链路 P95 约 3 秒，纯等待会让用户以为卡住。"""
        ...


def parse_json_lenient(text: str) -> dict[str, Any]:
    """容错解析模型返回的 JSON。即便要求了 json_object 也可能裹代码块。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"无法解析模型返回的 JSON: {text[:200]}") from exc
    raise LlmError(f"模型返回中未找到 JSON: {text[:200]}")


class OpenAiCompatClient:
    """任何 OpenAI 兼容端点：ai-gateway、DeepSeek、百炼、本地 vLLM。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        raw = (
            base_url
            or os.environ.get("SMARTMALL_LLM_BASE_URL")
            or os.environ.get("LITELLM_BASE_URL")
            or "http://localhost:9000"
        )
        self.base_url = raw.rstrip("/")
        key = (
            api_key
            or os.environ.get("SMARTMALL_LLM_API_KEY")
            or os.environ.get("LITELLM_API_KEY")
            or ""
        ).strip()
        if key and not key.isascii():
            raise LlmConfigError(
                "API Key 含非 ASCII 字符（复制时可能带进了全角引号）。"
            )
        self.api_key = key
        self.timeout = timeout
        self.use_response_format = True

    @property
    def chat_url(self) -> str:
        # 各家文档给的 base_url 层级不一致：DeepSeek 写到域名，
        # OpenAI/Kimi 写到 /v1，智谱写到 /api/paas/v4。已带版本段的不再补。
        if re.search(r"/v\d+$", self.base_url):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def _body(self, model: str, system: str, user: str,
              temperature: float, *, json_mode: bool, stream: bool) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if stream:
            body["stream"] = True
        if json_mode and self.use_response_format:
            body["response_format"] = {"type": "json_object"}
        return body

    def _raise_for_status(self, status: int, text: str) -> None:
        if status >= 500 or status == 429:
            raise LlmUnavailableError(f"模型服务返回 {status}: {text[:200]}")
        if status != 200:
            raise LlmConfigError(f"模型调用失败 HTTP {status}: {text[:400]}")

    def _post(self, body: dict, *, stream: bool = False):
        import httpx

        try:
            if stream:
                client = httpx.Client(timeout=self.timeout)
                return client.stream(
                    "POST", self.chat_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                ), client
            resp = httpx.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(
                f"无法连接模型服务 {self.base_url}：{type(exc).__name__}: {exc}"
            ) from exc
        return resp

    def complete(self, *, model: str, system: str, user: str,
                 temperature: float = 0.3) -> str:
        resp = self._post(self._body(model, system, user, temperature,
                                     json_mode=False, stream=False))
        self._raise_for_status(resp.status_code, resp.text)
        return resp.json()["choices"][0]["message"]["content"] or ""

    def complete_json(self, *, model: str, system: str,
                      user: str) -> dict[str, Any]:
        body = self._body(model, system, user, 0.0, json_mode=True, stream=False)
        resp = self._post(body)
        # response_format 不被支持时降级重试一次，而不是整条链路失败
        if resp.status_code == 400 and self.use_response_format:
            self.use_response_format = False
            resp = self._post(
                self._body(model, system, user, 0.0, json_mode=True, stream=False)
            )
        self._raise_for_status(resp.status_code, resp.text)
        return parse_json_lenient(resp.json()["choices"][0]["message"]["content"])

    def stream(self, *, model: str, system: str, user: str,
               temperature: float = 0.3) -> Iterator[str]:
        body = self._body(model, system, user, temperature,
                          json_mode=False, stream=True)
        ctx, client = self._post(body, stream=True)
        try:
            with ctx as resp:
                if resp.status_code != 200:
                    resp.read()
                    self._raise_for_status(resp.status_code, resp.text)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(
                f"流式读取中断：{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            client.close()


class FakeLlmClient:
    """确定性替身，让整张图可以离线测试。

    真实模型的输出不可预测，用它做断言等于测试一个随机数。
    这个替身按 system 提示词的开头区分调用类型，返回可预测的结果。
    """

    def __init__(
        self,
        answer: str = "这款是100%羊毛的，建议手洗。[#1]",
        intent: str = "product_knowledge",
        clarify: str = "您问的是哪一件呢？方便说下颜色吗？",
        raise_on: str | None = None,
    ) -> None:
        self.answer = answer
        self.intent = intent
        self.clarify = clarify
        self.raise_on = raise_on
        self.calls: list[tuple[str, str]] = []

    def _record(self, model: str, system: str, user: str) -> None:
        self.calls.append((model, system[:16]))
        if self.raise_on and self.raise_on in user:
            raise LlmUnavailableError("注入的故障")

    def complete(self, *, model: str, system: str, user: str,
                 temperature: float = 0.3) -> str:
        self._record(model, system, user)
        if system.startswith("你是电商店铺的客服"):
            return self.answer
        if "澄清" in system:
            return self.clarify
        return self.answer

    def complete_json(self, *, model: str, system: str,
                      user: str) -> dict[str, Any]:
        self._record(model, system, user)
        if "意图" in system:
            return {"intent": self.intent, "confidence": 0.9}
        if "改写" in system:
            return {"query": user[-30:]}
        return {}

    def stream(self, *, model: str, system: str, user: str,
               temperature: float = 0.3) -> Iterator[str]:
        text = self.complete(model=model, system=system, user=user)
        for i in range(0, len(text), 4):
            yield text[i : i + 4]
