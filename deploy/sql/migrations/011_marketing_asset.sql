-- 011 · 运营素材：AI 生成的商品图与宣传视频
--
-- ---------------------------------------------------------------- 为什么存本地文件
--
-- **模型返回的 URL 只有 24 小时有效期**（万相文生视频的文档明写；图片同理）。
-- 只把 URL 存进这张表的话，今天演示完、明天再打开就是一片裂图——而那时候
-- 免费额度可能也用完了，重新生成一遍都做不到。
--
-- 所以链路是：调模型 → 拿到临时 URL → **立刻下载到本地** → 表里存本地
-- 相对路径。``source_url`` 也留着，但它只是溯源用的历史记录，不是展示地址。
--
-- ---------------------------------------------------------------- 为什么必须有 AI 标识
--
-- 《人工智能生成合成内容标识办法》要求生成合成内容可被识别。``ai_generated``
-- 与 ``model`` 两个字段是这条要求在数据层的落点——展示层要不要打角标是
-- 展示层的事，但"这张图是机器生成的、用的哪个模型"这个事实必须留在库里，
-- 事后被问起要拿得出来。
--
-- ---------------------------------------------------------------- 为什么一律待审
--
-- 与 marketing_copy 同一条线：生成的图会挂到商品详情页上，是对外发布的内容。
-- 机器给自己盖章等于没有审核。``review_status`` 默认 pending，且这个字段
-- **不做成生成接口的入参**——能传参数就意味着某天会有人传 approved。
--
-- 执行：mysql -u root -p smartmall < deploy/sql/migrations/011_marketing_asset.sql

CREATE TABLE IF NOT EXISTS `marketing_asset` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `product_id`    BIGINT UNSIGNED NOT NULL,

  -- image | video
  `kind`          VARCHAR(16)   NOT NULL,
  -- 白底图 / 场景图 / 细节图 / 短视频…… 由生成时的用途决定
  `usage_tag`     VARCHAR(32)   NOT NULL DEFAULT '',

  -- 展示用的本地相对路径，形如 generated/9002-scene-1.png。
  -- **这才是页面该引用的地址**，见文件头
  `local_path`    VARCHAR(512)  NOT NULL DEFAULT '',
  -- 模型返回的临时 URL。24 小时后失效，只作溯源
  `source_url`    TEXT,

  -- 送给模型的提示词。**必须留**：被质疑"这图凭什么这么画"时要拿得出依据，
  -- 而且复现问题时没有它就只能猜
  `prompt`        TEXT,
  `negative_prompt` TEXT,

  -- 异步任务（视频）用。图是同步的，这两个字段为空
  `task_id`       VARCHAR(128)  DEFAULT NULL,
  -- pending | running | succeeded | failed
  `task_status`   VARCHAR(16)   NOT NULL DEFAULT 'succeeded',
  `error`         VARCHAR(512)  NOT NULL DEFAULT '',

  `model`         VARCHAR(64)   NOT NULL DEFAULT '',
  -- 《人工智能生成合成内容标识办法》：生成内容必须可识别
  `ai_generated`  TINYINT(1)    NOT NULL DEFAULT 1,
  `review_status` VARCHAR(16)   NOT NULL DEFAULT 'pending',

  `reviewer_id`   BIGINT UNSIGNED DEFAULT NULL,
  `reviewed_at`   DATETIME      DEFAULT NULL,
  `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (`id`),
  KEY `idx_product` (`product_id`, `kind`, `created_at`),
  KEY `idx_review` (`review_status`, `created_at`),
  -- 轮询视频任务时按它捞：只有未完成的才需要再查
  KEY `idx_task` (`task_status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='运营 Agent 生成的图与视频（待审）';
