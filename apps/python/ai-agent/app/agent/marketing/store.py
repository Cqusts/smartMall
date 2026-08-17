"""文案落库通道。

与知识运维的 store 同一个理由：这条链路上最需要测的是**什么情况下
不写**，而那些用例一条数据库记录都不该产生。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .state import CopyDraft


@runtime_checkable
class CopyStore(Protocol):
    def stage_copy(
        self, *, product_id: int, draft: CopyDraft,
        evidence: Sequence[str] = (), demand: Sequence[dict] = (),
        flags: Sequence[str] = (), model: str = "",
    ) -> int | None: ...


@dataclass
class MySqlCopyStore:
    engine: Any

    @classmethod
    def from_env(cls) -> "MySqlCopyStore":
        from smartmall_pipeline.repository import DwsRepository

        return cls(engine=DwsRepository.from_env().engine)

    def stage_copy(self, *, product_id, draft, evidence=(), demand=(),
                   flags=(), model="") -> int | None:
        """写一条**待审**文案。

        ``review_status`` 写死 pending、``ai_generated`` 写死 1——
        这两个值不做成参数是刻意的：**能传参数就意味着某天会有人传
        approved**，而那正是这道闸门要防的事。
        """
        from sqlalchemy import text

        def dump(x):
            return json.dumps(x, ensure_ascii=False)

        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "INSERT INTO marketing_copy (product_id, title, main_images,"
                " selling_points, detail, script, evidence, demand_signals,"
                " flags, review_status, ai_generated, model) VALUES "
                "(:pid, :title, :imgs, :points, :detail, :script, :ev, :dm,"
                " :flags, 'pending', 1, :model)"),
                {"pid": product_id, "title": draft.title[:120],
                 "imgs": dump(draft.main_images), "points": dump(draft.points),
                 "detail": draft.detail, "script": draft.script,
                 "ev": dump(list(evidence)), "dm": dump(list(demand)),
                 "flags": dump(list(flags)), "model": model[:64]})
            # lastrowid 而不是 LAST_INSERT_ID()：后者是 MySQL 方言，
            # 测试跑在 SQLite 上就炸——这个项目已经栽过一次
            return cur.lastrowid


@dataclass
class StubCopyStore:
    staged: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 700
    fail: bool = False

    def stage_copy(self, **kw) -> int | None:
        if self.fail:
            raise RuntimeError("注入的落库故障")
        self.staged.append(dict(kw))
        self.next_id += 1
        return self.next_id
