-- 迁移 001：ODS 处理日志 + knowledge_item.knowledge_type
--
-- 适用对象：已经导入过初版 schema（30 张表）的库。
-- 全新安装不需要执行——deploy/sql/mysql/*.sql 里已经包含这些定义。
--
-- 两处变更的原因：
--
-- 1. ods_process_log
--    原先靠 JOIN dwd_dialogue_session 判断"哪些 ODS 记录已处理"，但清洗
--    流水线并不写 DWD，且在关卡①②被淘汰的对话根本到不了 DWD。结果是
--    每次清洗都把同一批对话重新处理一遍，产出成倍的重复知识。
--    ODS 要保持只增不改，所以处理状态外置到独立的日志表。
--
-- 2. knowledge_item.knowledge_type
--    关卡③判定出的知识主题（spec/logistics/...）原先没有落库，
--    而它正是覆盖度矩阵的列维度——不落库矩阵永远显示 0% 覆盖率。

SET NAMES utf8mb4;

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

ALTER TABLE knowledge_item
  ADD COLUMN knowledge_type VARCHAR(32) NOT NULL DEFAULT 'other'
    COMMENT 'spec|logistics|aftersale|sizing|promotion|usage|other 知识主题，覆盖度矩阵的列维度'
    AFTER modality,
  ADD INDEX idx_coverage (category_id, knowledge_type);
