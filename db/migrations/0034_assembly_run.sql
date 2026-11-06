-- 0034 · 约束组卷的求解运行（阶段 B 任务 1）
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-02-constrained-assembly.md  任务 1
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §7（约束组卷与 A/B）、§10（数据模型）
--
-- 为什么要新表而不复用 candidate_snapshot：
--   候选快照回答"哪些题**可以**进卷"，一次求解还多出三样东西：求解配置
--   （seed/时限/方案数）、最多 3 个方案、以及"为什么不可行"的诊断。把它们塞进
--   candidate_snapshot 的 jsonb 会让"这份快照被哪几次运行用过"反向查询变成扫全表，
--   也会让一张本该不可变的表开始被 UPDATE。
--
-- 与计划书的差异（Alembic → 本仓库的 SQL 迁移）：
--   计划用 Alembic 的 `downgrade()`；本仓库的 db/migrate.py 是**只向前**的
--   SQL 迁移（schema_migration + checksum），没有 downgrade 通道。所以这里不写
--   假装可回滚的 down 脚本，而是把"回滚会毁掉什么"写清楚：删除 assembly_run
--   之前必须先解除 paper_version 的引用，否则已批准的卷子会失去组卷来源证据。
--
-- 幂等：全部 CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS /
-- DROP CONSTRAINT IF EXISTS 后重建，重复执行不报错、不改写既有行。
--
-- 三条数据库层的语义保证（服务层之外的第二道门）：
--   1. `ck_assembly_max_3`：一次运行最多 3 个方案（规格 §7.3）。
--   2. `ck_assembly_failed_has_no_solutions`：INFEASIBLE / FAILED / CANCELLED
--      不得带方案——"没有方案就没有卷子"不能只靠服务里记得判一下。
--   3. `ck_paper_assembly_source`：paper_version 的 assembly_run_id 与
--      selected_solution_index 要么同时为空（手工卷），要么同时有值（组卷）。
--      半有值等于"不知道这份卷是哪次运行的第几个方案"，回放就断了。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 一、求解运行
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.assembly_run (
  id                      uuid PRIMARY KEY,
  blueprint_version_id    uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
  -- 发起人：人类教师。worker 不改这一列，也不获得审核/发布权。
  actor_id                uuid NOT NULL,
  idempotency_key         text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  -- 运行开始那一刻固定的候选清单；求解器只读它，不回表重取
  candidate_manifest      jsonb NOT NULL,
  candidate_manifest_hash char(64) NOT NULL CHECK (candidate_manifest_hash ~ '^[0-9a-f]{64}$'),
  -- 阶段 A 的候选快照行：建卷时 paper_version.candidate_snapshot_id 必填，
  -- 这个引用让"运行 → 候选快照 → 卷子"是一条可查的链，而不是靠时间猜。
  candidate_snapshot_id   uuid NULL REFERENCES assessment.candidate_snapshot(id),
  solver_name             text NOT NULL DEFAULT 'cp-sat',
  solver_version          text NOT NULL,
  solver_config           jsonb NOT NULL,
  status                  text NOT NULL CHECK (status IN (
                            'queued','running','optimal','feasible',
                            'infeasible','timeout','failed','cancelled')),
  solution_manifest       jsonb NOT NULL DEFAULT '[]'::jsonb,
  objective_breakdown     jsonb NOT NULL DEFAULT '{}'::jsonb,
  diagnostics             jsonb NOT NULL DEFAULT '{}'::jsonb,
  best_bound              bigint NULL,
  optimality_gap_milli    integer NULL CHECK (
                            optimality_gap_milli IS NULL OR optimality_gap_milli >= 0),
  job_id                  uuid NULL,
  started_at              timestamptz NULL,
  completed_at            timestamptz NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  -- 同一操作者的同一把幂等键只能有一次运行：重试返回同一行，不产生第二个运行
  UNIQUE (actor_id, idempotency_key),
  CONSTRAINT ck_assembly_max_3 CHECK (jsonb_array_length(solution_manifest) <= 3),
  CONSTRAINT ck_assembly_no_solutions_without_solve CHECK (
    status NOT IN ('infeasible','failed','cancelled')
    OR jsonb_array_length(solution_manifest) = 0
  ),
  -- 终态必须有完成时间，运行中状态不能有：回放读到的行要自洽
  CONSTRAINT ck_assembly_completed_time CHECK (
    (status IN ('queued','running')) = (completed_at IS NULL)
  )
);

