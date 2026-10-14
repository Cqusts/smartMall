"""数据库访问层。

用 SQLAlchemy Core + 显式 SQL，不用 ORM——表结构由
``deploy/sql/mysql/*.sql`` 定义，是这个项目的契约。再用 ORM 声明一遍
会产生两份真相，迟早漂移。

领域逻辑（四道关卡）完全不碰这一层，因此可以离线测试；
这一层只做「模型 ↔ 行」的搬运。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from .models import Dialogue, KnowledgeItem, SftSample, Turn


def dsn_from_env() -> str:
    """应用当前会用的连库 DSN。缓存要把它当键用——配置变了，旧缓存就不作数。"""
    return _dsn_from_env()


def _dsn_from_env() -> str:
    return (
        f"mysql+pymysql://{os.environ.get('MYSQL_USER', 'smartmall')}:"
        f"{os.environ.get('MYSQL_PASSWORD', 'smartmall')}@"
        f"{os.environ.get('MYSQL_HOST', 'localhost')}:"
        f"{os.environ.get('MYSQL_PORT', '3306')}/"
        f"{os.environ.get('MYSQL_DATABASE', 'smartmall')}?charset=utf8mb4"
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


#: 进程内的 Engine 注册表：同一个 DSN 只建一个 Engine，也就是**只有一个连接池**。
_ENGINES: dict[str, Any] = {}
_ENGINES_LOCK = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def shared_engine(dsn: str):
    """同一个 DSN 在进程里只建一个 Engine。

    **为什么要共享。** 原先每个 ``from_env()`` 都 ``create_engine`` 一次，而 Engine
    就是连接池——于是调用方有多少个，进程里就有多少个池子。实测两处后果：

    · ``/api/products`` 每个请求都 ``MySqlToolBox.from_env()``，每请求新建一个池子、
      一条新连接（TCP + 认证握手），用完丢掉。压测里 MySQL 的 ``Connections`` 计数
      正好是**每请求 +1**，单请求耗时 209ms，复用连接只要 113ms。
    · 客服服务的装配（``build_deps``）里轨迹、工具、素材、任务、检索……各自
      ``from_env()`` 一次，至少五个互相独立的池子，每个默认 5+10 个连接。
      副本数一多，连接数是相乘的，而 MySQL 的 ``max_connections`` 只有 300。

    Engine 本身线程安全，共享是 SQLAlchemy 的标准用法。

    池参数可由环境变量调：``MYSQL_POOL_SIZE``（默认 10）、``MYSQL_MAX_OVERFLOW``
    （默认 10）、``MYSQL_POOL_RECYCLE``（默认 1800 秒，要小于服务端 wait_timeout，
    否则会拿到被服务端单方面关掉的连接；``pool_pre_ping`` 是第二道保险）。
    """
    with _ENGINES_LOCK:
        engine = _ENGINES.get(dsn)
        if engine is None:
            from sqlalchemy import create_engine

            engine = create_engine(
                dsn,
                pool_pre_ping=True,
                pool_size=_env_int("MYSQL_POOL_SIZE", 10),
                max_overflow=_env_int("MYSQL_MAX_OVERFLOW", 10),
                pool_recycle=_env_int("MYSQL_POOL_RECYCLE", 1800),
                future=True,
            )
            _ENGINES[dsn] = engine
        return engine


def dispose_shared_engines() -> None:
    """关掉并清空所有共享 Engine。进程退出前或测试之间调用。"""
    with _ENGINES_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for engine in engines:
        engine.dispose()


@dataclass
class _Base:
    dsn: str
    shared: bool = False
    """True 时复用进程内同 DSN 的 Engine（见 :func:`shared_engine`）。
    ``from_env()`` 走这条；直接 ``Repository(dsn=...)`` 保持原样各建各的——
    测试里的 ``sqlite://`` 内存库要靠"每个实例一个库"来互相隔离，共享了就串了。"""

    def __post_init__(self) -> None:
        if self.shared:
            self.engine = shared_engine(self.dsn)
            return

        from sqlalchemy import create_engine

        self.engine = create_engine(self.dsn, pool_pre_ping=True, future=True)

    @classmethod
    def from_env(cls):
        return cls(dsn=_dsn_from_env(), shared=True)


