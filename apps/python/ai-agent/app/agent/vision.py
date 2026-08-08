"""用户发图提问：图 → VLM 转述 → 文本 query → 走现有那条链路。

不做端到端多模态检索。图片转成文本以后，检索、覆盖率闸门、出口合规检查、
转人工**全部复用已有的那一套**——这是这个方案一开始就定下的取舍，
好处是这条新入口不需要自己再长一遍这些能力，也不会长歪。

**这条路径最大的风险不是答错，是泄露。**

电商客服里用户发的图，很大一部分不是商品：订单截图、快递面单、聊天记录、
发票。上面有姓名、电话、收货地址、身份证号。文本侧的关卡①做了脱敏，
而图片这条路是从旁边绕过去的——一张面单的照片，转述出来就是一段带
真实地址的文本，然后进 trace、进日志、进转人工工单，最后可能进知识库。

所以这里有三道，缺一不可：

1. **提示词**要求模型分类图片，并且对文档类图片只提取可执行的部分，
   不转述任何个人信息。
2. **规则复查**——提示词是请求不是约束（打标那边已经吃过一次亏）。
   转述出来的文本一律过 :mod:`smartmall_pipeline.gates.pii` 的同一套
   脱敏规则，不另写一份：PII 规则重复一次就会漂移一次，
   而这里漂移的后果是漏掉一类个人信息。
3. **失败关闭**——脱敏模块加载不出来时**拒绝处理图片**，而不是不脱敏
   照跑。这是隐私控制，不可用时的正确行为是不做，不是放行。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .llm import LlmConfigError, LlmError, LlmUnavailableError

#: 允许的图片类型。白名单——用户上传的东西不可信，
#: 而"能不能解码"这件事交给下游库去发现太晚了
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

#: 单图上限。qwen-vl 本身有限制，但更重要的是别让一次上传拖垮进程
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImageRejected(ValueError):
    """图片没通过入口校验。带上给用户看的话术。"""

    def __init__(self, reply: str, reason: str) -> None:
        super().__init__(reason)
        self.reply = reply
        self.reason = reason


def check_image(data: bytes, mime: str) -> None:
    if mime not in ALLOWED_MIME:
        raise ImageRejected(
            "这个格式我暂时看不了，发 JPG 或 PNG 可以吗？",
            f"不支持的类型 {mime}",
        )
    if not data:
        raise ImageRejected("图片好像没上传成功，麻烦再发一次～", "空文件")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(
            "图片有点大，压缩一下再发可以吗？",
            f"超过上限 {len(data)} > {MAX_IMAGE_BYTES}",
        )


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """复用数据中台那一套脱敏规则。

    **不在这里另写一份。** PII 规则重复一次就会漂移一次，而这里漂移的
    后果是漏掉一类个人信息——不是少答一个问题那么轻。

    加载不出来就抛错，由调用方拒绝处理图片：隐私控制不可用时，
    正确的行为是不做，不是不脱敏照跑。
    """
    try:
        from smartmall_pipeline.gates import pii
    except ImportError as exc:  # pragma: no cover - 取决于安装方式
        raise ImageRejected(
            "图片功能暂时不可用，您直接用文字描述一下可以吗？",
            "缺少 smartmall-pipeline，无法做 PII 脱敏；"
            "装 `pip install -e pipelines` 或 ai-agent[local]",
        ) from exc

    r = pii.mask(text)
    return r.text, dict(r.hits)


# ---------------------------------------------------------------- 客户端


@runtime_checkable
class VisionClient(Protocol):
    def describe(
        self, *, model: str, system: str, user: str, image: bytes, mime: str
    ) -> dict[str, Any]:
        ...


@dataclass
class DashScopeVisionClient:
    """DashScope 的 OpenAI 兼容多模态接口。图片用 base64 内联。"""

    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode"
    timeout: float = 60.0
    model_aliases = {"vision": "qwen-vl-max", "vision-light": "qwen-vl-plus"}

    def __post_init__(self) -> None:
        import os

        self.api_key = (self.api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.base_url = os.environ.get("DASHSCOPE_BASE_URL", self.base_url).rstrip("/")
        if not self.api_key:
            raise LlmConfigError("缺少 DASHSCOPE_API_KEY，图片理解不可用")
        if not self.api_key.isascii():
            raise LlmConfigError("DASHSCOPE_API_KEY 含非 ASCII 字符")

    @property
    def chat_url(self) -> str:
        if re.search(r"/v\d+$", self.base_url):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def describe(self, *, model, system, user, image, mime):
        import httpx

        from .llm import parse_json_lenient

        body = {
            "model": self.model_aliases.get(model, model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
                    {"type": "text", "text": user},
                ]},
            ],
            "temperature": 0,
        }
        try:
            resp = httpx.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body, timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise LlmUnavailableError(f"无法连接视觉模型：{type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise LlmUnavailableError(f"视觉模型返回 {resp.status_code}")
        if resp.status_code != 200:
            raise LlmConfigError(f"视觉模型调用失败 HTTP {resp.status_code}: "
                                 f"{resp.text[:300]}")
        return parse_json_lenient(resp.json()["choices"][0]["message"]["content"])


@dataclass
class FakeVisionClient:
    payload: dict[str, Any] | None = None
    raise_on: str | None = None
    calls: list[str] = field(default_factory=list)

    def describe(self, *, model, system, user, image, mime):
        self.calls.append(model)
        if self.raise_on and self.raise_on in user:
            raise LlmUnavailableError("注入的故障")
        return self.payload if self.payload is not None else {
            "kind": "product",
            "query": "米白色圆领针织衫 起球",
            "observations": "一件米白色圆领针织衫，表面有轻微起球。",
            "order_no": "",
        }


# ---------------------------------------------------------------- 提示词

UNDERSTAND_SYSTEM = """你是电商客服的看图助手。只输出 json，不要解释。

