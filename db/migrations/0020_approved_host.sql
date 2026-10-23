-- 出校域名：管理员可以在前端加，不必每次登服务器改 .env 再重启。
--
-- 独立成表而不是塞进 app_setting 的 jsonb：
--   · 每条要单独留痕（谁在什么时候放行了哪个域名）；
--   · 域名天然是主键，重复添加应该被数据库挡住，而不是靠应用层去重。
--
-- 注意：这张表只是"追加"。.env 的 MODEL_ALLOWED_HOSTS 仍然是基线，
-- MODEL_EGRESS=internal-only 仍然一票否决（此时谁都不能外发）。
CREATE TABLE approved_host (
  host       text PRIMARY KEY,
  note       text NOT NULL DEFAULT '',
  added_by   text REFERENCES app_account(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE approved_host IS '管理员批准的出校域名；与 .env 的 MODEL_ALLOWED_HOSTS 取并集，判定见 server/app/egress.py';
COMMENT ON COLUMN approved_host.note IS '为什么允许它接收课程内容，只有一句话也要写，事后要能解释';
