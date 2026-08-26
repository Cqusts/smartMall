"""ai-rag 的切分与检索逻辑测试。

这两块是纯逻辑，不依赖 Milvus，因此可以完整测试。
Milvus 的实际调用（milvus_store.py）需要真实实例，属于集成测试范畴。
"""

from __future__ import annotations

import time

import pytest

from smartmall_pipeline.rag.store import SEARCHABLE_REVIEW_STATUS

from app import chunking
from app.retrieval import (
    Hit,
    RetrievalConfig,
    RetrievalOutcome,
    apply_threshold,
    build_filter_expr,
    dedup_by_item,
    needs_rewrite,
    reciprocal_rank_fusion,
)


def _hit(cid: int, score: float, item_id: int | None = None) -> Hit:
    return Hit(
        chunk_id=cid,
        item_id=item_id if item_id is not None else cid,
        text=f"文本{cid}",
        score=score,
    )


# ---------------------------------------------------------------- 切分


class TestChunking:
    @pytest.mark.parametrize("biz_type", ["qa", "spec", "faq", "image", "video"])
    def test_atomic_types_are_not_split(self, biz_type):
        """能不切就不切：QA 切开会破坏问答对应关系。"""
        long_text = "这是很长的答案。" * 200
        chunks = chunking.chunk(biz_type, long_text)
        assert len(chunks) == 1

    def test_qa_indexes_question_with_answer(self):
        """用户的提问更接近"问题"，只索引答案会显著降低召回。"""
        chunks = chunking.chunk("qa", "100%羊毛", title="这件是什么面料")
        assert "这件是什么面料" in chunks[0].indexed_text
        assert "100%羊毛" in chunks[0].indexed_text

    def test_empty_text_yields_nothing(self):
        assert chunking.chunk("qa", "") == []
        assert chunking.chunk("policy", "   ") == []

    def test_script_splits_with_overlap(self):
        text = "".join(f"这是第{i}句话讲解商品卖点非常重要。" for i in range(60))
        chunks = chunking.chunk_script(text, max_chars=400, overlap=50)
        assert len(chunks) > 1
        assert all(len(c.text) <= 500 for c in chunks)
        # 相邻块应有重叠：前一块的尾部出现在后一块开头
        assert chunks[1].text[:10] in chunks[0].text

    def test_script_short_text_single_chunk(self):
        chunks = chunking.chunk_script("这个克重你摸一下就知道。")
        assert len(chunks) == 1

    def test_policy_keeps_heading_path(self):
        """切片脱离上下文后必须仍可理解。"""
        doc = (
            "# 售后政策\n"
            "## 退换货\n"
            "### 七天无理由\n"
            "需在签收后7日内提出申请，商品不影响二次销售。\n"
        )
        chunks = chunking.chunk("policy", doc)
        leaf = [c for c in chunks if "签收后7日内" in c.text]
        assert leaf, "未找到正文切片"
        path = leaf[0].heading_path
        assert "售后政策" in path and "七天无理由" in path
        # 标题路径必须进入被索引的文本
        assert "七天无理由" in leaf[0].indexed_text

    def test_policy_splits_long_section_by_clause(self):
        body = "".join(f"{i}. 这是第{i}条规则的详细说明内容。" for i in range(1, 40))
        chunks = chunking.chunk("policy", f"# 规则\n{body}")
        assert len(chunks) > 1
        assert all(len(c.text) <= 600 for c in chunks)

    def test_unknown_biz_type_falls_back_to_atomic(self):
        """未知类型宁可块大也不要切错。"""
        chunks = chunking.chunk("未知类型", "一些内容" * 100)
        assert len(chunks) == 1


# ---------------------------------------------------------------- 过滤条件


class TestFilterExpr:
    def test_always_includes_hard_filters(self):
        """三条硬性过滤任何查询都必须带上。"""
        expr = build_filter_expr(kb_version="kb-v3")
        assert "review_status in [" in expr
        assert "valid_to_ts" in expr
        assert 'kb_version == "kb-v3"' in expr

    def test_hard_filters_present_even_without_version(self):
        expr = build_filter_expr()
        assert "review_status in [" in expr
        assert "valid_to_ts" in expr

    def test_searchable_statuses_agree_with_the_local_backend(self):
        """两个后端对「什么算可检索」必须是同一份定义。

        改之前 ``build_filter_expr`` 只收 ``approved``，而
        ``LocalVectorStore.load()`` 收 ``('approved','revised')``——
        人工改写过的知识（revised，恰恰是质量最高的那批）在本地能查到、
        换到 Milvus 就查不到，**而且不报错**。
        """
        expr = build_filter_expr()
        for status in SEARCHABLE_REVIEW_STATUS:
            assert f'"{status}"' in expr, f"{status} 应该算可检索"
        assert "revised" in expr, "人工改写过的知识必须能被检索到"
        assert "pending" not in expr and "rejected" not in expr, "未审核的绝不能进结果"

    def test_expiry_uses_current_time(self):
        now = int(time.time())
        expr = build_filter_expr(now_ts=now)
        assert f"valid_to_ts > {now}" in expr
        # valid_to_ts == 0 表示永久有效，必须包含在内
        assert "valid_to_ts == 0" in expr

    def test_product_filter(self):
        expr = build_filter_expr(product_ids=[1024, 2048])
        assert "array_contains_any(product_ids, [1024, 2048])" in expr

    def test_category_filter(self):
        assert "category_id == 1024" in build_filter_expr(category_id=1024)

    def test_biz_type_filter(self):
        expr = build_filter_expr(biz_types=["qa", "spec"])
        assert 'biz_type in ["qa", "spec"]' in expr

    def test_escapes_quotes(self):
        """版本号来自外部输入，不能让引号破坏表达式。"""
        expr = build_filter_expr(kb_version='v1"; drop')
        assert '\\"' in expr

    def test_clauses_joined_with_and(self):
        expr = build_filter_expr(kb_version="v1", category_id=1)
        assert expr.count(" and ") >= 2


