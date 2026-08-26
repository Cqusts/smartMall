"""真跑 Milvus 的集成测试——不 mock。

跑在 **Milvus Lite** 上：``uri`` 给一个本地目录就是嵌入式模式，纯 Python，
不需要 Docker，Windows/macOS/Linux 都能跑，所以这些断言能进 CI。

**为什么必须有这一层。** ``milvus_store.py`` 曾经是死代码——写得挺完整，
但没有任何地方实例化它。单元测试测不到它：schema 对不对、``upsert``
认不认主键、``analyzer`` 的名字合不合法，全都要等真实例才会报。实测下来
它有三个阻塞缺陷，没有一个能被纯逻辑测试发现：

1. ``analyzer_params={"type": "chinese"}`` —— Lite 只认 ``standard`` /
   ``jieba``，服务端只认 ``chinese``，两边合法取值不同
2. ``auto_id=True`` 配 ``client.upsert()`` —— 主键自动生成就没法定位
   要覆盖哪一行，Milvus 直接拒收
3. ``hybrid_search`` 只返回融合分 —— 拿不到 Agent 阈值要用的余弦相似度
"""

from __future__ import annotations

import math
import shutil

import pytest

pytest.importorskip("pymilvus", reason="需要 pymilvus + milvus-lite")
pytest.importorskip("milvus_lite", reason="需要 milvus-lite（pip install 'milvus-lite[chinese]'）")

from smartmall_pipeline.rag import LexicalStats, chunk_pk  # noqa: E402
from smartmall_pipeline.rag.bm25 import tokenize  # noqa: E402

from app.milvus_store import (  # noqa: E402
    DENSE_DIM,
    F_CHUNK_ID,
    MilvusConfig,
    MilvusStore,
)
from app.retrieval import RetrievalConfig  # noqa: E402
from app.search import SearchService  # noqa: E402


# ---------------------------------------------------------------- 假向量


