"""agent_trace 归档：把超过保留期的行**搬**到 agent_trace_archive。

为什么要有（实测数字见迁移 015）：agent_trace 只增不减，写入不是问题，读才是——
``product_demand`` 的耗时随历史行数线性涨。归档让在线表只留近期的行。

**搬，不是删。**数据还在 agent_trace_archive，能查；只是不再占在线表的 B+ 树和
buffer pool。这个工具永远不会让一行数据"只剩删除"：每一批都是先复制、确认复制到了、
才删原行，而且删之前的数量对不上就整批回滚。

设计上要点：

* **按主键区间分批。**每批 ``batch_size`` 行一个事务——事务短，行锁和 undo 都小，
  线上写入（在表尾）不受影响。不用 ``DELETE ... WHERE created_at < x LIMIT n`` 反复删：
  那样每批都要从头扫一遍已经没有的行。
* **上界先定死。**开头查一次 ``MAX(id) WHERE created_at < cutoff``（走 created_at 索引，O(log n)），
  之后只在 ``(last, max_id]`` 里推进。否则最后一批查不到东西时，要把整张在线表扫一遍才敢收工。
* **可中断、可重跑。**每批独立提交；``NOT EXISTS`` 让复制幂等；``max_seconds`` 到点干净地停
  （放在维护窗口里跑）。中途 Ctrl-C 只会丢掉当前这一批（回滚），已搬的不受影响。
* **带点赞点踩的行默认不搬。**它们是训练数据（点踩直接当 DPO 负例），占比很小，
  留在在线表里导训练集时不必两张表 UNION。``include_feedback=True`` 才一并搬走。
* **SQL 不带 MySQL 方言**（没有 ``INSERT IGNORE``、``LIMIT`` 子查询用派生表），
  所以单测能跑在 SQLite 上；真 MySQL 上的行为另有验证（见提交说明）。

注意：删除行不会缩小 InnoDB 的文件，空出来的页留给后续插入复用。要把磁盘还给系统，
归档完之后再在维护窗口里 ``OPTIMIZE TABLE agent_trace``（会重建表、短暂阻塞写入，不自动做）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import text

#: 与 agent_trace 的列一一对应（003 + 009）。迁移 015 里归档表必须有同样的列——
#: 有单测读两份 SQL 对一遍，防止给在线表加了列却忘了归档表，归档时才报 Unknown column
TRACE_COLUMNS = (
    "id", "trace_id", "session_id", "user_id", "product_id",
    "input_text", "rewritten_query", "intent",
    "retrieval_hit_count", "retrieval_max_score", "retrieval_item_ids",
    "answer", "citations", "postcheck_flags",
    "handover", "handover_reason", "model", "latency_ms", "error",
    "thumb", "feedback_reason", "feedback_at", "created_at",
)


#: 一批遇到可重试的并发冲突（死锁、锁等待超时、复制后又被改）时最多试几次，之后才放弃整个任务
MAX_BATCH_ATTEMPTS = 3


class ArchiveInconsistency(RuntimeError):
    """删除之后，这一批里仍有行同时存在于两张表（复制之后、删除之前被并发改过）。
    这一批已回滚，没有数据丢失。"""


def _retryable(exc: BaseException) -> bool:
    """MySQL 的死锁（1213）与锁等待超时（1205）：事务已被回滚，重做这一批就行。"""
    if isinstance(exc, ArchiveInconsistency):
        return True
    code = (getattr(getattr(exc, "orig", None), "args", None) or (None,))[0]
    return code in (1213, 1205)


@dataclass
class ArchiveResult:
    cutoff: datetime
    archived: int = 0
    batches: int = 0
    seconds: float = 0.0
    #: 本次范围内、符合策略、还没搬的行数（dry-run 时就是将要搬的数）
    eligible_remaining: int = 0
    #: done 搬完了 | time_budget 到时间了 | max_batches 到批数了 | nothing 没有可搬的 | dry_run
    stopped_by: str = "done"
    skipped_feedback: int = 0
    """范围内因为带点赞点踩而被留下的行数（没开 include_feedback 时）"""
    retries: int = 0
    """因并发冲突（死锁、复制后被改）重做某一批的次数。线上有点赞/写入时出现是正常的"""


def db_now(engine) -> datetime:
    """数据库的当前时间。**不用 Python 的 datetime.now()**：created_at 是数据库按它自己的
    时区写的（compose 里是 +08:00），应用所在机器的时区未必一样，差几个小时对 90 天的
    保留期无关紧要，但"今天"这个概念该只有一个来源。"""
    with engine.connect() as conn:
        v = conn.execute(text("SELECT CURRENT_TIMESTAMP")).scalar()
    if isinstance(v, str):                              # SQLite 返回字符串
        v = datetime.fromisoformat(v)
    return v


def _move_batch(engine, last: int, max_id: int, batch_size: int, cutoff: datetime,
                policy: str, cols: str) -> tuple[int | None, int]:
    """搬一批：一个事务。返回 (这批的主键上界, 搬走的行数)；没有更多行时上界为 None。"""
    with engine.begin() as conn:
        hi = conn.execute(text(
            "SELECT MAX(id) FROM (SELECT id FROM agent_trace"
            " WHERE id > :last AND id <= :m ORDER BY id LIMIT :n) b"),
            {"last": last, "m": max_id, "n": batch_size}).scalar()
        if hi is None:
            return None, 0
        rng = {"last": last, "hi": hi, "c": cutoff}
        where = "id > :last AND id <= :hi AND created_at < :c" + policy

        conn.execute(text(
            f"INSERT INTO agent_trace_archive ({cols}) "
            f"SELECT {cols} FROM agent_trace WHERE {where} "
            "AND NOT EXISTS (SELECT 1 FROM agent_trace_archive a"
            " WHERE a.id = agent_trace.id)"), rng)
        # 只删**确认已经在归档表里**的行：即使上面的复制出了意外，也不会删掉没复制的数据
        moved = conn.execute(text(
            f"DELETE FROM agent_trace WHERE {where} AND id IN "
            "(SELECT id FROM agent_trace_archive WHERE id > :last AND id <= :hi)"),
            rng).rowcount

        # 不变量：这一批里不能有行同时在两张表里。检查"重复"而不是比较行数——
        # 数 expected 用的是快照读，INSERT/DELETE 是当前读，点赞恰好在两者之间提交时
        # 行数会差 1，但数据其实一致（那行没被复制也没被删）。真正要防的只有"复制了、没删掉"
        dup = conn.execute(text(
            "SELECT COUNT(*) FROM agent_trace t JOIN agent_trace_archive a ON a.id = t.id "
            "WHERE t.id > :last AND t.id <= :hi"), rng).scalar() or 0
        if dup:
            raise ArchiveInconsistency(
                f"批 (id {last}, {hi}] 删除后仍有 {dup} 行同时在两张表里，已回滚")
    return hi, moved


def archive_traces(
    engine,
    *,
    cutoff: datetime,
    include_feedback: bool = False,
    batch_size: int = 2000,
    sleep: float = 0.05,
    max_seconds: float | None = None,
    max_batches: int | None = None,
    dry_run: bool = False,
    progress: Callable[[ArchiveResult], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ArchiveResult:
    if batch_size < 1:
        raise ValueError("batch_size 至少为 1")
    res = ArchiveResult(cutoff=cutoff)
    started = clock()
    policy = "" if include_feedback else " AND thumb IS NULL"
    cols = ", ".join(TRACE_COLUMNS)

    with engine.connect() as conn:
        max_id = conn.execute(
            text("SELECT MAX(id) FROM agent_trace WHERE created_at < :c"), {"c": cutoff}).scalar()
        if max_id is None:
            res.stopped_by = "nothing"
            res.seconds = clock() - started
            return res
        if not include_feedback:
            res.skipped_feedback = conn.execute(text(
                "SELECT COUNT(*) FROM agent_trace WHERE id <= :m AND created_at < :c"
                " AND thumb IS NOT NULL"), {"m": max_id, "c": cutoff}).scalar() or 0
        res.eligible_remaining = conn.execute(text(
            "SELECT COUNT(*) FROM agent_trace WHERE id <= :m AND created_at < :c" + policy),
            {"m": max_id, "c": cutoff}).scalar() or 0

    if dry_run:
        res.stopped_by = "dry_run"
        res.seconds = clock() - started
        return res

    last = 0
    while True:
        if max_seconds is not None and clock() - started >= max_seconds:
            res.stopped_by = "time_budget"
            break
        if max_batches is not None and res.batches >= max_batches:
            res.stopped_by = "max_batches"
            break

        for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
            try:
                hi, moved = _move_batch(engine, last, max_id, batch_size, cutoff, policy, cols)
                break
            except Exception as exc:  # noqa: BLE001
                # 事务已回滚：这一批没有任何副作用，重做即可。只有试够了次数才放弃整个任务
                if not _retryable(exc) or attempt == MAX_BATCH_ATTEMPTS:
                    raise
                res.retries += 1
                sleeper(min(0.2 * attempt, 1.0))
        if hi is None:
            break
        res.archived += moved
        res.batches += 1
        last = hi
        res.seconds = clock() - started
        if progress is not None:
            progress(res)
        if sleep:
            sleeper(sleep)

    res.seconds = clock() - started
    with engine.connect() as conn:
        res.eligible_remaining = conn.execute(text(
            "SELECT COUNT(*) FROM agent_trace WHERE id <= :m AND created_at < :c" + policy),
            {"m": max_id, "c": cutoff}).scalar() or 0
    return res
