-- ============================================================
-- 初始化数据
-- ============================================================
SET NAMES utf8mb4;

-- ---- 数据源登记 ----
-- 授权范围必须如实填写：合规审计以此为依据，用途不得超出（见 docs/15-risks-compliance.md）
INSERT IGNORE INTO ds_source (source_code, name, source_type, license, license_scope, note) VALUES
('jddc',      'JDDC 京东客服对话语料',  'public_dataset',  '学术授权',
 '仅限学术研究，商用前须重新确认许可', '100 万+ 多轮对话，冷启动主力'),
('jddc2',     'JDDC 2.0 多模态语料',    'public_dataset',  '学术授权',
 '仅限学术研究，商用前须重新确认许可', '24.6 万会话 / 50.7 万图片，天然多模态'),
('ecd',       'ECD 电商对话数据集',      'public_dataset',  '学术授权',
 '仅限学术研究',                       '冷启动补充'),
('synthetic', 'LLM 合成数据',            'synthetic',       '模型服务条款',
 '需符合所用模型 API 的服务条款',      '覆盖公开数据集没有的自有商品'),
('trace',     '自有系统真实对话回流',    'internal_trace',  '用户授权',
 '隐私政策已明示用于服务改进与模型训练；PII 脱敏后使用', '长期主力数据来源'),
('handover',  '转人工工单回流',          'internal_trace',  '用户授权',
 '同 trace',                            '每次转人工都暴露一个知识盲点');

-- ---- 考核指标配置（售前模板）----
-- 权重与 rubric 见 docs/09-sales-kpi.md。上线前必须先完成校准。
INSERT IGNORE INTO kpi_metric (metric_code, metric_name, metric_type, template, weight, is_veto) VALUES
-- 确定性指标（规则计算），合计 25%
('first_reply_time',  '首次响应时长',  'rule',    'presale', 0.0800, 0),
('avg_reply_time',    '平均响应时长',  'rule',    'presale', 0.0700, 0),
('timeout_count',     '超时未回次数',  'rule',    'presale', 0.0500, 0),
('proactive_follow',  '主动跟进率',    'rule',    'presale', 0.0500, 0),
('forbidden_words',   '违禁词命中',    'rule',    'presale', 0.0000, 1),
-- 判断性指标（LLM-as-a-Judge），合计 55%
('resolve_rate',      '问题解决度',    'judge',   'presale', 0.1500, 0),
('need_mining',       '需求挖掘',      'judge',   'presale', 0.1000, 0),
('recommend_fit',     '推荐合理性',    'judge',   'presale', 0.1000, 0),
('emotion_care',      '情绪安抚',      'judge',   'presale', 0.0800, 0),
('professionalism',   '专业度',        'judge',   'presale', 0.0700, 0),
('tone_affinity',     '话术亲和力',    'judge',   'presale', 0.0500, 0),
-- 结果性指标（业务数据），合计 20%
('conversion_rate',   '咨询-下单转化', 'outcome', 'presale', 0.1200, 0),
('order_value',       '客单价贡献',    'outcome', 'presale', 0.0500, 0),
('handover_rate',     '转人工率',      'outcome', 'presale', 0.0300, 0);

-- ---- 示例类目（仅供 M0 联调，M1 后由真实数据替换）----
INSERT IGNORE INTO category (id, parent_id, name, level, path) VALUES
(1,    0, '服装',   1, '/1'),
(24,   1, '上装',   2, '/1/24'),
(1024, 24, '针织衫', 3, '/1/24/1024'),
(2,    0, '箱包',   1, '/2'),
(48,   2, '女包',   2, '/2/48');
