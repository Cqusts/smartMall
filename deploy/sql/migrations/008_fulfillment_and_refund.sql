-- 008 · 履约与退款
--
-- 007 之后订单只走到 paid 就停了。这一版把后半程补上：
--
--   pending_payment ──pay──> paid ──ship──> shipped ──deliver──> delivered ──confirm──> completed
--          │                  │              │                      │             │
--          │                  └──────────────┴──────────────────────┴─────────────┘
--       cancel                                  applyRefund
--          │                                         │
--          ▼                                         ▼
--      cancelled                                 refunding ──approve──> refunded（回补库存）
--                                                    │
--                                                    └──reject──> 回到申请前的状态
--
-- ---------------------------------------------------------------- 退款为什么要留三列
--
-- **status_before_refund**：驳回之后订单得回到原处。已发货的单申请退款被驳回，
-- 它应该还是 shipped，不能变回 paid——那会让"这单发没发货"这个事实凭空改变，
-- 而客服正是照着这个字段回答"我的货到哪了"。不记下来就没法还原。
--
-- **refund_amount**：不直接用 amount，因为部分退款是真实存在的场景（少发一件、
-- 运费补偿）。当前实现只做全额，但把列留出来，将来支持部分退款不用改表。
--
-- **refunded_at**：非空即表示钱已退、库存已回补。与 cancelled_at 一起，
-- 构成"这笔订单的库存到底还回去没有"的可查证据——出问题时靠它对账，
-- 而不是靠翻日志。
--
-- ---------------------------------------------------------------- 一个明确的简化
--
-- 一笔订单同时只能有一个退款申请，驳回后可以重新申请，但会覆盖上一次的原因。
-- 真实系统会单独建 refund 表记录每一次申请的完整历史（谁审的、什么时候、
-- 为什么驳回）。这里放在订单表上，是因为演示只需要跑通一轮；要支持
-- 申诉与多次协商时，该拆表。
--
-- 执行：
--   mysql -u root -p --default-character-set=utf8mb4 smartmall < 008_fulfillment_and_refund.sql

SET NAMES utf8mb4;

ALTER TABLE `mall_order`
  ADD COLUMN `delivered_at`        DATETIME     DEFAULT NULL
      COMMENT '物流送达时间' AFTER `shipped_at`,
  ADD COLUMN `completed_at`        DATETIME     DEFAULT NULL
      COMMENT '确认收货时间' AFTER `delivered_at`,
  ADD COLUMN `refund_applied_at`   DATETIME     DEFAULT NULL
      COMMENT '退款申请时间' AFTER `cancelled_at`,
  ADD COLUMN `refunded_at`         DATETIME     DEFAULT NULL
      COMMENT '退款完成时间。非空即表示钱已退、库存已回补' AFTER `refund_applied_at`,
  ADD COLUMN `refund_reason`       VARCHAR(255) NOT NULL DEFAULT ''
      COMMENT '用户填的退款原因' AFTER `refunded_at`,
  ADD COLUMN `refund_reject_reason` VARCHAR(255) NOT NULL DEFAULT ''
      COMMENT '商家驳回理由' AFTER `refund_reason`,
  ADD COLUMN `refund_amount`       DECIMAL(10,2) DEFAULT NULL
      COMMENT '退款金额。留列以支持将来的部分退款，当前实现只做全额' AFTER `refund_reject_reason`,
  ADD COLUMN `status_before_refund` VARCHAR(20)  NOT NULL DEFAULT ''
      COMMENT '申请退款前的状态。驳回时据此还原，否则"发没发货"会凭空改变'
      AFTER `refund_amount`;

-- 状态枚举补上 refunded。004 建表时的注释里没有它，因为那时没有退款链路
ALTER TABLE `mall_order`
  MODIFY COLUMN `status` VARCHAR(20) NOT NULL DEFAULT 'pending_payment'
  COMMENT 'pending_payment/paid/shipped/delivered/completed/cancelled/refunding/refunded';

-- 履约看板要按状态捞单（待发货、退款待审），全表扫描迟早成问题
ALTER TABLE `mall_order`
  ADD INDEX `idx_status_created` (`status`, `created_at`);
