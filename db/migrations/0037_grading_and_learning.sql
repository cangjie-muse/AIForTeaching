-- 0037 · 评分与学习投影的持久化边界（阶段 D 任务 1）
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-04-practice-and-grading.md  任务 1
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §12（练习、评分与反馈）
--
-- 这一迁移只做"边界"，不做业务：
--
--   1. 成绩只能**追加**：`grade_revision` 一旦写入，分数/理由/依据不可原位改；
--      允许变的只有"发布状态、发布人、发布时间"——改分必须新增一版并指回上一版。
--   2. 逐项判定（`item_grade_decision`）append-only：教师每一个决定都是证据，
--      改主意要追加 supersedes。
--   3. 练习投影（`learning.exercise_attempt`）与正式成绩**物理隔离**：它只回答
--      "这道题练过、结果如何"，不产生 submission、不产生 grade。
--
-- 兼容既有学习表：如果 `learning.exercise_attempt` 已存在（早期计划建的），
-- 本迁移只加两列（来源 revision、来源 attempt）与索引，不动任何既有列；
-- 表是我们新建时才打标记注释，降级时据此判断该不该删表。
--
-- 幂等：CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP ... IF EXISTS。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;
CREATE SCHEMA IF NOT EXISTS learning;

-- ---------------------------------------------------------------------------
-- 一、练习投影 learning.exercise_attempt
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS learning.exercise_attempt (
  id                            uuid PRIMARY KEY,
  exercise_version_id           uuid NOT NULL,
  exercise_instance_id          uuid NULL,
  input_status                  text NOT NULL,
  outcome                       text NULL,
  engine_version                text NULL,
  feedback_policy_version_id    uuid NULL,
  created_at                    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE learning.exercise_attempt IS
  'created_by:0037_assessment_grading';

-- 来源两列允许 NULL：兼容本迁移之前写入的历史学习记录。
ALTER TABLE learning.exercise_attempt
  ADD COLUMN IF NOT EXISTS source_response_revision_id uuid NULL;
ALTER TABLE learning.exercise_attempt
  ADD COLUMN IF NOT EXISTS paper_attempt_id uuid NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_learning_attempt_response'
      AND conrelid = 'learning.exercise_attempt'::regclass
  ) THEN
    ALTER TABLE learning.exercise_attempt
      ADD CONSTRAINT fk_learning_attempt_response
      FOREIGN KEY (source_response_revision_id)
      REFERENCES assessment.response_revision(id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_learning_attempt_paper_attempt'
      AND conrelid = 'learning.exercise_attempt'::regclass
  ) THEN
    ALTER TABLE learning.exercise_attempt
      ADD CONSTRAINT fk_learning_attempt_paper_attempt
      FOREIGN KEY (paper_attempt_id)
      REFERENCES assessment.paper_attempt(id);
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_learning_attempt_response
    ON learning.exercise_attempt (source_response_revision_id);
CREATE INDEX IF NOT EXISTS ix_learning_attempt_paper_attempt
    ON learning.exercise_attempt (paper_attempt_id);

-- 阶段 D 的练习投影必须带来源；历史行（来源为空）不受这条部分索引约束。
CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_attempt_source
    ON learning.exercise_attempt (source_response_revision_id)
    WHERE source_response_revision_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 二、answer_test_run：CAS / 检查器的一次运行
