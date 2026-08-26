"""本地检索：BM25、向量打包、混合检索与过滤。

不连数据库、不调 API——BM25 与融合是纯逻辑，向量用确定性的假实现，
因此这些断言在 CI 里能跑。
"""

from __future__ import annotations

import math

import pytest

from smartmall_pipeline.rag import bm25
from smartmall_pipeline.rag.embedding import l2_normalize
from smartmall_pipeline.rag.store import (
    LocalVectorStore,
    _Row,
    dot,
    pack_vector,
    unpack_vector,
)


# ---------------------------------------------------------------- 分词


class TestTokenize:
    def test_chinese_bigrams(self):
        toks = bm25.tokenize("羊毛针织衫")
        assert "羊毛" in toks and "毛针" in toks and "针织" in toks

    def test_single_char_chinese(self):
        assert bm25.tokenize("衣") == ["衣"]

    def test_ascii_words_kept_whole(self):
        toks = bm25.tokenize("型号A1234")
        assert "a1234" in toks

    def test_placeholders_kept_whole(self):
        """占位符是清洗后的有效标记，切碎会污染倒排表。"""
        toks = bm25.tokenize("您的订单<order_no>已发货")
        assert "<order_no>" in toks
        # 不应出现被切碎的片段
        assert not any(t.startswith("<o") and t != "<order_no>" for t in toks)

    def test_empty(self):
        assert bm25.tokenize("") == []
        assert bm25.tokenize("   ") == []


# ---------------------------------------------------------------- BM25


class TestBm25:
    @pytest.fixture
    def index(self):
        return bm25.Bm25Index.build([
            (0, "这件针织衫是100%羊毛的，克重320g"),
            (1, "发什么快递？默认中通，江浙沪两三天到"),
            (2, "针织衫会起球吗？做了抗起球处理"),
            (3, "退货运费谁承担？质量问题我们承担"),
        ])

    def test_exact_keyword_wins(self):
        """BM25 存在的意义：精确关键词匹配，纯向量对这类召回率偏低。"""
        idx = bm25.Bm25Index.build([
            (0, "这款采用莱赛尔纤维面料"),
            (1, "这款是纯棉面料的"),
        ])
        hits = idx.search("莱赛尔")
        assert hits and hits[0][0] == 0

    def test_relevant_doc_ranks_first(self, index):
        hits = index.search("针织衫起球")
        assert hits
        assert hits[0][0] in (0, 2)

    def test_unrelated_query_returns_nothing(self, index):
        assert index.search("量子力学薛定谔") == []

    def test_empty_index(self):
        assert bm25.Bm25Index.build([]).search("任何问题") == []

    def test_empty_query(self, index):
        assert index.search("") == []

    def test_idf_is_non_negative(self):
        """一个词出现在所有文档里时 idf 不应为负，否则排序会颠倒。"""
        idx = bm25.Bm25Index.build([(i, "针织衫很好") for i in range(5)])
        assert idx._idf("针织") >= 0

    def test_top_k_respected(self, index):
        assert len(index.search("的", top_k=2)) <= 2


