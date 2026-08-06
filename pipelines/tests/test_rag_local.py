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

    def _client(self, monkeypatch, recorder: list[int]):
        import httpx

        from smartmall_pipeline.rag import embedding as emb

        class _Resp:
            status_code = 200

            def __init__(self, n: int) -> None:
                self._n = n

            def json(self):
                return {
                    "data": [
                        {"index": i, "embedding": [1.0] + [0.0] * (emb.DIM - 1)}
                        for i in range(self._n)
                    ]
                }

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                recorder.append(len(json["input"]))
                return _Resp(len(json["input"]))

        monkeypatch.setattr(httpx, "Client", lambda **kw: _Client())

    def test_dashscope_limit_is_ten(self):
        from smartmall_pipeline.rag.embedding import DashScopeEmbedding

        assert DashScopeEmbedding.max_batch <= 10, (
            "DashScope text-embedding-v3 单次最多 10 条，超过直接 400"
        )

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
