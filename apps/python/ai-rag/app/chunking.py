"""切分策略。

**核心原则：能不切就不切。**

电商知识的天然单元是"一个问题一个答案"，机械按长度切分只会制造噪声——
把问题和答案切散之后，检索命中的可能是半个答案。
只有政策文档这类长文本才真正需要切分。

按 biz_type 分策略（见 docs/04-rag-knowledge.md）：

======== ============================ =========================
biz_type 内容形态                     策略
======== ============================ =========================
qa       问答对                       不切
spec     商品结构化属性               不切
image    VLM 生成的描述               不切
video    VLM 描述 + ASR               不切
script   直播口播话术                 语义段落，200-400 字，重叠 50
policy   售后政策、活动规则           按标题层级递归，≤500 字，保留标题路径
======== ============================ =========================
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 不切分的类型：一条就是一个语义单元
ATOMIC_BIZ_TYPES = frozenset({"qa", "spec", "faq", "image", "video"})

# 中文句子边界
_SENT_END = re.compile(r"(?<=[。！？；!?;])\s*")
# Markdown 标题
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
# 条款编号：一、 1. 1.1 （一）
_CLAUSE = re.compile(r"^\s*(?:[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.]|（[一二三四五六七八九十\d]+）)\s*", re.M)


@dataclass(frozen=True)
class Chunk:
    """一个切片。"""

    seq: int
    text: str
    heading_path: str = ""
    """标题路径，如 "退换货政策 > 七天无理由"。作为检索文本的前缀，
    让切片脱离上下文后仍然可理解——不带标题的"需在签收后7日内提出"
    检索时无法判断说的是退货还是换货。"""

    @property
    def indexed_text(self) -> str:
        """真正进向量库的文本。"""
        return f"{self.heading_path}\n{self.text}" if self.heading_path else self.text


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_END.split(text) if s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def chunk_script(text: str, *, max_chars: int = 400, overlap: int = 50) -> list[Chunk]:
    """直播口播话术：按句子聚合到目标长度，段间重叠。

    重叠是必要的——主播一句话的卖点常跨句，硬切会把"这个克重" 和
    "秋冬单穿就够" 分到两段，两边都不完整。
    """
    sentences = _split_sentences(text)
    if not sentences:
        return []

    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(Chunk(seq=len(chunks), text="".join(buf)))
            # 从尾部回退 overlap 个字符作为下一段的开头
            tail, kept = [], 0
            for s in reversed(buf):
                if kept >= overlap:
                    break
                tail.insert(0, s)
                kept += len(s)
            buf, size = tail, kept

    for sent in sentences:
        if size + len(sent) > max_chars and buf:
            flush()
        buf.append(sent)
        size += len(sent)

    if buf:
        chunks.append(Chunk(seq=len(chunks), text="".join(buf)))
    return chunks


def chunk_policy(text: str, *, max_chars: int = 500) -> list[Chunk]:
    """政策文档：按标题层级递归切分，保留标题路径。"""
    sections = _split_by_heading(text)
    chunks: list[Chunk] = []

    for path, body in sections:
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chars:
            chunks.append(Chunk(seq=len(chunks), text=body, heading_path=path))
            continue

        # 超长段落先按条款编号切，再按句子兜底
        for piece in _split_by_clause(body, max_chars):
            chunks.append(Chunk(seq=len(chunks), text=piece, heading_path=path))

    return chunks


def _split_by_heading(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切成 (标题路径, 正文) 对。"""
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []
    # 标题前的引言
    if matches[0].start() > 0:
        lead = text[: matches[0].start()].strip()
        if lead:
            sections.append(("", lead))

    stack: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((path, text[m.end() : end]))

    return sections


def _split_by_clause(text: str, max_chars: int) -> list[str]:
    """按条款编号切，超长的再按句子聚合。"""
    positions = [m.start() for m in _CLAUSE.finditer(text)]
    blocks: list[str]
    if len(positions) > 1:
        bounds = positions + [len(text)]
        blocks = [text[bounds[i] : bounds[i + 1]].strip() for i in range(len(positions))]
    else:
        blocks = [text]

    out: list[str] = []
    for block in blocks:
        if len(block) <= max_chars:
            if block:
                out.append(block)
            continue
        buf, size = [], 0
        for sent in _split_sentences(block):
            if size + len(sent) > max_chars and buf:
                out.append("".join(buf))
                buf, size = [], 0
            buf.append(sent)
            size += len(sent)
        if buf:
            out.append("".join(buf))
    return out


def chunk(biz_type: str, text: str, *, title: str | None = None) -> list[Chunk]:
    """按 biz_type 选择切分策略。

    Args:
        title: 对 qa 类型，标题（问题）会与正文拼接后索引——
            用户的提问更接近"问题"而不是"答案"，只索引答案会显著降低召回。
    """
    text = (text or "").strip()
    if not text:
        return []

    if biz_type in ATOMIC_BIZ_TYPES:
        body = f"{title}\n{text}" if title else text
        return [Chunk(seq=0, text=body)]

    if biz_type == "script":
        return chunk_script(text)

    if biz_type == "policy":
        return chunk_policy(text)

    # 未知类型退化为不切分，宁可块大也不要切错
    return [Chunk(seq=0, text=text)]
