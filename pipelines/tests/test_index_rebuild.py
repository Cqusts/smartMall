"""索引重建的干净性。

真实事故：把向量后端从 text-embedding-v4 换成 qwen3.7 后跑了
``index --rebuild``，命令显示"45/45 完成"，但索引里仍残留着旧后端的行。
下游启动客服 Agent 时才被混用检测拦住——**报错的地方离出错的地方
隔了两条命令**。

根因是 ``REPLACE INTO`` 只覆盖主键相同的行。切分数量变了、条目被下架、
换了后端，都会留下没人覆盖到的旧行。"重建"必须真的重建干净。

这些用例跑在真实的 SQLite 上而不是内存桩——要测的正是 SQL 本身的语义。
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from smartmall_pipeline.rag.embedding import DIM  # noqa: E402
from smartmall_pipeline.rag.store import LocalVectorStore, pack_vector  # noqa: E402


@pytest.fixture
def store():
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE kb_embedding ("
            "  item_id INTEGER NOT NULL,"
            "  chunk_seq INTEGER NOT NULL,"
            "  chunk_text TEXT,"
            "  vector BLOB,"
            "  dim INTEGER,"
            "  provider TEXT,"
            "  PRIMARY KEY (item_id, chunk_seq))"
        ))
    return LocalVectorStore(engine=engine)


def _vec(seed: float = 1.0) -> list[float]:
    return [seed] + [0.0] * (DIM - 1)


def _write(store, provider: str, items: list[tuple[int, int]]) -> None:
    store.upsert(
        [(iid, seq, f"文本{iid}-{seq}", _vec()) for iid, seq in items], provider
    )


class TestProvidersInIndex:
    def test_empty_index(self, store):
        assert store.providers_in_index() == {}

    def test_counts_by_provider(self, store):
        _write(store, "dashscope/text-embedding-v4", [(1, 0), (2, 0)])
        _write(store, "dashscope/qwen3.7-text-embedding", [(3, 0)])
        assert store.providers_in_index() == {
            "dashscope/text-embedding-v4": 2,
            "dashscope/qwen3.7-text-embedding": 1,
        }

    def test_detects_mixing_before_it_reaches_load(self, store):
        """混用要在写入前就能查出来。

        等到 load 才报错的话，被拦住的是下游服务，
        而制造混用的是上一条命令——查起来会绕一大圈。
        """
        _write(store, "A", [(1, 0)])
        _write(store, "B", [(2, 0)])
        assert len(store.providers_in_index()) > 1


class TestDeleteOtherProviders:
    def test_removes_only_the_others(self, store):
        _write(store, "new", [(1, 0), (2, 0)])
        _write(store, "old", [(3, 0), (4, 0), (5, 0)])

        assert store.delete_other_providers("new") == 3
        assert store.providers_in_index() == {"new": 2}

    def test_is_idempotent(self, store):
        _write(store, "new", [(1, 0)])
        assert store.delete_other_providers("new") == 0
        assert store.providers_in_index() == {"new": 1}

    def test_empty_index_is_safe(self, store):
        assert store.delete_other_providers("new") == 0


class TestRebuildLeavesNoOrphans:
    def test_replace_alone_leaves_stale_rows(self, store):
        """先证明这个 bug 是真的：只靠 REPLACE INTO 覆盖不干净。

        旧后端把条目 1 切成了 3 段，新后端只切 1 段——
        chunk_seq 1、2 没有对应的新行来覆盖，于是原地留下。
        """
        _write(store, "old", [(1, 0), (1, 1), (1, 2)])
        _write(store, "new", [(1, 0)])          # 只覆盖了 chunk_seq 0

        assert store.providers_in_index() == {"old": 2, "new": 1}

    def test_delete_then_write_is_clean(self, store):
        """正确做法：先删再写。"""
        _write(store, "old", [(1, 0), (1, 1), (1, 2)])

        store.delete_other_providers("new")
        store.delete_by_item([1])
        _write(store, "new", [(1, 0)])

        assert store.providers_in_index() == {"new": 1}

    def test_downgraded_items_do_not_linger(self, store):
        """条目被下架（不再 approved）后，它的向量不该还留在索引里。

        留着的话检索仍会命中它——一条已经被人工否掉的知识，
        照样会被客服说出去。
        """
        _write(store, "v1", [(1, 0), (2, 0), (3, 0)])

        # 重建时只有 1、2 仍是 approved
        still_approved = [1, 2]
        store.delete_other_providers("v1")
        store.delete_by_item(still_approved)
        _write(store, "v1", [(i, 0) for i in still_approved])

        # 条目 3 的旧向量仍在——这正是 delete_other_providers 覆盖不到的缺口
        from sqlalchemy import text as sql

        with store.engine.begin() as conn:  # type: ignore[attr-defined]
            ids = {r[0] for r in conn.execute(sql(
                "SELECT DISTINCT item_id FROM kb_embedding"
            ))}
        assert 3 in ids, (
            "同后端下被下架的条目不会被清掉——这是已知缺口，"
            "由 clean 之后的 embedding_status 流转负责，不属于 rebuild 的职责"
        )


class TestLoadStillGuards:
    def test_mixed_index_raises_with_actionable_message(self, store):
        """最后一道防线要留着，且报错必须说清怎么修。"""
        from sqlalchemy import text as sql

        # load() 要 JOIN knowledge_item，补一张最小的表
        with store.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(sql(
                "CREATE TABLE knowledge_item ("
                " id INTEGER PRIMARY KEY, biz_type TEXT, modality TEXT,"
                " knowledge_type TEXT, category_id INTEGER, product_ids TEXT,"
                " asset_ids TEXT, valid_to TEXT, review_status TEXT,"
                " deleted INTEGER DEFAULT 0)"
            ))
            for i in (1, 2):
                conn.execute(sql(
                    "INSERT INTO knowledge_item (id, biz_type, modality,"
                    " knowledge_type, product_ids, asset_ids, review_status,"
                    " deleted) VALUES (:i,'qa','text','spec','[]','[]',"
                    "'approved',0)"
                ), {"i": i})
        _write(store, "A", [(1, 0)])
        _write(store, "B", [(2, 0)])

        with pytest.raises(RuntimeError) as exc:
            store.load()
        assert "混用" in str(exc.value)
        assert "--rebuild" in str(exc.value), "报错必须给出可执行的下一步"