class FakeEmbedding:
    """确定性的假向量化：把字符二元组哈希进 1024 维再归一化。

    不调 API，但保留"文本越像余弦越高"这个性质——``dense_score`` 是不是
    一个真的余弦、能不能撑起 0.30/0.55 那两道阈值，靠它才验得出来。
    随机向量做不到这点：那样余弦恒在 0 附近，断言就退化成看运气。
    """

    name = "fake-1024"
    max_batch = 16

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)

    @staticmethod
    def _one(text: str) -> list[float]:
        vec = [0.0] * DENSE_DIM
        for tok in tokenize(text):
            vec[hash(tok) % DENSE_DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


# ---------------------------------------------------------------- 语料


#: 第 5 条是**未审核**的，用来验硬性过滤真的在过滤
DOCS = [
    (1, "七天无理由退换\n签收后七天内可申请退换，商品需保持吊牌完整未洗涤", "approved"),
    (2, "羊毛针织衫怎么洗\n建议手洗平铺晾干，不可机洗不可漂白", "approved"),
    (3, "发货时效\n现货商品 48 小时内发出，预售以详情页标注为准", "approved"),
    (4, "发票问题\n支持开具电子普通发票与增值税专用发票", "revised"),
    (5, "内部草稿\n这条还没审核，绝不该出现在任何检索结果里", "pending"),
]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    db = tmp_path_factory.mktemp("milvus") / "kb.db"
    cfg = MilvusConfig(uri=str(db), collection="kb_chunk_test", analyzer="jieba")
    s = MilvusStore(cfg)
    s.ensure_collection(drop_existing=True)

    emb = FakeEmbedding()
    s.upsert_chunks([
        {
            "item_id": iid, "chunk_seq": 0, "text": text,
            "dense_vec": emb.embed_query(text),
            "biz_type": "qa", "modality": "text", "category_id": 1024,
            "product_ids": [9001], "asset_ids": [],
            "review_status": status, "valid_to_ts": 0,
            "quality_score": 0.8, "kb_version": "",
        }
        for iid, text, status in DOCS
    ])
    yield s
    shutil.rmtree(db, ignore_errors=True)


@pytest.fixture(scope="module")
def service(store):
    return SearchService(
        store=store, provider=FakeEmbedding(),
        config=RetrievalConfig(kb_version=""),
    )


# ---------------------------------------------------------------- 建表与写入


class TestSchema:
    def test_collection_is_created_with_bm25(self, store):
        """建得起来就说明 BM25 Function + 稀疏倒排 + HNSW 都被接受了。

        ``analyzer`` 传错（比如服务端的 ``chinese`` 给了 Lite）会在这一步
        报 ``unknown tokenizer type``——单元测试永远发现不了。
        """
        assert store.client.has_collection(store.cfg.collection)

    def test_all_docs_written(self, store):
        assert store.count() == len(DOCS)

    def test_primary_key_follows_chunk_pk(self, store):
        """两个后端必须用同一个主键规则，否则同一条知识有两个 id。"""
        rows = store.client.query(
            collection_name=store.cfg.collection,
            filter="item_id == 3", output_fields=[F_CHUNK_ID],
        )
        assert rows[0][F_CHUNK_ID] == chunk_pk(3, 0)

    def test_delete_by_item_actually_deletes(self, store):
        """``index --rebuild`` 依赖它。删不掉的话重建会留下旧切片——
        表现是"重建完还是查到旧内容"，而命令显示成功。
        """
        emb = FakeEmbedding()
        store.upsert_chunks([{
            "item_id": 99, "chunk_seq": 0, "text": "一条待删除的临时知识",
            "dense_vec": emb.embed_query("临时"), "biz_type": "qa",
            "modality": "text", "category_id": 0, "product_ids": [],
            "asset_ids": [], "review_status": "approved", "valid_to_ts": 0,
            "quality_score": 0.5, "kb_version": "",
        }])
        assert store.client.query(
            collection_name=store.cfg.collection,
            filter="item_id == 99", output_fields=["item_id"])

        store.delete_by_item([99])
        assert not store.client.query(
            collection_name=store.cfg.collection,
            filter="item_id == 99", output_fields=["item_id"]), "没删掉"
        assert store.count() == len(DOCS), "别的条目被误删了"

    def test_upsert_is_idempotent(self, store):
        """再写一遍不该多出行来——auto_id=True 时它会，这正是那个缺陷。"""
        before = store.count()
        store.upsert_chunks([{
            "item_id": 3, "chunk_seq": 0, "text": DOCS[2][1],
            "dense_vec": FakeEmbedding().embed_query(DOCS[2][1]),
            "biz_type": "qa", "modality": "text", "category_id": 1024,
            "product_ids": [9001], "asset_ids": [], "review_status": "approved",
            "valid_to_ts": 0, "quality_score": 0.8, "kb_version": "",
        }])
        assert store.count() == before


# ---------------------------------------------------------------- 分路召回


class TestRecall:
    def test_dense_score_is_a_cosine_not_an_rrf_score(self, store):
        """**这条是 recall() 存在的理由。**

        Agent 的 ``state.max_score`` 取 ``dense_score``，两道阈值
        （0.30 / 0.55）是照余弦量出来的。``hybrid_search`` 回来的 RRF 分
        最大只有 ``2/(k+1)≈0.033``——填进去每条查询都低于 0.30，
        **结果是全部转人工，而且不报错**。
        """
        q = "签收后七天内可以退吗"
        dense, _ = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status in ["approved", "revised"]',
        )
        assert dense, "dense 这一路必须有召回"
        top = dense[0].score
        assert 0.0 <= top <= 1.0001, f"余弦应在 [0,1]，拿到 {top}"
        assert top > 0.2, (
            f"相关查询的余弦只有 {top:.4f}——低到这个程度说明拿到的不是"
            f"余弦，很可能是 RRF 分（其上限 {2 / 61:.4f}）"
        )

    def test_both_channels_recall(self, store):
        q = "发票能开专票吗"
        dense, sparse = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status in ["approved", "revised"]',
        )
        assert dense and sparse, "两路都得有结果，缺一路 RRF 就退化成单路"
        assert {h.source for h in dense} == {"dense"}
        assert {h.source for h in sparse} == {"sparse"}

    def test_bm25_finds_what_it_should(self, store):
        """BM25 那一路是独立证据。它要是没在工作，混合检索名不副实。"""
        q = "增值税专用发票"
        _, sparse = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status in ["approved", "revised"]',
        )
        assert sparse[0].item_id == 4


class TestHardFilter:
    """过滤器要双向验。只验"不该出现的没出现"，一个恒空的过滤器也能通过。"""

    def test_unreviewed_knowledge_never_surfaces(self, store):
        q = "内部草稿还没审核"
        dense, sparse = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status in ["approved", "revised"]',
        )
        assert all(h.item_id != 5 for h in dense + sparse), "未审核的知识漏出来了"

    def test_but_it_is_there_if_you_ask_for_it(self, store):
        """反向：把过滤条件换成 pending 就应该查得到——证明上一条不是
        因为这条数据根本没写进去。"""
        q = "内部草稿还没审核"
        dense, _ = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status == "pending"',
        )
        assert any(h.item_id == 5 for h in dense)

    def test_revised_is_searchable(self, store):
        """人工改写过的知识（revised）是质量最高的那批，必须能检索到。

        改之前 ``build_filter_expr`` 只收 ``approved``，item 4 会永远查不到。
        """
        q = "开发票"
        dense, sparse = store.recall(
            query_text=q, query_dense=FakeEmbedding().embed_query(q),
            filter_expr='review_status in ["approved", "revised"]',
        )
        assert any(h.item_id == 4 for h in dense + sparse)


