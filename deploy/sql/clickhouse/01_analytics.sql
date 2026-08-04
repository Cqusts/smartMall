-- ============================================================
-- ClickHouse 分析层
--
-- 只用于分析型查询，不做主存储。全部可从 MySQL 重算，因此不备份。
-- 由 deploy/scripts/init-db.sh 应用（不能放进 MySQL 的 initdb 目录）。
-- 详见 docs/03-data-platform.md 第 7 节。
-- ============================================================

CREATE DATABASE IF NOT EXISTS smartmall;

-- 会话级指标宽表，供考核报表聚合查询
CREATE TABLE IF NOT EXISTS smartmall.ch_session_metric
(
    session_id          UInt64,
    session_no          String,
    agent_id            Int64,
    agent_type          LowCardinality(String),
    channel             LowCardinality(String),
    score_date          Date,
    started_at          DateTime,
    turn_count          UInt16,
    first_reply_ms      UInt32,
    avg_reply_ms        UInt32,
    timeout_count       UInt16,
    transferred_human   UInt8,
    order_created       UInt8,
    order_amount        Decimal(12, 2),
    total_score         Decimal(5, 2),
    metric_scores       Map(String, Decimal(5, 2)),
    root_cause          LowCardinality(String),
    rubric_version      LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(score_date)
ORDER BY (score_date, agent_id, session_id);

-- Trace 明细，用于分析检索命中率、耗时分布、成本
CREATE TABLE IF NOT EXISTS smartmall.ch_trace_event
(
    trace_id            String,
    session_id          UInt64,
    agent_id            LowCardinality(String) COMMENT '五个 Agent 之一',
    ts                  DateTime,
    event_date          Date MATERIALIZED toDate(ts),

    intent              LowCardinality(String),
    kb_version          LowCardinality(String) COMMENT '效果归因的关键标签',
    model_version       LowCardinality(String),
    ab_group            LowCardinality(String),

    retrieval_hit_count UInt16,
    retrieval_max_score Float32,
    tools_called        Array(String),

    prompt_tokens       UInt32,
    completion_tokens   UInt32,
    cost_cny            Decimal(10, 6),

    total_ms            UInt32,
    vision_ms           UInt32,
    retrieval_ms        UInt32,
    generation_ms       UInt32,

    thumb               Int8 COMMENT '1 赞 / -1 踩 / 0 无反馈',
    thumb_reason        LowCardinality(String) COMMENT '答非所问|信息错误|态度生硬|太啰嗦',
    handover            UInt8,
    postcheck_flags     Array(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, agent_id, trace_id);

-- 知识条目命中统计。
-- "从未被命中的条目占比 >40%" 说明知识库在按运营的想象建设，而不是按用户真实需求建设。
CREATE TABLE IF NOT EXISTS smartmall.ch_kb_hit
(
    item_id       UInt64,
    biz_type      LowCardinality(String),
    modality      LowCardinality(String),
    category_id   UInt64,
    kb_version    LowCardinality(String),
    hit_date      Date,
    hit_count     UInt32,
    avg_rank      Float32,
    avg_score     Float32,
    cited_count   UInt32 COMMENT '实际被答案引用的次数（命中不等于被采用）'
)
ENGINE = SummingMergeTree((hit_count, cited_count))
PARTITION BY toYYYYMM(hit_date)
ORDER BY (hit_date, kb_version, item_id);

-- 成本明细，按 Agent 与场景维度拆分
CREATE TABLE IF NOT EXISTS smartmall.ch_cost
(
    ts            DateTime,
    cost_date     Date MATERIALIZED toDate(ts),
    agent_id      LowCardinality(String),
    scene         LowCardinality(String),
    model         LowCardinality(String),
    prompt_tokens UInt32,
    completion_tokens UInt32,
    cost_cny      Decimal(10, 6)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, agent_id, model);
