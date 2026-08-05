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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from .models import Dialogue, KnowledgeItem, SftSample, Turn


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


@dataclass
class _Base:
    dsn: str

    def __post_init__(self) -> None:
        from sqlalchemy import create_engine

        self.engine = create_engine(self.dsn, pool_pre_ping=True, future=True)

    @classmethod
    def from_env(cls):
        return cls(dsn=_dsn_from_env())


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
        """拉取尚未进入 DWD 的原始对话。"""
        from sqlalchemy import text

        sql = text(
            "SELECT o.id, o.raw_payload FROM ods_raw_dialogue o "
            "LEFT JOIN dwd_dialogue_session s ON s.ods_id = o.id "
            "WHERE s.id IS NULL ORDER BY o.id LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit}).fetchall()

        out: list[Dialogue] = []
        for ods_id, payload in rows:
            dlg = Dialogue.model_validate_json(payload)
            dlg.ods_id = ods_id
            out.append(dlg)
        return out


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
            "(biz_type, modality, title, content, summary, asset_ids, product_ids, "
            " category_id, tags, source, source_ref, quality_score, confidence, "
            " review_status, valid_from, valid_to, embedding_status) "
            "VALUES (:biz_type, :modality, :title, :content, :summary, :asset_ids, "
            " :product_ids, :category_id, :tags, :source, :source_ref, :quality_score, "
            " :confidence, :review_status, :valid_from, :valid_to, 'pending')"
        )
        rows = [
            {
                "biz_type": str(i.biz_type),
                "modality": str(i.modality),
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
