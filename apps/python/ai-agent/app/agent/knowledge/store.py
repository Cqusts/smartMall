"""知识运维的读写通道。

读盲点、写待审草稿。落在这里而不是直接用 :class:`DwsRepository`，
是为了让节点能在没有数据库的环境里测——而**这条链路最需要测的恰恰是
"什么情况下不写"**，那些用例一条数据库记录都不该产生。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .state import BlindSpot


@runtime_checkable
class KnowledgeStore(Protocol):
    def blind_spots(self, limit: int = 50) -> list[BlindSpot]: ...

    def stage_draft(
        self, *, question: str, answer: str, ticket_ids: Sequence[int],
        product_id: int | None = None, intent: str = "",
        evidence: Sequence[str] = (),
    ) -> int | None: ...


@dataclass
class MySqlKnowledgeStore:
    """生产实现，走数据中台那套模型与仓储。"""

    repo: Any

    @classmethod
    def from_env(cls) -> "MySqlKnowledgeStore":
        from smartmall_pipeline.repository import DwsRepository

        return cls(repo=DwsRepository.from_env())

    def blind_spots(self, limit: int = 50) -> list[BlindSpot]:
        """从工单里读盲点。

        只读 ``open``/``answered`` 的：已经回流过（imported）的工单再拿来
        起草，就是照着已有的知识再写一遍。
        """
        from sqlalchemy import text

        with self.repo.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT question, COUNT(*) AS times,"
                " GROUP_CONCAT(id) AS ids, MAX(reason) AS reason,"
                " MAX(intent) AS intent, MAX(product_id) AS product_id "
                "FROM handover_ticket WHERE status IN ('open', 'answered') "
                "GROUP BY question ORDER BY times DESC, MIN(id) ASC LIMIT :n"
            ), {"n": limit}).mappings().all()

        return [
            BlindSpot(
                question=r["question"], times=int(r["times"]),
                ticket_ids=[int(i) for i in str(r["ids"] or "").split(",") if i],
                reason=r["reason"] or "", intent=r["intent"] or "",
                product_id=r["product_id"],
            )
            for r in rows
        ]

    def stage_draft(
        self, *, question, answer, ticket_ids, product_id=None, intent="",
        evidence=(),
    ) -> int | None:
        """写一条**待审**知识，并把工单标成已回流。

        ``source='knowledge_ops'`` 而不是混进 handover：发版门禁要能按
        来源算占比，而"机器起草的"和"人工写的"在可信度上完全不是一回事。
        混在一起统计，门禁就失去了意义。
        """
        from smartmall_pipeline.handover import Ticket, to_knowledge_item

        ticket = Ticket(
            id=ticket_ids[0] if ticket_ids else 0, question=question,
            reason="知识运维起草", intent=intent, product_id=product_id,
        )
        item = to_knowledge_item(ticket, answer,
                                 product_category=self._product_category())
        item.source = "knowledge_ops"
        item.source_ref = "ticket:" + ",".join(str(i) for i in ticket_ids)
        # 机器起草的置信度必须低于人工回流（0.9/0.85）。审核队列按这个
        # 排序，机器写的该排在人工写的后面被优先复核
        item.quality_score = 0.6
        item.confidence = 0.5
        if evidence:
            item.tags = list(item.tags) + ["机器起草"]

        item_id = self.repo.save_knowledge_item_returning_id(item)
        if item_id:
            for tid in ticket_ids:
                self.repo.answer_ticket(tid, answer, item_id)
        return item_id

    def _product_category(self) -> dict[int, int]:
        from sqlalchemy import text

        with self.repo.engine.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(
                text("SELECT id, category_id FROM product WHERE deleted = 0"))}


@dataclass
class StubKnowledgeStore:
    """测试替身。"""

    spots: list[BlindSpot] = field(default_factory=list)
    staged: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 5001
    fail: bool = False

    def blind_spots(self, limit: int = 50) -> list[BlindSpot]:
        return self.spots[:limit]

    def stage_draft(self, **kw) -> int | None:
        if self.fail:
            raise RuntimeError("注入的落库故障")
        self.staged.append(dict(kw))
        self.next_id += 1
        return self.next_id
