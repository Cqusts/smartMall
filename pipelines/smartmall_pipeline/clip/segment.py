"""语义分段 + 商品对齐 + 双出口。

**这一段是市面上的切片工具做不了的部分**，因为它们没有商品库。

FunClip 按关键词或说话人切，HotClip 按高光信号切——两者切出来的都是
"一段好看的视频"。而带货直播要的是"**这一段在讲哪个商品**"：切片要挂到
商品详情页，话术要按商品进知识库，对不上商品的切片没有用处。

商品对齐做完还要**回写别名**：主播管"米白色圆领宽松针织衫"叫"这件米白"，
人工确认一次之后，"米白"就该成为下一场直播的 ASR 热词。
没有商品库就没有这个闭环。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..gates.gate3_model import LlmClient, LlmError, parse_json_lenient
from ..models import (
    BizType, GateStats, KnowledgeItem, KnowledgeType, Modality, ReviewStatus,
)
from .transcript import Sentence, Transcript

# ---------------------------------------------------------------- 提示词

SEGMENT_SYSTEM = """你是带货直播的分段助手。只输出 json，不要解释。"""

SEGMENT_USER = """把下面这段直播转写切成若干个**讲解片段**。

一个片段 = 主播完整讲完一件商品的一个方面。切换商品、或者从讲卖点转到
讲优惠，都是新片段的开始。

在售商品（片段必须对到其中之一，对不上填 null）：
{catalog}

判断要点：

- **"三二一上链接""还有最后三十单"这类催单话术不是讲解片段**，
  它们没有可复用的信息，标 ``kind: "urge"``，会被丢掉。
- 主播回答观众提问（"有人问会不会起球"）是**最有价值**的片段，
  标 ``kind: "qa"``——那是真实的客服知识。
- 单纯介绍卖点标 ``kind: "feature"``。
- 一个片段至少覆盖一句话，不要切得比一句话还碎。

转写（每行前面是行号，用行号表示范围，闭区间）：
{lines}

