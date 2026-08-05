"""dag_clean_dialogue —— 每日对话清洗。

ODS 原始对话 → 四道关卡 → knowledge_item + sft_sample + 人工标注队列。

DAG 只是 ``smartmall_pipeline.orchestrator`` 的薄封装：
业务逻辑全在包里、可被 pytest 完整覆盖，Airflow 只负责调度、重试与告警。
这样"流水线对不对"和"调度配没配好"是两个可以分开验证的问题。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.decorators import dag, task

DEFAULT_ARGS = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
}


@dag(
    dag_id="dag_clean_dialogue",
    description="四道清洗关卡：ODS 对话 → knowledge_item / sft_sample",
    schedule="0 2 * * *",          # 每日 02:00，与 GPU 全卡独占窗口错开
    start_date=datetime(2026, 3, 1),
    catchup=False,
    max_active_runs=1,             # 去重是批内 O(n²)，并发跑会互相看不到对方的签名
    default_args=DEFAULT_ARGS,
    tags=["dataplat", "M1"],
)
def clean_dialogue():

    @task
    def load_batch(batch_size: int = 5000) -> list[dict]:
        """从 ODS 拉取未处理的原始对话。

        ODS 只增不改，因此任何一批都可以安全重跑——这是整个数据中台
        可复现性的基础。
        """
        from smartmall_pipeline.repository import OdsRepository

        repo = OdsRepository.from_env()
        dialogues = repo.fetch_unprocessed(limit=batch_size)
        return [d.model_dump(mode="json") for d in dialogues]

    @task
    def run_gates(raw: list[dict]) -> dict:
        from smartmall_pipeline.gates.gate3_model import LiteLlmClient
        from smartmall_pipeline.models import Dialogue
        from smartmall_pipeline.orchestrator import run_pipeline

        dialogues = [Dialogue.model_validate(d) for d in raw]
        llm = LiteLlmClient(
            base_url=os.environ.get("LITELLM_BASE_URL", "http://ai-gateway:9000"),
            api_key=os.environ.get("LITELLM_API_KEY", ""),
        )
        batch_id = f"clean-{datetime.now():%Y%m%d%H%M}"
        out = run_pipeline(dialogues, llm, batch_id=batch_id)

        # 漏斗报表进日志，也写 ds_job.stats 供后台展示
        print(out.report.render())

        return {
            "batch_id": batch_id,
            "stats": out.report.to_stats_json(),
            "items": [i.model_dump(mode="json") for i in out.knowledge_items],
            "tasks": [t.to_label_studio() for t in out.annotation_tasks],
            "samples": [s.model_dump(mode="json") for s in out.sft_samples],
        }

    @task
    def persist(payload: dict) -> int:
        """落库并触发增量向量化。

        自动通过的条目置 embedding_status=pending，由
        dag_kb_incremental_index 在 30 分钟内取走向量化。
        """
        from smartmall_pipeline.models import KnowledgeItem, SftSample
        from smartmall_pipeline.repository import DwsRepository

        repo = DwsRepository.from_env()
        n_items = repo.save_knowledge_items(
            [KnowledgeItem.model_validate(i) for i in payload["items"]]
        )
        repo.save_sft_samples(
            [SftSample.model_validate(s) for s in payload["samples"]]
        )
        repo.record_job(payload["batch_id"], payload["stats"])
        return n_items

    @task
    def push_annotations(payload: dict) -> int:
        """推送人工标注任务（带预标注）。

        人工只看模型拿不准的——这是主动学习，也是这一关可行的前提。
        """
        from smartmall_pipeline.gates.gate4_human import HttpLabelStudioClient

        tasks = payload["tasks"]
        if not tasks:
            return 0
        client = HttpLabelStudioClient(
            base_url=os.environ.get("LABEL_STUDIO_URL", "http://label-studio:8080"),
            api_token=os.environ.get("LABEL_STUDIO_TOKEN", ""),
        )
        return client.import_tasks(
            project_id=int(os.environ.get("LABEL_STUDIO_PROJECT_ID", "1")),
            tasks=tasks,
        )

    payload = run_gates(load_batch())
    persist(payload)
    push_annotations(payload)


clean_dialogue()
