-- 006：主播叫法的待确认队列
--
-- 切片链路里那个闭环缺的就是这一张表：
--
--   商品名/别名 → ASR 热词 → 转写更准 → 商品对齐更准
--        ↑                                      ↓
--        └────── 人工确认后回写 ←──── 主播实际用的叫法
--
-- 主播管「米白色圆领宽松针织衫」叫「这件米白」。这个叫法一旦回写成别名，
-- 下一场直播它就是 ASR 热词，转写就不会再把它听成「米百」。
--
-- **为什么要人工确认而不是自动回写。** 对齐命中的那个词可能是巧合——
-- 主播说「这个颜色跟刚才那件米白很像」，讲的其实是另一件商品。自动回写
-- 会把错的叫法喂回热词表，而热词是带权重的：一个错别名会让往后每一场
-- 直播都朝错的方向偏。这类错误自己会放大，必须有人看一眼。

CREATE TABLE IF NOT EXISTS `clip_alias_proposal` (
  `id`         BIGINT       PRIMARY KEY AUTO_INCREMENT,
  `product_id` BIGINT       NOT NULL,
  `alias`      VARCHAR(64)  NOT NULL COMMENT '主播实际说出口的叫法',
  `times`      INT          NOT NULL DEFAULT 1 COMMENT '被说到的次数，越多越可信',
  `source_ref` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '哪一场直播，用于回溯',
  `sample`     TEXT         COMMENT '原话片段。人工判断"这个叫法指的是不是这件商品"只能看上下文',
  `status`     VARCHAR(16)  NOT NULL DEFAULT 'pending'
                            COMMENT 'pending|approved|rejected',
  `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
  -- 同一个商品的同一个叫法只留一条，重复出现累加 times
  UNIQUE KEY `uk_product_alias` (`product_id`, `alias`),
  KEY `idx_pending` (`status`, `times`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='主播叫法待确认队列（切片闭环）';
