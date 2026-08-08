"""知识覆盖度矩阵与补写任务生成。

**「人工补充」是与「清洗」并列的独立流程**——有些知识对话里根本没有，
必须人工补写。找出"哪里没有"就是这个模块的职责。

矩阵是「类目 × 问题类型」的二维热力图，空白与低密度格子自动生成补写任务。
这也是知识运营的日常工作界面。

与它配套的另一个指标在 ClickHouse 的 ``ch_kb_hit``：**从未被命中的条目占比**。
两者一起看才完整——
覆盖度回答"我们缺什么"，命中率回答"我们建的东西有没有用"。
若从未命中占比超过 40%，说明知识库在按运营的想象建设，
而不是按用户的真实需求建设。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Sequence

from .models import KnowledgeItem, KnowledgeType
from .textwidth import display_width, pad, truncate


class CellLevel(StrEnum):
    EMPTY = "empty"
    """完全没有知识，最高优先级"""
    SPARSE = "sparse"
    """有但不足"""
    OK = "ok"
    HOT = "hot"
    """条目很多，可能有重复，值得合并精修"""


@dataclass(frozen=True)
class Cell:
    category_id: int
    category_name: str
    knowledge_type: KnowledgeType
    count: int
    level: CellLevel
    demand: int = 0
    """该格子的真实用户需求量（来自 Trace 的问题分布）。
    需求高但知识为空的格子，优先级最高。"""

    @property
    def priority(self) -> int:
        """补写优先级，数值越小越紧急。"""
        if self.level is CellLevel.EMPTY:
            return 0 if self.demand > 0 else 1
        if self.level is CellLevel.SPARSE:
            return 2 if self.demand > 0 else 3
        return 9


@dataclass
class WriteTask:
    """补写任务。"""

    category_id: int
    category_name: str
    knowledge_type: KnowledgeType
    priority: int
    reason: str
    current_count: int
    target_count: int
    sample_questions: list[str] = field(default_factory=list)
    """来自 Trace 的真实用户提问，作为补写的参考。
    比让运营凭空想"用户会问什么"有效得多。"""

    @property
    def gap(self) -> int:
        return max(0, self.target_count - self.current_count)


@dataclass
class CoverageConfig:
    sparse_below: int = 3
    """低于此条目数视为稀疏。"""
    hot_above: int = 30
    target_per_cell: int = 5
    """每个格子的目标条目数。"""
    max_sample_questions: int = 5


@dataclass
class CoverageMatrix:
    cells: list[Cell] = field(default_factory=list)
    categories: dict[int, str] = field(default_factory=dict)
    knowledge_types: list[KnowledgeType] = field(default_factory=list)

    def cell(self, category_id: int, ktype: KnowledgeType) -> Cell | None:
        for c in self.cells:
            if c.category_id == category_id and c.knowledge_type is ktype:
                return c
        return None

    @property
    def empty_cells(self) -> list[Cell]:
        return [c for c in self.cells if c.level is CellLevel.EMPTY]

    @property
    def coverage_ratio(self) -> float:
        """非空格子占比。"""
        if not self.cells:
            return 0.0
        return sum(1 for c in self.cells if c.level is not CellLevel.EMPTY) / len(self.cells)

    def render(self) -> str:
        """渲染为文本热力图，供 Airflow 日志与后台展示。"""
        if not self.cells:
            return "(空矩阵)"

        symbols = {
            CellLevel.EMPTY: "  ·",
            CellLevel.SPARSE: "  ▁",
            CellLevel.OK: "  ▆",
            CellLevel.HOT: "  █",
        }
        types = self.knowledge_types
        # 全部按**显示列**排版：类目名与知识类型都是中文，汉字占两列，
        # 按字符补齐会让每一行按自己名字的长度错开，热力图就没法竖着看了
        # 截到 6 列而列宽 8：留出两列水槽，否则相邻的类型名会连在一起
        header = pad("类目", 16) + "".join(
            pad(truncate(str(t), 6), 8, right=True) for t in types
        )
        rule = "=" * display_width(header)
        lines = ["知识覆盖度矩阵", rule, header, "-" * display_width(header)]

        for cid, name in self.categories.items():
            row = pad(truncate(name, 16), 16)
            for t in types:
                c = self.cell(cid, t)
                row += pad(symbols[c.level] if c else "  ·", 8, right=True)
            lines.append(row)

        lines.append("-" * display_width(header))
        lines.append("· 空  ▁ 稀疏  ▆ 正常  █ 密集")
        lines.append(f"覆盖率 {self.coverage_ratio:.1%}，空白格 {len(self.empty_cells)} 个")
        return "\n".join(lines)


def build_matrix(
    items: Iterable[KnowledgeItem],
    categories: dict[int, str],
    *,
    config: CoverageConfig | None = None,
    demand: dict[tuple[int, KnowledgeType], int] | None = None,
    knowledge_types: Sequence[KnowledgeType] | None = None,
) -> CoverageMatrix:
    """构建覆盖度矩阵。

    Args:
        categories: {类目 ID: 类目名}。决定矩阵的行。
        demand: {(类目, 知识类型): 用户提问次数}。来自 Trace 统计。
            没有它矩阵只能告诉你"哪里空"，有了它才能告诉你"哪里空且用户在问"。
    """
    cfg = config or CoverageConfig()
    demand = demand or {}
    types = list(knowledge_types) if knowledge_types else [
        KnowledgeType.SPEC, KnowledgeType.SIZING, KnowledgeType.LOGISTICS,
        KnowledgeType.AFTERSALE, KnowledgeType.USAGE, KnowledgeType.PROMOTION,
    ]

    counter: Counter[tuple[int, KnowledgeType]] = Counter()
    for item in items:
        # 一条知识可能关联多个商品，但类目通常唯一
        if item.category_id is not None:
            counter[(item.category_id, item.knowledge_type)] += 1

    cells: list[Cell] = []
    for cid, name in categories.items():
        for ktype in types:
            n = counter[(cid, ktype)]
            if n == 0:
                level = CellLevel.EMPTY
            elif n < cfg.sparse_below:
                level = CellLevel.SPARSE
            elif n > cfg.hot_above:
                level = CellLevel.HOT
            else:
                level = CellLevel.OK
            cells.append(Cell(
                category_id=cid, category_name=name, knowledge_type=ktype,
                count=n, level=level, demand=demand.get((cid, ktype), 0),
            ))

    return CoverageMatrix(cells=cells, categories=dict(categories), knowledge_types=types)


def generate_write_tasks(
    matrix: CoverageMatrix,
    *,
    config: CoverageConfig | None = None,
    sample_questions: dict[tuple[int, KnowledgeType], list[str]] | None = None,
    limit: int | None = None,
) -> list[WriteTask]:
    """从矩阵的空白与稀疏格子生成补写任务，按优先级排序。"""
    cfg = config or CoverageConfig()
    samples = sample_questions or {}
    tasks: list[WriteTask] = []

    for cell in matrix.cells:
        if cell.level not in (CellLevel.EMPTY, CellLevel.SPARSE):
            continue

        if cell.level is CellLevel.EMPTY:
            reason = (
                f"该类目下无「{cell.knowledge_type}」类知识"
                + (f"，但用户已提问 {cell.demand} 次" if cell.demand else "")
            )
        else:
            reason = f"仅 {cell.count} 条，低于目标 {cfg.target_per_cell} 条"

        key = (cell.category_id, cell.knowledge_type)
        tasks.append(WriteTask(
            category_id=cell.category_id,
            category_name=cell.category_name,
            knowledge_type=cell.knowledge_type,
            priority=cell.priority,
            reason=reason,
            current_count=cell.count,
            target_count=cfg.target_per_cell,
            sample_questions=samples.get(key, [])[: cfg.max_sample_questions],
        ))

    # 优先级 → 需求量降序 → 缺口降序
    tasks.sort(key=lambda t: (t.priority, -t.gap))
    return tasks[:limit] if limit else tasks


def demand_from_questions(
    questions: Iterable[tuple[int, KnowledgeType]],
) -> dict[tuple[int, KnowledgeType], int]:
    """把 Trace 里的真实提问聚合为需求分布。"""
    d: defaultdict[tuple[int, KnowledgeType], int] = defaultdict(int)
    for key in questions:
        d[key] += 1
    return dict(d)
