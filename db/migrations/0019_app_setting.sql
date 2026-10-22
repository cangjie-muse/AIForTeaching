-- 平台级开关：让管理员在前端改行为，而不是每次登服务器改 .env。
--
-- 只放"改了立刻生效、且需要留痕"的开关。密钥、白名单这类仍然只走 .env：
-- 那些改错了会让平台对外发数据，不适合给一个网页按钮。
CREATE TABLE app_setting (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text REFERENCES app_account(id)
);

COMMENT ON TABLE app_setting IS '平台开关与运行期配置；读取方见 server/app/settings.py';
