"""转写结果的统一形状，以及喂给 ASR 的热词。

时间戳是这一层的全部意义。没有它，转写只是一段文本，切不出视频来；
有了它，"这句话在第 47 分 12 秒"才能变成一次 ffmpeg 调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class Sentence:
    """一句转写。时间是**毫秒**——秒会在切片时累积成可见的漂移。"""

    text: str
    begin_ms: int
    end_ms: int
    speaker: str = ""
    """说话人标识。带货直播里主播与助播的话价值完全不同：
    主播讲卖点，助播多在喊"三二一上链接"。没有这个字段就分不开。"""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.begin_ms)

    def shifted(self, delta_ms: int) -> "Sentence":
        return Sentence(self.text, self.begin_ms + delta_ms,
                        self.end_ms + delta_ms, self.speaker)


@dataclass
class Transcript:
    """一场直播的完整转写。"""

    sentences: list[Sentence] = field(default_factory=list)
    source: str = ""
    """来源视频/音频标识，用于溯源。"""

    @property
    def duration_ms(self) -> int:
        return max((s.end_ms for s in self.sentences), default=0)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.sentences)

    def slice(self, begin_ms: int, end_ms: int) -> list[Sentence]:
        """取一个时间区间内的句子。**按重叠取而不是按包含取**——

        按包含取的话，跨越边界的那一句会被整句丢掉，而分段边界恰恰
        经常落在句子中间，丢的又常常是承上启下的那一句。
        """
        return [s for s in self.sentences
                if s.end_ms > begin_ms and s.begin_ms < end_ms]

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]], source: str = "") -> "Transcript":
        """从 ASR 返回的句子数组构建。

        各家字段名不统一（``begin_time`` / ``start`` / ``bg``），
        在这里一次性归一，别让下游每个地方都猜一遍。
        """
        out: list[Sentence] = []
        for r in rows:
            text = str(r.get("text") or r.get("sentence") or "").strip()
            if not text:
                continue
            begin = r.get("begin_time", r.get("start", r.get("bg", 0)))
            end = r.get("end_time", r.get("end", r.get("ed", 0)))
            out.append(Sentence(
                text=text, begin_ms=int(begin or 0), end_ms=int(end or 0),
                speaker=str(r.get("speaker_id", r.get("speaker", "")) or ""),
            ))
        out.sort(key=lambda s: s.begin_ms)
        return cls(sentences=out, source=source)


# ---------------------------------------------------------------- 热词

#: 不值得当热词的字。太短的词会把热词表变成噪音——ASR 的热词是有权重的，
#: 塞进"款""色"这种字会让它到处误召回。
_MIN_HOTWORD = 2

#: 从商品名里剥掉的通用修饰。"米白色圆领宽松针织衫"真正需要纠正的是
#: "针织衫"和"米白"，"宽松"这类词 ASR 本来就认得。
_STOP_FRAGMENTS = re.compile(
    r"新款|新品|经典|复古|基础款|百搭|韩版|日系|法式|高腰|宽松|修身|加厚|轻薄|"
    r"重磅|手工|头层|正品|包邮|限时|特价"
)


def build_hotwords(
    products: Sequence[dict[str, Any]], extra: Sequence[str] = ()
) -> list[str]:
    """从商品表生成 ASR 热词表。

    **这是这条链路的闭环所在。** 主播嘴里的"莱赛尔""醋酸缎面""马丁靴"
    是通用 ASR 最容易听错的一类词——它们是专有名词，而且新词层出不穷。
    但这些词恰好全都写在商品表里。

    把商品名与别名喂给 ASR 当热词 → 转写更准 → 商品对齐更准 → 人工确认
    时回写的别名又变成下一次的热词。没有商品库的切片工具做不了这一步，
    这也是不去 vendor 现成工具的原因之一。
    """
    seen: dict[str, None] = {}

    def add(word: str) -> None:
        w = word.strip()
        if len(w) >= _MIN_HOTWORD and w not in seen:
            seen[w] = None

    for p in products:
        for key in ("short_name", "name"):
            v = str(p.get(key) or "").strip()
            if not v:
                continue
            add(v)
            # 长商品名整条当热词效果差——主播不会一字不差地念完
            # "米白色圆领宽松针织衫"。剥掉通用修饰后的碎片才是他会说的
            for frag in _STOP_FRAGMENTS.split(v):
                add(frag)
        for a in (p.get("alias") or []):
            add(str(a))
        for v in (p.get("attrs") or {}).values():
            # 材质是误识别重灾区："莱赛尔"会被听成"来赛尔"
            for frag in re.split(r"[\d%／/、，,\s]+", str(v)):
                add(frag)

    for w in extra:
        add(w)
    return list(seen)
