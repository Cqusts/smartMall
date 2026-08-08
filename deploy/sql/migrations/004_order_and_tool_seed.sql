-- 004 · 订单表与工具层种子数据
--
-- MCP 工具层要回答的是**实时**问题：还有货吗、多少钱、我的快递到哪了。
-- 这类答案每分钟都在变，知识库里的历史对话说"有货"毫无意义——
-- 所以它们必须查结构化数据，不能走 RAG。
--
-- 尺码表同理：它是一张二维表，向量检索处理这类查询很差。
-- 「160cm 90斤穿什么码」应该查表，不是找一段相似的对话。
--
-- 执行：mysql -u root -p smartmall < 004_order_and_tool_seed.sql

SET NAMES utf8mb4;

-- ---------------------------------------------------------------- 订单

CREATE TABLE IF NOT EXISTS `mall_order` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `order_no`        VARCHAR(32)   NOT NULL,
  `user_id`         BIGINT UNSIGNED NOT NULL         COMMENT '越权校验靠它：查订单必须同时匹配订单号与当前会话用户',
  `product_id`      BIGINT UNSIGNED NOT NULL,
  `sku_no`          VARCHAR(64)   DEFAULT NULL,
  `spec`            VARCHAR(128)  NOT NULL DEFAULT '',
  `quantity`        INT           NOT NULL DEFAULT 1,
  `amount`          DECIMAL(10,2) NOT NULL,
  `status`          VARCHAR(20)   NOT NULL DEFAULT 'paid'
                    COMMENT 'pending_payment/paid/shipped/delivered/completed/cancelled/refunding',
  `express_company` VARCHAR(32)   NOT NULL DEFAULT '',
  `express_no`      VARCHAR(64)   NOT NULL DEFAULT '',
  `tracks`          JSON          DEFAULT NULL       COMMENT '物流节点，[{ts, desc}]',
  `created_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `shipped_at`      DATETIME      DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  KEY `idx_user` (`user_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单（工具层只读）';


-- ---------------------------------------------------------------- SKU

-- 价格与库存是 MCP 工具的主要数据源。故意留了缺货与低库存的 SKU：
-- 客服说"有货"结果用户下不了单，比说"这个码没有了"糟糕得多
INSERT IGNORE INTO `sku` (sku_no, product_id, spec, price, origin_price, stock, status) VALUES
('S9001-BEIGE-S',  9001, '{"颜色":"米白","尺码":"S"}',  299.00, 399.00, 12, 'on_sale'),
('S9001-BEIGE-M',  9001, '{"颜色":"米白","尺码":"M"}',  299.00, 399.00, 38, 'on_sale'),
('S9001-BEIGE-L',  9001, '{"颜色":"米白","尺码":"L"}',  299.00, 399.00,  0, 'on_sale'),
('S9001-NAVY-M',   9001, '{"颜色":"藏青","尺码":"M"}',  299.00, 399.00,  5, 'on_sale'),
('S9001-NAVY-L',   9001, '{"颜色":"藏青","尺码":"L"}',  299.00, 399.00, 21, 'on_sale'),
('S9002-WHITE-M',  9002, '{"颜色":"白","尺码":"M"}',    899.00, 1299.00, 7, 'on_sale'),
('S9002-WHITE-L',  9002, '{"颜色":"白","尺码":"L"}',    899.00, 1299.00, 15, 'on_sale'),
('S9002-BLACK-L',  9002, '{"颜色":"黑","尺码":"L"}',    899.00, 1299.00, 0, 'sold_out'),
('S9003-FLORAL-S', 9003, '{"颜色":"碎花","尺码":"S"}',  459.00, 599.00, 23, 'on_sale'),
('S9003-FLORAL-M', 9003, '{"颜色":"碎花","尺码":"M"}',  459.00, 599.00, 31, 'on_sale'),
('S9004-BROWN',    9004, '{"颜色":"棕"}',               1280.00, 1580.00, 9, 'on_sale'),
('S9004-BLACK',    9004, '{"颜色":"黑"}',               1280.00, 1580.00, 2, 'on_sale');


-- ---------------------------------------------------------------- 尺码表

-- 二维表结构。向量检索对这类查询很差——「160cm 90斤穿什么码」
-- 需要的是查表加换算，不是找一段相似的对话
INSERT IGNORE INTO `size_chart` (product_id, chart, note) VALUES
(9001, '{"表头":["尺码","胸围(cm)","衣长(cm)","肩宽(cm)","建议身高(cm)","建议体重(kg)"],
         "行":[["S",96,60,38,"155-162","45-52"],
               ["M",100,62,40,"160-168","50-60"],
               ["L",104,64,42,"166-175","58-70"]]}',
 '宽松版型，介于两码之间建议选小码；喜欢更宽松可选大一码'),
(9002, '{"表头":["尺码","胸围(cm)","衣长(cm)","建议身高(cm)","建议体重(kg)"],
         "行":[["M",104,70,"160-170","50-62"],
               ["L",110,73,"168-178","60-75"]]}',
 '含绒量 90%，版型正常，内搭厚毛衣建议大一码'),
(9003, '{"表头":["尺码","胸围(cm)","腰围(cm)","裙长(cm)","建议身高(cm)"],
         "行":[["S",86,68,110,"155-163"],
               ["M",90,72,112,"162-170"]]}',
 '收腰设计，腰围偏小，腰粗建议选大一码');


-- ---------------------------------------------------------------- 订单

-- user_id 10086 是 CLI 默认用户；10087 的那单专门用来验证越权拦截：
-- 用户说出别人的订单号不该能查到
INSERT IGNORE INTO `mall_order`
  (order_no, user_id, product_id, sku_no, spec, quantity, amount, status,
   express_company, express_no, tracks, created_at, shipped_at) VALUES
('2026080100001', 10086, 9001, 'S9001-BEIGE-M', '米白 M', 1, 299.00, 'shipped',
 '中通', '78123456789',
 '[{"ts":"2026-08-01 14:20","desc":"商品已出库"},
   {"ts":"2026-08-02 09:15","desc":"到达杭州转运中心"},
   {"ts":"2026-08-03 08:40","desc":"派送中，快递员 138****6677"}]',
 '2026-08-01 10:05:00', '2026-08-01 14:20:00'),

('2026080200002', 10086, 9004, 'S9004-BROWN', '棕色', 1, 1280.00, 'paid',
 '', '', NULL, '2026-08-02 16:30:00', NULL),

('2026080300003', 10086, 9003, 'S9003-FLORAL-M', '碎花 M', 1, 459.00, 'completed',
 '顺丰', 'SF1234567890',
 '[{"ts":"2026-07-28 11:00","desc":"商品已出库"},
   {"ts":"2026-07-29 15:30","desc":"已签收，签收人：本人"}]',
 '2026-07-27 20:11:00', '2026-07-28 11:00:00'),

('2026080400004', 10087, 9002, 'S9002-WHITE-L', '白色 L', 1, 899.00, 'shipped',
 '京东', 'JD9988776655', NULL, '2026-08-04 09:00:00', '2026-08-04 18:00:00');
