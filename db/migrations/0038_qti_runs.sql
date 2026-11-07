-- 0038 · QTI 导入导出的留痕（阶段 E 任务 1）
--
-- 对应：docs/superpowers/plans/2026-11-05-paper-05-qti-and-acceptance.md 任务 1—3
--      设计 §13.2（QTI 边界）
--
-- 为什么导入导出也要留痕：QTI 是**跨系统**的交换格式，一份卷子进来以后
-- "少了什么、被降级了什么、谁在什么时候导的"必须可查。否则教师会问
-- "我从超星导出的卷子怎么少了两道题"，而平台没有任何记录能回答。
--
-- 三条记录：
--   qti_import_run         一次导入（来源哈希、结果、条目数、谁导的）
--   qti_import_difference  这次导入的每一条差异（error/warning/info）
--   qti_export_run         一次导出（成功产物或拒绝原因，拒绝也算证据）
--
-- 不变量：
--   * 导入结果永远是 draft（不继承外部审核结论），status 只有
--     draft_ready / rejected；
--   * 拒绝也要落一行（含全部 reasons），而不是只返回 422 就丢掉；
--   * 差异表的 severity 只有 error/warning/info。
--
-- 幂等：IF NOT EXISTS 全量。

BEGIN;

CREATE TABLE IF NOT EXISTS assessment.qti_import_run (
  id                  uuid PRIMARY KEY,
  course_ref          text NOT NULL,
  source_sha256       char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  source_name         text NULL,
  status              text NOT NULL CHECK (status IN ('draft_ready', 'rejected')),
  item_count          integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
  error_count         integer NOT NULL DEFAULT 0 CHECK (error_count >= 0),
  warning_count       integer NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
  imported_by         uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  -- 同一份文件重复导入应当命中同一行（内容寻址），便于"再点一次"不产生垃圾记录
  UNIQUE (course_ref, source_sha256, imported_by)
);

CREATE TABLE IF NOT EXISTS assessment.qti_import_difference (
  id          uuid PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES assessment.qti_import_run(id),
  code        text NOT NULL,
  severity    text NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
  message     text NOT NULL,
  item_ref    text NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS qti_import_difference_run_idx
    ON assessment.qti_import_difference (run_id);

CREATE TABLE IF NOT EXISTS assessment.qti_export_run (
  id               uuid PRIMARY KEY,
  paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
  status           text NOT NULL CHECK (status IN ('exported', 'blocked')),
  reasons          jsonb NOT NULL DEFAULT '[]'::jsonb,
  item_count       integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
  payload_sha256   char(64) NULL CHECK (payload_sha256 IS NULL
                                        OR payload_sha256 ~ '^[0-9a-f]{64}$'),
  exported_by      uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  -- blocked 必须有原因；exported 必须有产物哈希
  CHECK ((status = 'blocked') = (jsonb_array_length(reasons) > 0)),
  CHECK ((status = 'exported') OR payload_sha256 IS NULL)
);

-- 同样的 FORCE RLS：语言/审计数据也不能靠"忘了加条件"漏出去
DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY['qti_import_run', 'qti_import_difference', 'qti_export_run']
  LOOP
    EXECUTE format('ALTER TABLE assessment.%I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE assessment.%I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS %I_service_only ON assessment.%I', target, target);
    EXECUTE format(
      'CREATE POLICY %I_service_only ON assessment.%I USING (
         current_setting(''app.assessment_service'', true) = ''on'')', target, target);
  END LOOP;
END
$$;

COMMIT;