输出 json：
{{"segments": [
  {{"begin_line": 0, "end_line": 2, "kind": "feature", "product_id": 9001,
    "topic": "米白针织衫的材质与克重",
    "script": "把主播这段话整理成通顺的一段，去掉口头禅与重复"}}
]}}"""


# ---------------------------------------------------------------- 结构


@dataclass
class Segment:
    begin_ms: int
    end_ms: int
    kind: str = "feature"
    """feature 卖点 / qa 答疑 / urge 催单 / other。urge 会被丢掉。"""
    product_id: int | None = None
    topic: str = ""
    script: str = ""
    """规整后的口播话术。这是进知识库的那一份，不是原始转写——
    原始转写里有大量"那个""对吧""家人们"，直接入库会污染检索。"""
    raw_text: str = ""
    matched_alias: str = ""
    """主播实际用的叫法。人工确认后回写进商品别名，成为下一场的热词。"""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.begin_ms)

    @property
    def is_useful(self) -> bool:
        return self.kind in ("feature", "qa") and bool(self.script)


# ---------------------------------------------------------------- 分段

#: 一次喂给模型的行数。太长模型会漏掉中间的商品切换，
#: 太短则跨窗口的片段会被切断——重叠正是为后者准备的
WINDOW_LINES = 60
OVERLAP_LINES = 8


def _catalog(products: Sequence[dict[str, Any]]) -> str:
    out = []
    for p in products:
        alias = "、".join(str(a) for a in (p.get("alias") or []))
        out.append(f"- {p['id']}：{p.get('short_name') or p.get('name')}"
                   + (f"（别名：{alias}）" if alias else ""))
    return "\n".join(out) or "（没有在售商品）"


def segment_transcript(
    llm: LlmClient, transcript: Transcript, products: Sequence[dict[str, Any]],
    *, model: str = "chat-default", window: int = WINDOW_LINES,
    overlap: int = OVERLAP_LINES,
) -> tuple[list[Segment], GateStats]:
    """滑窗分段。

    **窗口之间要重叠。** 一个讲解片段被窗口边界劈开时，两半都会因为
    "没头没尾"被判成 other 而丢掉——丢的还常常是承上启下的那段。
    重叠让边界附近的片段至少在一个窗口里是完整的，代价是要去重。
    """
    stats = GateStats(gate="⑦直播分段", input_count=len(transcript.sentences),
                      input_unit="句", output_unit="片段")
    sents = transcript.sentences
    if not sents:
        return [], stats

    catalog = _catalog(products)
    raw: list[Segment] = []
    start = 0
    while start < len(sents):
        chunk = sents[start : start + window]
        lines = "\n".join(f"{start + i}. {s.text}" for i, s in enumerate(chunk))
        try:
            data = llm.complete_json(
                model=model, system=SEGMENT_SYSTEM,
                user=SEGMENT_USER.format(catalog=catalog, lines=lines),
            )
        except LlmError as exc:
            # 一个窗口失败不该毁掉整场。**但也不能当成"这段没内容"**——
            # 记进 dropped，否则漏斗会把模型故障显示成主播没说话
            n = stats.dropped.get("分段调用失败", 0) + 1
            stats.dropped["分段调用失败"] = n
            if n == 1:
                # **第一条原文必须打出来。** 只记个数的话，命令行上看到的
                # 是"分段 0 段"，而真正的原因（模型名不对被拒 400）
                # 藏在一个计数器里——实测就是这么绕进去的
                print(f"  ✗ 分段调用失败：{exc}")
            start += max(1, window - overlap)
            continue

        for item in data.get("segments") or []:
            seg = _to_segment(item, sents)
            if seg:
                raw.append(seg)
        start += max(1, window - overlap)

    merged = _dedup(raw)
    stats.output_count = len(merged)
    for s in raw:
        if s.kind == "urge":
            stats.dropped["催单话术"] = stats.dropped.get("催单话术", 0) + 1
    return merged, stats


def _to_segment(item: dict, sents: list[Sentence]) -> Segment | None:
    try:
        b = int(item.get("begin_line", -1))
        e = int(item.get("end_line", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= b < len(sents)) or not (b <= e < len(sents)):
        # 模型报了越界的行号。丢掉这一条而不是整窗——它经常只是
        # 把最后一段的 end_line 多写了一行
        e = min(max(e, b), len(sents) - 1)
        if not (0 <= b <= e < len(sents)):
            return None

    pid = item.get("product_id")
    return Segment(
        begin_ms=sents[b].begin_ms, end_ms=sents[e].end_ms,
        kind=str(item.get("kind") or "other"),
        product_id=int(pid) if isinstance(pid, (int, str)) and str(pid).isdigit()
        else None,
        topic=str(item.get("topic") or "").strip(),
        script=str(item.get("script") or "").strip(),
        raw_text="".join(s.text for s in sents[b : e + 1]),
    )


def _dedup(segs: list[Segment]) -> list[Segment]:
    """重叠窗口会产出同一个片段两次。按时间区间重叠度合并。

    留下**话术更长**的那份：被窗口边界截断的那半，整理出来的 script
    通常明显更短，这个判据比"留先出现的"稳。
    """
    out: list[Segment] = []
    for s in sorted(segs, key=lambda x: (x.begin_ms, -x.duration_ms)):
        hit = None
        for kept in out:
            overlap = min(kept.end_ms, s.end_ms) - max(kept.begin_ms, s.begin_ms)
            if overlap > 0 and overlap > 0.5 * min(kept.duration_ms or 1,
                                                   s.duration_ms or 1):
                hit = kept
                break
        if hit is None:
            out.append(s)
        elif len(s.script) > len(hit.script):
            out[out.index(hit)] = s
    return out


# ---------------------------------------------------------------- 商品对齐


def align_products(
    segments: Sequence[Segment], products: Sequence[dict[str, Any]]
) -> tuple[list[Segment], list[Segment]]:
    """把没对上商品的片段用文本再兜一次，返回 ``(已对齐, 待人工确认)``。

    模型分段时已经给过 product_id，但它看的是一个窗口的上下文，
    经常在商品切换处判错。这里用主播实际说出口的**叫法**再校一遍——
    命中了就记下那个叫法（``matched_alias``），人工确认后回写成别名，
    下一场直播它就是 ASR 热词。这就是闭环。
    """
    lookup: list[tuple[str, int]] = []
    for p in products:
        for name in filter(None, [p.get("short_name"), p.get("name"),
                                  *(p.get("alias") or [])]):
            lookup.append((str(name), int(p["id"])))
    # 长的叫法优先：「马丁短靴」比「靴」更可信
    lookup.sort(key=lambda kv: -len(kv[0]))

    aligned, pending = [], []
    for seg in segments:
        text = seg.raw_text or seg.script
        for name, pid in lookup:
            if name and name in text:
                if seg.product_id != pid:
                    seg.matched_alias = name
                seg.product_id = pid
                break
        (aligned if seg.product_id else pending).append(seg)
    return aligned, pending


# ---------------------------------------------------------------- 出口


def to_knowledge_items(
    segments: Sequence[Segment], *, source_ref: str,
    category_of: dict[int, int] | None = None,
) -> list[KnowledgeItem]:
    """**第二个出口：口播话术进知识库。**

    这是整条链路接进这套体系的理由。主播讲了两小时"这个面料起球吗、
    掉色吗、薄不薄"，那是最真实的商品知识，而客服知识库现在全是通用问答。

    ``biz_type=script`` 而不是 qa：它是口播话术不是问答对，
    分错了覆盖度矩阵会算进错误的格子。审核状态一律 pending——
    转写有错字、模型整理有偏差，这条路上没有任何东西印证过它，
    和商品图那边"颜色能对上 SKU"的情况不同。
    """
    category_of = category_of or {}
    items = []
    for i, seg in enumerate(segments):
        if not seg.is_useful:
            continue
        items.append(KnowledgeItem(
            biz_type=BizType.SCRIPT,
            modality=Modality.TEXT,
            title=seg.topic or f"直播讲解片段 {i + 1}",
            content=seg.script,
            summary=seg.topic or None,
            product_ids=[seg.product_id] if seg.product_id else [],
            category_id=category_of.get(seg.product_id or -1),
            tags=["直播", "口播话术"] + (["答疑"] if seg.kind == "qa" else []),
            source="live_clip",
            source_ref=f"{source_ref}#{seg.begin_ms}-{seg.end_ms}",
            confidence=0.7,
            quality_score=0.7,
            # 没有任何权威数据印证过它——转写有错字，整理有偏差。
            # 和商品图那边"颜色能对上 SKU"的情况不一样，这里只能等人看
            review_status=ReviewStatus.PENDING,
            knowledge_type=(KnowledgeType.SPEC if seg.kind == "feature"
                            else KnowledgeType.OTHER),
        ))
    return items
