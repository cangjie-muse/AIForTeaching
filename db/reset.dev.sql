-- 仅用于开发：清空并重建 public schema。
-- 生产/服务器上**绝对不要执行**。
-- 正常初始化请用：python db/migrate.py --apply
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
