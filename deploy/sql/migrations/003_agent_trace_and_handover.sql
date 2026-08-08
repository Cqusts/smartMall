-- 003 · Agent 埋点与转人工工单
--
-- 这两张表把数据飞轮的后半圈接上。此前只有「对话 → 清洗 → 知识 → 检索」，
-- Agent 跑出来的东西没有任何回路。
--
--   agent_trace      每一轮对话的全量埋点。**它不是日志，是训练数据的采集管道**，
--                    所以字段按训练数据的标准设计：每个字段都要能回答
--                    "下游谁会用它"（见 docs/05 第 7 节）。
--   handover_ticket  每一次转人工。转人工 = 一个已经暴露的知识盲点，
--                    人工答完回灌知识库，下次同样的问题就能自动回答。
--
-- 执行：mysql -u root -p smartmall < 003_agent_trace_and_handover.sql

CREATE TABLE IF NOT EXISTS `agent_trace` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `trace_id`          CHAR(32)      NOT NULL                COMMENT '随回复返回给前端，反馈靠它回写',
  `session_id`        CHAR(32)      NOT NULL                COMMENT '同一通对话的多轮共享',
  `user_id`           BIGINT UNSIGNED DEFAULT NULL,

  `input_text`        TEXT          NOT NULL                COMMENT '用户原话',
  `rewritten_query`   VARCHAR(512)  NOT NULL DEFAULT ''     COMMENT '首次命中不足时的改写查询',
  `intent`            VARCHAR(32)   NOT NULL DEFAULT ''     COMMENT '七类意图之一',

  `retrieval_hit_count` INT         NOT NULL DEFAULT 0,
  `retrieval_max_score` DECIMAL(6,4) NOT NULL DEFAULT 0     COMMENT '最高余弦相似度，用来标定转人工阈值',
  `retrieval_item_ids`  JSON        DEFAULT NULL            COMMENT '命中分布 → 识别从未被命中的死知识',

  `answer`            TEXT          DEFAULT NULL,
  `citations`         JSON          DEFAULT NULL            COMMENT '答案正文里实际出现的引用',
  `postcheck_flags`   JSON          DEFAULT NULL            COMMENT '合规标记 → 幻觉率趋势监控',

  `handover`          TINYINT(1)    NOT NULL DEFAULT 0,
  `handover_reason`   VARCHAR(64)   NOT NULL DEFAULT '',

  `model`             VARCHAR(64)   NOT NULL DEFAULT '',
  `latency_ms`        JSON          DEFAULT NULL            COMMENT '分阶段耗时，定位瓶颈用',
  `error`             VARCHAR(512)  NOT NULL DEFAULT ''     COMMENT '内部异常，用户看不到但要留痕',

  -- 反馈是最有价值的字段：点踩直接当 DPO 负例，
  -- 而"态度生硬""太啰嗦"这两个原因正对应微调的风格目标
  `thumb`             TINYINT       DEFAULT NULL            COMMENT '1 赞 / -1 踩 / NULL 未反馈',
  `feedback_reason`   VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '答非所问/信息错误/态度生硬/太啰嗦',
  `feedback_at`       DATETIME      DEFAULT NULL,

  `created_at`        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_trace` (`trace_id`),
  KEY `idx_session` (`session_id`),
  KEY `idx_created` (`created_at`),
  -- 「命中低分但用户点了赞」这类样本是标定阈值的关键，要能高效捞出来
  KEY `idx_thumb_score` (`thumb`, `retrieval_max_score`),
  KEY `idx_handover` (`handover`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客服 Agent 对话埋点（训练数据采集管道）';


CREATE TABLE IF NOT EXISTS `handover_ticket` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `trace_id`          CHAR(32)      NOT NULL,
  `session_id`        CHAR(32)      NOT NULL,
  `user_id`           BIGINT UNSIGNED DEFAULT NULL,

  `reason`            VARCHAR(64)   NOT NULL                COMMENT '知识库无内容/置信度低/敏感意图/用户要求/服务故障',
  `intent`            VARCHAR(32)   NOT NULL DEFAULT '',
  `product_id`        BIGINT UNSIGNED DEFAULT NULL,
  `question`          VARCHAR(512)  NOT NULL                COMMENT '没答上来的那个问题——补写任务的题面',
  `summary`           JSON          DEFAULT NULL            COMMENT '交接摘要，目的是让用户不必重复描述',

  -- 飞轮的关键三列：人工答完 → 生成 knowledge_item → 下次能自动回答
  `status`            VARCHAR(16)   NOT NULL DEFAULT 'open' COMMENT 'open / answered / imported / closed',
  `human_answer`      TEXT          DEFAULT NULL,
  `knowledge_item_id` BIGINT UNSIGNED DEFAULT NULL          COMMENT '回灌后生成的知识条目',

  `created_at`        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `answered_at`       DATETIME      DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_status` (`status`, `created_at`),
  KEY `idx_trace` (`trace_id`),
  -- 同一个问题反复转人工 = 高优先级盲点，按题面聚合能直接排出补写顺序
  KEY `idx_question` (`question`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='转人工工单（知识盲点的入口）';
