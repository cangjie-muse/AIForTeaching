-- 0013：账号与会话（本地账号密码登录），为统一认证留同一张网
--
-- 为什么要有这张表：
--   之前身份只来自请求头（AUTH_MODE=dev），能跑但等于没有登录——浏览器里
--   谁都能把自己写成 admin。要给学生/教师/管理员分权限，先得有一个"人"的实体。
--
-- 为什么现在就把 CAS 的位置留出来：
--   app_account.password_hash 允许为 NULL，表示"这个人只能走统一认证"。
--   接 CAS 时只做两件事：回调里按 uid 找到（或自动创建）account 行，然后
--   签发同一种 app_session。权限判断、审计、页面闸门一行都不用改——
--   它们都只看 Actor.roles，不关心身份从哪来。
--
-- 角色只有三个：student / teacher / admin。
--   审核相关的旧代码里写着 require("reviewer")，由 Actor.has() 把 teacher
--   映射成 reviewer（见 server/app/auth.py），不在这里多造一个人工角色。

BEGIN;

CREATE TABLE app_account (
  id                    text PRIMARY KEY,
  username              text NOT NULL UNIQUE,          -- 登录名，统一小写
  display_name          text NOT NULL,
  role                  text NOT NULL
    CONSTRAINT app_account_role_check CHECK (role IN ('student', 'teacher', 'admin')),
  password_hash         text,                          -- NULL = 只能走统一认证
  password_updated_at   timestamptz,
  must_change_password  boolean NOT NULL DEFAULT false,
  -- 连续失败与锁定：破密码要先过这一关。次数够了锁一段时间，
  -- 成功登录清零。不锁的后果是脚本半夜跑字典，日志里只有几千条 401。
  failed_attempts       integer NOT NULL DEFAULT 0,
  locked_until          timestamptz,
  disabled_at           timestamptz,
  last_login_at         timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX app_account_role_idx ON app_account (role) WHERE disabled_at IS NULL;

CREATE TRIGGER app_account_touch BEFORE UPDATE ON app_account
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE app_session (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- 只存令牌的 sha256：库泄漏时拿不到能直接用的 cookie 值
  token_hash    text NOT NULL UNIQUE,
  account_id    text NOT NULL REFERENCES app_account(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  expires_at    timestamptz NOT NULL,
  revoked_at    timestamptz,
  ip            text,
  user_agent    text
);

CREATE INDEX app_session_account_idx ON app_session (account_id);
CREATE INDEX app_session_expiry_idx  ON app_session (expires_at);

COMMENT ON TABLE  app_account IS
  '登录账号。role 三选一：student / teacher / admin；password_hash 为空表示只能走统一认证。';
COMMENT ON TABLE  app_session IS
  '登录会话。cookie 里是原文令牌，这里只存 sha256；退出即 revoked_at。';
COMMENT ON COLUMN app_account.must_change_password IS
  '首次登录或管理员重置后置 true，前端提示改密（不强制拦截，避免演示时锁死）。';

COMMIT;
