-- 0005：模型端点语义调整（数据可出校，改用国内 API）
--
-- 背景变化（2026-10-20 学院确认）：
--   0004 写的时候假设"数据不出校"，所以把外部端点当成例外，要求必须写说明。
--   现在确认可以出校、使用国内大模型 API，所以外部端点变成常态。
--
-- 但约束不放松，只换方向：
--   原来防"数据被发出去" → 现在防"发给了没批准的供应商"。
--   具体落在两处：代码里的 allowlist（server/app/llm.py），和这张表里的审批人。
--
-- 表结构改动很小，主要是把"谁批准了这个供应商"记下来。
-- 半年后审计会问：为什么教材内容会出现在这家公司的服务器上？

BEGIN;

ALTER TABLE model_endpoint
  ADD COLUMN approved_by text,                 -- 谁批准的（校内责任人）
  ADD COLUMN approved_at timestamptz;          -- 何时批准

COMMENT ON TABLE model_endpoint IS
  '模型端点登记表。记录用了哪个供应商、哪个模型、内容是否会离开校园网，以及谁批准的。';

COMMENT ON COLUMN model_endpoint.data_scope IS
  'internal=校内服务，内容不出内网；external=外部供应商，请求会离开校园网，'
  '需先确认合规并在 approved_by 留痕。';

COMMENT ON COLUMN model_endpoint.approved_by IS
  '批准使用该外部供应商的责任人。外部端点必须填写，否则半年后无人能回答"为什么允许"。';

-- 外部端点必须同时具备说明与批准人：这是审计唯一能追的两条线索
CREATE OR REPLACE VIEW v_external_endpoint_audit AS
  SELECT name, model, base_url, notes, approved_by, approved_at, enabled
  FROM model_endpoint
  WHERE data_scope = 'external';

COMMIT;
