-- ============================================================
-- 销售考核 · mall-kpi
-- 详见 docs/09-sales-kpi.md
-- ============================================================
SET NAMES utf8mb4;

-- 指标配置。权重可按岗位（售前/售后）配不同模板。
CREATE TABLE IF NOT EXISTS kpi_metric (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    metric_code   VARCHAR(32)  NOT NULL,
    metric_name   VARCHAR(64)  NOT NULL,
    metric_type   VARCHAR(16)  NOT NULL COMMENT 'rule 确定性 | judge 判断性 | outcome 结果性',
    template      VARCHAR(32)  NOT NULL DEFAULT 'presale' COMMENT '权重模板：presale|aftersale',
    weight        DECIMAL(5,4) NOT NULL DEFAULT 0,
    is_veto       TINYINT      NOT NULL DEFAULT 0 COMMENT '一票否决项',
    rubric        TEXT         COMMENT '判断性指标的评分细则。每档必须是可观察的行为描述',
    enabled       TINYINT      NOT NULL DEFAULT 1,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_template (metric_code, template)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='考核指标配置';

CREATE TABLE IF NOT EXISTS kpi_score (
    id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
    session_id     BIGINT       NOT NULL,
    agent_id       BIGINT       NOT NULL COMMENT 'AI 客服用负数 ID 区分',
    agent_type     VARCHAR(16)  NOT NULL COMMENT 'human|ai',
    score_date     DATE         NOT NULL,

    total_score    DECIMAL(5,2) NOT NULL,
    metric_scores  JSON         NOT NULL,
    evidences      JSON         NOT NULL COMMENT '每项扣分的原话证据。无证据的评分应被拒绝',
    veto_flags     JSON         COMMENT '一票否决项命中列表',
    root_cause     VARCHAR(32)  COMMENT 'AI 低分归因：knowledge_gap|style_issue|retrieval_error|tool_error',

    judge_model    VARCHAR(64)  NOT NULL,
    rubric_version VARCHAR(32)  NOT NULL COMMENT 'Rubric 变更后分数不可跨版本比较',
    calib_batch    VARCHAR(32)  COMMENT '当时生效的校准批次',

    appeal_status  VARCHAR(16)  NOT NULL DEFAULT 'none'
                                COMMENT 'none|pending|accepted|rejected',
    final_score    DECIMAL(5,2) COMMENT '申诉后的最终分；NULL 表示以 total_score 为准',

    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_session_rubric (session_id, rubric_version),
    INDEX idx_agent_date (agent_id, score_date),
    INDEX idx_root_cause (root_cause),
    INDEX idx_low_score (score_date, total_score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话评分';

-- 校准黄金集。没有校准的 Judge 就是随机数生成器——
-- 人工一致性 α≥0.7 且 Judge vs Golden 的 Spearman ρ≥0.7 才允许上线。
CREATE TABLE IF NOT EXISTS kpi_golden_set (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    session_id    BIGINT       NOT NULL,
    metric_code   VARCHAR(32)  NOT NULL,
    human_scores  JSON         NOT NULL COMMENT '[{reviewer_id, score}] 多人独立评分',
    golden_score  DECIMAL(3,1) NOT NULL COMMENT '人工评分中位数',
    judge_score   DECIMAL(3,1),
    judge_model   VARCHAR(64),
    abs_error     DECIMAL(3,1) GENERATED ALWAYS AS (ABS(golden_score - judge_score)) STORED,
    batch_no      VARCHAR(32)  NOT NULL COMMENT '校准批次',
    from_appeal   TINYINT      NOT NULL DEFAULT 0 COMMENT '来自申诉成功的会话，用于 Rubric 迭代',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_session_metric_batch (session_id, metric_code, batch_no),
    INDEX idx_batch (batch_no, metric_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='考核校准黄金集';

CREATE TABLE IF NOT EXISTS kpi_calibration (
    id              BIGINT       PRIMARY KEY AUTO_INCREMENT,
    batch_no        VARCHAR(32)  NOT NULL,
    rubric_version  VARCHAR(32)  NOT NULL,
    judge_model     VARCHAR(64)  NOT NULL,
    sample_count    INT          NOT NULL,
    krippendorff_a  DECIMAL(4,3) COMMENT '人工标注者之间一致性，需 ≥0.7',
    spearman_rho    DECIMAL(4,3) COMMENT 'Judge vs Golden，需 ≥0.7',
    mae             DECIMAL(4,3) COMMENT '平均绝对误差，需 ≤0.5（5 分制）',
    extreme_err_pct DECIMAL(5,4) COMMENT '差 ≥2 分的比例，需 ≤5%',
    passed          TINYINT      NOT NULL DEFAULT 0 COMMENT '未通过不允许上线',
    note            VARCHAR(512),
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_batch (batch_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='校准报告（上线门禁依据）';

CREATE TABLE IF NOT EXISTS kpi_appeal (
    id           BIGINT       PRIMARY KEY AUTO_INCREMENT,
    score_id     BIGINT       NOT NULL,
    agent_id     BIGINT       NOT NULL,
    metric_code  VARCHAR(32)  NOT NULL,
    reason       TEXT         NOT NULL,
    status       VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending|accepted|rejected',
    reviewer_id  BIGINT,
    review_note  VARCHAR(512),
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at  DATETIME,
    INDEX idx_score (score_id),
    INDEX idx_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='考核申诉';