--
-- outcome 只有三态；fail 必须带 failure_kind（"判不出来"要能说清为什么）。
-- 正式卷的一次提交对一个 (提交项, 修订, 检查器版本) 只允许一行。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.answer_test_run (
  id                        uuid PRIMARY KEY,
  response_revision_id      uuid NOT NULL REFERENCES assessment.response_revision(id),
  answer_test_version_id    uuid NULL,
  submission_item_id        uuid NULL REFERENCES assessment.submission_item(id),
  learning_exercise_attempt_id uuid NULL REFERENCES learning.exercise_attempt(id),
  input_hash                char(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
  engine_name               text NOT NULL,
  engine_version            text NOT NULL,
  assumptions               jsonb NOT NULL DEFAULT '[]'::jsonb,
  branch                    text NULL,
  outcome                   text NOT NULL CHECK (outcome IN ('true', 'false', 'fail')),
  failure_kind              text NULL,
  counterexample            jsonb NULL,
  elapsed_ms                integer NOT NULL DEFAULT 0 CHECK (elapsed_ms >= 0),
  created_at                timestamptz NOT NULL DEFAULT now(),
  CHECK ((outcome = 'fail') = (failure_kind IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_formal_answer_run
    ON assessment.answer_test_run (submission_item_id, response_revision_id,
                                   answer_test_version_id)
    WHERE submission_item_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 三、grade / grade_revision / item_grade_decision
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.grade (
  id                            uuid PRIMARY KEY,
  submission_id                 uuid NOT NULL UNIQUE
                                REFERENCES assessment.submission(id),
  status                        text NOT NULL
                                CHECK (status IN ('draft','published','withdrawn','regraded')),
  current_published_revision_id uuid NULL,
  created_at                    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assessment.grade_revision (
  id                    uuid PRIMARY KEY,
  grade_id              uuid NOT NULL REFERENCES assessment.grade(id),
  submission_id         uuid NOT NULL REFERENCES assessment.submission(id),
  revision_no           integer NOT NULL CHECK (revision_no >= 1),
  previous_revision_id  uuid NULL REFERENCES assessment.grade_revision(id),
  total_score           numeric(8,2) NOT NULL CHECK (total_score >= 0),
  status                text NOT NULL
                        CHECK (status IN ('draft','published','withdrawn','regraded')),
  change_reason         text NULL,
  student_message       text NULL,
  regrade_run_id        uuid NULL,
  withdrawal_action     text NULL,
  created_by            uuid NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  published_by          uuid NULL,
  published_at          timestamptz NULL,
  UNIQUE (grade_id, revision_no),
  CHECK ((status = 'published') = (published_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS assessment.item_grade_decision (
  id                  uuid PRIMARY KEY,
  submission_item_id  uuid NOT NULL REFERENCES assessment.submission_item(id),
  grade_revision_id   uuid NOT NULL REFERENCES assessment.grade_revision(id),
  decision_seq        integer NOT NULL CHECK (decision_seq >= 1),
  rubric_version_id   uuid NULL,
  criterion_scores    jsonb NOT NULL DEFAULT '{}'::jsonb,
  score               numeric(8,2) NULL CHECK (score IS NULL OR score >= 0),
  rationale           text NULL,
  decided_by          uuid NOT NULL,
  decided_at          timestamptz NOT NULL,
  regrade_item_id     uuid NULL,
  supersedes_id       uuid NULL REFERENCES assessment.item_grade_decision(id),
  UNIQUE (submission_item_id, decision_seq)
);

-- ---------------------------------------------------------------------------
-- 四、申诉与重评
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.grade_appeal (
  id                  uuid PRIMARY KEY,
  submission_id       uuid NOT NULL REFERENCES assessment.submission(id),
  student_id          uuid NOT NULL,
  grade_revision_id   uuid NOT NULL REFERENCES assessment.grade_revision(id),
  reason              text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
  status              text NOT NULL
                      CHECK (status IN ('open','accepted','rejected','closed')),
  resolution          text NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  resolved_at         timestamptz NULL,
  resolved_by         uuid NULL,
  UNIQUE (submission_id, grade_revision_id, student_id)
);

CREATE TABLE IF NOT EXISTS assessment.regrade_run (
  id                  uuid PRIMARY KEY,
  appeal_id           uuid NULL REFERENCES assessment.grade_appeal(id),
  grade_id            uuid NOT NULL REFERENCES assessment.grade(id),
  started_by          uuid NOT NULL,
  started_at          timestamptz NOT NULL DEFAULT now(),
  finished_at         timestamptz NULL,
  status              text NOT NULL
                      CHECK (status IN ('running','succeeded','failed'))
);

CREATE TABLE IF NOT EXISTS assessment.regrade_item (
  id                    uuid PRIMARY KEY,
  regrade_run_id        uuid NOT NULL REFERENCES assessment.regrade_run(id),
  submission_item_id    uuid NOT NULL REFERENCES assessment.submission_item(id),
  old_answer            jsonb NULL,
  new_answer            jsonb NULL,
  old_rubric_version_id uuid NULL,
  new_rubric_version_id uuid NULL,
  old_score             numeric(8,2) NULL,
  new_score             numeric(8,2) NULL,
  outcome               text NULL CHECK (outcome IN ('unchanged','changed','needs_human')),
  teacher_confirmed     boolean NOT NULL DEFAULT false,
  UNIQUE (regrade_run_id, submission_item_id)
);

CREATE TABLE IF NOT EXISTS assessment.grading_suggestion (
  id                    uuid PRIMARY KEY,
  submission_item_id    uuid NOT NULL REFERENCES assessment.submission_item(id),
  suggested_score       numeric(8,2) NULL,
  rationale             text NULL,
  model_name            text NOT NULL,
  model_version         text NOT NULL,
  adopted               boolean NOT NULL DEFAULT false,
  adopted_by_decision_id uuid NULL REFERENCES assessment.item_grade_decision(id),
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 五、append-only 触发器
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION assessment.guard_grade_revision_score() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.grade_id IS DISTINCT FROM OLD.grade_id
     OR NEW.submission_id IS DISTINCT FROM OLD.submission_id
     OR NEW.revision_no IS DISTINCT FROM OLD.revision_no
     OR NEW.previous_revision_id IS DISTINCT FROM OLD.previous_revision_id
     OR NEW.total_score IS DISTINCT FROM OLD.total_score
     OR NEW.change_reason IS DISTINCT FROM OLD.change_reason
     OR NEW.student_message IS DISTINCT FROM OLD.student_message
     OR NEW.regrade_run_id IS DISTINCT FROM OLD.regrade_run_id
     OR NEW.withdrawal_action IS DISTINCT FROM OLD.withdrawal_action
     OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
    RAISE EXCEPTION 'grade_revision_score_is_append_only';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS grade_revision_score_guard ON assessment.grade_revision;
CREATE TRIGGER grade_revision_score_guard
  BEFORE UPDATE ON assessment.grade_revision
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_grade_revision_score();

CREATE OR REPLACE FUNCTION assessment.guard_item_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'item_grade_decision is append-only: % 被拒绝', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS item_grade_decision_guard ON assessment.item_grade_decision;
CREATE TRIGGER item_grade_decision_guard
  BEFORE UPDATE OR DELETE ON assessment.item_grade_decision
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_item_decision();

-- 练习投影保护：只保护"带来源"的新行；历史学习记录（来源为空）仍可维护。
CREATE OR REPLACE FUNCTION learning.guard_projection_source() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.source_response_revision_id IS NOT NULL OR OLD.paper_attempt_id IS NOT NULL THEN
    RAISE EXCEPTION 'learning_projection_is_append_only: % 被拒绝', TG_OP;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS learning_projection_source_guard
    ON learning.exercise_attempt;
CREATE TRIGGER learning_projection_source_guard
  BEFORE UPDATE OR DELETE ON learning.exercise_attempt
  FOR EACH ROW EXECUTE FUNCTION learning.guard_projection_source();

-- ---------------------------------------------------------------------------
-- 六、RLS
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY[
      'answer_test_run', 'grade', 'grade_revision', 'item_grade_decision',
      'grade_appeal', 'regrade_run', 'regrade_item', 'grading_suggestion'
  ] LOOP
    EXECUTE format('ALTER TABLE assessment.%I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE assessment.%I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS %I_service_only ON assessment.%I', target, target);
    EXECUTE format(
      'CREATE POLICY %I_service_only ON assessment.%I USING (
         current_setting(''app.assessment_service'', true) = ''on'')', target, target);
  END LOOP;
END
$$;

-- 学生能看自己的成绩版本与申诉（只读由 API 保证；这里给行级范围）。
DROP POLICY IF EXISTS grade_revision_student_scope ON assessment.grade_revision;
CREATE POLICY grade_revision_student_scope ON assessment.grade_revision
  USING (EXISTS (
    SELECT 1 FROM assessment.submission s
     WHERE s.id = submission_id
       AND s.student_id::text = current_setting('app.actor_id', true)
  ));

DROP POLICY IF EXISTS grade_appeal_student_scope ON assessment.grade_appeal;
CREATE POLICY grade_appeal_student_scope ON assessment.grade_appeal
  USING (student_id::text = current_setting('app.actor_id', true))
  WITH CHECK (student_id::text = current_setting('app.actor_id', true));

ALTER TABLE learning.exercise_attempt ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_learning_attempt_source'
      AND conrelid = 'learning.exercise_attempt'::regclass
  ) THEN
    NULL;  -- 索引已建，无需动作
  END IF;
END
$$;

COMMIT;
