"""关卡③ 模型清洗：LLM 判定 + 结构化抽取 + 风格标注。

**这一关是从「干净的对话」到「可用的知识」的关键转换。**
前两关只是把脏数据洗掉，这一关做提炼——一段 20 轮的会话里可能藏着
3 个独立知识点，要拆成 3 条 knowledge_item。

三件事：

1. **判定** —— 是否含可复用知识、能否泛化、是否已过期。决定这段对话值不值得留。
2. **抽取** —— 把多轮对话拆成独立的 QA 对。
3. **风格标注** —— 标注真人客服回复的语气特征，写入 sft_sample.style_tags，
   供 M7 的微调使用（学风格，不学知识）。

成本控制：这是四关中唯一按量付费的环节。策略是两段式——
先用便宜模型粗筛（只判定 has_reusable_knowledge），再用强模型做完整抽取。

LLM 客户端通过 :class:`LlmClient` 协议注入，测试时用 :class:`FakeLlmClient`，
因此这一关的编排逻辑完全可以离线测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import (
    BizType,
    Dialogue,
    GateStats,
    KnowledgeItem,
    KnowledgeType,
    ReviewStatus,
    SftSample,
)

# ---------------------------------------------------------------- LLM 客户端


@runtime_checkable
class LlmClient(Protocol):
    """LLM 调用协议。生产实现走 ai-gateway（LiteLLM），测试用 FakeLlmClient。"""

    def complete_json(self, *, model: str, system: str, user: str) -> dict[str, Any]:
        """调用模型并返回解析后的 JSON。实现方负责重试与 JSON 修复。"""
        ...


class LlmError(RuntimeError):
    pass


class LiteLlmClient:
    """经 ai-gateway 调用模型。

    统一走网关而不是直连厂商 SDK：成本记账、失败降级、A/B 切流都在网关层，
    业务代码不需要知道背后是 qwen 还是 deepseek。
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def complete_json(self, *, model: str, system: str, user: str) -> dict[str, Any]:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return parse_json_lenient(content)


def parse_json_lenient(text: str) -> dict[str, Any]:
    """容错解析 LLM 返回的 JSON。

    即便要求了 json_object，模型仍可能裹上 ```json 代码块或加前后缀说明。
    与其在每个调用点重复处理，不如在这里一次做干净。
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退一步：截取第一个完整的 {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"无法解析 LLM 返回的 JSON: {text[:200]}") from exc
    raise LlmError(f"LLM 返回中未找到 JSON: {text[:200]}")


# ---------------------------------------------------------------- 提示词

TRIAGE_SYSTEM = """你是电商客服对话的质量审核员。判断一段对话是否值得进入知识库。

只输出 JSON，不要任何解释文字。"""

TRIAGE_USER = """判断下面这段客服对话是否包含可复用的知识。

判断标准：
- has_reusable_knowledge：是否包含对**其他用户也有用**的信息。
  只涉及某一笔具体订单的处理过程，不算。
- generalizable：0-1，能否泛化。具体到某个用户的特殊情况，分数低。
- is_outdated：是否明显过期（提到已下线的活动、停产的款式等）。
- knowledge_type：spec 商品规格 / logistics 物流 / aftersale 售后 /
  sizing 尺码 / promotion 活动 / usage 使用养护 / other

输出 JSON：
{{"has_reusable_knowledge": true, "generalizable": 0.8, "is_outdated": false,
  "knowledge_type": "spec", "quality_score": 0.85, "confidence": 0.9}}

对话：
{transcript}"""

EXTRACT_SYSTEM = """你是电商知识库的编辑。把客服对话提炼成独立的问答条目。

只输出 JSON，不要任何解释文字。"""

EXTRACT_USER = """把下面这段对话拆成若干条**独立的**问答对。

要求：
- 一段对话里可能有多个知识点，拆成多条。
- 问题要写成**其他用户也会这样问**的通用形式，不要带具体订单号或人名。
- 答案要完整、确定。原对话里客服说得含糊的（"应该""大概""我确认一下"），
  不要编造，把 confidence 打低，交给人工补写。
- 文本中的 <ORDER_NO> <PHONE> <AMOUNT> <DATE> 是占位符，保留原样。
- tags 从：材质、尺码、物流、售后、养护、活动、搭配 中选。

输出 JSON：
{{"items": [
  {{"question": "...", "answer": "...", "tags": ["材质"], "confidence": 0.9}}
]}}

