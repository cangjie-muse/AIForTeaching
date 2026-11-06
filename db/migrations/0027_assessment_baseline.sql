-- =============================================================================
-- 0027 · 阶段 A 数据基线：固定卷、曝光、打印作业，以及最小通用 Artifact
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-01-manual-fixed-papers.md  任务 1
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §10.1—§10.12（数据模型）与 §14.1（学生投影白名单）
--
-- 为什么单独一套命名空间：固定卷的对象是 UUID + NUMERIC(8,2) + JSONB + char(64)
-- 哈希，和既有 public 表（text id、num(5,1) 分）不是同一套口径。硬塞进 public
-- 会让 homework 的 assignment 语义被改（本仓库只做加法，不改既有列语义），
-- 所以阶段 A 的全部对象落在 assessment.*，文件凭证落在 agent.*。
--
-- 与计划书的差异（按本仓库现有技术栈改写）：
--   * 不用 Alembic，这里是 db/migrations/NNNN_*.sql，由 db/migrate.py 应用；
--   * 不用 ops.migration_owned_object 记录归属：migrate.py 是只前进 + 校验和的，
--     没有 downgrade 概念，所以本文件不写 downgrade 段；
--   * 全部语句幂等（IF NOT EXISTS / DROP ... IF EXISTS 后再建），重复执行不报错；
--   * public.assignment 一行都不动：阶段 A 的打印作业是 assessment.assignment。
--
-- 幂等写法说明：CREATE TABLE IF NOT EXISTS 已覆盖"表已存在"；
-- 策略、触发器这类没有 IF NOT EXISTS 语法的对象先 DROP IF EXISTS 再建。
--
-- ⚠ RLS 提醒：paper_version / paper_section / paper_slot / paper_render 开了
-- FORCE ROW LEVEL SECURITY，非超级用户（含表 owner）默认读不到，除非同一事务里
-- `SET LOCAL app.assessment_service = 'on'`（服务账号）或
-- `SET LOCAL app.course_ids = '<uuid>,<uuid>'`（教师/学生范围）。
-- 本次任务没有任何代码读这些表，所以不影响现有功能；阶段 A 后续服务必须显式设置。
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS assessment;
CREATE SCHEMA IF NOT EXISTS agent;


