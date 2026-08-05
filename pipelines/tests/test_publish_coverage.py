"""数据资产发布门禁与覆盖度矩阵测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

from smartmall_pipeline import coverage, publish
from smartmall_pipeline.models import (
    BizType,
    KnowledgeItem,
    KnowledgeType,
    ReviewStatus,
)


def _item(
    *,
    title: str = "这件是什么面料",
    content: str = "100%羊毛",
    status: ReviewStatus = ReviewStatus.APPROVED,
    quality: float = 0.85,
    source: str = "jddc",
    category_id: int | None = 1024,
    ktype: KnowledgeType = KnowledgeType.SPEC,
    valid_to: datetime | None = None,
) -> KnowledgeItem:
    return KnowledgeItem(
        biz_type=BizType.QA,
        title=title,
        content=content,
        source=source,
        source_ref="ods:1",
        review_status=status,
        quality_score=quality,
        category_id=category_id,
        knowledge_type=ktype,
        valid_to=valid_to,
    )


def _batch(n: int, *, prefix: str = "问题", **kw) -> list[KnowledgeItem]:
    """生成 n 条互不重复的条目。

    prefix 用于拼接多个批次时避免标题撞车——dedup_key 由标题决定，
    两批都从「问题0」开始会被门禁正确地判为重复。
    """
    return [_item(title=f"{prefix}{i}", **kw) for i in range(n)]


# ---------------------------------------------------------------- 门禁


class TestGates:
    def test_clean_batch_passes(self):
        r = publish.publish(_batch(100), "kb-v1")
        assert r.passed, r.render()

    def test_rejects_unapproved(self):
        items = _batch(90) + _batch(10, prefix="待审", status=ReviewStatus.PENDING)
        r = publish.publish(items, "kb-v1")
        assert not r.passed
        assert any(c.name == "已审核占比" and not c.passed for c in r.checks)

    def test_rejects_low_quality(self):
        r = publish.publish(_batch(50, quality=0.4), "kb-v1")
        assert not r.passed
        assert any(c.name == "平均质量分" and not c.passed for c in r.checks)

    def test_rejects_synthetic_dominance(self):
        """全是合成数据学不到真实语感。"""
        items = _batch(90, source="synthetic") + _batch(10, prefix="真实", source="jddc")
        r = publish.publish(items, "kb-v1")
        assert not r.passed
        assert any(c.name == "合成数据占比" and not c.passed for c in r.checks)

    def test_cold_start_jddc_majority_passes(self):
        """冷启动期 JDDC 合法地占大头，不该被阻断——只出告警。"""
        items = _batch(70, source="jddc") + _batch(30, prefix="合成", source="synthetic")
        r = publish.publish(items, "kb-v1")
        assert r.passed, r.render()

    def test_source_concentration_warns_not_blocks(self):
        r = publish.publish(_batch(100, source="jddc"), "kb-v1")
        conc = next(c for c in r.checks if c.name == "来源集中度")
        assert not conc.passed and not conc.blocking
        assert r.passed, "来源集中度不应阻断发版"

    def test_rejects_duplicates(self):
        """去重失效会导致同一问题多份答案互相打架。"""
        items = [_item(title="同一个问题") for _ in range(50)]
        r = publish.publish(items, "kb-v1")
        assert not r.passed
        assert any(c.name == "重复条目率" and not c.passed for c in r.checks)

    def test_rejects_pii_residual(self):
        """合规红线：出口不得残留 PII。"""
        items = _batch(50, source="jddc") + _batch(50, prefix="回流", source="trace")
        items[0].content = "请拨打13812345678联系我们"
        r = publish.publish(items, "kb-v1")
        assert not r.passed
        assert any(c.name == "PII 残留" and not c.passed for c in r.checks)

    def test_rejects_count_drift(self):
        """条目数腰斩通常意味着清洗规则改错，误杀了大批数据。"""
        prev = {"count": 1000, "categories": [1024]}
        r = publish.publish(_batch(100), "kb-v2", previous_stats=prev)
        assert not r.passed
        assert any(c.name == "条目数波动" and not c.passed for c in r.checks)

    def test_rejects_category_regression(self):
        prev = {"count": 100, "categories": [1024, 1025, 48]}
        r = publish.publish(_batch(100, category_id=1024), "kb-v2", previous_stats=prev)
        assert not r.passed
        assert any(c.name == "类目覆盖不倒退" and not c.passed for c in r.checks)

    def test_empty_batch_fails(self):
        assert not publish.publish([], "kb-v1").passed

    def test_force_overrides_but_records(self):
        """强制发版必须留下审计痕迹。"""
        r = publish.publish(_batch(50, quality=0.3), "kb-v1", force=True)
        assert r.passed
        assert r.stats["forced"] is True
        assert "平均质量分" in r.stats["forced_over"]

    def test_render_is_readable(self):
        out = publish.publish(_batch(10), "kb-v1").render()
        assert "kb-v1" in out and "已审核占比" in out


class TestSnapshot:
    def test_writes_jsonl(self, tmp_path):
        r = publish.publish(_batch(20), "kb-v1", snapshot_dir=tmp_path)
        assert r.snapshot_path is not None
        content = (tmp_path / "kb-v1.jsonl").read_text(encoding="utf-8")
        assert len(content.splitlines()) == 20

    def test_no_snapshot_when_gate_fails(self):
        r = publish.publish(_batch(50, quality=0.1), "kb-v1", snapshot_dir="/tmp/never")
        assert r.snapshot_path is None

    def test_jsonl_is_valid_json(self, tmp_path):
        import json

        publish.publish(_batch(5), "kb-v1", snapshot_dir=tmp_path)
        for line in (tmp_path / "kb-v1.jsonl").read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["content"]


class TestSelectPublishable:
    def test_excludes_pending_and_rejected(self):
        items = [
            _item(status=ReviewStatus.APPROVED),
            _item(status=ReviewStatus.REVISED),
            _item(status=ReviewStatus.PENDING),
            _item(status=ReviewStatus.REJECTED),
        ]
        assert len(publish.select_publishable(items)) == 2

    def test_excludes_expired(self):
        """过期的活动知识不能进版本，否则客服会播报已结束的活动。"""
        items = [
            _item(valid_to=datetime.now() - timedelta(days=1)),
            _item(valid_to=datetime.now() + timedelta(days=7)),
            _item(valid_to=None),
        ]
        assert len(publish.select_publishable(items)) == 2


# ---------------------------------------------------------------- 覆盖度


CATS = {1024: "针织衫", 1025: "羽绒服", 48: "女包"}


class TestCoverageMatrix:
    def test_empty_cells_detected(self):
        items = [_item(category_id=1024, ktype=KnowledgeType.SPEC) for _ in range(5)]
        m = coverage.build_matrix(items, CATS)
        assert m.cell(1024, KnowledgeType.SPEC).level is coverage.CellLevel.OK
        assert m.cell(1025, KnowledgeType.SPEC).level is coverage.CellLevel.EMPTY
        assert len(m.empty_cells) > 0

    def test_sparse_level(self):
        items = [_item(category_id=1024, ktype=KnowledgeType.SIZING)]
        m = coverage.build_matrix(items, CATS)
        assert m.cell(1024, KnowledgeType.SIZING).level is coverage.CellLevel.SPARSE

    def test_hot_level(self):
        items = [_item(category_id=48, ktype=KnowledgeType.AFTERSALE) for _ in range(40)]
        m = coverage.build_matrix(items, CATS)
        assert m.cell(48, KnowledgeType.AFTERSALE).level is coverage.CellLevel.HOT

    def test_coverage_ratio(self):
        assert coverage.build_matrix([], CATS).coverage_ratio == 0.0

    def test_render_contains_categories(self):
        out = coverage.build_matrix(_batch(5), CATS).render()
        assert "针织衫" in out and "覆盖率" in out


class TestWriteTasks:
    def test_empty_cell_with_demand_is_top_priority(self):
        """空白 + 用户在问 = 最高优先级。"""
        items = [_item(category_id=1024, ktype=KnowledgeType.SPEC) for _ in range(5)]
        demand = {(1025, KnowledgeType.SIZING): 120}
        m = coverage.build_matrix(items, CATS, demand=demand)
        tasks = coverage.generate_write_tasks(m)
        assert tasks[0].category_id == 1025
        assert tasks[0].knowledge_type is KnowledgeType.SIZING
        assert "120 次" in tasks[0].reason

    def test_no_tasks_for_healthy_cells(self):
        items = [
            _item(category_id=cid, ktype=kt)
            for cid in CATS
            for kt in [KnowledgeType.SPEC, KnowledgeType.SIZING, KnowledgeType.LOGISTICS,
                       KnowledgeType.AFTERSALE, KnowledgeType.USAGE, KnowledgeType.PROMOTION]
            for _ in range(5)
        ]
        m = coverage.build_matrix(items, CATS)
        assert coverage.generate_write_tasks(m) == []

    def test_includes_sample_questions(self):
        """给运营真实用户提问作参考，比让他们凭空想有效得多。"""
        m = coverage.build_matrix([], CATS)
        samples = {(1024, KnowledgeType.SPEC): ["这件什么面料", "会起球吗", "厚不厚"]}
        tasks = coverage.generate_write_tasks(m, sample_questions=samples)
        t = next(t for t in tasks if t.category_id == 1024 and t.knowledge_type is KnowledgeType.SPEC)
        assert t.sample_questions == ["这件什么面料", "会起球吗", "厚不厚"]

    def test_gap_computed(self):
        items = [_item(category_id=1024, ktype=KnowledgeType.SPEC)]
        m = coverage.build_matrix(items, CATS)
        t = next(t for t in coverage.generate_write_tasks(m)
                 if t.category_id == 1024 and t.knowledge_type is KnowledgeType.SPEC)
        assert t.current_count == 1
        assert t.gap == 4

    def test_limit(self):
        m = coverage.build_matrix([], CATS)
        assert len(coverage.generate_write_tasks(m, limit=3)) == 3

    def test_demand_aggregation(self):
        d = coverage.demand_from_questions([
            (1024, KnowledgeType.SPEC),
            (1024, KnowledgeType.SPEC),
            (48, KnowledgeType.SIZING),
        ])
        assert d[(1024, KnowledgeType.SPEC)] == 2
        assert d[(48, KnowledgeType.SIZING)] == 1