对话：
{transcript}"""

STYLE_SYSTEM = """你是对话风格分析员。标注客服回复的语气特征，用于风格模型训练。

只输出 JSON，不要任何解释文字。"""

STYLE_USER = """标注下面客服回复的风格特征。

style_tags 从这些里选（可多选）：口语化、书面语、短句、长句、带表情、
带语气词、热情、克制、分点罗列、模板化

scene_tag 单选：售前咨询、尺码推荐、催单、售后安抚、议价、物流查询

输出 JSON：
{{"style_tags": ["口语化", "短句"], "scene_tag": "售前咨询"}}

客服回复：
{replies}"""


# ---------------------------------------------------------------- 配置


@dataclass
class Gate3Config:
    triage_model: str = "chat-light"
    """粗筛用便宜模型。100 万会话全量跑强模型，成本不可接受。"""
    extract_model: str = "chat-default"
    style_model: str = "chat-light"

    min_generalizable: float = 0.5
    min_quality: float = 0.5
    min_extract_confidence: float = 0.3
    """低于此值的抽取结果直接丢弃；介于它与 human_review_below 之间的进人工队列。"""
    human_review_below: float = 0.6
    """置信度低于此值推送人工审核（主动学习：人工只看模型拿不准的）。"""

    enable_style_tagging: bool = True
    uncertain_words: tuple[str, ...] = (
        "可能", "大概", "应该是", "估计", "不太清楚", "我确认一下", "我问一下",
    )
    promotion_valid_days: int = 7
    """活动类知识的默认有效期。不设期限的话客服会一直播报过期活动。"""


# ---------------------------------------------------------------- 执行


def _transcript(dlg: Dialogue, max_turns: int = 40) -> str:
    turns = dlg.turns[:max_turns]
    label = {"user": "用户", "agent": "客服", "system": "系统"}
    return "\n".join(f"{label.get(t.role, t.role)}：{t.content}" for t in turns)


def _has_uncertain(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def run(
    dialogues: list[Dialogue],
    llm: LlmClient,
    config: Gate3Config | None = None,
) -> tuple[list[KnowledgeItem], list[SftSample], GateStats]:
    """执行关卡③。

    Returns:
        (知识条目, 微调样本, 统计)。两个出口——知识进 RAG，样本进微调。
    """
    cfg = config or Gate3Config()
    stats = GateStats(gate="③ 模型清洗", input_count=len(dialogues))
    items: list[KnowledgeItem] = []
    samples: list[SftSample] = []

    for dlg in dialogues:
        transcript = _transcript(dlg)

        # ---- 1. 判定（粗筛，便宜模型）----
        try:
            verdict = llm.complete_json(
                model=cfg.triage_model,
                system=TRIAGE_SYSTEM,
                user=TRIAGE_USER.format(transcript=transcript),
            )
        except (LlmError, Exception) as exc:  # noqa: BLE001
            stats.drop(f"判定失败:{type(exc).__name__}")
            continue

        if not verdict.get("has_reusable_knowledge"):
            stats.drop("无可复用知识")
            continue
        if verdict.get("is_outdated"):
            stats.drop("已过期")
            continue
        generalizable = float(verdict.get("generalizable", 0))
        if generalizable < cfg.min_generalizable:
            stats.drop("不可泛化")
            continue
        quality = float(verdict.get("quality_score", 0))
        if quality < cfg.min_quality:
            stats.drop("质量分过低")
            continue

        try:
            ktype = KnowledgeType(verdict.get("knowledge_type", "other"))
        except ValueError:
            ktype = KnowledgeType.OTHER

        # ---- 2. 抽取（强模型）----
        try:
            extracted = llm.complete_json(
                model=cfg.extract_model,
                system=EXTRACT_SYSTEM,
                user=EXTRACT_USER.format(transcript=transcript),
            )
        except (LlmError, Exception) as exc:  # noqa: BLE001
            stats.drop(f"抽取失败:{type(exc).__name__}")
            continue

        raw_items = extracted.get("items") or []
        if not raw_items:
            stats.drop("未抽出问答对")
            continue

        kept_any = False
        for raw in raw_items:
            question = (raw.get("question") or "").strip()
            answer = (raw.get("answer") or "").strip()
            if not question or not answer:
                stats.drop("问答不完整")
                continue

            conf = float(raw.get("confidence", 0))
            if conf < cfg.min_extract_confidence:
                stats.drop("抽取置信度过低")
                continue

            # 含不确定表述的答案不能作为权威知识直接上线，压低置信度推人工改写
            if _has_uncertain(answer, cfg.uncertain_words):
                conf = min(conf, cfg.human_review_below - 0.01)
                stats.modify("含不确定表述→降置信度")

            valid_to = None
            if ktype is KnowledgeType.PROMOTION:
                # 活动类必须设有效期，这是防止播报过期活动的关键防线
                from datetime import datetime, timedelta

                valid_to = datetime.now() + timedelta(days=cfg.promotion_valid_days)
                stats.modify("活动类设置有效期")

            items.append(
                KnowledgeItem(
                    biz_type=BizType.QA,
                    title=question,
                    content=answer,
                    tags=list(raw.get("tags") or []),
                    product_ids=list(dlg.product_ids),
                    source=dlg.source_type,
                    source_ref=f"ods:{dlg.ods_id}" if dlg.ods_id else f"session:{dlg.session_no}",
                    quality_score=quality,
                    confidence=conf,
                    knowledge_type=ktype,
                    valid_to=valid_to,
                    # 一律 pending：未审核的知识绝不进索引
                    review_status=ReviewStatus.PENDING,
                )
            )
            kept_any = True

        if not kept_any:
            continue

        # ---- 3. 风格标注（供 M7 微调）----
        if cfg.enable_style_tagging:
            replies = [t.content for t in dlg.agent_turns()]
            if replies:
                try:
                    style = llm.complete_json(
                        model=cfg.style_model,
                        system=STYLE_SYSTEM,
                        user=STYLE_USER.format(replies="\n".join(replies[:10])),
                    )
                except Exception:  # noqa: BLE001
                    style = {}

                samples.append(
                    _build_sft_sample(dlg, style, quality)
                )

    stats.output_count = len(items)
    return items, samples, stats


def _build_sft_sample(
    dlg: Dialogue, style: dict[str, Any], quality: float
) -> SftSample:
    """把一段对话转成 SFT 样本。

    注意：system prompt **不放具体商品知识**。训练目标只有风格，
    如果把"这件是 100% 羊毛"塞进训练数据，模型会把它当成通用知识记住，
    回答其他商品时也说羊毛（见 docs/10-finetune.md 的失败模式）。
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": "你是电商店铺的客服。"}
    ]
    for t in dlg.turns:
        if t.role in ("user", "agent"):
            messages.append(
                {"role": "user" if t.role == "user" else "assistant", "content": t.content}
            )

    return SftSample(
        sample_type="sft",
        messages=messages,
        style_tags=list(style.get("style_tags") or []),
        scene_tag=style.get("scene_tag"),
        quality_score=quality,
        agent_id=dlg.agent_id,
        source=dlg.source_type,
        source_ref=f"session:{dlg.session_no}",
        review_status=ReviewStatus.PENDING,
    )


