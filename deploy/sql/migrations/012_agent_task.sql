-- 012 · 跨 Agent 任务表：让四个 Agent 互相派活
--
-- ---------------------------------------------------------------- 为什么需要它
--
-- 在这张表之前，四个 Agent 是**各跑各的**：客服转人工，工单躺在
-- handover_ticket 里等人看；知识运维要人手动跑一次 `smartmall-agent kb`
-- 才知道有盲点；运营完全不知道知识补过了。准确的说法是「四个共用底座的
-- Agent」，不是「Agent 集群」。
--
-- 这张表把那条链闭上：
--
--   客服答不上来 → 派「补写知识」给知识运维
--                → 补完了，且这个盲点属于某个商品
--                → 派「更新文案」给运营
--
-- ---------------------------------------------------------------- 为什么不用 Kafka
--
-- 这是单机作品集，消费者只有一个进程、吞吐是每天几十条。上 Kafka 要多一个
-- Broker、一套消费组语义、一份重复消费的处理——**换来的是本来就不需要的
-- 吞吐**，而丢掉的是「一条 SQL 就能看清现在有哪些活没干完」。
--
-- 用表的代价是要自己做两件事，下面两段各说一件。
--
-- ---------------------------------------------------------------- 一、怎么防重复派
--
-- 同一个问题被 50 个人问，会派 50 次活。**去重不能靠 UNIQUE(dedupe_key)**：
-- 那样一个问题一辈子只能派一次，而知识是会过期、会被下线的——半年后同一个
-- 问题再次答不上来，本该重新排一次。
--
-- 所以是 ``open_key``：它在 pending/running 时等于 dedupe_key，任务一结束
-- 就置 NULL。UNIQUE 索引允许多个 NULL，于是「未完成的同类任务只能有一条」，
-- 而历史记录想留多少留多少。
--
-- 重复派到已存在的任务时不新建，只把 ``times`` 加一 —— 那个数字同时是
-- 优先级信号：**被问得多的盲点该先补**。
--
-- ---------------------------------------------------------------- 二、怎么防两个 worker 抢同一条
--
-- 认领是一条带条件的 UPDATE：
--
--   UPDATE agent_task SET status='running' WHERE id=? AND status='pending'
--
-- 只有 rowcount=1 的那个抢到了。SELECT 完再 UPDATE 是错的——两个进程会
-- 同时读到 pending 然后都去做。
--
-- 执行：mysql -u root -p smartmall < deploy/sql/migrations/012_agent_task.sql

CREATE TABLE IF NOT EXISTS `agent_task` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- write_knowledge 补写知识 | refresh_copy 更新文案
  `kind`          VARCHAR(32)   NOT NULL,
  -- pending | running | done | needs_human | failed | cancelled
  --
  -- **needs_human 与 failed 必须分开。** failed 是"跑挂了，可以重试"；
  -- needs_human 是"跑完了，结论就是得人来写"。混成一个的话，重试循环会
  -- 一遍遍去跑一件机器永远做不成的事，而真正的故障淹没在里面看不见。
  `status`        VARCHAR(16)   NOT NULL DEFAULT 'pending',

  -- 谁派的 / 派给谁。**留着是为了事后能画出那条链**：
  -- 少了它，一条「更新文案」任务看起来就像凭空冒出来的
  `source_agent`  VARCHAR(32)   NOT NULL DEFAULT '',
  `target_agent`  VARCHAR(32)   NOT NULL DEFAULT '',

  -- 去重键与「未完成」去重键，见文件头第一段
  `dedupe_key`    VARCHAR(160)  NOT NULL,
  `open_key`      VARCHAR(160)  DEFAULT NULL,
  -- 这件事被派了几次。同时是优先级信号
  `times`         INT           NOT NULL DEFAULT 1,
  `priority`      INT           NOT NULL DEFAULT 0,

  `payload`       JSON,
  `result`        JSON,
  `product_id`    BIGINT UNSIGNED DEFAULT NULL,

  -- 上一环 / 整条链的头。看闭环时按 root_id 聚
  `parent_id`     BIGINT UNSIGNED DEFAULT NULL,
  `root_id`       BIGINT UNSIGNED DEFAULT NULL,

  `attempts`      INT           NOT NULL DEFAULT 0,
  `max_attempts`  INT           NOT NULL DEFAULT 3,
  `error`         VARCHAR(512)  NOT NULL DEFAULT '',

  `claimed_at`    DATETIME      DEFAULT NULL,
  `finished_at`   DATETIME      DEFAULT NULL,
  `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  -- 未完成的同类任务只能有一条。NULL 不参与唯一性校验，所以已完成的
  -- 历史记录想留多少留多少
  UNIQUE KEY `uk_open` (`open_key`),
  -- 拉活时按它扫：先按优先级，同优先级按先来后到
  KEY `idx_pull` (`status`, `priority`, `id`),
  KEY `idx_chain` (`root_id`, `id`),
  KEY `idx_product` (`product_id`, `kind`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跨 Agent 任务队列';
