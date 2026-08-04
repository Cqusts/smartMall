-- ============================================================
-- 商品域 · mall-product
-- ============================================================
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS category (
    id          BIGINT       PRIMARY KEY AUTO_INCREMENT,
    parent_id   BIGINT       NOT NULL DEFAULT 0,
    name        VARCHAR(128) NOT NULL,
    level       TINYINT      NOT NULL DEFAULT 1,
    path        VARCHAR(256) NOT NULL COMMENT '类目路径，如 /1/24/1024',
    sort_order  INT          NOT NULL DEFAULT 0,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted     TINYINT      NOT NULL DEFAULT 0,
    INDEX idx_parent (parent_id),
    INDEX idx_path (path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品类目';

CREATE TABLE IF NOT EXISTS product (
    id           BIGINT        PRIMARY KEY AUTO_INCREMENT,
    product_no   VARCHAR(64)   NOT NULL COMMENT '业务编号，对外暴露',
    name         VARCHAR(256)  NOT NULL,
    short_name   VARCHAR(64)   COMMENT '简称，直播口播商品对齐时作为热词',
    alias        JSON          COMMENT '别名数组。主播的口语化叫法，由人工纠正记录反哺（见 docs/08）',
    category_id  BIGINT        NOT NULL,
    brand        VARCHAR(128),
    subtitle     VARCHAR(512)  COMMENT '卖点副标题',
    description  TEXT,
    main_image   VARCHAR(512)  COMMENT '主图 URL，运营 Agent 生成时作为 IP-Adapter 视觉锚点',
    status       VARCHAR(16)   NOT NULL DEFAULT 'draft' COMMENT 'draft|on_sale|off_shelf',
    created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted      TINYINT       NOT NULL DEFAULT 0,
    UNIQUE KEY uk_product_no (product_no),
    INDEX idx_category (category_id, status),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品';

CREATE TABLE IF NOT EXISTS sku (
    id            BIGINT        PRIMARY KEY AUTO_INCREMENT,
    sku_no        VARCHAR(64)   NOT NULL,
    product_id    BIGINT        NOT NULL,
    spec          JSON          NOT NULL COMMENT '规格：{"颜色":"米白","尺码":"M"}',
    price         DECIMAL(10,2) NOT NULL,
    origin_price  DECIMAL(10,2),
    stock         INT           NOT NULL DEFAULT 0,
    status        VARCHAR(16)   NOT NULL DEFAULT 'on_sale',
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted       TINYINT       NOT NULL DEFAULT 0,
    UNIQUE KEY uk_sku_no (sku_no),
    INDEX idx_product (product_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SKU';

-- 商品结构化属性。运营 Agent 的文案合规检查会拿文案中的材质/规格与本表比对，
-- 冲突则拦截——这是防虚假宣传的技术闸门（见 docs/06-agent-marketing.md）。
CREATE TABLE IF NOT EXISTS product_attr (
    id          BIGINT       PRIMARY KEY AUTO_INCREMENT,
    product_id  BIGINT       NOT NULL,
    attr_key    VARCHAR(64)  NOT NULL COMMENT '材质|克重|工艺|产地|洗涤方式',
    attr_value  VARCHAR(512) NOT NULL,
    is_core     TINYINT      NOT NULL DEFAULT 0 COMMENT '核心属性，文案必须与之一致',
    sort_order  INT          NOT NULL DEFAULT 0,
    UNIQUE KEY uk_product_key (product_id, attr_key),
    INDEX idx_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品结构化属性';

CREATE TABLE IF NOT EXISTS size_chart (
    id          BIGINT      PRIMARY KEY AUTO_INCREMENT,
    product_id  BIGINT      NOT NULL,
    chart       JSON        NOT NULL COMMENT '尺码表数据',
    note        VARCHAR(512),
    updated_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='尺码表';

-- 直播排品表。商品对齐时把候选集从全店收窄到本场 20-30 个商品，
-- 这是匹配准确率的关键优化（见 docs/08-agent-live-clip.md）。
CREATE TABLE IF NOT EXISTS live_schedule (
    id          BIGINT      PRIMARY KEY AUTO_INCREMENT,
    live_id     BIGINT      NOT NULL,
    product_id  BIGINT      NOT NULL,
    seq         INT         NOT NULL DEFAULT 0 COMMENT '讲解顺序',
    created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_live_product (live_id, product_id),
    INDEX idx_live (live_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='直播排品';
