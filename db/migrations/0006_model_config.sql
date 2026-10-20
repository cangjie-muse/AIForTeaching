-- 0006：模型接口配置界面所需字段
--
-- 密钥的存放方式（重要）：
--   不把密钥放进数据库。原因有两个，都不是洁癖：
--     1. 数据库备份、只读副本、讲师临时取的 dump，都会连带把密钥带走；
--     2. 加密入库要依赖 pgcrypto 之类的扩展，还得自己管密钥轮换；
--        这些复杂度换不来多少实际收益，不如让密钥根本不进库。
--   所以密钥存服务器文件（0600），库里只存相对路径。接口永不回显密钥，
--   只返回"是否已配置"。
--
-- 这样做的代价：密钥与数据库不在同一个备份里。迁移服务器时要单独搬 secrets/ 目录
-- ——这条写进部署指南。

BEGIN;

ALTER TABLE model_endpoint
  ADD COLUMN api_key_path       text,          -- 相对 SECRETS_DIR，如 deepseek.key
  ADD COLUMN api_key_updated_at timestamptz,
  ADD COLUMN updated_by         text,          -- 最后一次修改人（谁配的）
  ADD COLUMN last_test_at       timestamptz,   -- 最近一次连通性自检
  ADD COLUMN last_test_ok       boolean,
  ADD COLUMN last_test_note     text;

COMMENT ON COLUMN model_endpoint.api_key_path IS
  '密钥文件名（相对 SECRETS_DIR）。库里不存密钥本身，只存"密钥在哪"。';
COMMENT ON COLUMN model_endpoint.last_test_note IS
  '自检失败原因摘要，截断到 300 字。成功时为空。';

-- 给配置界面用的读视图。刻意不包含 api_key_path：
-- 前端不需要知道路径，知道路径只会让攻击者少查一步。
CREATE VIEW v_model_endpoint_public AS
  SELECT name,
         kind,
         base_url,
         model,
         data_scope,
         dim,
         max_context,
         enabled,
         notes,
         approved_by,
         approved_at,
         updated_by,
         updated_at,
         (api_key_path IS NOT NULL) AS key_configured,
         api_key_updated_at,
         last_test_at,
         last_test_ok,
         last_test_note
  FROM model_endpoint;

COMMENT ON VIEW v_model_endpoint_public IS
  '配置界面的读取来源。不含密钥路径，只给 key_configured 布尔值。';

-- 启用状态只能有一个 chat / 一个 embedding（0004 已建索引，这里补个可读的约束说明）
COMMENT ON INDEX model_endpoint_one_enabled_chat IS
  '同一时刻只能启用一个对话端点，避免"到底在用哪个模型"说不清。';

COMMIT;
