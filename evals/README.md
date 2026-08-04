# evals —— 评测集与评测脚本

13 个评测集，与代码同仓版本管理。详见 [docs/12-eval-observability.md](../docs/12-eval-observability.md)。

## 硬性约束

**评测集样本绝不能出现在训练集中。**
`dataset_version.filter_sql` 必须包含
`session_id NOT IN (SELECT session_id FROM eval_sample)`，
否则会出现"在训练集上评测"的经典错误——指标虚高但线上无效。

## 评测集清单

| 评测集 | 规模 | 检验 | 里程碑 |
|---|---|---|---|
| `eval-retrieval` | 300 | Recall@5 ≥ 0.85 | M2 |
| `eval-answer` | 200 | 忠实度 ≥ 0.90 | M2 |
| `eval-negative` | 100 | 拒答准确率 ≥ 0.90 | M2 |
| `eval-multimodal` | 100 | 多模态链路 | M4 |
| `eval-intent` | 300 | 意图分类 ≥ 0.85 | M2 |
| `eval-style` | 200 | 盲评「像真人」≥ 40% | M7 |
| `eval-rag-faithfulness` | 200 | 🔴 防退化红线 | M7 |
| `eval-instruction` | 100 | 防退化 | M7 |
| `eval-safety` | 100 | 违禁词漏出 = 0 | M2 |
| `eval-hallucination` | 100 | 幻觉检测 | M2 |
| `eval-asr` | 50 | 商品名识别 ≥ 0.9 | M5 |
| `eval-segment` | 30 | 分段准确率 ≥ 0.8 | M5 |
| `kpi-golden-set` | 300–500 | Judge 校准 ρ ≥ 0.7 | M6 |

`baseline.json` 存放各指标的当前基线，CI 门禁以此比对（低于基线 3% 则失败）。
