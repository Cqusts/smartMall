"""dag_kb_incremental_index —— 增量向量化。

每 30 分钟扫描 ``embedding_status IN ('pending','stale')`` 的已审核条目，
批量向量化后写入 Milvus。

``stale`` 状态是必需的：知识内容改了但向量没更新，是最隐蔽的 bug——
检索还能命中（旧向量），但返回的 content 是新的，两者不匹配。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.decorators import dag, task

BATCH_SIZE = 500
"""单次最多处理 500 条。bge-m3 批量推理占 GPU，
一次吃太多会拖慢在线检索——常驻区只有约 6G 显存。"""


@dag(
    dag_id="dag_kb_incremental_index",
    description="增量向量化：pending/stale → Milvus",
    schedule=timedelta(minutes=30),
    start_date=datetime(2026, 3, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "data-platform", "retries": 1},
    tags=["dataplat", "rag", "M1"],
)
def kb_incremental_index():

    @task
    def index_batch() -> dict:
        import httpx

        from smartmall_pipeline.repository import DwsRepository

        repo = DwsRepository.from_env()
        pending = repo.fetch_for_indexing(limit=BATCH_SIZE)
        if not pending:
            return {"indexed": 0}

        rag_url = os.environ.get("AI_RAG_BASE_URL", "http://ai-rag:9001")
        payload = [
            {
                "item_id": item_id,
                "biz_type": str(item.biz_type),
                "modality": str(item.modality),
                "title": item.title,
                "content": item.content,
                "category_id": item.category_id,
                "product_ids": item.product_ids,
                "asset_ids": item.asset_ids,
                "quality_score": item.quality_score,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            }
            for item_id, item in pending
        ]

        resp = httpx.post(f"{rag_url}/index/upsert", json={"items": payload}, timeout=300)
        resp.raise_for_status()

        # 只有 Milvus 确认写入后才标记 indexed，失败的下轮重试
        indexed_ids = resp.json().get("indexed_item_ids", [])
        repo.mark_indexed(indexed_ids)
        return {"indexed": len(indexed_ids), "attempted": len(payload)}

    index_batch()


kb_incremental_index()