class TestLexicalCoverage:
    """"有 BM25 分"和"真的匹配上了"是两回事。

    bigram 分词下，随便一个common bigram 就能让毫不相干的查询拿到分。
    实测代价：Agent 用 ``bm25_score > 0`` 当"知识库里有没有"的判据，
    四条库里根本没有的问题（花呗/以旧换新/会员等级/定制刺绣）全部
    因此走到澄清而不是转人工——那道闸门本来就是为拦住它们设的。
    """

    @pytest.fixture
    def index(self):
        return bm25.Bm25Index.build([
            (0, "七天无理由退换：签收后七天内可申请退换"),
            (1, "发货时效：现货商品 48 小时内发出"),
            (2, "支持的快递：默认发顺丰，偏远地区改发中通"),
            (3, "退货运费：非质量问题由买家承担"),
        ])

    def test_a_common_bigram_scores_but_does_not_cover(self, index):
        """**这条是这个指标存在的理由。**

        "你们支持花呗分期吗"和"支持的快递…"共享一个``支持``，
        BM25 就有分——而知识库里关于花呗一个字都没有。
        """
        q = "你们支持花呗分期吗"
        assert index.search(q), "BM25 确实给了分"
        assert max(index.coverage(q).values()) < 0.15, "但覆盖率必须很低"

    def test_a_real_match_covers_a_lot(self, index):
        assert max(index.coverage("七天无理由能退吗").values()) > 0.3

    def test_words_absent_from_the_corpus_still_count_against_coverage(self):
        """库里根本没有的词要计入分母——它们正是查询里信息量最大的部分。
        只按"匹配上的词"归一化的话，问什么都是满分。"""
        idx = bm25.Bm25Index.build([(0, "退货运费由买家承担")])
        only_common = idx.coverage("退货运费")
        with_unknown = idx.coverage("退货运费能用花呗分期付吗")
        assert max(only_common.values()) > max(with_unknown.values())

    def test_coverage_is_independent_of_query_length(self):
        """归一化之后，长查询不该天然吃亏。"""
        idx = bm25.Bm25Index.build([(0, "针织衫会起球吗？做了抗起球处理")])
        short = max(idx.coverage("起球").values())
        assert 0.9 <= short <= 1.0

    def test_unmatched_docs_are_absent(self, index):
        assert 1 not in index.coverage("七天无理由退换")

    def test_empty_query_and_index(self, index):
        assert index.coverage("") == {}
        assert bm25.Bm25Index.build([]).coverage("任何问题") == {}


class TestLexicalStats:
    """接 Milvus 之后算词汇覆盖率的那条路。

    Milvus 的 ``hybrid_search`` 只返回融合后的 RRF 分——给不出分路分数，
    更给不出词汇覆盖率。而 ``graph.has_lexical_support`` 正是靠覆盖率
    决定「转人工」还是「澄清」。所以换到 Milvus 后台，这个数得由
    :class:`LexicalStats` 从语料级 IDF 表 + 命中自身的文本补出来。
    """

    DOCS = [
        "七天无理由退换：签收后七天内可申请退换",
        "发货时效：现货商品 48 小时内发出",
        "支持的快递：默认发顺丰，偏远地区改发中通",
        "退货运费：非质量问题由买家承担",
    ]

    @pytest.fixture
    def stats(self):
        return bm25.LexicalStats.build(self.DOCS)

    @pytest.fixture
    def index(self):
        return bm25.Bm25Index.build(list(enumerate(self.DOCS)))

    @pytest.mark.parametrize("query", [
        "七天无理由能退吗",
        "你们支持花呗分期吗",
        "退货运费谁出",
        "48小时发货吗",
    ])
    def test_agrees_with_bm25index_to_the_digit(self, stats, index, query):
        """**这条是 LexicalStats 能存在的前提。**

        ``lexical_support_min`` 这个阈值是照着 ``Bm25Index.coverage``
        量出来的。两个实现给出不同的数，就等于换个检索后端阈值含义
        就变了——而且不会有任何报错，只会安静地把闸门调松或调死。
        """
        expected = index.coverage(query)
        got = [stats.coverage(query, t) for t in self.DOCS]
        assert got == pytest.approx([expected.get(i, 0.0) for i in range(len(self.DOCS))])
        # 别让这条退化成 0 == 0 的自我安慰
        assert max(got) > 0, "这个查询在语料里应该有覆盖，否则这条断言什么也没验"

    def test_the_gate_still_discriminates(self, stats):
        """覆盖率换了实现，但拦花呗的能力必须还在——这才是它的用途。"""
        absent = max(stats.coverage("你们支持花呗分期吗", t) for t in self.DOCS)
        present = max(stats.coverage("七天无理由能退吗", t) for t in self.DOCS)
        assert absent < 0.15, "库里没有花呗，覆盖率必须低"
        assert present > 0.3, "库里有七天无理由，覆盖率必须高"

    def test_only_keeps_document_frequencies(self, stats):
        """内存是 O(词表) 不是 O(语料)：只存 df 与总数，不存每篇文档的词。

        这正是它与 Bm25Index 的分工——召回交给 Milvus，这里只算判据。
        """
        assert stats.total == len(self.DOCS)
        assert not hasattr(stats, "doc_tokens")
        assert stats.df["退货"] == 1 and stats.df["七天"] == 1

    def test_df_counts_documents_not_occurrences(self):
        """一篇文档里出现十次也只算一次 df，否则 IDF 会被词频污染。"""
        s = bm25.LexicalStats.build(["退货退货退货退货"])
        assert s.df["退货"] == 1

    def test_empty_query_or_corpus(self, stats):
        assert stats.coverage("", self.DOCS[0]) == 0.0
        assert bm25.LexicalStats.build([]).coverage("任何问题", "任何文本") == 0.0


