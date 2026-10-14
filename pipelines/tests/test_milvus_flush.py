"""批量写入 Milvus 之后必须 flush。

实测（Milvus Lite，5000 条）：写完不 flush，dense 572ms + sparse 765ms；flush 之后
40ms + 21ms。数据量大到自动封存（约 2 万条）时它自己会好，所以**规模小的时候反而更慢**——
几千条规模下，不 flush 的 Milvus 比进程内暴力检索慢约 6 倍，且没有任何报错。
这里只测调用顺序与契约；真实的快慢差异见 ai-rag 的 test_milvus_flush_effect。
"""

from __future__ import annotations

import pytest

from smartmall_pipeline.cli import _finalize_milvus_index
from smartmall_pipeline.rag.milvus import MilvusConfig, MilvusIndex


class FakeMilvus:
    def __init__(self):
        self.log = []

    def flush(self):
        self.log.append("flush")

    def count(self):
        self.log.append("count")
        return 42


class TestFinalize:
    def test_flushes_before_counting(self):
        """先 flush 再 count：count 报的是 collection 的实际条数，
        而 flush 之前写进去的数据还在没有索引的 growing segment 里。"""
        m = FakeMilvus()
        assert _finalize_milvus_index(m) == 42
        assert m.log == ["flush", "count"]

    def test_flush_happens_exactly_once(self):
        """每批都 flush 会产生一堆小 segment，flush 本身也有开销（5000 条约 1.2 秒）。"""
        m = FakeMilvus()
        _finalize_milvus_index(m)
        assert m.log.count("flush") == 1


class TestMilvusIndexFlush:
    def test_flush_targets_the_configured_collection(self):
        calls = []

        class Client:
            def flush(self, name):
                calls.append(name)

        idx = MilvusIndex(MilvusConfig(uri="./x.db", collection="kb_chunk_v2"))
        idx._client = Client()
        idx.flush()
        assert calls == ["kb_chunk_v2"]


class TestCmdIndexFlushes:
    """走真实的 ``cmd_index``：flush 必须在最后一批写完之后、且只调一次。

    只测 ``_finalize_milvus_index`` 不够——把 ``cmd_index`` 里对它的调用删掉，
    那几条照样绿。这里把 cmd_index 本身跑起来（SQLite 放知识条目、桩 provider、
    桩 Milvus），观察完整的调用序列。
    """

    def _run(self, monkeypatch, n_items=5, batch=2, fail_on_batch=None):
        import argparse
        from types import SimpleNamespace

        sqlalchemy = pytest.importorskip("sqlalchemy")
        from sqlalchemy import create_engine, text

        import smartmall_pipeline.cli as cli
        import smartmall_pipeline.rag as rag_pkg
        import smartmall_pipeline.rag.milvus as milvus_mod

        engine = create_engine("sqlite://")
        with engine.begin() as c:
            c.execute(text(
                "CREATE TABLE knowledge_item (id INTEGER PRIMARY KEY, biz_type TEXT, title TEXT,"
                " content TEXT, modality TEXT, category_id INTEGER, product_ids TEXT, asset_ids TEXT,"
                " review_status TEXT, valid_to TEXT, quality_score REAL, deleted INTEGER DEFAULT 0,"
                " embedding_status TEXT DEFAULT 'pending')"))
            for i in range(1, n_items + 1):
                c.execute(text(
                    "INSERT INTO knowledge_item (id, biz_type, title, content, modality, category_id,"
                    " product_ids, asset_ids, review_status, quality_score)"
                    " VALUES (:i, 'qa', :t, :c, 'text', 1, '[]', '[]', 'approved', 0.9)"),
                    {"i": i, "t": f"问题{i}", "c": f"答案{i}"})

        log: list[str] = []

        class Provider:
            name = "fake/p"
            dim = 4
            max_batch = batch

            def embed(self, texts):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        class FakeIndex:
            def __init__(self, cfg):
                pass

            def ensure_collection(self, *, drop_existing=False):
                log.append("ensure")

            def upsert_chunks(self, rows):
                log.append(f"upsert:{len(rows)}")
                if fail_on_batch is not None and log.count("upsert:2") + log.count("upsert:1") == fail_on_batch:
                    raise RuntimeError("Milvus 写失败")

            def flush(self):
                log.append("flush")

            def count(self):
                log.append("count")
                return n_items

        repo = SimpleNamespace(engine=engine, mark_indexed=lambda ids: log.append(f"mark:{len(ids)}"))
        monkeypatch.setattr(cli.DwsRepository, "from_env", classmethod(lambda cls: repo))
        monkeypatch.setattr(rag_pkg, "build_provider", lambda kind: Provider())
        monkeypatch.setattr(milvus_mod, "MilvusIndex", FakeIndex)

        args = argparse.Namespace(
            embedding="fake", backend="milvus", milvus_uri="./x.db", milvus_analyzer="jieba",
            milvus_collection="c", rebuild=False, limit=1000, batch_size=None, kb_version="")
        return cli.cmd_index(args), log

    def test_flush_comes_after_the_last_batch_and_before_count(self, monkeypatch):
        rc, log = self._run(monkeypatch, n_items=5, batch=2)      # 批次 2+2+1
        assert rc == 0
        upserts = [i for i, e in enumerate(log) if e.startswith("upsert")]
        assert len(upserts) == 3
        assert log.count("flush") == 1, f"flush 应只调一次，实际序列 {log}"
        assert log.index("flush") > upserts[-1], "flush 必须在最后一批写完之后"
        assert log.index("count") == log.index("flush") + 1, "count 紧跟在 flush 之后"

    def test_no_flush_when_a_batch_fails(self, monkeypatch):
        """写失败时提前返回，不该对着半截数据去 flush 并报一个看起来成功的条数。"""
        rc, log = self._run(monkeypatch, n_items=5, batch=2, fail_on_batch=2)
        assert rc == 1
        assert "flush" not in log and "count" not in log