# ---------------------------------------------------------------- 端到端


class TestSearchService:
    def test_returns_the_right_answer(self, service):
        hits = service.search("签收后七天内可以退吗", top_k=3)
        assert hits and hits[0].item_id == 1

    def test_every_hit_carries_the_judgments_the_agent_needs(self, service):
        """少一个字段，Agent 那边就有一道闸门失效——而且不会报错。"""
        hits = service.search("羊毛针织衫能机洗吗", top_k=3)
        assert hits
        h = hits[0]
        assert 0.0 <= h.dense_score <= 1.0001
        assert h.bm25_score >= 0.0
        assert 0.0 <= h.lexical_overlap <= 1.0001
        assert h.title and h.content

    def test_a_good_match_clears_the_agents_handover_threshold(self, service):
        """**这条是照着 Agent 的阈值写的，不是照着实现写的。**

        ``AgentConfig.handover_below = 0.30``，判据取 ``dense_score``。
        一个明显相关的查询必须高过这条线，否则 Agent 会判成「知识库里
        根本没有」直接转人工。

        为什么不写成 ``0 <= dense_score <= 1``：RRF 分（上限
        ``2/(k+1)≈0.033``）也满足那个区间，**那样的断言在服务端融合的
        实现下照样通过**——试过了，注入缺陷后 18 条全绿。判据得卡在
        真正会出事的那个位置上。
        """
        hits = service.search("签收后七天内可以退吗", top_k=3)
        assert hits
        top = hits[0]
        assert top.dense_score > 0.30, (
            f"dense_score={top.dense_score:.4f} 低于 Agent 的转人工线 0.30。"
            f"拿到的多半不是余弦而是 RRF 分（上限 {2 / 61:.4f}）——"
            f"那样每条查询都会被判成知识库里没有"
        )

    def test_rrf_score_and_dense_score_are_not_the_same_number(self, service):
        """两个分数混成一个，就等于丢了一路判据。"""
        hits = service.search("签收后七天内可以退吗", top_k=3)
        assert hits
        assert hits[0].score != pytest.approx(hits[0].dense_score), (
            "score（RRF 融合分）和 dense_score（余弦）不该相等——"
            "相等说明分路分数没取到"
        )
        assert hits[0].score < 0.1, "RRF 分上限是 2/(k+1)≈0.033，比这大说明取错了"

    def test_lexical_overlap_still_blocks_what_it_was_built_to_block(self, service):
        """``has_lexical_support`` 的整个用途就在这条上。

        「花呗」在语料里一个字都没有，但 bigram 分词下随便一个共有的词
        就能让 BM25 有分——所以判据不能是 ``bm25_score > 0``。
        """
        absent = service.search("你们支持花呗分期吗", top_k=5)
        present = service.search("七天无理由能退吗", top_k=5)
        assert max((h.lexical_overlap for h in absent), default=0.0) < 0.15
        assert max(h.lexical_overlap for h in present) > 0.3

    def test_lexical_overlap_matches_the_local_backend(self, service):
        """Milvus 这条路算出来的覆盖率，要和本地实现是同一个数。

        不然 ``lexical_support_min=0.15`` 这个阈值换个后端就变了含义。
        """
        q = "七天无理由能退吗"
        approved = [t for _, t, s in DOCS if s in ("approved", "revised")]
        expected = LexicalStats.build(approved)
        for h in service.search(q, top_k=5):
            assert h.lexical_overlap == pytest.approx(expected.coverage(q, h.content))

    def test_unreviewed_knowledge_never_reaches_the_agent(self, service):
        hits = service.search("内部草稿还没审核", top_k=5)
        assert all(h.item_id != 5 for h in hits)

    def test_empty_query_returns_nothing(self, service):
        assert service.search("   ") == []

    def test_idf_table_describes_the_indexed_corpus(self, service):
        """IDF 表要从 Milvus 自己扫，不能从 MySQL 建——索引落后时，
        从 MySQL 建出来的 IDF 描述的是一个并不存在的语料。"""
        n = service.refresh_stats()
        approved = [d for d in DOCS if d[2] in ("approved", "revised")]
        assert n == len(approved), "未审核的不该进 IDF 表"


