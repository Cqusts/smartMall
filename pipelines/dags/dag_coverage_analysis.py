"""dag_coverage_analysis —— 每周知识覆盖度分析与补写任务生成。

「人工补充」是与「清洗」并列的独立流程：有些知识对话里根本没有，
必须人工补写。这个 DAG 负责找出「哪里没有」。

需求分布来自 Trace 的真实用户提问——没有它，矩阵只能告诉你"哪里空"；
有了它才能告诉你"哪里空**且用户在问**"，后者才是真正该补的。
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.decorators import dag, task


@dag(
    dag_id="dag_coverage_analysis",
    description="知识覆盖度矩阵 → 补写任务",
    schedule="0 6 * * 1",          # 每周一 06:00，运营上班前算好
    start_date=datetime(2026, 3, 1),
    catchup=False,
    default_args={"owner": "data-platform", "retries": 1},
    tags=["dataplat", "M1"],
)
def coverage_analysis():

    @task
    def analyze() -> dict:
        import httpx
        from sqlalchemy import text

        from smartmall_pipeline import coverage
        from smartmall_pipeline.models import KnowledgeItem, KnowledgeType
        from smartmall_pipeline.repository import DwsRepository

        repo = DwsRepository.from_env()

        with repo.engine.connect() as conn:
            cats = {
                r[0]: r[1]
                for r in conn.execute(
                    text("SELECT id, name FROM category WHERE level = 3 AND deleted = 0")
                )
            }
            rows = conn.execute(text(
                "SELECT category_id, tags, content, title, source, source_ref "
                "FROM knowledge_item "
                "WHERE review_status IN ('approved','revised') AND deleted = 0"
            )).mappings().fetchall()

        items = [
            KnowledgeItem(
                biz_type="qa", content=r["content"], title=r["title"],
                source=r["source"], source_ref=r["source_ref"],
                category_id=r["category_id"],
            )
            for r in rows
        ]

        matrix = coverage.build_matrix(items, cats)
        tasks = coverage.generate_write_tasks(matrix, limit=50)
        print(matrix.render())

        # 补写任务推给运营后台
        dataplat = os.environ.get("DATAPLAT_BASE_URL", "http://mall-dataplat:8083")
        if tasks:
            httpx.post(
                f"{dataplat}/api/dataplat/write-tasks",
                json=[
                    {
                        "categoryId": t.category_id,
                        "categoryName": t.category_name,
                        "knowledgeType": str(t.knowledge_type),
                        "priority": t.priority,
                        "reason": t.reason,
                        "gap": t.gap,
                        "sampleQuestions": t.sample_questions,
                    }
                    for t in tasks
                ],
                timeout=30,
            ).raise_for_status()

        return {
            "coverage_ratio": matrix.coverage_ratio,
            "empty_cells": len(matrix.empty_cells),
            "write_tasks": len(tasks),
        }

    analyze()


coverage_analysis()
