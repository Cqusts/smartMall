-- ============================================================
-- AI 素材中心 · mall-asset
-- 详见 docs/07-asset-center.md
-- ============================================================
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS asset (
    id                 BIGINT       PRIMARY KEY AUTO_INCREMENT,
    asset_no           VARCHAR(64)  NOT NULL COMMENT '业务编号，对外暴露',
    modality           VARCHAR(16)  NOT NULL COMMENT 'image|video|audio',
    scene              VARCHAR(32)  COMMENT 'white_bg|model|flatlay|detail|poster|clip|ad_video',

    -- 存储
    oss_key            VARCHAR(512) NOT NULL,
    cdn_url            VARCHAR(512),
    thumb_url          VARCHAR(512) COMMENT '缩略图 / 视频封面帧',
    file_size          BIGINT,
    mime_type          VARCHAR(64),
    width              INT,
    height             INT,
    duration_ms        INT,
    file_hash          CHAR(64)     NOT NULL COMMENT 'SHA256，去重与溯源',

    -- 来源与可复现性
    source             VARCHAR(32)  NOT NULL COMMENT 'ai_generate|live_clip|manual_upload',
    gen_task_id        VARCHAR(64),
    gen_workflow       VARCHAR(64)  COMMENT 'ComfyUI 工作流 ID@版本，如 model_scene@v3',
    gen_model          VARCHAR(64)  COMMENT '如 wan2.2-ti2v-5b',
    gen_params         JSON         COMMENT '完整生成参数（prompt/seed/steps）。缺了就无法复现',
    ref_asset_ids      JSON         COMMENT '参考素材：IP-Adapter 输入图 / 视频首帧',

    -- AI 标识（《人工智能生成合成内容标识办法》要求，三项必须齐全）
    ai_generated       TINYINT      NOT NULL DEFAULT 0,
    ai_label_applied   TINYINT      NOT NULL DEFAULT 0 COMMENT '显式角标已注入',
    ai_meta_applied    TINYINT      NOT NULL DEFAULT 0 COMMENT '隐式元数据已写入',
    ai_watermark       TINYINT      NOT NULL DEFAULT 0 COMMENT '数字水印已嵌入',

    -- 生命周期
    status             VARCHAR(16)  NOT NULL DEFAULT 'draft'
                                    COMMENT 'draft|reviewing|approved|online|offline|rejected|archived',
    version            INT          NOT NULL DEFAULT 1,
    parent_asset_id    BIGINT       COMMENT '版本链，指向上一版本',

    -- 标注（供 RAG 使用）
    vlm_description    TEXT         COMMENT 'VLM 生成的结构化描述，组装为自然语言后入 knowledge_item',
    vlm_attrs          JSON,
    desc_review_status VARCHAR(16)  NOT NULL DEFAULT 'pending'
                                    COMMENT '描述审核，独立于内容审核（status）',
    tags               JSON,

    -- 质量
    aesthetic_score    DECIMAL(4,3),
    subject_similarity DECIMAL(4,3) COMMENT '与商品实拍图的 CLIP 相似度，<0.75 不允许进审核',

    created_by         BIGINT,
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted            TINYINT      NOT NULL DEFAULT 0,

    UNIQUE KEY uk_asset_no (asset_no),
    INDEX idx_hash (file_hash),
    INDEX idx_status (status, modality),
    INDEX idx_scene (scene, status),
    INDEX idx_gen_task (gen_task_id),
    INDEX idx_desc_review (desc_review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 素材主表';

CREATE TABLE IF NOT EXISTS asset_product_rel (
    id           BIGINT       PRIMARY KEY AUTO_INCREMENT,
    asset_id     BIGINT       NOT NULL,
    product_id   BIGINT       NOT NULL,
    sku_id       BIGINT       COMMENT '精确到 SKU（如某个颜色的图）',
    rel_type     VARCHAR(32)  NOT NULL COMMENT 'main|detail|scene|clip|size_chart|ad',
    sort_order   INT          NOT NULL DEFAULT 0,
    is_primary   TINYINT      NOT NULL DEFAULT 0,
    bind_source  VARCHAR(16)  NOT NULL DEFAULT 'manual' COMMENT 'auto|manual 自动匹配还是人工绑定',
    bind_conf    DECIMAL(4,3) COMMENT '自动匹配置信度。<0.85 必须进人工确认队列',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_asset_product_type (asset_id, product_id, rel_type),
    INDEX idx_product (product_id, rel_type, sort_order),
    INDEX idx_pending_bind (bind_source, bind_conf)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='素材与商品关联（多对多）';

CREATE TABLE IF NOT EXISTS asset_clip_meta (
    asset_id        BIGINT       PRIMARY KEY,
    live_id         BIGINT       NOT NULL,
    live_title      VARCHAR(256),
    anchor_id       BIGINT       COMMENT '主播 ID',
    start_ms        INT          NOT NULL COMMENT '在原直播中的起始位置',
    end_ms          INT          NOT NULL,
    transcript      TEXT         COMMENT 'ASR 转写文本',
    selling_points  JSON         COMMENT 'LLM 抽取的卖点',
    objection_qa    JSON         COMMENT '异议处理话术。价值最高的一类，直接进知识库',
    subtitle_key    VARCHAR(512) COMMENT 'SRT 字幕文件路径',
    INDEX idx_live (live_id, start_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='直播切片素材元数据';

CREATE TABLE IF NOT EXISTS asset_audit (
    id           BIGINT       PRIMARY KEY AUTO_INCREMENT,
    asset_id     BIGINT       NOT NULL,
    audit_type   VARCHAR(16)  NOT NULL COMMENT 'content 内容审核 | description 描述审核',
    action       VARCHAR(16)  NOT NULL COMMENT 'approve|reject|revise',
    reason       VARCHAR(512),
    before_value TEXT,
    after_value  TEXT,
    auditor_id   BIGINT       NOT NULL,
    audited_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_asset (asset_id, audited_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='素材审核流水';
