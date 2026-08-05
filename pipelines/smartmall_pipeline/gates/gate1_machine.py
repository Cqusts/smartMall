"""关卡① 机器清洗。

纯规则、无模型、可大批量并行。对应 Data-Juicer 的算子集合
（配方见 pipelines/recipes/dialogue_clean_v1.yaml）——本模块是同一套规则的
Python 实现，用于单元测试与不依赖 Data-Juicer 的轻量运行。

淘汰项：近重复、过短/过长、非中文、乱码、敏感词、外链。
修改项：PII 脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Dialogue, GateStats
from . import pii

# ---------------------------------------------------------------- MinHash 去重


def _shingles(text: str, k: int = 4) -> set[str]:
    """字符 k-gram。中文没有天然词边界，字符 shingle 比分词更稳。"""
    cleaned = re.sub(r"\s+", "", text)
    if len(cleaned) < k:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + k] for i in range(len(cleaned) - k + 1)}


def _hash_family(n: int) -> list[tuple[int, int]]:
    """生成 n 组 (a, b) 系数，用于 h(x) = (a*x + b) mod P。"""
    rng = __import__("random").Random(0xC0FFEE)  # 固定种子：去重结果必须可复现
    return [(rng.randrange(1, _PRIME), rng.randrange(0, _PRIME)) for _ in range(n)]


_PRIME = (1 << 61) - 1
_NUM_PERM = 64
_COEFFS = _hash_family(_NUM_PERM)


def minhash_signature(text: str) -> tuple[int, ...]:
    """计算 MinHash 签名。"""
    sh = _shingles(text)
    if not sh:
        return tuple([0] * _NUM_PERM)
    hashed = [hash(s) & 0xFFFFFFFF for s in sh]
    return tuple(
        min((a * h + b) % _PRIME for h in hashed) for a, b in _COEFFS
    )


def jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    """由签名估算 Jaccard 相似度。"""
    if not sig_a or not sig_b:
        return 0.0
    same = sum(1 for x, y in zip(sig_a, sig_b) if x == y)
    return same / len(sig_a)


# ---------------------------------------------------------------- 过滤规则

CJK_RE = re.compile(r"[一-鿿]")
# 私域引流与外链：这类内容不该进知识库
URL_RE = re.compile(r"(https?://|www\.|微信号?[:：]?\s*[\w-]{6,}|加\s*(微信|VX|vx|威信))")
# 乱码：连续的替换字符或控制字符（非原始串，转义序列才会被解析）
GARBLED_RE = re.compile("[\ufffd\x00-\x08\x0b-\x0c\x0e-\x1f]{2,}")

DEFAULT_FLAGGED_WORDS = frozenset({
    "淘宝", "拼多多", "京东自营", "闲鱼",   # 竞品/引流
    "代购", "高仿", "A货", "原单",           # 违规品类
})


@dataclass
class Gate1Config:
    min_chars: int = 10
    max_chars: int = 8000
    min_turns: int = 2
    min_cjk_ratio: float = 0.5
    """中文字符占比下限。JDDC 是中文语料，占比过低通常是乱码或外文噪声。"""
    dedup_threshold: float = 0.85
    """MinHash Jaccard 阈值。0.85 能抓到"多加一句寒暄"的近重复。"""
    flagged_words: frozenset[str] = field(default_factory=lambda: DEFAULT_FLAGGED_WORDS)
    mask_pii: bool = True


def _cjk_ratio(text: str) -> float:
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return 0.0
    return len(CJK_RE.findall(stripped)) / len(stripped)


def run(
    dialogues: list[Dialogue],
    config: Gate1Config | None = None,
) -> tuple[list[Dialogue], GateStats]:
    """执行关卡①。

    返回保留下来的会话与统计。会话对象会被就地修改（PII 脱敏），
    调用方若需保留原始对象请先 deep copy——但正常流程中原始数据已在 ODS，
    这里的修改是期望行为。
    """
    cfg = config or Gate1Config()
    stats = GateStats(gate="① 机器清洗", input_count=len(dialogues))
    kept: list[Dialogue] = []
    signatures: list[tuple[int, ...]] = []

    for dlg in dialogues:
        # 用纯内容而非带角色前缀的全文：前缀是 ASCII，会污染中文占比与长度统计
        text = dlg.content_text

        if dlg.turn_count < cfg.min_turns:
            stats.drop("轮次过少")
            continue

        n_chars = len(re.sub(r"\s", "", text))
        if n_chars < cfg.min_chars:
            stats.drop("文本过短")
            continue
        if n_chars > cfg.max_chars:
            stats.drop("文本过长")
            continue

        if _cjk_ratio(text) < cfg.min_cjk_ratio:
            stats.drop("非中文/乱码")
            continue

        if GARBLED_RE.search(text):
            stats.drop("控制字符乱码")
            continue

        if URL_RE.search(text):
            stats.drop("含外链/引流")
            continue

        hit_word = next((w for w in cfg.flagged_words if w in text), None)
        if hit_word:
            stats.drop("敏感词/竞品词")
            continue

        # 近重复检测。O(n²) 在批内是可接受的（单批 ≤ 1 万），
        # 全量去重由 Data-Juicer 的 LSH 实现承担
        sig = minhash_signature(text)
        if any(jaccard(sig, prev) >= cfg.dedup_threshold for prev in signatures):
            stats.drop("近重复")
            continue

        if cfg.mask_pii:
            for turn in dlg.turns:
                result = pii.mask(turn.content)
                if result.changed:
                    # 原文留给 raw_content，落库时加密
                    turn.raw_content = turn.content
                    turn.content = result.text
                    for kind, n in result.hits.items():
                        stats.modify(f"PII脱敏:{kind}", n)

        signatures.append(sig)
        kept.append(dlg)

    stats.output_count = len(kept)
    return kept, stats
