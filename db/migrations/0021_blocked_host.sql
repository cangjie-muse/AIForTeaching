-- 出校域名黑名单：把内置白名单里的域名也搬进前端可管理的范围。
--
-- 为什么不能只靠"删掉 approved_host"：.env 里的基线删不掉，而真实需求常常是
-- "某家供应商出了合规问题，先从可用列表里拿掉"——这件事不该等到下次登录服务器。
--
-- 优先级：黑名单 > 白名单（含 .env 基线）。屏蔽永远安全（少发数据），
-- 所以 internal-only 模式下也允许屏蔽，只是不允许放开。
CREATE TABLE blocked_host (
  host       text PRIMARY KEY,
  note       text NOT NULL DEFAULT '',
  blocked_by text REFERENCES app_account(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE blocked_host IS '管理员屏蔽的出校域名；优先于 .env 与 approved_host，判定见 server/app/egress.py' ;
COMMENT ON COLUMN blocked_host.note IS '为什么屏蔽；出处不明的合规要求也要写下来，否则以后没人敢恢复';