class OdsRepository(_Base):
    """ODS 原始层。只增不改——任何加工出错都能重跑。"""

    def insert_dialogues(self, dialogues: Iterable[Dialogue], batch_id: str) -> int:
        """幂等导入。

        以内容哈希做唯一键：同一段对话换个 session_no 重复导入也会被识别为重复。
        用 INSERT IGNORE 而不是先查后插——并发导入时先查后插会漏。
        """
        from sqlalchemy import text

        sql = text(
            "INSERT IGNORE INTO ods_raw_dialogue "
            "(source_type, batch_id, external_id, raw_payload, payload_hash) "
            "VALUES (:source_type, :batch_id, :external_id, :raw_payload, :payload_hash)"
        )
        rows = [
            {
                "source_type": d.source_type,
                "batch_id": batch_id,
                "external_id": d.external_id,
                "raw_payload": d.model_dump_json(),
                "payload_hash": d.content_hash(),
            }
            for d in dialogues
        ]
        if not rows:
            return 0
        with self.engine.begin() as conn:
            result = conn.execute(sql, rows)
        return result.rowcount or 0

    def fetch_unprocessed(self, limit: int = 5000) -> list[Dialogue]:
        """拉取尚未被清洗处理过的原始对话。

        判据是 ``ods_process_log`` 而不是 ``dwd_dialogue_session``——
        在关卡①②被淘汰的对话根本到不了 DWD，用 DWD 判断会把它们
        永远当成待处理，每次清洗重新处理一遍，产出成倍的重复知识。
        """
        from sqlalchemy import text

        sql = text(
            "SELECT o.id, o.raw_payload FROM ods_raw_dialogue o "
            "LEFT JOIN ods_process_log p ON p.ods_id = o.id "
            "WHERE p.id IS NULL ORDER BY o.id LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit}).fetchall()

        out: list[Dialogue] = []
        for ods_id, payload in rows:
            dlg = Dialogue.model_validate_json(payload)
            dlg.ods_id = ods_id
            out.append(dlg)
        return out

    def mark_processed(
        self,
        outcomes: dict[int, tuple[str, str | None]],
        batch_id: str,
    ) -> int:
        """记录每条 ODS 记录的处理结果。

        必须与知识条目写入在同一个流程里完成，否则中途失败会导致
        "知识已写入但记录未标记"，下次重跑产生重复。
        用 INSERT IGNORE 保证重复标记是安全的。
        """
        from sqlalchemy import text

        if not outcomes:
            return 0

        sql = text(
            "INSERT IGNORE INTO ods_process_log "
            "(ods_id, batch_id, outcome, dropped_at_gate) "
            "VALUES (:ods_id, :batch_id, :outcome, :gate)"
        )
        rows = [
            {"ods_id": ods_id, "batch_id": batch_id, "outcome": outcome, "gate": gate}
            for ods_id, (outcome, gate) in outcomes.items()
        ]
        with self.engine.begin() as conn:
            conn.execute(sql, rows)
        return len(rows)


class DwsRepository(_Base):
    """DWS 服务层：knowledge_item / sft_sample / 任务统计。"""

    def save_knowledge_items(self, items: Sequence[KnowledgeItem]) -> int:
        """写入知识条目。

        ``embedding_status`` 一律 pending —— 由 dag_kb_incremental_index
        扫描后向量化。内容被修改时应置为 stale（见 :meth:`mark_stale`）。
        """
        from sqlalchemy import text

        if not items:
            return 0

        sql = text(
            "INSERT INTO knowledge_item "
            "(biz_type, modality, knowledge_type, title, content, summary, asset_ids, product_ids, "
            " category_id, tags, source, source_ref, quality_score, confidence, "
            " review_status, valid_from, valid_to, embedding_status) "
            "VALUES (:biz_type, :modality, :knowledge_type, :title, :content, :summary, :asset_ids, "
            " :product_ids, :category_id, :tags, :source, :source_ref, :quality_score, "
            " :confidence, :review_status, :valid_from, :valid_to, 'pending')"
        )
        rows = [
            {
                "biz_type": str(i.biz_type),
                "modality": str(i.modality),
                "knowledge_type": str(i.knowledge_type),
                "title": i.title,
                "content": i.content,
                "summary": i.summary,
                "asset_ids": _json(i.asset_ids),
                "product_ids": _json(i.product_ids),
                "category_id": i.category_id,
                "tags": _json(i.tags),
                "source": i.source,
                "source_ref": i.source_ref,
                "quality_score": i.quality_score,
                "confidence": i.confidence,
                "review_status": str(i.review_status),
                "valid_from": i.valid_from or datetime.now(),
                "valid_to": i.valid_to,
            }
            for i in items
        ]
        with self.engine.begin() as conn:
            conn.execute(sql, rows)
        return len(rows)

    def save_sft_samples(self, samples: Sequence[SftSample]) -> int:
        from sqlalchemy import text

        if not samples:
            return 0

        sql = text(
            "INSERT INTO sft_sample "
            "(sample_type, messages, chosen, rejected, style_tags, scene_tag, "
            " quality_score, is_golden, agent_id, source, source_ref, review_status) "
            "VALUES (:sample_type, :messages, :chosen, :rejected, :style_tags, "
            " :scene_tag, :quality_score, :is_golden, :agent_id, :source, "
            " :source_ref, :review_status)"
        )
        rows = [
            {
                "sample_type": s.sample_type,
                "messages": _json(s.messages),
                "chosen": s.chosen,
                "rejected": s.rejected,
                "style_tags": _json(s.style_tags),
                "scene_tag": s.scene_tag,
                "quality_score": s.quality_score,
                "is_golden": int(s.is_golden),
                "agent_id": s.agent_id,
                "source": s.source,
                "source_ref": s.source_ref,
                "review_status": str(s.review_status),
            }
            for s in samples
        ]
        with self.engine.begin() as conn:
            conn.execute(sql, rows)
        return len(rows)

    def mark_stale(self, item_ids: Sequence[int]) -> None:
        """内容被修改后置为 stale。

        没有这个状态，知识改了但向量没更新是最隐蔽的 bug——
        检索还能命中（旧向量），返回的 content 却是新的，两者不匹配。
        """
        from sqlalchemy import text

        if not item_ids:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE knowledge_item SET embedding_status='stale' "
                     "WHERE id IN :ids").bindparams(ids=tuple(item_ids))
            )

    def fetch_for_indexing(self, limit: int = 500) -> list[tuple[int, KnowledgeItem]]:
        """取待向量化的条目（pending 或 stale），且必须已审核。"""
        from sqlalchemy import text

        sql = text(
            "SELECT id, biz_type, modality, title, content, asset_ids, product_ids, "
            "       category_id, tags, source, source_ref, quality_score, valid_to "
            "FROM knowledge_item "
            "WHERE review_status IN ('approved','revised') "
            "  AND embedding_status IN ('pending','stale') AND deleted = 0 "
            "ORDER BY id LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit}).mappings().fetchall()

        out: list[tuple[int, KnowledgeItem]] = []
        for r in rows:
            out.append((
                r["id"],
                KnowledgeItem(
                    id=r["id"],
                    biz_type=r["biz_type"],
                    modality=r["modality"],
                    title=r["title"],
                    content=r["content"],
                    asset_ids=json.loads(r["asset_ids"] or "[]"),
                    product_ids=json.loads(r["product_ids"] or "[]"),
                    category_id=r["category_id"],
                    tags=json.loads(r["tags"] or "[]"),
                    source=r["source"],
                    source_ref=r["source_ref"],
                    quality_score=r["quality_score"],
                    valid_to=r["valid_to"],
                    review_status="approved",  # type: ignore[arg-type]
                ),
            ))
        return out

    def mark_indexed(self, item_ids: Sequence[int]) -> None:
        from sqlalchemy import text

        if not item_ids:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE knowledge_item SET embedding_status='indexed', "
                     "indexed_at=NOW() WHERE id IN :ids").bindparams(ids=tuple(item_ids))
            )

    # ------------------------------------------------------------ 转人工回流

    def blind_spots(self, limit: int = 30) -> list["BlindSpot"]:
        """按题面聚合的知识盲点。

        同一个问题反复转人工，说明它既是真需求又确实没有知识——
        频次就是补写优先级，比拍脑袋准得多。
        """
        from sqlalchemy import text

        from .handover import BlindSpot

        with self.engine.connect() as conn:
            return [
                BlindSpot(
                    question=r["question"], times=r["times"], reason=r["reason"],
                    ticket_id=r["ticket_id"], status=r["status"],
                )
                for r in conn.execute(text(
                    "SELECT question, COUNT(*) AS times, MAX(reason) AS reason,"
                    " MIN(id) AS ticket_id, MIN(status) AS status "
                    "FROM handover_ticket GROUP BY question "
                    "ORDER BY times DESC, ticket_id ASC LIMIT :n"
                ), {"n": limit}).mappings()
            ]

    def fetch_ticket(self, ticket_id: int) -> "Ticket | None":
        from sqlalchemy import text

        from .handover import Ticket

        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, question, reason, intent, product_id, status,"
                " human_answer FROM handover_ticket WHERE id = :i"
            ), {"i": ticket_id}).mappings().first()
        if row is None:
            return None
        return Ticket(
            id=row["id"], question=row["question"], reason=row["reason"],
            intent=row["intent"] or "", product_id=row["product_id"],
            status=row["status"], human_answer=row["human_answer"],
        )

    def answer_ticket(
        self, ticket_id: int, answer: str, knowledge_item_id: int | None
    ) -> None:
        """回填人工答案并关联生成的知识条目。

        两件事必须在同一个事务里：只写答案不关联条目，就无从判断
        这张工单有没有真的回流；只写条目不改状态，重跑会重复生成。
        """
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE handover_ticket SET human_answer = :a, status = :st,"
                " knowledge_item_id = :kid, answered_at = CURRENT_TIMESTAMP"
                " WHERE id = :i"
            ), {
                "a": answer,
                "st": "imported" if knowledge_item_id else "answered",
                "kid": knowledge_item_id,
                "i": ticket_id,
            })

    def save_knowledge_item_returning_id(self, item: KnowledgeItem) -> int | None:
        """写一条知识并拿回自增 ID。

        回流链路需要这个 ID 把工单和知识条目关联起来——
        "这张工单补出了哪条知识"是飞轮能否被验证的关键。
        """
        before = self._max_knowledge_id()
        if not self.save_knowledge_items([item]):
            return None
        after = self._max_knowledge_id()
        return after if after != before else None

    def _max_knowledge_id(self) -> int:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM knowledge_item")
            ).scalar() or 0

    def record_job(self, batch_id: str, stats: dict[str, Any]) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO ds_job (job_type, batch_id, status, stats, "
                     "started_at, finished_at) VALUES "
                     "('clean', :batch_id, 'success', :stats, NOW(), NOW())"),
                {"batch_id": batch_id, "stats": _json(stats)},
            )

    def apply_annotations(self, updates: Sequence[dict[str, Any]]) -> int:
        """回写人工标注结果，并触发重新向量化。"""
        from sqlalchemy import text

        if not updates:
            return 0
        sql = text(
            "UPDATE knowledge_item SET content=:content, review_status=:review_status, "
            "  tags=:tags, confidence=1.0, reviewer_id=:reviewer_id, reviewed_at=NOW(), "
            "  embedding_status='stale' "
            "WHERE id=:id"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, list(updates))
        return len(updates)
