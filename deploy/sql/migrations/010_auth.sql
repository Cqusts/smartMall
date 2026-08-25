-- 010 · 认证：用户表与角色
--
-- 在这之前，项目**没有任何认证体系**。`CreateOrderRequest` 的注释里写着这句：
--
--     「来自会话或 JWT——现在这样，任何人都能以任意身份下单。」
--
-- 而且不止下单。`/api/product/admin/orders/**` 那几个接口——发货、确认送达、
-- 退款审批、退款驳回——**一个校验都没有**，知道订单号就能把别人的单发掉、
-- 把退款批掉。这不是"还没做鉴权"，是一个正在生效的洞。
--
-- ---------------------------------------------------------------- 为什么要建表
--
-- 可以不建表：把用户名密码写死在配置里，演示也能跑。不这么做是因为订单表
-- 的 `user_id` 已经在用真实的整数 ID 了（越权校验就靠它），配置里写死的话
-- 那个 ID 从哪来说不清楚，最后一定会变成"前端传什么就是什么"——
-- 那正是现在这个洞。
--
-- ---------------------------------------------------------------- 角色
--
-- 只有两个：`customer` 和 `merchant`。不做 RBAC 权限表。
--
-- 理由是这个项目里权限的粒度就是二元的：要么是买东西的，要么是店里的。
-- 提前上一套「角色-权限-资源」三张表，在只有两种角色时是纯粹的复杂度，
-- 而真需要细分时（比如客服只能看订单不能审批退款）再加也不迟——
-- 那时候才知道该怎么切。
--
-- ---------------------------------------------------------------- 密码
--
-- 存 BCrypt 哈希，不存明文也不存 MD5。BCrypt 自带盐、可调工作因子，
-- 是 Spring Security 的默认实现，不需要自己拼盐。
--
-- 演示账号的密码都是 `smartmall123`，哈希用工作因子 10 实际生成并 checkpw
-- 验证过（`AuthServiceTest` 里也钉了一条：种子哈希必须能验开这个口令）。
-- **这是公开的演示口令，别在任何真实环境里用这份种子数据。**
--
-- 执行：mysql -u root -p smartmall < deploy/sql/migrations/010_auth.sql

CREATE TABLE IF NOT EXISTS `mall_user` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `username`      VARCHAR(64)   NOT NULL,
  `password_hash` VARCHAR(100)  NOT NULL  COMMENT 'BCrypt，含盐与工作因子',
  `nickname`      VARCHAR(64)   NOT NULL DEFAULT '',
  -- customer 买东西的 / merchant 店里的。**不做 RBAC 三张表**，理由见文件头
  `role`          VARCHAR(16)   NOT NULL DEFAULT 'customer',
  `status`        VARCHAR(16)   NOT NULL DEFAULT 'active'
                                COMMENT 'active / disabled',
  `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`),
  KEY `idx_role` (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户与角色';


-- 种子账号。
--
-- **id 是写死的**：10086 早就散落在种子订单、CLI 默认参数、前端默认值里，
-- 让它自增会对不上——那些订单会变成"不属于任何人"，而越权校验恰好是靠
-- user_id 做的，对不上就等于校验形同虚设。
--
-- 密码统一 smartmall123（BCrypt 工作因子 10）。
INSERT INTO `mall_user` (id, username, password_hash, nickname, role) VALUES
  (10086, 'demo',
   '$2a$10$0T1bQy1/VNXmPAQT/9h7Tuwr8KSFPUtISz5esok09np6/gjbvZNR.',
   '演示买家', 'customer'),
  (10087, 'buyer2',
   '$2a$10$0T1bQy1/VNXmPAQT/9h7Tuwr8KSFPUtISz5esok09np6/gjbvZNR.',
   '另一位买家', 'customer'),
  (1, 'merchant',
   '$2a$10$0T1bQy1/VNXmPAQT/9h7Tuwr8KSFPUtISz5esok09np6/gjbvZNR.',
   '店铺管理员', 'merchant')
ON DUPLICATE KEY UPDATE
  password_hash = VALUES(password_hash),
  role          = VALUES(role);
