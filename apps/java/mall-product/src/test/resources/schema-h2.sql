-- 测试用 schema，对应 deploy/sql/mysql/01_product.sql 与
-- deploy/sql/migrations/004 + 007 里下单链路用到的两张表。
--
-- 只保留下单链路碰得到的列。与生产 DDL 的差异只有类型别名（JSON → VARCHAR、
-- 无 ENGINE 子句），列名、非空约束、唯一索引与默认值都保持一致——
-- 尤其是 uk_request_id，幂等的全部保证都压在它身上，测试里必须是真的唯一索引。

DROP TABLE IF EXISTS mall_order;
DROP TABLE IF EXISTS sku;

CREATE TABLE sku (
    id            BIGINT        PRIMARY KEY AUTO_INCREMENT,
    sku_no        VARCHAR(64)   NOT NULL,
    product_id    BIGINT        NOT NULL,
    spec          VARCHAR(512)  NOT NULL,
    price         DECIMAL(10,2) NOT NULL,
    origin_price  DECIMAL(10,2),
    stock         INT           NOT NULL DEFAULT 0,
    status        VARCHAR(16)   NOT NULL DEFAULT 'on_sale',
    created_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted       TINYINT       NOT NULL DEFAULT 0,
    CONSTRAINT uk_sku_no UNIQUE (sku_no)
);

CREATE TABLE mall_order (
    id              BIGINT        PRIMARY KEY AUTO_INCREMENT,
    order_no        VARCHAR(32)   NOT NULL,
    request_id      VARCHAR(64)   DEFAULT NULL,
    user_id         BIGINT        NOT NULL,
    product_id      BIGINT        NOT NULL,
    sku_no          VARCHAR(64)   DEFAULT NULL,
    spec            VARCHAR(128)  NOT NULL DEFAULT '',
    quantity        INT           NOT NULL DEFAULT 1,
    amount          DECIMAL(10,2) NOT NULL,
    status          VARCHAR(20)   NOT NULL DEFAULT 'pending_payment',
    express_company VARCHAR(32)   NOT NULL DEFAULT '',
    express_no      VARCHAR(64)   NOT NULL DEFAULT '',
    tracks          VARCHAR(4000) DEFAULT NULL,
    created_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    shipped_at      TIMESTAMP     DEFAULT NULL,
    delivered_at    TIMESTAMP     DEFAULT NULL,
    completed_at    TIMESTAMP     DEFAULT NULL,
    cancelled_at    TIMESTAMP     DEFAULT NULL,
    refund_applied_at    TIMESTAMP     DEFAULT NULL,
    refunded_at          TIMESTAMP     DEFAULT NULL,
    refund_reason        VARCHAR(255)  NOT NULL DEFAULT '',
    refund_reject_reason VARCHAR(255)  NOT NULL DEFAULT '',
    refund_amount        DECIMAL(10,2) DEFAULT NULL,
    status_before_refund VARCHAR(20)   NOT NULL DEFAULT '',
    CONSTRAINT uk_order_no   UNIQUE (order_no),
    CONSTRAINT uk_request_id UNIQUE (request_id)
);

-- 用户与角色。与 deploy/sql/migrations/010_auth.sql 对齐。
--
-- 种子密码统一 smartmall123（BCrypt 工作因子 10），哈希是实算并 checkpw
-- 验过的——凭印象抄一个示例哈希的话，登录测试会一直红而且看不出为什么。
CREATE TABLE IF NOT EXISTS mall_user (
    id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    nickname      VARCHAR(64)  NOT NULL DEFAULT '',
    role          VARCHAR(16)  NOT NULL DEFAULT 'customer',
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_username UNIQUE (username)
);

-- 商品与结构化属性。与 deploy/sql/mysql/01_product.sql 对齐。
--
-- 此前测试库里没有这两张表：订单链路只碰 sku，碰不到它们。补商品维护时
-- 表现是 16 条错误全报「Table not found」——**测试库的 schema 是一份会
-- 悄悄落后于真库的副本**，加领域时得记着同步。
CREATE TABLE IF NOT EXISTS product (
    id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_no  VARCHAR(64)  NOT NULL,
    name        VARCHAR(256) NOT NULL,
    short_name  VARCHAR(64),
    alias       VARCHAR(1024),
    category_id BIGINT       NOT NULL,
    brand       VARCHAR(128),
    subtitle    VARCHAR(512),
    description CLOB,
    main_image  VARCHAR(512),
    status      VARCHAR(16)  NOT NULL DEFAULT 'draft',
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted     TINYINT      NOT NULL DEFAULT 0,
    CONSTRAINT uk_product_no UNIQUE (product_no)
);

CREATE TABLE IF NOT EXISTS product_attr (
    id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id  BIGINT       NOT NULL,
    attr_key    VARCHAR(64)  NOT NULL,
    attr_value  VARCHAR(512) NOT NULL,
    is_core     TINYINT      NOT NULL DEFAULT 0,
    sort_order  INT          NOT NULL DEFAULT 0,
    CONSTRAINT uk_product_key UNIQUE (product_id, attr_key)
);
