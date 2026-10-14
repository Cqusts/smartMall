-- 015 · agent_trace 归档表
--
-- ---------------------------------------------------------------- 为什么要归档
--
-- agent_trace 每一轮对话写一行，只增不减。实测（MySQL 8、O_DIRECT、100 万行 ≈ 1GB）：
--
--   · 写入不是问题：16 线程单行自动提交 ≈ 2500 条/秒，从 1.3 万行到 100 万行没有退化。
--     对话入口的上限本来就只有几十轮/秒（见 ai-agent 的准入控制）。
--   · 读才是问题：product_demand（"用户对这件商品问得最多的是什么"）按商品聚合，
--     耗时随该商品的历史行数线性增长——0.8ms（1 万行）→ 12ms（100 万行），
--     一年下来是秒级。它是分析查询，没有理由扫三年前的对话。
--   · 磁盘：每轮约 1.5KB 数据 + 索引，每秒 30 轮是每天 ~5GB。
--
-- 归档 = 把超过保留期的行**搬**到 agent_trace_archive，不是删。数据还在、能查，
-- 只是不再占着在线表的 B+ 树和 buffer pool。工具见
-- ``python -m smartmall_pipeline.cli archive-traces``（分批、可中断、可重跑）。
--
-- ---------------------------------------------------------------- 为什么这样设计这张表
--
-- · 列与 agent_trace 完全一致，主键沿用原表的 id：归档不换 id，事后按 id 反查才对得上。
--   id 不能自增（NOT NULL 而非 AUTO_INCREMENT）——它是搬来的，不是新生成的。
-- · 索引只留三个：主键、created_at（按时间导出/清理）、trace_id（见下）。
--   在线表的 idx_session / idx_thumb_score / idx_handover / idx_product 在这里都不要：
--   归档表几乎只写不读，每多一个索引就是每次归档多一次 B+ 树维护。
-- · trace_id 要有索引：点赞可能迟到。用户在一个开了很久的页面上点了赞，这时那一行
--   可能已经被归档走了；record_feedback 在在线表 UPDATE 不到时会回头更新归档表。
--   点赞是最值钱的字段（点踩直接当 DPO 负例），不能因为归档就丢。
-- · 带 thumb 的行默认**不归档**（见 archive-traces --include-feedback）：它们是训练数据，
--   占比很小（点赞点踩的人不多），留在在线表里导训练集时不必两张表 UNION。
--
-- 执行：mysql -u root -p smartmall < deploy/sql/migrations/015_agent_trace_archive.sql

CREATE TABLE IF NOT EXISTS `agent_trace_archive` (
  `id`                BIGINT UNSIGNED NOT NULL                COMMENT '沿用 agent_trace.id，不重新生成',
  `trace_id`          CHAR(32)      NOT NULL,
  `session_id`        CHAR(32)      NOT NULL,
  `user_id`           BIGINT UNSIGNED DEFAULT NULL,
  `product_id`        BIGINT UNSIGNED DEFAULT NULL,

  `input_text`        TEXT          NOT NULL,
  `rewritten_query`   VARCHAR(512)  NOT NULL DEFAULT '',
  `intent`            VARCHAR(32)   NOT NULL DEFAULT '',

  `retrieval_hit_count` INT         NOT NULL DEFAULT 0,
  `retrieval_max_score` DECIMAL(6,4) NOT NULL DEFAULT 0,
  `retrieval_item_ids`  JSON        DEFAULT NULL,

  `answer`            TEXT          DEFAULT NULL,
  `citations`         JSON          DEFAULT NULL,
  `postcheck_flags`   JSON          DEFAULT NULL,

  `handover`          TINYINT(1)    NOT NULL DEFAULT 0,
  `handover_reason`   VARCHAR(64)   NOT NULL DEFAULT '',

  `model`             VARCHAR(64)   NOT NULL DEFAULT '',
  `latency_ms`        JSON          DEFAULT NULL,
  `error`             VARCHAR(512)  NOT NULL DEFAULT '',

  `thumb`             TINYINT       DEFAULT NULL,
  `feedback_reason`   VARCHAR(64)   NOT NULL DEFAULT '',
  `feedback_at`       DATETIME      DEFAULT NULL,

  `created_at`        DATETIME      NOT NULL,
  `archived_at`       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '什么时候被搬过来的',
  PRIMARY KEY (`id`),
  KEY `idx_created` (`created_at`),
  KEY `idx_trace` (`trace_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='agent_trace 归档（超过保留期的对话埋点，几乎只写不读）';
