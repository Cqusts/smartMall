"""任务表的读写。

两处值得单独看：:meth:`MySqlTaskStore.enqueue` 的去重，
和 :meth:`MySqlTaskStore.claim` 的原子认领。别处都是普通 CRUD。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .state import AgentTask, Status


@runtime_checkable
class TaskStore(Protocol):
    def enqueue(self, **kw: Any) -> AgentTask | None: ...
    def pull(self, limit: int = 10,
             kinds: tuple[str, ...] = ()) -> list[AgentTask]: ...
    def claim(self, task_id: int) -> bool: ...
    def finish(self, task_id: int, status: str, **kw: Any) -> bool: ...
    def recent(self, limit: int = 30,
               status: str = "") -> list[AgentTask]: ...


def _loads(raw: Any) -> dict[str, Any]:
    """JSON 列。MySQL 的 JSON 类型回来是 dict，SQLite 上是 str——
    两种都兜住，不然测试绿、线上炸（或者反过来）。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _row(r: Any) -> AgentTask:
    d = dict(r)
    return AgentTask(
        id=d.get("id"), kind=d.get("kind", ""), status=d.get("status", ""),
        dedupe_key=d.get("dedupe_key", ""),
        source_agent=d.get("source_agent") or "",
        target_agent=d.get("target_agent") or "",
        times=int(d.get("times") or 1), priority=int(d.get("priority") or 0),
        product_id=d.get("product_id"), parent_id=d.get("parent_id"),
        root_id=d.get("root_id"), attempts=int(d.get("attempts") or 0),
        max_attempts=int(d.get("max_attempts") or 3),
        error=d.get("error") or "", payload=_loads(d.get("payload")),
        result=_loads(d.get("result")), created_at=str(d.get("created_at") or ""),
    )


@dataclass
class MySqlTaskStore:
    engine: Any

    @classmethod
    def from_env(cls) -> "MySqlTaskStore":
        from smartmall_pipeline.repository import DwsRepository

        return cls(engine=DwsRepository.from_env().engine)

    # ------------------------------------------------------------ 派活

    def enqueue(self, *, kind: str, dedupe_key: str, payload: dict,
                source_agent: str = "", target_agent: str = "",
                priority: int = 0, product_id: int | None = None,
                parent_id: int | None = None, root_id: int | None = None,
                max_attempts: int = 3) -> AgentTask | None:
        """排一条任务。**同一件事已经在排队就不新建，只把 times 加一。**

        那个计数不是统计口径的装饰：被问得多的盲点该先补，所以它同时
        推高 priority。

        实现是「插，撞了再改」而不是「先查再插」：后者在两个进程同时派
        同一件活时会双双查到"没有"然后插两条，而唯一索引会让其中一条
        直接抛异常——把客服那一轮带崩。这里让唯一索引来当裁判，
        撞上就走更新分支，两个进程谁先谁后都对。

        **SQL 刻意写成方言中立的**（不用 ``ON DUPLICATE KEY UPDATE``，
        不用 ``GREATEST``）。写成 MySQL 专用的话，测试只能跑在替身上，
        这段真正的 SQL 就一次都没被执行过——而这个项目已经栽过一次
        （``LAST_INSERT_ID()`` 在 SQLite 上直接炸）。
        """
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        params = {
            "kind": kind, "src": source_agent, "dst": target_agent,
            "dk": dedupe_key, "prio": priority,
            "payload": json.dumps(payload, ensure_ascii=False),
            "pid": product_id, "parent": parent_id, "root": root_id,
            "max_att": max_attempts,
        }
        try:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO agent_task (kind, status, source_agent,"
                    " target_agent, dedupe_key, open_key, times, priority,"
                    " payload, product_id, parent_id, root_id, max_attempts)"
                    " VALUES (:kind, 'pending', :src, :dst, :dk, :dk, 1,"
                    " :prio, :payload, :pid, :parent, :root, :max_att)"),
                    params)
        except IntegrityError:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "UPDATE agent_task SET times = times + 1,"
                    " priority = CASE WHEN priority < :prio THEN :prio"
                    "                 ELSE priority END,"
                    # 已经跑挂过的任务再次被派到，重新排队——盲点又出现了，
                    # 说明它还没解决。running 的不动，那是别人正在做
                    " status = CASE WHEN status = 'running' THEN status"
                    "               ELSE 'pending' END"
                    " WHERE open_key = :dk"),
                    {"dk": dedupe_key, "prio": priority})

        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM agent_task WHERE open_key = :dk"),
                {"dk": dedupe_key}).mappings().all()
        if not rows:
            return None
        task = _row(rows[0])
        # root_id 指向自己：这条就是链的头。**建表时填不了**，
        # 那时还没有自增出来的 id
        if task.root_id is None and task.id is not None:
            self._set_root(task.id)
            task.root_id = task.id
        return task

    def _set_root(self, task_id: int) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_task SET root_id = :id WHERE id = :id"
                " AND root_id IS NULL"), {"id": task_id})

    # ------------------------------------------------------------ 干活

    def pull(self, limit: int = 10, kinds: tuple[str, ...] = ()) -> list[AgentTask]:
        """待办，按优先级。**只是候选**——真正拿到手要靠 claim。"""
        from sqlalchemy import text

        sql = ("SELECT * FROM agent_task WHERE status = 'pending'"
               " AND attempts < max_attempts")
        params: dict[str, Any] = {"n": int(limit)}
        if kinds:
            marks = ",".join(f":k{i}" for i in range(len(kinds)))
            sql += f" AND kind IN ({marks})"
            params.update({f"k{i}": k for i, k in enumerate(kinds)})
        sql += " ORDER BY priority DESC, id ASC LIMIT :n"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [_row(r) for r in rows]

    def claim(self, task_id: int) -> bool:
        """认领。**带条件的 UPDATE，不是先查后改。**

        先 SELECT 出 pending 再 UPDATE 的话，两个 worker 会同时读到
        pending 然后都去做——一条盲点被补写两次，库里多一条重复知识。
        """
        from sqlalchemy import text

        with self.engine.begin() as conn:
            cur = conn.execute(text(
                "UPDATE agent_task SET status = 'running',"
                " attempts = attempts + 1, claimed_at = CURRENT_TIMESTAMP"
                " WHERE id = :id AND status = 'pending'"), {"id": task_id})
            return cur.rowcount == 1

    def finish(self, task_id: int, status: str, *, error: str = "",
               result: dict | None = None) -> bool:
        """收尾。终态清掉 ``open_key``，同类任务才能再排下一条。

        ``failed`` 且还有重试次数时退回 pending —— 而不是留在 running，
        那样它会永远卡在那里，且不占 open_key、还会被再派一条出来。
        """
        from sqlalchemy import text

        retry = status == Status.FAILED
        with self.engine.begin() as conn:
            if retry:
                cur = conn.execute(text(
                    "UPDATE agent_task SET"
                    "  status = CASE WHEN attempts < max_attempts"
                    "                THEN 'pending' ELSE 'failed' END,"
                    "  open_key = CASE WHEN attempts < max_attempts"
                    "                  THEN dedupe_key ELSE NULL END,"
                    "  error = :err, result = :res,"
                    "  finished_at = CASE WHEN attempts < max_attempts"
                    "                     THEN NULL ELSE CURRENT_TIMESTAMP END"
                    " WHERE id = :id"),
                    {"id": task_id, "err": error[:512],
                     "res": json.dumps(result or {}, ensure_ascii=False)})
            else:
                cur = conn.execute(text(
                    "UPDATE agent_task SET status = :st, open_key = NULL,"
                    " error = :err, result = :res,"
                    " finished_at = CURRENT_TIMESTAMP WHERE id = :id"),
                    {"id": task_id, "st": status, "err": error[:512],
                     "res": json.dumps(result or {}, ensure_ascii=False)})
            return cur.rowcount > 0

    # ------------------------------------------------------------ 看

    def recent(self, limit: int = 30, status: str = "") -> list[AgentTask]:
        from sqlalchemy import text

        sql = "SELECT * FROM agent_task"
        params: dict[str, Any] = {"n": int(limit)}
        if status:
            sql += " WHERE status = :st"
            params["st"] = status
        sql += " ORDER BY id DESC LIMIT :n"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [_row(r) for r in rows]

    def chain(self, root_id: int) -> list[AgentTask]:
        """一整条链。**闭环长什么样，就看这个返回几条。**"""
        from sqlalchemy import text

        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM agent_task WHERE root_id = :r ORDER BY id"),
                {"r": int(root_id)}).mappings().all()
        return [_row(r) for r in rows]