-- ---------------------------------------------------------------------------
-- 一、最小通用 Artifact（plan 任务 1：从基础计划任务 9 抽出）
--
-- 如果并行工作已经建过 agent.artifact_version，这里的 IF NOT EXISTS 会跳过，
-- 后续列兼容检查由 server/tests/test_assessment_migration.py 之外的集成测试负责
-- （本次未连真库，见交付说明）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent.artifact (
  id            uuid PRIMARY KEY,
  artifact_kind text NOT NULL,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent.artifact_version (
  id                   uuid PRIMARY KEY,
  artifact_id          uuid NOT NULL REFERENCES agent.artifact(id),
  version_no           integer NOT NULL,
  release_context_id   uuid NOT NULL,
  -- private：仅本人/服务；class_shared：本 offering；course_published：整个 Release Context
  visibility           text NOT NULL CHECK (visibility IN ('private','class_shared','course_published')),
  contains_solution    boolean NOT NULL,
  contains_answer_test boolean NOT NULL,
  media_kind           text NOT NULL,
  mime_type            text NOT NULL,
  -- s3://<bucket>/<media_kind>/<sha256>：内容寻址，不可由题号/卷号猜出，不含签名参数
  storage_uri          text NOT NULL,
  byte_size            bigint NOT NULL CHECK (byte_size >= 0),
  output_hash          char(64) NOT NULL,
  created_by           uuid NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (artifact_id, version_no),
  -- 重复注册同一份内容（同创建者、同媒体类型）复用同一版本：重试不产生第二份文件
  UNIQUE (output_hash, media_kind, created_by)
);

CREATE TABLE IF NOT EXISTS agent.artifact_access_event (
  id                  uuid PRIMARY KEY,
  artifact_version_id uuid NOT NULL REFERENCES agent.artifact_version(id),
  actor_id            uuid NOT NULL,
  action              text NOT NULL CHECK (action IN ('open','download')),
  occurred_at         timestamptz NOT NULL DEFAULT now()
);

-- 「谁在什么时候导出过这份保密卷」的审计查询按版本走
CREATE INDEX IF NOT EXISTS artifact_access_event_version_idx
    ON agent.artifact_access_event (artifact_version_id, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- 二、蓝图与候选快照（§10.1、§10.2 的手工子集；不建 assembly_run）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_blueprint_version (
  id                 uuid PRIMARY KEY,
  blueprint_id       uuid NOT NULL,
  version_no         integer NOT NULL CHECK (version_no > 0),
  course_id          uuid NOT NULL,
  offering_id        uuid NULL,
  release_context_id uuid NOT NULL,
  purpose            text NOT NULL CHECK (purpose IN ('formal','coursework','practice')),
  assembly_mode      text NOT NULL CHECK (assembly_mode IN ('manual','constraint_assisted')),
  total_score        numeric(8,2) NOT NULL CHECK (total_score > 0),
  duration_seconds   integer NOT NULL CHECK (duration_seconds > 0),
  blueprint_ast      jsonb NOT NULL,
  base_version_id    uuid NULL REFERENCES assessment.paper_blueprint_version(id),
  content_hash       char(64) NOT NULL,
  change_set_id      uuid NOT NULL,
  created_by         uuid NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (blueprint_id, version_no),
  UNIQUE (content_hash, blueprint_id)
);

CREATE TABLE IF NOT EXISTS assessment.candidate_snapshot (
  id                       uuid PRIMARY KEY,
  blueprint_version_id     uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
  candidate_manifest       jsonb NOT NULL,
  candidate_manifest_hash  char(64) NOT NULL,
  created_by               uuid NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now(),
  -- 同一候选清单可复用于两个蓝图；同一蓝图重复清单由复合键去重
  UNIQUE (blueprint_version_id, candidate_manifest_hash)
);


-- ---------------------------------------------------------------------------
-- 三、固定卷本体（§10.3—§10.5）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_version (
  id                     uuid PRIMARY KEY,
  paper_id               uuid NOT NULL,
  version_no             integer NOT NULL CHECK (version_no > 0),
  blueprint_version_id   uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
  candidate_snapshot_id  uuid NOT NULL REFERENCES assessment.candidate_snapshot(id),
  -- 手工卷恒为空；列保留是为了与规格对齐且能明确拒绝约束组卷
  assembly_run_id        uuid NULL,
  selected_solution_index integer NULL,
  base_paper_version_id  uuid NULL REFERENCES assessment.paper_version(id),
  course_id              uuid NOT NULL,
  offering_id            uuid NULL,
  release_context_id     uuid NOT NULL,
  paper_kind             text NOT NULL CHECK (paper_kind IN ('formal','coursework','practice')),
  variant_group_id       uuid NULL,
  variant_label          text NULL,
  total_score            numeric(8,2) NOT NULL,
  duration_seconds       integer NOT NULL,
  status                 text NOT NULL CHECK (status IN ('draft','in_review','approved','withdrawn')),
  edit_seq               integer NOT NULL DEFAULT 1,
  manifest_hash          char(64) NOT NULL,
  submitted_manifest_hash char(64) NULL,
  review_manifest_hash   char(64) NULL,
  created_by             uuid NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  approved_at            timestamptz NULL,
  withdrawn_by           uuid NULL,
  withdrawn_at           timestamptz NULL,
  withdrawal_reason      text NULL,
  UNIQUE (paper_id, version_no),
  UNIQUE (manifest_hash, paper_id),
  -- 本阶段不组卷：手工卷不允许出现求解器字段
  CONSTRAINT ck_paper_manual_only CHECK (assembly_run_id IS NULL AND selected_solution_index IS NULL),
  CHECK (status <> 'approved' OR approved_at IS NOT NULL),
  CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS assessment.paper_section (
  id              uuid PRIMARY KEY,
  paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
  code            text NOT NULL,
  title           text NOT NULL,
  instruction     text NOT NULL,
  sort_order      integer NOT NULL,
  section_score   numeric(8,2) NOT NULL,
  display_config  jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash    char(64) NOT NULL,
  UNIQUE (paper_version_id, code),
  UNIQUE (paper_version_id, sort_order)
);

CREATE TABLE IF NOT EXISTS assessment.paper_slot (
  id                      uuid PRIMARY KEY,
  section_id              uuid NOT NULL REFERENCES assessment.paper_section(id),
  code                    text NOT NULL,
  slot_no                 integer NOT NULL,
  exercise_id             uuid NULL,
  exercise_version_id     uuid NULL,
  exercise_instance_id    uuid NULL,
  exercise_family_id      uuid NULL,
  source_release_id       uuid NULL,
  score                   numeric(8,2) NOT NULL CHECK (score >= 0),
  estimated_seconds       integer NULL,
  display_config          jsonb NOT NULL DEFAULT '{}'::jsonb,
  dependency_manifest_hash char(64) NULL,
  content_hash            char(64) NOT NULL,
  UNIQUE (section_id, slot_no),
  UNIQUE (section_id, code),
  -- 参数化题必须有版本；静态题可以没有实例
  CHECK (exercise_instance_id IS NULL OR exercise_version_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS assessment.paper_review_round (
  id                 uuid PRIMARY KEY,
  paper_version_id   uuid NOT NULL REFERENCES assessment.paper_version(id),
  manifest_hash      char(64) NOT NULL,
  required_approvals integer NOT NULL DEFAULT 2 CHECK (required_approvals >= 1),
  status             text NOT NULL CHECK (status IN ('open','approved','returned','invalidated')),
  submitted_by       uuid NOT NULL,
  submitted_at       timestamptz NOT NULL DEFAULT now(),
  closed_at          timestamptz NULL,
  -- 一个 manifest 只审一次；manifest 变了就开新一轮，旧轮次不能再返回 approved
  UNIQUE (paper_version_id, manifest_hash)
);


-- ---------------------------------------------------------------------------
-- 四、作答策略与曝光（§10.6、§10.11）
--
-- 本阶段不建 attempt / response_revision：阶段 A 没有任何在线作答行为，
-- 这里只把阶段 C/D 要直接引用的策略与曝光表建出来，避免以后重复建表。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.attempt_policy_version (
  id                         uuid PRIMARY KEY,
  policy_id                  uuid NOT NULL,
  version_no                 integer NOT NULL CHECK (version_no > 0),
  policy_kind                text NOT NULL CHECK (policy_kind IN ('formal','coursework','practice')),
  content_hash               char(64) NOT NULL UNIQUE,
  max_attempts               integer NULL CHECK (max_attempts IS NULL OR max_attempts > 0),
  behavior                   text NOT NULL CHECK (behavior IN ('deferred','adaptive_practice')),
  time_limit_seconds         integer NULL,
  grace_seconds              integer NOT NULL DEFAULT 0,
  late_policy                text NOT NULL CHECK (late_policy IN ('reject','read_only_grace','auto_submit')),
  show_syntax_errors         boolean NOT NULL,
  show_math_interpretation   boolean NOT NULL,
  show_input_preview         boolean NOT NULL,
  correctness_release        text NOT NULL,
  score_release              text NOT NULL,
  hint_release               text NOT NULL,
  solution_release           text NOT NULL,
  answer_release             text NOT NULL,
  allow_item_check           boolean NOT NULL,
  max_hint_level             integer NOT NULL DEFAULT 0,
  allow_retry                boolean NOT NULL,
  practice_penalty           numeric(5,2) NOT NULL DEFAULT 0,
  allow_flag                 boolean NOT NULL,
  allow_review               boolean NOT NULL,
  allow_upload               boolean NOT NULL,
  autosave_debounce_ms       integer NOT NULL DEFAULT 800,
  autosave_max_interval_ms   integer NOT NULL DEFAULT 5000,
  conflict_policy            text NOT NULL DEFAULT 'per_slot',
  created_at                 timestamptz NOT NULL DEFAULT now(),
  UNIQUE (policy_id, version_no),
  -- 不限次数只允许出现在练习策略上
  CHECK (max_attempts IS NOT NULL OR policy_kind = 'practice')
);

CREATE TABLE IF NOT EXISTS assessment.item_exposure_event (
  id                  uuid PRIMARY KEY,
  exercise_version_id uuid NOT NULL,
  exercise_instance_id uuid NULL,
  exercise_family_id  uuid NOT NULL,
  paper_version_id    uuid NULL REFERENCES assessment.paper_version(id),
  assignment_id       uuid NULL,   -- 外键在 assessment.assignment 建好后补（见文件末尾）
  scope_offering_id   uuid NULL,
  event_kind          text NOT NULL CHECK (event_kind IN ('secure_assigned','student_opened','practice_answer_released','formal_answer_released','withdrawn')),
  visible_from        timestamptz NOT NULL,
  occurred_at         timestamptz NOT NULL DEFAULT now(),
  metadata_hash       char(64) NOT NULL
);

-- 曝光冲突检查的典型查询：同族题目最近一次放答案的时间
CREATE INDEX IF NOT EXISTS exposure_family_kind_idx
    ON assessment.item_exposure_event (exercise_family_id, event_kind, occurred_at DESC);


-- ---------------------------------------------------------------------------
-- 五、打印渲染（§10.10）：文件本身在 agent.artifact_version
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_render (
  id                    uuid PRIMARY KEY,
  paper_version_id      uuid NOT NULL REFERENCES assessment.paper_version(id),
  render_kind           text NOT NULL CHECK (render_kind IN ('student','answer','rubric')),
  paper_manifest_hash   char(64) NOT NULL,
  template_version      text NOT NULL,
  language_code         text NOT NULL,
  artifact_version_id   uuid NOT NULL REFERENCES agent.artifact_version(id),
  renderer_version      text NOT NULL,
  font_manifest_hash    char(64) NOT NULL,
  -- 学生卷的 security_manifest_hash 只允许包含题干与公开资产（§14.1）
  security_manifest_hash char(64) NOT NULL,
  output_hash           char(64) NOT NULL,
  status                text NOT NULL CHECK (status IN ('ready','failed','blocked')),
  created_by            uuid NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (paper_version_id, render_kind, output_hash)
);


-- ---------------------------------------------------------------------------
-- 六、打印作业与不可变作业项（§10.9 的 assessment 命名空间版本）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.assignment (
  id                        uuid PRIMARY KEY,
  course_id                 uuid NOT NULL,
  offering_id               uuid NOT NULL,
  paper_version_id          uuid NOT NULL REFERENCES assessment.paper_version(id),
  attempt_policy_version_id uuid NOT NULL REFERENCES assessment.attempt_policy_version(id),
  release_context_id        uuid NOT NULL,
  visible_at                timestamptz NOT NULL,
  due_at                    timestamptz NOT NULL,
  hard_close_at             timestamptz NOT NULL,
  distribution_kind         text NOT NULL CHECK (distribution_kind = 'print'),
  status                    text NOT NULL CHECK (status IN ('draft','scheduled','published','closed','withdrawn')),
  created_by                uuid NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  published_at              timestamptz NULL,
  CHECK (visible_at <= due_at AND due_at <= hard_close_at)
);

CREATE TABLE IF NOT EXISTS assessment.assignment_item (
  id                  uuid PRIMARY KEY,
  assignment_id       uuid NOT NULL REFERENCES assessment.assignment(id),
  paper_slot_id       uuid NOT NULL REFERENCES assessment.paper_slot(id),
  ord                 integer NOT NULL,
  release_context_id  uuid NOT NULL,
  source_release_id   uuid NOT NULL,
  exercise_version_id uuid NOT NULL,
  exercise_instance_id uuid NULL,
  score               numeric(8,2) NOT NULL,
  content_hash        char(64) NOT NULL,
  UNIQUE (assignment_id, paper_slot_id),
  UNIQUE (assignment_id, ord)
);

-- item_exposure_event 建表时 assignment 还不存在，这里补外键；用 DO 块保证幂等
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'exposure_assignment_fk'
      AND conrelid = 'assessment.item_exposure_event'::regclass
  ) THEN
    ALTER TABLE assessment.item_exposure_event
      ADD CONSTRAINT exposure_assignment_fk
      FOREIGN KEY (assignment_id) REFERENCES assessment.assignment(id);
  END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 七、不可变性守卫（§0.3 第 4 条）
--
-- approved 之后题目、分值、顺序、manifest 不可 UPDATE；普通纠错建新 paper 版本，
-- 严重纠错走撤回。assignment_item 只允许 INSERT。
-- 用触发器而不是应用层判断，是因为"改卷"这件事必须挡在数据库这一层。
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION assessment.guard_fixed_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'paper_version' THEN
    IF OLD.status = 'withdrawn' THEN
      RAISE EXCEPTION 'withdrawn paper is immutable';
    END IF;
    IF OLD.status = 'approved' AND NEW.status <> 'withdrawn' THEN
      RAISE EXCEPTION 'approved paper is immutable';
    END IF;
    IF OLD.status <> 'draft'
       AND (NEW.manifest_hash <> OLD.manifest_hash OR NEW.edit_seq <> OLD.edit_seq) THEN
      RAISE EXCEPTION 'submitted manifest is immutable';
    END IF;
    IF NEW.status = 'approved'
       AND (OLD.status <> 'in_review' OR NEW.review_manifest_hash IS NULL) THEN
      RAISE EXCEPTION 'dual review manifest required';
    END IF;
  ELSIF TG_TABLE_NAME = 'assignment_item' THEN
    RAISE EXCEPTION 'assignment_item is append-only';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS paper_version_guard ON assessment.paper_version;
CREATE TRIGGER paper_version_guard BEFORE UPDATE ON assessment.paper_version
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();

DROP TRIGGER IF EXISTS assignment_item_guard ON assessment.assignment_item;
CREATE TRIGGER assignment_item_guard BEFORE UPDATE OR DELETE ON assessment.assignment_item
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();


-- ---------------------------------------------------------------------------
-- 八、行级安全（§14.2）
--
-- 保密卷、学生卷与答案卷不允许普通学生角色读到。服务账号在同一事务里
-- SET LOCAL app.assessment_service = 'on'；课程范围用 app.course_ids。
-- paper_render 只允许服务账号读：三类卷的差异本身就是答案信息。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_version FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS paper_course_scope ON assessment.paper_version;
CREATE POLICY paper_course_scope ON assessment.paper_version
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
  );

ALTER TABLE assessment.paper_section ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_section FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS section_course_scope ON assessment.paper_section;
CREATE POLICY section_course_scope ON assessment.paper_section
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_version p
      WHERE p.id = paper_version_id
        AND p.course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
    )
  );

ALTER TABLE assessment.paper_slot ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_slot FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS slot_course_scope ON assessment.paper_slot;
CREATE POLICY slot_course_scope ON assessment.paper_slot
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_section s
      JOIN assessment.paper_version p ON p.id = s.paper_version_id
      WHERE s.id = section_id
        AND p.course_id::text = ANY (string_to_array(current_setting('app.course_ids', true), ','))
    )
  );

ALTER TABLE assessment.paper_render ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_render FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_service_only ON assessment.paper_render;
CREATE POLICY render_service_only ON assessment.paper_render
  USING (current_setting('app.assessment_service', true) = 'on');
