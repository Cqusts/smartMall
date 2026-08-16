-- 007 · 下单链路：幂等键与生命周期时间戳
--
-- 004 建 `mall_order` 时它只是**只读工具层的数据源**——种子数据，供客服回答
-- "我的订单到哪了"。现在这张表要开始接真实写入，缺三样东西。
--
-- ---------------------------------------------------------------- request_id
--
-- **幂等键。没有它，用户手抖点两下「立即购买」就是两笔订单、两次扣库存。**
--
-- 为什么是唯一索引而不是先查后插：先 SELECT 再 INSERT 之间有窗口，两个并发
-- 请求可以同时查到"没有"，然后各插一条。唯一索引把判重下沉到数据库，
-- 让第二个请求撞 DuplicateKey——那不是错误，是幂等命中，服务层捕获后
-- 回查并返回第一笔的结果，调用方看到的是同一个订单号。
--
-- 允许 NULL：004 的种子订单没有 request_id，而 MySQL 的唯一索引允许多个 NULL
-- （NULL 不等于 NULL），所以历史数据不用回填也不会互相冲突。
--
-- ---------------------------------------------------------------- 时间戳
--
-- `cancelled_at` 不只是审计字段。下单即扣库存（预占），那么"取消"就必须回补，
-- 而回补是笔要防重放的写操作——同一张单被取消两次就会凭空多出一件库存。
-- 状态机 + 这个时间戳一起构成"只回补一次"的凭据。
--
-- 执行：
--   mysql -u root -p --default-character-set=utf8mb4 smartmall < 007_order_placement.sql
--
-- ⚠️ 字符集参数不能省。缺了它中文注释会以 latin1 解释，写进去就是 Tæ¤ 这种乱码。

SET NAMES utf8mb4;

ALTER TABLE `mall_order`
  ADD COLUMN `request_id` VARCHAR(64) DEFAULT NULL
      COMMENT '幂等键。同一个 request_id 重复提交只会产生一笔订单' AFTER `order_no`,
  ADD COLUMN `updated_at` DATETIME NOT NULL
      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER `created_at`,
  ADD COLUMN `cancelled_at` DATETIME DEFAULT NULL
      COMMENT '取消时间。非空即表示库存已回补，防止重复回补' AFTER `shipped_at`,
  ADD UNIQUE KEY `uk_request_id` (`request_id`);

-- 新单一律从 pending_payment 起步。004 建表时默认值是 'paid'，因为那时表里
-- 只有种子数据、没有下单链路；现在有了，默认值改回状态机的真正起点。
ALTER TABLE `mall_order`
  ALTER COLUMN `status` SET DEFAULT 'pending_payment';
