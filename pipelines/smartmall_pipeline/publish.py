"""数据资产版本发布与质量门禁。

**为什么必须版本化**：模型评测得分是 ``模型 × 数据版本`` 的函数。
没有版本号，"这次微调效果变好了"这句话无法归因——是超参调对了，
还是数据变多了？发版本 + 评测结果绑版本，才能做因果归因，
也才能在效果变差时回滚。

质量门禁不通过不许发版。门禁不是形式——每一条都对应一类真实事故：

============================ ==========================================
门禁                          防的是什么
============================ ==========================================
已审核占比 ≥ 95%              未审核的知识进了索引，客服说错话
平均质量分 ≥ 0.7              低质量知识稀释检索结果
单一来源占比 ≤ 70%            全是合成数据，学不到真实语感
类目覆盖不倒退                新版本漏掉了某个类目的知识
条目数波动 ≤ 30%              清洗规则改错导致大批数据被误杀
重复条目率 ≤ 2%               去重失效，同一问题多份答案互相打架
PII 残留 = 0                  合规红线
============================ ==========================================
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .gates import pii
from .models import KnowledgeItem, ReviewStatus
from .textwidth import pad


@dataclass
class GateThresholds:
    min_approved_ratio: float = 0.95
    min_avg_quality: float = 0.70
    max_synthetic_ratio: float = 0.50
    """合成数据占比上限（阻断）。

    风险是"全是合成数据学不到真实语感"，所以约束的对象是**合成来源**，
    而不是任何来源。冷启动期 JDDC 合法地占 70%（见 docs/00-overview.md
    的三条腿策略），若在这里一刀切限制单一来源，第一次发版就会被迫 force，
    门禁也就失去意义了。
    """
    warn_single_source_ratio: float = 0.80
    """单一来源集中度（仅告警，不阻断）。用于提醒数据多样性在下降。"""
    max_duplicate_ratio: float = 0.02
    max_count_drift: float = 0.30
    """相对上一版本的条目数波动上限。超过需人工确认。"""
    require_category_non_regression: bool = True
    max_pii_residual: int = 0


@dataclass
class GateCheck:
    name: str
    passed: bool
    actual: float | int | str
    expected: str
    blocking: bool = True

    def render(self) -> str:
        mark = "✓" if self.passed else ("✗" if self.blocking else "⚠")
        return (f"  {mark} {pad(self.name, 20)}"
                f" 实际={pad(str(self.actual), 12)} 要求={self.expected}")


@dataclass
class PublishResult:
    version: str
    passed: bool
    checks: list[GateCheck] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    item_count: int = 0
    snapshot_path: str | None = None

    @property
    def blocking_failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed and c.blocking]

    def render(self) -> str:
        lines = [f"数据资产发布检查 {self.version}", "=" * 62]
        lines += [c.render() for c in self.checks]
        lines.append("=" * 62)
        if self.passed:
            lines.append(f"✅ 门禁通过，可发布 {self.item_count} 条")
        else:
            reasons = "、".join(c.name for c in self.blocking_failures)
            lines.append(f"❌ 门禁未通过：{reasons}")
        return "\n".join(lines)


def compute_stats(items: Sequence[KnowledgeItem]) -> dict[str, Any]:
    """统计信息，写入 dataset_version.stats。"""
    if not items:
        return {"count": 0}

    approved = [i for i in items if i.review_status in (ReviewStatus.APPROVED, ReviewStatus.REVISED)]
    quality = [i.quality_score for i in items if i.quality_score is not None]
    dedup_keys = Counter(i.dedup_key() for i in items)
    duplicates = sum(c - 1 for c in dedup_keys.values() if c > 1)

    return {
        "count": len(items),
        "approved_count": len(approved),
        "approved_ratio": len(approved) / len(items),
        "avg_quality": sum(quality) / len(quality) if quality else 0.0,
        "duplicate_count": duplicates,
        "duplicate_ratio": duplicates / len(items),
        "by_source": dict(Counter(i.source for i in items)),
        "by_biz_type": dict(Counter(str(i.biz_type) for i in items)),
        "by_modality": dict(Counter(str(i.modality) for i in items)),
        "by_knowledge_type": dict(Counter(str(i.knowledge_type) for i in items)),
        "categories": sorted({i.category_id for i in items if i.category_id}),
        "with_assets": sum(1 for i in items if i.asset_ids),
        "with_expiry": sum(1 for i in items if i.valid_to is not None),
    }


def check_gates(
    items: Sequence[KnowledgeItem],
    thresholds: GateThresholds | None = None,
    *,
    previous_stats: dict[str, Any] | None = None,
) -> list[GateCheck]:
    """执行全部质量门禁。"""
    th = thresholds or GateThresholds()
    stats = compute_stats(items)
    checks: list[GateCheck] = []

    if not items:
        return [GateCheck("非空", False, 0, "> 0")]

    ratio = stats["approved_ratio"]
    checks.append(GateCheck(
        "已审核占比", ratio >= th.min_approved_ratio,
        f"{ratio:.1%}", f"≥ {th.min_approved_ratio:.0%}",
    ))

    avg_q = stats["avg_quality"]
    checks.append(GateCheck(
        "平均质量分", avg_q >= th.min_avg_quality,
        f"{avg_q:.3f}", f"≥ {th.min_avg_quality}",
    ))

    by_source = stats["by_source"]

    # 合成数据占多数 → 阻断。风格数据必须来自真实业务，合成数据学不出真人的语感。
    synthetic = sum(v for k, v in by_source.items() if k in ("synthetic", "manual_synth"))
    syn_ratio = synthetic / len(items)
    checks.append(GateCheck(
        "合成数据占比", syn_ratio <= th.max_synthetic_ratio,
        f"{syn_ratio:.1%}", f"≤ {th.max_synthetic_ratio:.0%}",
    ))

    # 单一来源集中度 → 仅告警。冷启动期公开数据集合法地占大头。
    top_ratio = max(by_source.values()) / len(items) if by_source else 0.0
    top_source = max(by_source, key=by_source.get) if by_source else "-"  # type: ignore[arg-type]
    checks.append(GateCheck(
        "来源集中度", top_ratio <= th.warn_single_source_ratio,
        f"{top_source} {top_ratio:.1%}", f"≤ {th.warn_single_source_ratio:.0%}（告警）",
        blocking=False,
    ))

    dup_ratio = stats["duplicate_ratio"]
    checks.append(GateCheck(
        "重复条目率", dup_ratio <= th.max_duplicate_ratio,
        f"{dup_ratio:.2%}", f"≤ {th.max_duplicate_ratio:.0%}",
    ))

    # PII 残留是合规红线，全量扫描
    residual = sum(
        1 for i in items
        if pii.contains_pii(i.content) or pii.contains_pii(i.title or "")
    )
    checks.append(GateCheck(
        "PII 残留", residual <= th.max_pii_residual,
        residual, f"≤ {th.max_pii_residual}",
    ))

    if previous_stats:
        prev_count = previous_stats.get("count", 0)
        if prev_count:
            drift = abs(len(items) - prev_count) / prev_count
            checks.append(GateCheck(
                "条目数波动", drift <= th.max_count_drift,
                f"{drift:+.1%}", f"≤ {th.max_count_drift:.0%}",
            ))

        if th.require_category_non_regression:
            prev_cats = set(previous_stats.get("categories") or [])
            cur_cats = set(stats["categories"])
            missing = prev_cats - cur_cats
            checks.append(GateCheck(
                "类目覆盖不倒退", not missing,
                f"缺失 {sorted(missing)}" if missing else "无倒退", "无缺失",
            ))

    return checks


def to_jsonl(items: Iterable[KnowledgeItem]) -> str:
    """导出为 JSONL 快照。ODS 只增不改，因此任何版本都可从原始数据完整重跑复现。"""
    lines = []
    for item in items:
        payload = item.model_dump(mode="json", exclude_none=True)
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def publish(
    items: Sequence[KnowledgeItem],
    version: str,
    *,
    thresholds: GateThresholds | None = None,
    previous_stats: dict[str, Any] | None = None,
    snapshot_dir: Path | str | None = None,
    force: bool = False,
) -> PublishResult:
    """执行发版检查，通过则写快照。

    Args:
        force: 跳过阻断性门禁。**只应由人工在后台显式确认后使用**，
            并且会在 stats 里留下 forced 标记供审计。
    """
    checks = check_gates(items, thresholds, previous_stats=previous_stats)
    blocking = [c for c in checks if not c.passed and c.blocking]
    passed = not blocking or force

    result = PublishResult(
        version=version,
        passed=passed,
        checks=checks,
        stats=compute_stats(items),
        item_count=len(items),
    )
    if force and blocking:
        result.stats["forced"] = True
        result.stats["forced_over"] = [c.name for c in blocking]

    if passed and snapshot_dir is not None:
        d = Path(snapshot_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{version}.jsonl"
        path.write_text(to_jsonl(items), encoding="utf-8")
        result.snapshot_path = str(path)
        result.stats["published_at"] = datetime.now().isoformat()

    return result


def select_publishable(items: Iterable[KnowledgeItem]) -> list[KnowledgeItem]:
    """筛选可发布的条目。

    对应 dataset_version.filter_sql 的语义：已审核、未删除、未过期。
    """
    now = datetime.now()
    return [
        i for i in items
        if i.review_status in (ReviewStatus.APPROVED, ReviewStatus.REVISED)
        and (i.valid_to is None or i.valid_to > now)
    ]
