-- 迁移 002：知识向量表
--
-- 用途：在没有 Milvus 的环境（比如未安装 Docker 的开发机）也能跑通检索。
-- 几万条以内，向量全量载入内存做暴力检索完全够用；
-- 数据量再大时切到 Milvus，只改配置，检索语义不变。
--
-- 向量存 BLOB 而非 JSON：1024 维 float32 二进制 4KB，JSON 化约 20KB，差 5 倍。

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS kb_embedding (
    item_id    BIGINT       NOT NULL COMMENT '关联 knowledge_item.id',
    chunk_seq  SMALLINT     NOT NULL DEFAULT 0 COMMENT '同一条知识切出的第几块',
    chunk_text TEXT         NOT NULL COMMENT '被索引的文本（含标题/标题路径前缀）',
    vector     BLOB         NOT NULL COMMENT 'float32 小端紧凑二进制，已 L2 归一化',
    dim        SMALLINT     NOT NULL DEFAULT 1024,
    provider   VARCHAR(64)  NOT NULL COMMENT '向量化后端。混用不同后端会让相似度失去意义，加载时校验',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (item_id, chunk_seq),
    INDEX idx_provider (provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识向量（无 Milvus 环境的轻量索引）';
