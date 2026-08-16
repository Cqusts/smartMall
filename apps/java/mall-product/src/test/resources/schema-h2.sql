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
    cancelled_at    TIMESTAMP     DEFAULT NULL,
    CONSTRAINT uk_order_no   UNIQUE (order_no),
    CONSTRAINT uk_request_id UNIQUE (request_id)
);
