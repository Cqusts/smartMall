"""中文 BM25。

**为什么必须有这一路**：电商查询里大量是精确关键词——商品型号（A1234）、
专有名词（莱赛尔）、活动名（双十一满减）。纯向量检索对这类精确匹配的
召回率明显偏低，BM25 是必需的补充（见 docs/04-rag-knowledge.md）。

分词用**字符二元组**而非 jieba：
中文 BM25 用 bigram 是成熟做法，效果与分词接近，但不需要多一个依赖，
也不会因为词典里没有「莱赛尔」这类新词而切错。代价是索引大一些——
在几万条知识的量级下无所谓。

线上换成 Milvus 后，这一路由 Milvus 2.5 的原生 BM25 承担，本模块只用于
无 Milvus 的开发环境。两者的 k1/b 参数保持一致，使行为可比。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# 与 milvus_store.MilvusConfig 保持一致，换后端时排序行为可比
K1 = 1.2
B = 0.75

_NON_TOKEN = re.compile(r"[\s　]+")
_ASCII_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list[str]:
    """中文取字符二元组，英文数字按词切。

    ``<ORDER_NO>`` 这类占位符会被当作 ASCII 词整体保留——
    它们是清洗后知识里的有效标记，切碎了会污染倒排表。
    """
    text = _NON_TOKEN.sub("", text.lower())
    tokens: list[str] = []

    # 占位符整体保留
    placeholders = re.findall(r"<[a-z_]+>", text)
    tokens.extend(placeholders)
    text = re.sub(r"<[a-z_]+>", " ", text)

    tokens.extend(_ASCII_WORD.findall(text))

    cjk = "".join(_CJK.findall(text))
    if len(cjk) == 1:
        tokens.append(cjk)
    else:
        tokens.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))

    return tokens


def idf(total: int, df: int) -> float:
    """逆文档频率。加一平滑，避免 df 接近 total 时出现负 idf。

    抽成模块级函数是刻意的：:class:`Bm25Index` 与 :class:`LexicalStats`
    都要用它，而 ``lexical_support_min`` 这个阈值是照着这个公式量出来的。
    两边各写一份的话，哪天有人只改了其中一处，同一个阈值在两个检索后端
    下就是两个含义——而且不会有任何报错。
    """
    return math.log(1 + (total - df + 0.5) / (df + 0.5))


def _coverage_of(
    q_terms: Mapping[str, int],
    q_idf: Mapping[str, float],
    total_idf: float,
    doc_terms: Mapping[str, int],
) -> float:
    """查询里的信息量，这篇文档命中了多少，0~1。

    :class:`Bm25Index.coverage` 与 :class:`LexicalStats.coverage` 的公共内核。
    """
    if total_idf <= 0:
        return 0.0
    matched = sum(q_idf[t] * n for t, n in q_terms.items() if doc_terms.get(t))
    return matched / total_idf


@dataclass
class LexicalStats:
    """语料级词汇统计：只有 ``term -> df`` 和文档总数。

    **为什么不直接用 Bm25Index。** ``Bm25Index`` 存了每篇文档的词频表
    （``doc_tokens``），内存是 O(语料)。接上 Milvus 之后召回由 Milvus 做，
    真正需要算覆盖率的只有它返回的那几十条命中——而这些命中的文本本来
    就随结果一起回来了。所以这里只需要一张 ``term -> df`` 表，
    内存是 O(词表)，与语料规模基本脱钩。

    **为什么非要有这个类。** Milvus 的 ``hybrid_search`` 只返回融合后的
    RRF 分，给不出分路分数，更给不出词汇覆盖率。而
    ``graph.has_lexical_support`` 正是靠覆盖率决定「转人工」还是「澄清」。
    照直接接，``lexical_overlap`` 缺失会让那道闸门退回 ``bm25 > 0``——
    见 :meth:`Bm25Index.coverage` 的注释，那个判据近乎恒真、形同虚设。
    换句话说：**不做这一步，接 Milvus 等于把已有的闸门拆了。**
    """

    total: int = 0
    df: Counter[str] = field(default_factory=Counter)

    @classmethod
    def build(cls, texts: Iterable[str]) -> "LexicalStats":
        stats = cls()
        for text in texts:
            stats.add(text)
        return stats

    def add(self, text: str) -> None:
        self.total += 1
        self.df.update(set(tokenize(text)))

    @property
    def size(self) -> int:
        return self.total

    def coverage(self, query: str, text: str) -> float:
        """单篇文档对查询的词汇覆盖率。

        语义与 :meth:`Bm25Index.coverage` 完全一致——同一语料、同一查询、
        同一文档，两者必须给出同一个数（见 ``test_lexical_stats``）。
        """
        q_terms = Counter(tokenize(query))
        if not q_terms or not self.total:
            return 0.0
        q_idf = {t: idf(self.total, self.df.get(t, 0)) for t in q_terms}
        total_idf = sum(q_idf[t] * n for t, n in q_terms.items())
        return _coverage_of(q_terms, q_idf, total_idf, Counter(tokenize(text)))


@dataclass
class Bm25Index:
    """内存 BM25 索引。几万条以内够用。"""

    k1: float = K1
    b: float = B
    doc_ids: list[int] = field(default_factory=list)
    doc_tokens: list[Counter[str]] = field(default_factory=list)
    doc_len: list[int] = field(default_factory=list)
    df: Counter[str] = field(default_factory=Counter)
    avg_len: float = 0.0

    @classmethod
    def build(cls, docs: list[tuple[int, str]]) -> "Bm25Index":
        idx = cls()
        for doc_id, text in docs:
            tokens = tokenize(text)
            counts = Counter(tokens)
            idx.doc_ids.append(doc_id)
            idx.doc_tokens.append(counts)
            idx.doc_len.append(len(tokens))
            idx.df.update(counts.keys())
        idx.avg_len = (sum(idx.doc_len) / len(idx.doc_len)) if idx.doc_len else 0.0
        return idx

    @property
    def size(self) -> int:
        return len(self.doc_ids)

    def _idf(self, term: str) -> float:
        return idf(self.size, self.df.get(term, 0))

    def coverage(self, query: str) -> dict[int, float]:
        """每篇文档命中了查询多少**信息量**，取值 0~1。

        BM25 分数本身回答不了"到底匹配上了没有"。bigram 分词下，
        「你们支持花呗分期吗」和「支持的快递：默认发顺丰」共享一个
        ``支持``，分数就有 1.49——而知识库里关于花呗一个字都没有。
        于是 ``bm25_score > 0`` 这个判据近乎恒真，本该拦住"装作有"的
        那道闸门形同虚设（实测：四条知识库根本没有的问题全部走到了澄清）。

        换一个问法：**查询里那些有区分度的词，匹配上了吗。**
        用 IDF 加权——``支持``在语料里到处都是，IDF 低，匹配上不说明
        什么；``花呗``只要出现过就 IDF 高，匹配上才是真的沾边。
        分母是查询全部词的 IDF 之和，所以结果与查询长短无关。

        语料里根本不存在的词（df=0）也计入分母：它们正是这个查询里
        信息量最大的部分，「问了但库里没有」必须体现为覆盖率低。
        """
        q_terms = Counter(tokenize(query))
        if not q_terms:
            return {}
        q_idf = {t: self._idf(t) for t in q_terms}
        total = sum(q_idf[t] * n for t, n in q_terms.items())
        if total <= 0:
            return {}

        out: dict[int, float] = {}
        for i, counts in enumerate(self.doc_tokens):
            score = _coverage_of(q_terms, q_idf, total, counts)
            if score > 0:
                out[self.doc_ids[i]] = score
        return out

    def search(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        """返回 ``[(doc_id, score)]``，按分数降序。"""
        if not self.size:
            return []

        q_terms = Counter(tokenize(query))
        if not q_terms:
            return []

        idf = {t: self._idf(t) for t in q_terms}
        scored: list[tuple[int, float]] = []

        for i, counts in enumerate(self.doc_tokens):
            length = self.doc_len[i] or 1
            score = 0.0
            for term, qn in q_terms.items():
                tf = counts.get(term)
                if not tf:
                    continue
                denom = tf + self.k1 * (
                    1 - self.b + self.b * length / (self.avg_len or 1)
                )
                score += idf[term] * (tf * (self.k1 + 1)) / denom * qn
            if score > 0:
                scored.append((self.doc_ids[i], score))

        scored.sort(key=lambda kv: -kv[1])
        return scored[:top_k]