# ---------------------------------------------------------------- 向量打包


class TestVectorPacking:
    def test_roundtrip(self):
        vec = [0.1, -0.5, 0.9, 0.0]
        assert unpack_vector(pack_vector(vec)) == pytest.approx(vec, abs=1e-6)

    def test_binary_is_compact(self):
        """二进制比 JSON 省 5 倍——1024 维是热路径，这个差距不能忽略。"""
        import json

        vec = [0.123456] * 1024
        assert len(pack_vector(vec)) == 4096
        assert len(json.dumps(vec)) > 4096 * 2

    def test_l2_normalize(self):
        v = l2_normalize([3.0, 4.0])
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-9)

    def test_normalize_zero_vector(self):
        assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]

    def test_dot_of_normalized_is_cosine(self):
        a, b = l2_normalize([1.0, 0.0]), l2_normalize([1.0, 0.0])
        assert dot(a, b) == pytest.approx(1.0)
        c = l2_normalize([0.0, 1.0])
        assert dot(a, c) == pytest.approx(0.0)


# ---------------------------------------------------------------- 混合检索


def _row(item_id: int, text: str, vec: list[float], **kw) -> _Row:
    base = dict(
        biz_type="qa", modality="text", knowledge_type="spec",
        category_id=1024, product_ids=(9001,), asset_ids=(),
        valid_to_ts=0, review_status="approved",
    )
    base.update(kw)
    return _Row(
        item_id=item_id, chunk_seq=0, text=text,
        vector=l2_normalize(vec), **base,  # type: ignore[arg-type]
    )


def _store(rows: list[_Row]) -> LocalVectorStore:
    s = LocalVectorStore(engine=None)  # type: ignore[arg-type]
    s.rows = rows
    s.bm25 = bm25.Bm25Index.build([(i, r.text) for i, r in enumerate(rows)])
    return s


