-- ============================================================
-- 数据中台 · mall-dataplat
-- 分层：ODS 原始层 → DWD 明细层 → DWS 服务层 → 数据资产层
-- 详见 docs/03-data-platform.md
-- ============================================================
SET NAMES utf8mb4;

-- ------------------------------------------------------------
-- 数据源登记与任务
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ds_source (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    source_code   VARCHAR(32)  NOT NULL COMMENT 'jddc|jddc2|ecd|trace|synthetic|manual',
    name          VARCHAR(128) NOT NULL,
    source_type   VARCHAR(32)  NOT NULL COMMENT 'public_dataset|internal_trace|synthetic',
    license       VARCHAR(128) COMMENT '许可证/授权类型',
    license_scope VARCHAR(512) COMMENT '授权范围。合规要求：用途不得超出此范围（见 docs/15）',
    authorized_at DATE,
    expires_at    DATE,
    note          VARCHAR(512),
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_source_code (source_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据源登记（含授权范围，合规审计依据）';

CREATE TABLE IF NOT EXISTS ds_job (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    job_type      VARCHAR(32)  NOT NULL COMMENT 'ingest|clean|annotate|index|publish',
    source_code   VARCHAR(32),
    batch_id      VARCHAR(64),
    status        VARCHAR(16)  NOT NULL DEFAULT 'pending'
                               COMMENT 'pending|running|success|failed|cancelled',
    stats         JSON         COMMENT '各关卡进出量与淘汰率',
    error_msg     TEXT,
    started_at    DATETIME,
    finished_at   DATETIME,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_type_status (job_type, status),
    INDEX idx_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据中台任务';

-- ------------------------------------------------------------
-- ODS 原始层 —— 只增不改，保证任何加工出错都能重跑
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ods_raw_dialogue (
    id            BIGINT      PRIMARY KEY AUTO_INCREMENT,
    source_type   VARCHAR(32) NOT NULL COMMENT 'jddc|ecd|trace|manual_import',
    batch_id      VARCHAR(64) NOT NULL COMMENT '导入批次，便于按批重跑/回滚',
    external_id   VARCHAR(128),
    raw_payload   LONGTEXT    NOT NULL COMMENT '原样保存，不做任何加工',
    payload_hash  CHAR(64)    NOT NULL COMMENT 'SHA256，幂等导入',
    ingested_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_hash (payload_hash),
    INDEX idx_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原始对话（只增不改）';

-- ODS 记录处理日志。
--
-- 为什么需要单独一张表：ODS 是只增不改的（任何加工出错都要能重跑），
-- 所以不能往 ods_raw_dialogue 加"已处理"状态列。而"哪些已处理"又必须可查，
-- 否则每次清洗都会把同一批对话重新处理一遍，产出成倍的重复知识。
--
-- 顺带解决另一个问题：在关卡①②被淘汰的对话根本到不了 DWD，
-- 靠 JOIN dwd_dialogue_session 判断"已处理"会把它们永远当成待处理。
-- 这张表按记录粒度留痕，也让"这条对话为什么没进知识库"可追。
CREATE TABLE IF NOT EXISTS ods_process_log (
    id              BIGINT      PRIMARY KEY AUTO_INCREMENT,
    ods_id          BIGINT      NOT NULL,
    batch_id        VARCHAR(64) NOT NULL COMMENT '清洗批次',
    outcome         VARCHAR(16) NOT NULL COMMENT 'passed 产出知识 | dropped 被淘汰',
    dropped_at_gate VARCHAR(32)          COMMENT '被哪一关淘汰',
    processed_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ods (ods_id),
    INDEX idx_batch (batch_id),
    INDEX idx_outcome (outcome)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ODS 处理日志（ODS 只增不改，处理状态外置）';

CREATE TABLE IF NOT EXISTS ods_raw_asset (
    id            BIGINT      PRIMARY KEY AUTO_INCREMENT,
    asset_id      BIGINT      NOT NULL,
    batch_id      VARCHAR(64) NOT NULL,
    raw_payload   LONGTEXT    NOT NULL,
    oss_key       VARCHAR(512),
    payload_hash  CHAR(64)    NOT NULL,
    ingested_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_hash (payload_hash),
    INDEX idx_asset (asset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原始素材元数据';

CREATE TABLE IF NOT EXISTS ods_raw_clip (
    id            BIGINT      PRIMARY KEY AUTO_INCREMENT,
    live_id       BIGINT      NOT NULL,
    batch_id      VARCHAR(64) NOT NULL,
    raw_payload   LONGTEXT    NOT NULL COMMENT 'ASR 全片转写 + 分段结果',
    oss_key       VARCHAR(512) COMMENT '原始录像路径',
    payload_hash  CHAR(64)    NOT NULL,
    ingested_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_hash (payload_hash),
    INDEX idx_live (live_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原始直播切片数据';

CREATE TABLE IF NOT EXISTS ods_raw_doc (
    id            BIGINT      PRIMARY KEY AUTO_INCREMENT,
    doc_type      VARCHAR(32) NOT NULL COMMENT 'policy|activity|manual',
    batch_id      VARCHAR(64) NOT NULL,
    title         VARCHAR(256),
    raw_payload   LONGTEXT    NOT NULL,
    oss_key       VARCHAR(512),
    payload_hash  CHAR(64)    NOT NULL,
    ingested_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_hash (payload_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原始文档';

-- ------------------------------------------------------------
-- DWD 明细层 —— 标准化、脱敏、拆到最小粒度
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwd_dialogue_session (
    id                BIGINT      PRIMARY KEY AUTO_INCREMENT,
    ods_id            BIGINT      NOT NULL,
    session_no        VARCHAR(64) NOT NULL,
    channel           VARCHAR(32) COMMENT 'jddc|web|app|live',
    agent_type        VARCHAR(16) COMMENT 'human|ai|mixed',
    agent_id          BIGINT      COMMENT '人工客服 ID；AI 客服用负数区分',
    product_ids       JSON,
    turn_count        INT,
    started_at        DATETIME,
    ended_at          DATETIME,
    has_image         TINYINT     NOT NULL DEFAULT 0,
    transferred_human TINYINT     NOT NULL DEFAULT 0,
    order_created     TINYINT     NOT NULL DEFAULT 0 COMMENT '是否成单，考核转化率用',
    UNIQUE KEY uk_session (session_no),
    INDEX idx_agent (agent_id, started_at),
    INDEX idx_ods (ods_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标准化会话';

CREATE TABLE IF NOT EXISTS dwd_dialogue_turn (
    id            BIGINT      PRIMARY KEY AUTO_INCREMENT,
    session_id    BIGINT      NOT NULL,
    turn_index    INT         NOT NULL,
    role          VARCHAR(16) NOT NULL COMMENT 'user|agent|system',
    content       TEXT        NOT NULL COMMENT '已脱敏',
    raw_content   TEXT        COMMENT '脱敏前，加密存储，仅审计角色可解密（合规要求）',
    image_urls    JSON,
    intent        VARCHAR(64),
    sentiment     VARCHAR(16) COMMENT 'positive|neutral|negative',
    reply_cost_ms INT         COMMENT '响应耗时，考核时效指标用',
    said_at       DATETIME    NOT NULL,
    INDEX idx_session (session_id, turn_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标准化对话轮次';

CREATE TABLE IF NOT EXISTS dwd_clip_segment (
    id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
    ods_id         BIGINT       NOT NULL,
    live_id        BIGINT       NOT NULL,
    seq            INT          NOT NULL,
    start_ms       INT          NOT NULL,
    end_ms         INT          NOT NULL,
    transcript     TEXT         NOT NULL,
    speaker        VARCHAR(32)  COMMENT '主播|助播',
    product_id     BIGINT       COMMENT 'NULL 表示待人工确认',
    match_conf     DECIMAL(4,3),
    selling_points JSON,
    asset_id       BIGINT       COMMENT '切好的视频素材 ID',
    INDEX idx_live (live_id, seq),
    INDEX idx_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='直播切片段';

-- ------------------------------------------------------------
-- DWS 服务层 —— 两个统一抽象
-- ------------------------------------------------------------

-- knowledge_item：RAG 知识库的唯一数据源。
-- 清洗后的对话 QA、素材 VLM 描述、直播口播话术、商品属性、售后政策，全部归一到这张表。
CREATE TABLE IF NOT EXISTS knowledge_item (
    id                BIGINT        PRIMARY KEY AUTO_INCREMENT,
    biz_type          VARCHAR(32)   NOT NULL COMMENT 'qa|spec|script|policy|faq 知识的形态',
    modality          VARCHAR(16)   NOT NULL DEFAULT 'text' COMMENT 'text|image|video',
    knowledge_type    VARCHAR(32)   NOT NULL DEFAULT 'other'
                                    COMMENT 'spec|logistics|aftersale|sizing|promotion|usage|other 知识的主题。
与 biz_type 是两个维度：biz_type 说"这是什么形态的知识"（问答/属性/话术），
knowledge_type 说"这条知识讲的是什么"。后者是覆盖度矩阵的列维度——
不落库的话矩阵永远显示 0% 覆盖率，"哪里缺知识"就无从谈起',

    title             VARCHAR(512)  COMMENT '问题 / 标题',
    content           TEXT          NOT NULL COMMENT '答案 / 正文',
    summary           VARCHAR(1024) COMMENT '摘要，长文本粗排用',

    asset_ids         JSON          COMMENT '关联素材。回答时由程序挂载 URL，不由模型决定',
    product_ids       JSON          COMMENT '检索时的元数据过滤依据',
    category_id       BIGINT,
    tags              JSON,

    source            VARCHAR(32)   NOT NULL COMMENT 'dataset|manual|agent|live|trace|doc|handover',
    source_ref        VARCHAR(256)  COMMENT '溯源：ods 记录 ID / trace_id / clip_id',

    quality_score     DECIMAL(4,3),
    confidence        DECIMAL(4,3)  COMMENT '模型抽取置信度。低于阈值优先推人工（主动学习）',
    review_status     VARCHAR(16)   NOT NULL DEFAULT 'pending'
                                    COMMENT 'pending|approved|rejected|revised。未审核绝不进索引',
    reviewer_id       BIGINT,
    reviewed_at       DATETIME,

    valid_from        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to          DATETIME      COMMENT 'NULL=永久有效。活动/促销类必填，防止播报过期活动',

    version           INT           NOT NULL DEFAULT 1,
    embedding_status  VARCHAR(16)   NOT NULL DEFAULT 'pending'
                                    COMMENT 'pending|indexed|stale|failed。stale=内容改了但向量没更新',
    indexed_at        DATETIME,

    created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted           TINYINT       NOT NULL DEFAULT 0,

    INDEX idx_review_embed (review_status, embedding_status),
    INDEX idx_category (category_id),
    INDEX idx_biz_modality (biz_type, modality),
    INDEX idx_coverage (category_id, knowledge_type),
    INDEX idx_valid (valid_from, valid_to),
    INDEX idx_source (source, source_ref(64))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识条目：RAG 知识库的唯一数据源';

-- sft_sample：微调的唯一数据源。目标是学风格，不是学知识。
CREATE TABLE IF NOT EXISTS sft_sample (
    id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
    sample_type    VARCHAR(16)  NOT NULL COMMENT 'sft|dpo',

    messages       JSON         NOT NULL COMMENT 'SFT: 完整对话；DPO: 上下文',
    chosen         TEXT         COMMENT 'DPO 正例：真人客服 / 人工改写',
    rejected       TEXT         COMMENT 'DPO 负例：未微调模型的输出，即"AI 味"样本',

    style_tags     JSON         COMMENT '["口语化","短句","带表情","热情"]',
    scene_tag      VARCHAR(32)  COMMENT '售前咨询|尺码推荐|催单|售后安抚|议价',

    quality_score  DECIMAL(4,3),
    is_golden      TINYINT      NOT NULL DEFAULT 0 COMMENT '人工精修的黄金样本',
    agent_id       BIGINT       COMMENT '来源客服。单人占比 >15% 需降采样，避免学成某一个人',

    source         VARCHAR(32)  NOT NULL COMMENT 'dataset|trace|manual_rewrite|contrastive',
    source_ref     VARCHAR(256),
    review_status  VARCHAR(16)  NOT NULL DEFAULT 'pending',

    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted        TINYINT      NOT NULL DEFAULT 0,

    INDEX idx_type_status (sample_type, review_status),
    INDEX idx_scene (scene_tag),
    INDEX idx_agent (agent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='微调样本：SFT 与 DPO 的统一形态';

-- ------------------------------------------------------------
-- 数据资产层 —— 版本化发布
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dataset_version (
    id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
    dataset_type  VARCHAR(16)  NOT NULL COMMENT 'kb|sft|dpo|eval',
    version       VARCHAR(32)  NOT NULL COMMENT 'kb-v3 / sft-v2',
    item_count    INT          NOT NULL,
    filter_sql    TEXT         COMMENT '生成该版本的筛选条件，用于复现。必须含评测集排除子句',
    snapshot_key  VARCHAR(256) COMMENT '对象存储 JSONL 快照路径',
    stats         JSON         COMMENT '类目/来源/质量分分布',
    status        VARCHAR(16)  NOT NULL DEFAULT 'draft'
                               COMMENT 'draft|published|deprecated|rollback',
    published_by  BIGINT,
    published_at  DATETIME,
    note          VARCHAR(512),
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_type_version (dataset_type, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据资产版本';

-- 评测样本。dataset_version.filter_sql 必须排除这里的 session_id，
-- 否则会出现"在训练集上评测"的经典错误（见 docs/12-eval-observability.md）。
CREATE TABLE IF NOT EXISTS eval_sample (
    id           BIGINT       PRIMARY KEY AUTO_INCREMENT,
    eval_set     VARCHAR(32)  NOT NULL COMMENT 'eval-retrieval|eval-answer|eval-negative|...',
    session_id   BIGINT       COMMENT '来源会话，用于与训练集做硬性隔离',
    query        TEXT         NOT NULL,
    expected     JSON         COMMENT '期望结果：item_id 列表 / golden 答案 / 期望素材',
    meta         JSON,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_set (eval_set),
    INDEX idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评测样本';

CREATE TABLE IF NOT EXISTS eval_result (
    id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
    eval_set       VARCHAR(32)  NOT NULL,
    kb_version     VARCHAR(32)  COMMENT '与数据版本绑定，否则效果变化无法归因',
    model_version  VARCHAR(32),
    metrics        JSON         NOT NULL COMMENT 'recall@5 / mrr / faithfulness ...',
    passed         TINYINT      NOT NULL DEFAULT 0 COMMENT '是否通过门禁',
    ran_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_set_time (eval_set, ran_at),
    INDEX idx_versions (kb_version, model_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评测结果（与数据/模型版本双绑定）';

CREATE TABLE IF NOT EXISTS model_version (
    id             BIGINT       PRIMARY KEY AUTO_INCREMENT,
    model_name     VARCHAR(64)  NOT NULL COMMENT 'smartmall-cs-style',
    version        VARCHAR(32)  NOT NULL,
    base_model     VARCHAR(64)  NOT NULL COMMENT 'Qwen/Qwen3-8B',
    train_stage    VARCHAR(16)  NOT NULL COMMENT 'sft|dpo',
    parent_version VARCHAR(32)  COMMENT '接续训练的上一版本',
    sft_dataset    VARCHAR(32)  COMMENT '绑定数据资产版本，使模型迭代可归因',
    dpo_dataset    VARCHAR(32),
    hyperparams    JSON,
    eval_result    JSON,
    lora_path      VARCHAR(256),
    status         VARCHAR(16)  NOT NULL DEFAULT 'training'
                                COMMENT 'training|evaluating|staged|online|rollback',
    traffic_ratio  DECIMAL(4,3) NOT NULL DEFAULT 0 COMMENT 'A/B 流量占比',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_name_version (model_name, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型版本';