class TestIndexerRowMapping:
    """``cli._milvus_row`` 把 knowledge_item 的一行摊平成 Milvus 的字段。

    这一步全是类型转换：``DECIMAL`` 要变 float、``DATETIME`` 要变 epoch、
    JSON 字符串要变 list[int]。转错了 Milvus 会拒收**整批**，而错的是
    其中一行——所以要验的是"真实 schema 收不收"，不是"字段名齐不齐"。
    """

    @staticmethod
    def _row(**over):
        import datetime as dt
        import decimal

        base = {
            "id": 42, "biz_type": "qa", "modality": "text",
            "category_id": 1024,
            "product_ids": "[9001, 9002]", "asset_ids": "[7]",
            "review_status": "approved",
            "valid_to": dt.datetime(2030, 1, 1),
            "quality_score": decimal.Decimal("0.875"),
        }
        base.update(over)
        return base

    def test_types_are_converted(self):
        from smartmall_pipeline.cli import _milvus_row

        r = _milvus_row(self._row(), 0, "正文", [0.1] * DENSE_DIM, "kb-v1")
        assert r["product_ids"] == [9001, 9002]
        assert r["asset_ids"] == [7]
        assert isinstance(r["quality_score"], float)
        assert r["valid_to_ts"] > 0
        assert r["kb_version"] == "kb-v1"

    def test_null_valid_to_means_forever(self):
        """0 表示永久有效，与 build_filter_expr 的 `valid_to_ts == 0` 对应。
        转成别的值（比如现在时间）会让所有永久知识立刻"过期"。"""
        from smartmall_pipeline.cli import _milvus_row

        r = _milvus_row(self._row(valid_to=None), 0, "正文",
                        [0.1] * DENSE_DIM, "")
        assert r["valid_to_ts"] == 0

    def test_null_json_columns(self):
        from smartmall_pipeline.cli import _milvus_row

        r = _milvus_row(self._row(product_ids=None, asset_ids=None), 0,
                        "正文", [0.1] * DENSE_DIM, "")
        assert r["product_ids"] == [] and r["asset_ids"] == []

    def test_the_row_is_accepted_by_a_real_collection(self, store):
        """**这条才是重点。** 字段名齐了不代表类型对——Decimal、datetime、
        JSON 字符串直接塞进去，Milvus 会拒收整批。"""
        from smartmall_pipeline.cli import _milvus_row

        row = _milvus_row(self._row(), 0, "一条用来验类型的知识",
                          FakeEmbedding().embed_query("类型验证"), "")
        assert store.upsert_chunks([row]) == 1
        got = store.client.query(
            collection_name=store.cfg.collection,
            filter="item_id == 42", output_fields=["product_ids", "quality_score"],
        )
        assert got and list(got[0]["product_ids"]) == [9001, 9002]
        store.delete_by_item([42])

    def test_overlong_text_is_truncated_not_rejected(self, store):
        """text 是 VARCHAR(4096)。超长不截断的话 Milvus 拒收整批——
        一条超长知识会把同批次其他几十条一起带走。"""
        from smartmall_pipeline.cli import _milvus_row

        row = _milvus_row(self._row(id=43), 0, "超长" * 5000,
                          FakeEmbedding().embed_query("x"), "")
        assert len(row["text"]) <= 4096
        assert store.upsert_chunks([row]) == 1
        store.delete_by_item([43])


class TestEnvNameCollision:
    """配置项的名字不能和 pymilvus 自己读的环境变量撞。

    端到端跑的时候踩到的：把 URI 配成 ``MILVUS_URI=<文件路径>``，
    pymilvus 在 import 阶段就抛 ``Illegal uri``——它自己也读这个变量，
    而且按 URL 校验。报错点在 pymilvus 内部，看不出跟本项目有什么关系。
    """

    def test_our_settings_avoid_pymilvus_reserved_names(self):
        from app.config import PYMILVUS_RESERVED_ENV, Settings

        ours = {f.upper() for f in Settings.model_fields}
        clash = ours & set(PYMILVUS_RESERVED_ENV)
        assert not clash, f"配置项名字和 pymilvus 抢环境变量：{clash}"

    def test_the_reserved_list_is_not_stale(self):
        """这份名单是照着 pymilvus 源码抄的，得确认它还对得上——
        名单过期了，上面那条断言就变成了摆设。"""
        import pathlib
        import re

        import pymilvus

        root = pathlib.Path(pymilvus.__file__).parent
        found = set()
        for py in root.rglob("*.py"):
            found |= set(re.findall(
                r'getenv\(\s*["\'](MILVUS_[A-Z_]+)', py.read_text(
                    encoding="utf-8", errors="ignore")))

        from app.config import PYMILVUS_RESERVED_ENV

        missing = found - set(PYMILVUS_RESERVED_ENV)
        assert found, "一个都没扫到，说明这条断言本身失效了"
        assert not missing, f"pymilvus 新占了环境变量，名单要补：{missing}"


class TestChunkSeqOverflow:
    def test_too_many_chunks_raises_instead_of_colliding(self):
        """``item_id=7, chunk_seq=1000`` 与 ``item_id=8, chunk_seq=0`` 会算出
        同一个主键。静默覆盖比报错难查得多——检索时只是少一条知识。"""
        assert chunk_pk(7, 999) == 7999
        with pytest.raises(ValueError, match="chunk_seq"):
            chunk_pk(7, 1000)