COMMENT ON TABLE assessment.assembly_run IS
  '一次约束组卷求解：固定的候选 manifest、求解配置、最多 3 个方案与诊断；终态不可改写';

COMMENT ON COLUMN assessment.assembly_run.candidate_manifest_hash IS
  '运行开始时的候选清单哈希；与 manifest_json 重算不一致即说明数据被改写';

COMMENT ON COLUMN assessment.assembly_run.solution_manifest IS
  '方案数组，最多 3 项；INFEASIBLE/FAILED/CANCELLED 必须为空';

-- 教师工作台：这份蓝图最近跑过什么（新→旧）
CREATE INDEX IF NOT EXISTS assembly_run_blueprint_idx
    ON assessment.assembly_run (blueprint_version_id, created_at DESC);

-- worker 领取：按状态找未完成运行
CREATE INDEX IF NOT EXISTS assembly_run_status_idx
    ON assessment.assembly_run (status, created_at)
    WHERE status IN ('queued','running');

CREATE INDEX IF NOT EXISTS assembly_run_job_idx
    ON assessment.assembly_run (job_id)
    WHERE job_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 二、paper_version：允许记录"这份卷来自哪次运行的第几个方案"
--
-- 0027 的 ck_paper_manual_only 是阶段 A 的"本阶段不组卷"闸门。阶段 B 要打开它，
-- 但打开的方式必须是"两者同时有值"，否则一份卷子可以声称自己来自某次运行却
-- 说不出是第几个方案。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_version
  ADD COLUMN IF NOT EXISTS assembly_run_id uuid NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_paper_manual_only'
      AND conrelid = 'assessment.paper_version'::regclass
  ) THEN
    ALTER TABLE assessment.paper_version DROP CONSTRAINT ck_paper_manual_only;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_paper_assembly_run'
      AND conrelid = 'assessment.paper_version'::regclass
  ) THEN
    ALTER TABLE assessment.paper_version
      ADD CONSTRAINT fk_paper_assembly_run
      FOREIGN KEY (assembly_run_id) REFERENCES assessment.assembly_run(id);
  END IF;
END
$$;

ALTER TABLE assessment.paper_version
  DROP CONSTRAINT IF EXISTS ck_paper_assembly_source;
ALTER TABLE assessment.paper_version
  ADD CONSTRAINT ck_paper_assembly_source CHECK (
    (assembly_run_id IS NULL AND selected_solution_index IS NULL)
    OR (assembly_run_id IS NOT NULL AND selected_solution_index IS NOT NULL)
  );

ALTER TABLE assessment.paper_version
  DROP CONSTRAINT IF EXISTS ck_paper_solution_index_range;
ALTER TABLE assessment.paper_version
  ADD CONSTRAINT ck_paper_solution_index_range CHECK (
    selected_solution_index IS NULL OR selected_solution_index BETWEEN 0 AND 2
  );

CREATE INDEX IF NOT EXISTS paper_version_assembly_run_idx
    ON assessment.paper_version (assembly_run_id)
    WHERE assembly_run_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 三、RLS：与 paper_blueprint_version 同口径（课程范围），服务账号全量
--
-- 求解运行里含着候选清单（哪些题被排除、为什么），这本身就是"这门课考什么"
-- 的信息，不能给非本课程的人读。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.assembly_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.assembly_run FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS assembly_run_course_scope ON assessment.assembly_run;
CREATE POLICY assembly_run_course_scope ON assessment.assembly_run
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_blueprint_version b
      WHERE b.id = blueprint_version_id
        AND b.course_id::text = ANY (
          string_to_array(current_setting('app.course_ids', true), ',')
        )
    )
  );

COMMIT;
