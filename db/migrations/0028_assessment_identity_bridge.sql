-- 阶段 A 任务 2 的两张辅助表：幂等记录 + 文本 id 桥接。
--
-- 为什么必须新开 0028 而不是塞进 0027：0027 已提交，是本阶段数据基线的历史；
-- 这两张表是任务 2 的实现细节（幂等键、身份映射），不属于规格 §10 的领域表。
-- 0027 只做了 11 张领域表 + 3 张 Artifact 表，且没有任何幂等存储。
--
-- 一、assessment.idempotency_record
--   写接口要求 Idempotency-Key（计划书 §0.3 规则 3）。只在内存里记是不够的：
--   进程重启、多 worker、HTTP 超时后的重试都会绕过内存。记录必须与版本插入在
--   同一个事务里落库，唯一键 (scope, idempotency_key, actor_id) 保证同一用户
--   的同一个键只被消费一次；键按用户隔离，避免 A 的重试读到 B 的蓝图。
--   response 存当时的响应快照：重放时原样返回，不重新计算（否则"重放"会变成
--   "按现在的库状态再生成一份"，两者在并发下可能不同）。
--
-- 二、assessment.id_ref
--   0027 的 assessment 表把 course_id / created_by 定义为 uuid，而本仓库既有的
--   身份真源是文本：course.id 形如 `analysis`，app_account.id 形如 `acc-1a2b3c4d5e6f`，
--   teaching_class.id / teacher_id 同样是 text。两边不可能直接相等。
--   应用层用固定命名空间的 uuid5 做确定性映射（见 server/app/blueprints.py 的
--   ref_uuid），这张表保存 subject(uuid) ↔ (kind, ref) 的反向映射，让读取接口
--   能把文本 id 还原出来给前端。唯一键 (kind, ref) 保证一个文本 id 永远对应
--   同一个 uuid；subject 是主键，保证一个 uuid 不会被记成两个不同的文本 id。
--
-- 三、RLS
--   0027 只对 paper_version / paper_section / paper_slot / paper_render 开了
--   FORCE ROW LEVEL SECURITY；蓝图版本表与这两张新表都没有策略，因此本迁移不
--   添加策略，也不改变 0027 的行级安全姿态。写路径仍然统一走
--   db.transaction(assessment_service=True)：任务 3/4 写 paper_* 时同一段代码
--   已经满足 SET LOCAL 要求，不会出现"事务里忘了设作用域"的返工。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 幂等记录
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.idempotency_record (
  id              uuid PRIMARY KEY,
  -- 作用域写成 `资源.动作`，同一把键可以在不同接口复用而互不干扰
  scope           text NOT NULL CHECK (length(scope) BETWEEN 1 AND 80),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  -- 幂等键按用户隔离：不同用户的同名字符串不是同一次重试
  actor_id        uuid NOT NULL,
  -- 载荷指纹（canonical JSON 的 sha256）：同键不同载荷必须 409，而不是返回旧结果
  request_hash    char(64) NOT NULL,
  -- 当时的响应快照；重放原样返回，不按当前库状态重算
  response        jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope, idempotency_key, actor_id)
);

COMMENT ON TABLE assessment.idempotency_record IS
  '写接口幂等键：与业务写入同事务落库，同键同载荷重放、同键不同载荷 409';

-- "清理多久以前的幂等记录"是唯一的批量查询方向
CREATE INDEX IF NOT EXISTS idempotency_record_created_idx
    ON assessment.idempotency_record (created_at);

-- ---------------------------------------------------------------------------
-- 文本 id ↔ 确定性 uuid 的桥接
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.id_ref (
  subject    uuid PRIMARY KEY,
  -- 与 server/app/blueprints.py 的 REF_KINDS 一一对应
  kind       text NOT NULL CHECK (kind IN ('course', 'offering', 'account', 'release_context')),
  ref        text NOT NULL CHECK (length(ref) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now(),
  -- 一个文本 id 只能映射到一个 uuid（uuid5 本身就是确定的，这里再加一道库级保证）
  UNIQUE (kind, ref)
);

COMMENT ON TABLE assessment.id_ref IS
  'assessment 表的 uuid 身份 ↔ 本仓库文本 id（course/app_account/teaching_class）的确定性映射';

-- 反向查询（uuid → 文本）走主键；这里补的是"某类身份有哪些"的巡检方向
CREATE INDEX IF NOT EXISTS id_ref_kind_idx ON assessment.id_ref (kind);

COMMIT;
