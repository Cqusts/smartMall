"""flush 的真实效果，以及 ai-rag 侧的兜底。

这条用的是真 Milvus Lite：flush 前后的检索耗时差是这一处修复存在的全部理由，
用桩测不出来。断言用**相对比**（flush 后至少快 3 倍）而不是绝对毫秒——实测差 15–30 倍，
留足余量给慢机器；绝对阈值在 CI 上会随机红。
"""

from __future__ import annotations

import random
import shutil
import statistics
import time

import pytest

pytest.importorskip("pymilvus", reason="需要 pymilvus + milvus-lite")
pytest.importorskip("milvus_lite", reason="需要 milvus-lite")

from app import main as rag  # noqa: E402
from app.milvus_store import F_DENSE, OUTPUT_FIELDS, MilvusConfig, MilvusStore  # noqa: E402
from app.retrieval import build_filter_expr  # noqa: E402
from test_milvus_integration import FakeEmbedding  # noqa: E402

VOCAB = "退货 运费 尺码 偏大 偏小 面料 洗涤 发货 快递 顺丰 保修 发票 优惠 满减 库存 颜色".split()


class TestFlushEffect:
    def test_search_is_much_faster_after_flush(self, tmp_path):
        random.seed(3)
        emb = FakeEmbedding()
        store = MilvusStore(MilvusConfig(uri=str(tmp_path / "kb.db"), collection="flush_t",
                                         analyzer="jieba"))
        store.ensure_collection(drop_existing=True)

        n = 4000
        for a in range(0, n, 1000):
            docs = ["".join(random.choice(VOCAB) for _ in range(12)) for _ in range(1000)]
            store.upsert_chunks([
                {"item_id": a + i, "chunk_seq": 0, "text": d, "dense_vec": emb.embed_query(d),
                 "biz_type": "qa", "modality": "text", "category_id": 0, "product_ids": [1],
                 "asset_ids": [], "review_status": "approved", "valid_to_ts": 0,
                 "quality_score": 0.8, "kb_version": ""}
                for i, d in enumerate(docs)])

        expr = build_filter_expr(kb_version="")
        qv = emb.embed_query("退货运费")

        def dense_ms() -> float:
            xs = []
            for _ in range(5):
                t = time.perf_counter()
                store.client.search(collection_name="flush_t", data=[qv], anns_field=F_DENSE,
                                    search_params={"params": {"ef": 64}}, limit=50,
                                    filter=expr, output_fields=OUTPUT_FIELDS)
                xs.append(time.perf_counter() - t)
            return statistics.median(xs) * 1000

        before = dense_ms()
        store.flush()
        after = dense_ms()
        shutil.rmtree(tmp_path, ignore_errors=True)

        assert after * 3 < before, (
            f"flush 前 {before:.0f}ms，flush 后 {after:.0f}ms —— 没有明显变快，"
            "flush 没起到封存+建索引的作用？")


class TestReloadFlushes:
    class Svc:
        def __init__(self, flush_raises=False):
            self.log = []
            self.store = self
            self._raises = flush_raises

        def flush(self):
            self.log.append("flush")
            if self._raises:
                raise RuntimeError("Milvus 暂时忙")

        def refresh_stats(self):
            self.log.append("refresh")
            return 5

    def _call(self, svc):
        import asyncio

        rag._service = svc
        return asyncio.run(rag.reload_stats())

    def test_reload_flushes_before_rebuilding_idf(self):
        saved = rag._service
        try:
            svc = self.Svc()
            out = self._call(svc)
            assert svc.log == ["flush", "refresh"]
            assert out["data"]["documents"] == 5
        finally:
            rag._service = saved

    def test_flush_failure_does_not_block_idf_refresh(self):
        """两件事互不依赖：flush 失败不该挡住 IDF 表的刷新。"""
        saved = rag._service
        try:
            svc = self.Svc(flush_raises=True)
            out = self._call(svc)
            assert svc.log == ["flush", "refresh"]
            assert out["data"]["documents"] == 5
        finally:
            rag._service = saved


class TestEvalIndexingFlushes:
    def test_index_corpus_flushes_after_loading(self):
        from app.eval_retrieval import index_corpus

        class Store:
            def __init__(self):
                self.log = []

            def upsert_chunks(self, rows):
                self.log.append("upsert")

            def flush(self):
                self.log.append("flush")

        store = Store()
        n = index_corpus(store, FakeEmbedding(), [{"id": 1, "title": "t", "content": "c"}])
        assert n == 1
        assert store.log == ["upsert", "flush"], "评测要量的是建好索引之后的检索，不是 growing segment 上的扫描"