class TestHybridSearch:
    @pytest.fixture
    def store(self):
        return _store([
            _row(1, "针织衫是100%羊毛的克重320g", [1.0, 0.0, 0.0]),
            _row(2, "针织衫会起球吗做了抗起球处理", [0.9, 0.1, 0.0]),
            _row(3, "发什么快递默认中通", [0.0, 1.0, 0.0], knowledge_type="logistics"),
            _row(4, "退货运费谁承担", [0.0, 0.0, 1.0], knowledge_type="aftersale"),
        ])

    def test_returns_relevant(self, store):
        hits = store.search("针织衫起球", l2_normalize([0.9, 0.1, 0.0]))
        assert hits
        assert hits[0].item_id in (1, 2)

    def test_empty_store(self):
        assert _store([]).search("任何", [1.0, 0.0, 0.0]) == []

    def test_lexical_overlap_reaches_the_hits(self, store):
        """Agent 靠这个字段判"知识库里到底有没有"。它在这一层算好，
        中间断一环，上面那道闸门就退化成永远放行。"""
        hits = store.search("退货运费谁承担", l2_normalize([0.0, 0.0, 1.0]))
        top = next(h for h in hits if h.item_id == 4)
        assert top.lexical_overlap > 0.5

    def test_overlap_is_computed_for_dense_only_hits(self, store):
        """纯 dense 召回的条目不在 BM25 的 top_k 里，但照样要有覆盖率——
        按 sparse 列表去取的话，这些条目会被记成 0，然后被判成
        "知识库里没有"。"""
        hits = store.search("羊毛", l2_normalize([1.0, 0.0, 0.0]))
        assert any(h.lexical_overlap > 0 for h in hits)

    def test_an_unrelated_query_gets_near_zero_overlap(self, store):
        """向量基线相似度会让不相干的查询也有分数，覆盖率必须能分开。"""
        hits = store.search("能不能定制刺绣名字", l2_normalize([0.6, 0.5, 0.3]))
        assert all(h.lexical_overlap < 0.15 for h in hits)

    def test_top_k(self, store):
        assert len(store.search("针织衫", l2_normalize([1.0, 0.0, 0.0]), top_k=2)) == 2

    def test_dedups_by_item(self):
        """同一条知识的多个切片只留最高分，避免长文档占满结果。"""
        rows = [
            _row(1, "第一段内容讲材质", [1.0, 0.0, 0.0]),
            _row(1, "第二段内容讲克重", [0.99, 0.0, 0.0]),
            _row(2, "另一条知识", [0.0, 1.0, 0.0]),
        ]
        rows[1].chunk_seq = 1
        hits = _store(rows).search("材质", l2_normalize([1.0, 0.0, 0.0]))
        assert len({h.item_id for h in hits}) == len(hits)

    def test_expired_knowledge_excluded(self):
        """过期活动必须排除，否则客服会播报已结束的促销。"""
        import time

        now = int(time.time())
        rows = [
            _row(1, "双十一定金50抵100", [1.0, 0.0, 0.0],
                 valid_to_ts=now - 86400, knowledge_type="promotion"),
            _row(2, "针织衫是羊毛的", [0.99, 0.0, 0.0]),
        ]
        hits = _store(rows).search("活动", l2_normalize([1.0, 0.0, 0.0]))
        assert all(h.item_id != 1 for h in hits), "过期知识未被排除"

    def test_valid_knowledge_with_future_expiry_kept(self):
        import time

        rows = [_row(1, "双十一活动进行中", [1.0, 0.0, 0.0],
                     valid_to_ts=int(time.time()) + 86400)]
        assert _store(rows).search("活动", l2_normalize([1.0, 0.0, 0.0]))

    def test_permanent_knowledge_kept(self):
        rows = [_row(1, "针织衫是羊毛的", [1.0, 0.0, 0.0], valid_to_ts=0)]
        assert _store(rows).search("材质", l2_normalize([1.0, 0.0, 0.0]))

    def test_product_filter(self, store):
        """用户在问 A 商品，不能召回 B 商品的知识。"""
        store.rows[2].product_ids = (9002,)
        hits = store.search(
            "快递", l2_normalize([0.0, 1.0, 0.0]), product_ids=[9001]
        )
        assert all(h.item_id != 3 for h in hits)

    def test_category_filter(self, store):
        store.rows[3].category_id = 2048
        hits = store.search(
            "退货", l2_normalize([0.0, 0.0, 1.0]), category_id=1024
        )
        assert all(h.item_id != 4 for h in hits)

    def test_biz_type_filter(self, store):
        store.rows[0].biz_type = "script"
        hits = store.search(
            "针织衫", l2_normalize([1.0, 0.0, 0.0]), biz_types=["qa"]
        )
        assert all(h.item_id != 1 for h in hits)

    def test_filter_excluding_everything_returns_empty(self, store):
        assert store.search(
            "针织衫", l2_normalize([1.0, 0.0, 0.0]), product_ids=[99999]
        ) == []

    def test_keyword_only_match_still_found(self):
        """向量完全不相关但关键词精确命中时，BM25 那一路应当把它捞回来。"""
        rows = [
            _row(1, "本款采用莱赛尔纤维", [0.0, 0.0, 1.0]),
            _row(2, "无关内容甲乙丙丁", [1.0, 0.0, 0.0]),
        ]
        hits = _store(rows).search("莱赛尔", l2_normalize([1.0, 0.0, 0.0]))
        assert any(h.item_id == 1 for h in hits), "BM25 通路未生效"

    def test_source_marked_fused(self, store):
        hits = store.search("针织衫", l2_normalize([1.0, 0.0, 0.0]))
        assert all(h.source == "fused" for h in hits)


# ---------------------------------------------------------------- 批次契约


