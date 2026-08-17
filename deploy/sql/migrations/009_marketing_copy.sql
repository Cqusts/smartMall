-- 009 · 运营 Agent：商品文案与需求信号
--
-- 两件事：
--   ① agent_trace 补 product_id —— 没有它就答不出"用户对这个商品问得最多的
--      是什么"，而那恰恰是运营 Agent 区别于"套模板写文案"的唯一依据
--   ② marketing_copy 存生成的文案，**一律待审**
--
-- 执行：mysql -u root -p smartmall < deploy/sql/migrations/009_marketing_copy.sql

-- ---------------------------------------------------------------- 需求信号

-- 埋点里一直缺这一维。客服会话是从商品页进来的，current_product_id 在
-- SessionContext 里躺着，却没落库——于是"这件商品用户最关心什么"只能靠
-- 转人工工单来猜，而工单只记录了**答不上来**的那些，是有偏的样本。
ALTER TABLE `agent_trace`
  ADD COLUMN `product_id` BIGINT UNSIGNED DEFAULT NULL
    COMMENT '会话当时聚焦的商品。运营 Agent 靠它统计真实需求分布'
    AFTER `user_id`,
  ADD KEY `idx_product` (`product_id`, `created_at`);


-- ---------------------------------------------------------------- 文案

CREATE TABLE IF NOT EXISTS `marketing_copy` (
  `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `product_id`     BIGINT UNSIGNED NOT NULL,

  `title`          VARCHAR(120)  NOT NULL DEFAULT ''   COMMENT '商品标题',
  `main_images`    JSON          DEFAULT NULL          COMMENT '主图角标文案，短句数组',
  `selling_points` JSON          DEFAULT NULL          COMMENT '卖点短句数组',
  `detail`         TEXT                                COMMENT '详情长文案',
  `script`         TEXT                                COMMENT '短视频/直播口播脚本',

  -- 卖点是从哪来的。**不留这个就没法回答"这条文案凭什么这么写"**，
  -- 而合规被质询时要拿得出依据
  `evidence`       JSON          DEFAULT NULL          COMMENT '生成时用到的属性与需求信号',
  `demand_signals` JSON          DEFAULT NULL          COMMENT '用户对该商品的高频提问',

  `flags`          JSON          DEFAULT NULL          COMMENT '合规检查命中项',
  -- 机器生成的文案永远不自动发布：广告法的责任在店铺，不在模型
  `review_status`  VARCHAR(16)   NOT NULL DEFAULT 'pending'
                                 COMMENT 'pending/approved/rejected',
  -- 《人工智能生成合成内容标识办法》要求生成内容可识别。
  -- 存成字段而不是在正文里塞"AI生成"三个字：展示层要不要加角标是展示层的事，
  -- 但"这段文案是机器写的"这个事实必须留在数据里
  `ai_generated`   TINYINT(1)    NOT NULL DEFAULT 1,

  `model`          VARCHAR(64)   NOT NULL DEFAULT '',
  `reviewer_id`    BIGINT UNSIGNED DEFAULT NULL,
  `reviewed_at`    DATETIME      DEFAULT NULL,
  `created_at`     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  KEY `idx_product` (`product_id`, `created_at`),
  KEY `idx_review` (`review_status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='运营 Agent 生成的商品文案（待审）';