@dataclass
class StubTaskStore:
    """内存替身。行为要和 MySQL 那份**一致到去重与认领**——
    这两条正是最容易两边分叉、而分叉了测试还全绿的地方。"""

    rows: list[AgentTask] = field(default_factory=list)
    next_id: int = 900
    fail: bool = False

    def enqueue(self, *, kind, dedupe_key, payload, source_agent="",
                target_agent="", priority=0, product_id=None, parent_id=None,
                root_id=None, max_attempts=3) -> AgentTask | None:
        if self.fail:
            raise RuntimeError("注入的派活故障")
        for t in self.rows:
            if t.dedupe_key == dedupe_key and t.status in Status.OPEN:
                t.times += 1
                t.priority = max(t.priority, priority)
                return t
        self.next_id += 1
        task = AgentTask(
            id=self.next_id, kind=kind, dedupe_key=dedupe_key,
            payload=dict(payload), source_agent=source_agent,
            target_agent=target_agent, priority=priority,
            product_id=product_id, parent_id=parent_id,
            root_id=root_id or self.next_id, max_attempts=max_attempts)
        self.rows.append(task)
        return task

    def pull(self, limit=10, kinds=()) -> list[AgentTask]:
        todo = [t for t in self.rows
                if t.status == Status.PENDING and t.attempts < t.max_attempts
                and (not kinds or t.kind in kinds)]
        todo.sort(key=lambda t: (-t.priority, t.id or 0))
        return todo[:limit]

    def claim(self, task_id) -> bool:
        for t in self.rows:
            if t.id == task_id and t.status == Status.PENDING:
                t.status = Status.RUNNING
                t.attempts += 1
                return True
        return False

    def finish(self, task_id, status, *, error="", result=None) -> bool:
        for t in self.rows:
            if t.id != task_id:
                continue
            if status == Status.FAILED and t.attempts < t.max_attempts:
                t.status = Status.PENDING     # 还能重试就退回队列
            else:
                t.status = status
            t.error = error
            t.result = dict(result or {})
            return True
        return False

    def recent(self, limit=30, status="") -> list[AgentTask]:
        rows = [t for t in self.rows if not status or t.status == status]
        return list(reversed(rows))[:limit]

    def chain(self, root_id) -> list[AgentTask]:
        return [t for t in self.rows if t.root_id == root_id]
