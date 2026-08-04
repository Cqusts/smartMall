# pipelines —— 数据流水线

Airflow DAG 与 Data-Juicer 配方。挂载进 Airflow 容器（只读）。

```
dags/      Airflow DAG（M1 起陆续落地）
recipes/   Data-Juicer 清洗配方 YAML
```

## 计划中的 DAG

| DAG | 频率 | 说明 |
|---|---|---|
| `dag_ingest_public_dataset` | 一次性 | JDDC/ECD 导入 ODS |
| `dag_ingest_trace` | 每小时 | Langfuse Trace → ODS |
| `dag_clean_dialogue` | 每日 | 四道清洗关卡 → DWD → DWS |
| `dag_asset_to_knowledge` | 事件驱动 | 素材审核通过 → VLM 打标 → knowledge_item |
| `dag_clip_to_knowledge` | 事件驱动 | 切片完成 → 话术抽取 → knowledge_item |
| `dag_kb_incremental_index` | 每 30 分钟 | pending/stale → 向量化 |
| `dag_kb_staleness_check` | 每日 | 过期知识下线、冲突检测 |
| `dag_coverage_analysis` | 每周 | 知识覆盖度矩阵 → 补写任务 |
| `dag_kpi_daily` | 每日 | 会话指标计算 → ClickHouse |

配方变更 = 数据资产变更，必须发新版本。详见 [docs/03-data-platform.md](../docs/03-data-platform.md)。