# ---------------------------------------------------------------- RRF


class TestRrf:
    def test_document_in_both_rankings_wins(self):
        """既语义相关又关键词匹配的结果应该排最前。"""
        dense = [_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)]
        sparse = [_hit(3, 20.0), _hit(1, 15.0), _hit(4, 10.0)]
        fused = reciprocal_rank_fusion(dense, sparse, k=60)
        # 1 在两路都靠前（rank 1 和 rank 2），应排第一
        assert fused[0].chunk_id == 1

    def test_immune_to_score_scale(self):
        """dense 余弦 0-1、BM25 可以是 20+，RRF 只看排名不看分数。"""
        dense = [_hit(1, 0.01), _hit(2, 0.009)]
        sparse = [_hit(2, 9999.0), _hit(1, 8888.0)]
        fused = reciprocal_rank_fusion(dense, sparse)
        # 两者排名互补，得分应几乎相同，不被 BM25 的大数值支配
        assert abs(fused[0].score - fused[1].score) < 1e-9

    def test_respects_top_k(self):
        dense = [_hit(i, 1.0 / i) for i in range(1, 20)]
        assert len(reciprocal_rank_fusion(dense, top_k=5)) == 5

    def test_marks_source_as_fused(self):
        fused = reciprocal_rank_fusion([_hit(1, 0.9)])
        assert fused[0].source == "fused"

    def test_empty_input(self):
        assert reciprocal_rank_fusion([], []) == []

    def test_preserves_metadata(self):
        h = Hit(chunk_id=1, item_id=99, text="t", score=0.5, asset_ids=(7, 8))
        fused = reciprocal_rank_fusion([h])
        assert fused[0].item_id == 99
        assert fused[0].asset_ids == (7, 8)


class TestDedupByItem:
    def test_keeps_highest_scoring_chunk_per_item(self):
        """政策文档切 5 块全命中会占满 Top-5，把其他知识挤掉。"""
        hits = [
            _hit(1, 0.9, item_id=100),
            _hit(2, 0.8, item_id=100),
            _hit(3, 0.7, item_id=200),
        ]
        out = dedup_by_item(hits)
        assert len(out) == 2
        assert out[0].chunk_id == 1
        assert {h.item_id for h in out} == {100, 200}


# ---------------------------------------------------------------- 阈值


class TestThreshold:
    def test_high_score_answers(self):
        r = apply_threshold([_hit(1, 0.8)])
        assert r.outcome is RetrievalOutcome.ANSWER
        assert r.should_answer

    def test_mid_score_hedges(self):
        r = apply_threshold([_hit(1, 0.4)])
        assert r.outcome is RetrievalOutcome.ANSWER_HEDGED
        assert r.should_answer

    def test_mid_score_vague_query_clarifies(self):
        r = apply_threshold([_hit(1, 0.4)], query_is_vague=True)
        assert r.outcome is RetrievalOutcome.CLARIFY

    def test_low_score_refuses(self):
        """检索不到就不回答——宁可转人工也不要幻觉。"""
        r = apply_threshold([_hit(1, 0.1)])
        assert r.outcome is RetrievalOutcome.HANDOVER
        assert not r.should_answer
        assert r.hits == [], "拒答时不应把低分结果传给生成阶段"

    def test_empty_hits_handover(self):
        r = apply_threshold([])
        assert r.outcome is RetrievalOutcome.HANDOVER
        assert r.max_score == 0.0

    def test_custom_thresholds(self):
        cfg = RetrievalConfig(answer_threshold=0.9, hedge_threshold=0.8)
        assert apply_threshold([_hit(1, 0.85)], cfg).outcome is RetrievalOutcome.ANSWER_HEDGED
        assert apply_threshold([_hit(1, 0.95)], cfg).outcome is RetrievalOutcome.ANSWER


# ---------------------------------------------------------------- 查询改写


class TestNeedsRewrite:
    def test_short_query(self):
        assert needs_rewrite("会起球吗")

    def test_pronoun_query(self):
        assert needs_rewrite("这个和我上次买的一样吗质量怎么样")

    def test_complete_query_does_not_need_rewrite(self):
        assert not needs_rewrite("100%羊毛的针织衫会不会起球以及如何避免")