class TestEmbeddingBatching:
    """批次上限由 provider 声明，调用方不该自己猜。

    猜错的后果是跑到一半被服务端 400 拒绝——DashScope 对
    text-embedding-v3 的上限是 10，曾误设为 25 导致向量化全量失败。
    """

    def _client(self, monkeypatch, recorder: list[int], sent: list | None = None):
        """按请求体形状自动应答两种协议。

        原生接口收 ``{"input": {"texts": [...]}}`` 回 ``output.embeddings``；
        OpenAI 兼容接口收 ``{"input": [...]}`` 回 ``data``。桩必须两种都认，
        否则测试只能证明其中一条路是通的。
        """
        import httpx

        from smartmall_pipeline.rag import embedding as emb

        def _vec():
            return [1.0] + [0.0] * (emb.DIM - 1)

        class _Resp:
            status_code = 200

            def __init__(self, n: int, native: bool) -> None:
                self._n, self._native = n, native

            def json(self):
                if self._native:
                    return {"output": {"embeddings": [
                        {"text_index": i, "embedding": _vec()} for i in range(self._n)
                    ]}}
                return {"data": [
                    {"index": i, "embedding": _vec()} for i in range(self._n)
                ]}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                if sent is not None:
                    sent.append((url, json))
                native = isinstance(json["input"], dict)
                n = len(json["input"]["texts"]) if native else len(json["input"])
                recorder.append(n)
                return _Resp(n, native)

        monkeypatch.setattr(httpx, "Client", lambda **kw: _Client())

    def test_batch_limit_is_declared_per_model(self, monkeypatch):
        """条数上限随模型而变，不能写死一个常量。

        v4/v3 是 10，v2 是 25——按 v2 的上限去调 v4 会 400，
        按 v4 的上限去调 v2 则白白多发两倍请求。
        """
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        assert DashScopeEmbedding(api_key="stub").max_batch <= 10
        assert DashScopeEmbedding(api_key="stub", model="text-embedding-v2").max_batch == 25

    def test_unknown_model_falls_back_to_safest_limit(self, monkeypatch):
        """没见过的模型宁可多发几次请求，也不要撞 400。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        p = DashScopeEmbedding(api_key="stub", model="text-embedding-v9")
        assert p.max_batch <= 10

    def test_provider_name_carries_the_model(self, monkeypatch):
        """换了模型，索引必须能看出来。

        LocalVectorStore 靠 provider 名检测混用。名字写死成常量的话，
        v3 和 v4 的向量会被塞进同一个索引——维度一样、报错也不会有，
        但相似度从此不可信。
        """
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        v4 = DashScopeEmbedding(api_key="stub", model="text-embedding-v4")
        v3 = DashScopeEmbedding(api_key="stub", model="text-embedding-v3")
        assert v4.name != v3.name
        assert "text-embedding-v4" in v4.name

    def test_model_can_be_set_by_env(self, monkeypatch):
        """换模型不该改代码——聊天和向量的额度是分开的，会需要单独切。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v2")
        p = DashScopeEmbedding(api_key="stub")
        assert p.model == "text-embedding-v2"
        assert p.max_batch == 25

    def test_explicit_model_beats_env(self, monkeypatch):
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v2")
        assert DashScopeEmbedding(
            api_key="stub", model="text-embedding-v4"
        ).model == "text-embedding-v4"

    def test_query_and_document_are_embedded_differently(self, monkeypatch):
        """非对称模型的核心：查询侧和文档侧要打不同的 text_type。

        Qwen3-Embedding 系列专门区分"待检索的问题"与"库里的文档"。
        两侧都按 document 去编码，等于花了钱却用不上这个模型的长处。
        """
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
        sent: list = []
        self._client(monkeypatch, [], sent)

        p = DashScopeEmbedding(api_key="stub", model="qwen3.7-text-embedding")
        p.embed(["库里的一条知识"])
        p.embed_query("这件能机洗吗")

        assert sent[0][1]["parameters"]["text_type"] == "document"
        assert sent[1][1]["parameters"]["text_type"] == "query"

    def test_native_endpoint_used_when_talking_to_dashscope(self, monkeypatch):
        """只有原生接口认 text_type，直连时必须走它。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
        sent: list = []
        self._client(monkeypatch, [], sent)
        DashScopeEmbedding(api_key="stub").embed(["x"])

        assert sent[0][0] == DashScopeEmbedding.NATIVE_URL
        assert sent[0][1]["parameters"]["dimension"] == 1024  # 原生是单数

    def test_gateway_routing_falls_back_to_openai_shape(self, monkeypatch):
        """指到网关时只能说 OpenAI 兼容协议，不能硬发原生请求体。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        sent: list = []
        self._client(monkeypatch, [], sent)
        p = DashScopeEmbedding(api_key="stub", base_url="http://gw:9000/v1")
        p.embed(["x"])

        assert not p.native
        assert sent[0][0] == "http://gw:9000/v1/embeddings"
        assert sent[0][1]["input"] == ["x"]        # 兼容协议是扁平数组
        assert "dimensions" in sent[0][1]          # 兼容协议是复数

    def test_query_still_works_without_text_type_support(self, monkeypatch):
        """走网关时拿不到 text_type，但不能因此报错——降级即可。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        self._client(monkeypatch, [])
        p = DashScopeEmbedding(api_key="stub", base_url="http://gw:9000/v1")
        assert len(p.embed_query("这件能机洗吗")) == 1024

    def test_instruct_only_attached_to_queries(self, monkeypatch):
        """instruct 只在 query 侧生效，挂到文档侧会被接口拒绝。"""
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
        sent: list = []
        self._client(monkeypatch, [], sent)
        p = DashScopeEmbedding(api_key="stub", query_instruct="Retrieve FAQ answers")
        p.embed(["文档"])
        p.embed_query("问题")

        assert "instruct" not in sent[0][1]["parameters"]
        assert sent[1][1]["parameters"]["instruct"] == "Retrieve FAQ answers"

    def test_store_embed_query_prefers_the_query_path(self, monkeypatch):
        """检索入口必须真的走 embed_query，否则上面这些都白做。"""
        from smartmall_pipeline.rag.embedding import DIM
        from smartmall_pipeline.rag.store import embed_query

        class _Asymmetric:
            name, dim, max_batch = "stub", DIM, 10
            def __init__(self):
                self.calls = []
            def embed(self, texts):
                self.calls.append("document")
                return [[1.0] + [0.0] * (DIM - 1) for _ in texts]
            def embed_query(self, text):
                self.calls.append("query")
                return [1.0] + [0.0] * (DIM - 1)

        p = _Asymmetric()
        embed_query(p, "这件能机洗吗")
        assert p.calls == ["query"]

    def test_store_falls_back_for_symmetric_providers(self):
        """bge-m3 没有 embed_query，退回 embed，行为与从前一致。"""
        from smartmall_pipeline.rag.embedding import DIM
        from smartmall_pipeline.rag.store import embed_query

        class _Symmetric:
            name, dim, max_batch = "stub", DIM, 10
            def embed(self, texts):
                return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

        assert len(embed_query(_Symmetric(), "问题")) == DIM

    def test_dim_stays_1024_across_models(self, monkeypatch):
        """维度变了就得重建整个索引，不能因为换模型悄悄改掉。"""
        from smartmall_pipeline.rag.embedding import DIM, DashScopeEmbedding

        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        for m in ("text-embedding-v3", "text-embedding-v4"):
            assert DashScopeEmbedding(api_key="stub", model=m).dim == DIM == 1024

    def test_splits_into_allowed_batches(self, monkeypatch):
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        sizes: list[int] = []
        self._client(monkeypatch, sizes)
        provider = DashScopeEmbedding(api_key="stub")
        provider.embed([f"文本{i}" for i in range(23)])

        assert sizes == [10, 10, 3]
        assert all(n <= provider.max_batch for n in sizes)

    def test_returns_one_vector_per_input(self, monkeypatch):
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        self._client(monkeypatch, [])
        provider = DashScopeEmbedding(api_key="stub")
        assert len(provider.embed([f"文本{i}" for i in range(23)])) == 23

    def test_missing_api_key_gives_actionable_error(self, monkeypatch):
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding, EmbeddingError

        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        with pytest.raises(EmbeddingError) as exc:
            DashScopeEmbedding()
        assert "bailian" in str(exc.value)          # 给出申请地址
        assert "--embedding local" in str(exc.value)  # 给出替代方案