# ---------------------------------------------------------------- 测试替身


@dataclass
class FakeLlmClient:
    """确定性的 LLM 替身，让关卡③的编排逻辑可以离线测试。

    按 system 提示词区分三类调用，返回可预测的结果。
    通过 ``overrides`` 可以针对特定输入注入异常场景。
    """

    generalizable: float = 0.8
    quality: float = 0.85
    confidence: float = 0.9
    has_knowledge: bool = True
    is_outdated: bool = False
    knowledge_type: str = "spec"
    items_per_dialogue: int = 2
    raise_on: str | None = None
    """若对话文本中含此子串则抛异常，用于测试失败路径。"""
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete_json(self, *, model: str, system: str, user: str) -> dict[str, Any]:
        self.calls.append((model, system[:20]))
        if self.raise_on and self.raise_on in user:
            raise LlmError("注入的失败")

        if system.startswith("你是电商客服对话的质量审核员"):
            return {
                "has_reusable_knowledge": self.has_knowledge,
                "generalizable": self.generalizable,
                "is_outdated": self.is_outdated,
                "knowledge_type": self.knowledge_type,
                "quality_score": self.quality,
                "confidence": self.confidence,
            }

        if system.startswith("你是电商知识库的编辑"):
            return {
                "items": [
                    {
                        "question": f"合成问题{i}",
                        "answer": f"合成答案{i}",
                        "tags": ["材质"],
                        "confidence": self.confidence,
                    }
                    for i in range(self.items_per_dialogue)
                ]
            }

        return {"style_tags": ["口语化", "短句"], "scene_tag": "售前咨询"}