**你看到的图可能包含用户的个人信息。** 电商场景里用户经常发订单截图、
快递面单、聊天记录、发票——上面有姓名、电话、收货地址、身份证号。
这些一个字都不要转述出来。你的输出会被记录、会进工单、可能进知识库。"""

UNDERSTAND_USER = """看这张图，判断它是什么，并把用户的意思转成一句可检索的话。

用户附带的文字：{caption}

先分类 kind：

- product：商品照、实物照、穿着效果图。**绝大多数应该是这一类。**
- document：订单截图、快递面单、聊天记录、发票、支付凭证——
  任何以文字信息为主、可能含个人信息的图。
- other：看不清、与购物无关、或者你判断不了。

然后：

* kind = product —— 描述你看见的东西（品类、颜色、图案、版型、
  有没有破损或瑕疵），并结合用户的话写出 query。
  **不要猜面料成分、克重、价格**，那些照片上看不出来。

* kind = document —— **不要转述图上的任何个人信息**：不要姓名、
  不要电话、不要地址、不要身份证号、不要完整卡号。
  observations 只写这是什么单据、状态是什么（如"一张显示已签收的
  快递单"）。如果图上有订单号，单独放进 order_no 字段，不要写进
  observations 或 query。

* kind = other —— query 留空。

输出 json：
{{"kind": "product",
  "query": "米白色针织衫 起球怎么处理",
  "observations": "一件米白色圆领针织衫，表面有轻微起球。",
  "order_no": ""}}"""


# ---------------------------------------------------------------- 理解


@dataclass
class ImageUnderstanding:
    kind: str = "other"
    query: str = ""
    observations: str = ""
    order_no: str = ""
    pii_hits: dict[str, int] = field(default_factory=dict)
    """脱敏规则实际命中了什么。**非零就说明提示词没拦住**——
    这个数字要能看见，否则永远不知道那道防线在不在工作。"""

    @property
    def is_document(self) -> bool:
        return self.kind == "document"


_ORDER_NO = re.compile(r"(?<!\d)(\d{10,24})(?!\d)")


def understand(
    client: VisionClient, data: bytes, mime: str, caption: str = "",
    *, model: str = "vision",
) -> ImageUnderstanding:
    """把一张图转成可检索的文本。**转述出来的一切都要过脱敏。**"""
    check_image(data, mime)

    raw = client.describe(
        model=model,
        system=UNDERSTAND_SYSTEM,
        user=UNDERSTAND_USER.format(caption=caption or "（没有文字）"),
        image=data, mime=mime,
    )

    kind = str(raw.get("kind") or "other")
    if kind not in ("product", "document", "other"):
        kind = "other"

    # 提示词已经要求过不要转述个人信息，这里再过一遍规则。
    # 打标那边的教训：模型不总听话，而这里不听话的代价是泄露。
    query, h1 = mask_pii(str(raw.get("query") or "").strip())
    obs, h2 = mask_pii(str(raw.get("observations") or "").strip())
    hits = {k: h1.get(k, 0) + h2.get(k, 0) for k in set(h1) | set(h2)}

    # 订单号单独取：它是查单必需的参数，但不该混在自由文本里到处传
    order_no = ""
    m = _ORDER_NO.search(str(raw.get("order_no") or ""))
    if m:
        order_no = m.group(1)

    return ImageUnderstanding(
        kind=kind, query=query, observations=obs,
        order_no=order_no, pii_hits=hits,
    )
