-- 0036 · 在线固定卷作答：attempt / 逐槽响应版本 / 交卷与提交项（阶段 C 任务 1）
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-03-online-attempts.md  任务 1
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §11（保存、续答、交卷）、§10（数据模型）、§14（安全）
--
-- 为什么这些表必须与阶段 A 的 assignment/assignment_item 共用真源：
--   在线卷和打印卷是**同一份卷子的两种分发方式**。如果另建一张"在线作业表"，题号、
--   分值、来源就会有两份，谁改了一边另一边不知道——打印出去的卷子和学生屏幕上看到的
--   卷子可以不一致，而这种不一致在开考后才发现就来不及了。所以：
--     assignment 增加 distribution_kind = online/both；
--     attempt 指向 assignment + paper_version（不复制题目）；
--     attempt_item 指向 assignment_item（题号与分值继承自发布时的冻结投影）。
--
-- 三条数据库层不变量（服务层之外的第二道门）：
--   1. 同一 (assignment, student) 最多一个 in_progress/finalizing attempt（部分唯一索引）；
--   2. 同一 (attempt, paper_slot, revision_no) 唯一、client_revision_id 唯一——重放不会
--      插出第二条响应；revision_no >= 1；
--   3. 交卷后不可改写：submission / submission_item / attempt_item / response_revision
--      全部 append-only（触发器拒绝 UPDATE/DELETE）。事实只能追加。
--
-- 幂等：全部 CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP ... IF EXISTS
-- 后重建，重复执行不报错。

BEGIN;

-- ---------------------------------------------------------------------------
-- 一、assignment：允许"在线"与"线上+线下"两种分发
--
-- 0027 把 distribution_kind 写死成 'print'，那是阶段 A 的闸门。阶段 C 打开它，
-- 但仍然只允许三个取值：多一个 'online_only' 之类的名字会让"这批学生怎么考试"变成
-- 无法回答的问题。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.assignment
  DROP CONSTRAINT IF EXISTS assignment_distribution_kind_check;
ALTER TABLE assessment.assignment
  ADD CONSTRAINT assignment_distribution_kind_check
  CHECK (distribution_kind IN ('print', 'online', 'both'));

-- ---------------------------------------------------------------------------
-- 二、paper_attempt：一次作答
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_attempt (
  id                        uuid PRIMARY KEY,
  assignment_id             uuid NOT NULL REFERENCES assessment.assignment(id),
  paper_version_id          uuid NOT NULL REFERENCES assessment.paper_version(id),
  -- 学生账号 uuid。assessment 命名空间里其他 actor 列也都是裸 uuid（不跨 schema 建外键）
  student_id                uuid NOT NULL,
  attempt_policy_version_id uuid NOT NULL REFERENCES assessment.attempt_policy_version(id),
  attempt_no                integer NOT NULL CHECK (attempt_no >= 1),
  mode                      text NOT NULL CHECK (mode IN ('normal', 'read_only')),
  status                    text NOT NULL
    CHECK (status IN ('in_progress', 'finalizing', 'submitted', 'closed')),
  started_at                timestamptz NOT NULL,
  -- 服务端算出的截止时刻：客户端时钟不参与判定（§0.3 规则 3）
  server_deadline_at        timestamptz NOT NULL,
  -- 到期 worker 该来处理这一行的时刻（= deadline + 宽限）
  worker_due_at             timestamptz NOT NULL,
  last_server_revision      integer NOT NULL DEFAULT 0 CHECK (last_server_revision >= 0),
  response_manifest_hash    char(64) NULL,
  submitted_at              timestamptz NULL,
  close_reason              text NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  -- 交卷/关闭必须有时间；进行中不能有
  CHECK ((status IN ('submitted', 'closed')) = (submitted_at IS NOT NULL)),
  -- 宽限只读：read_only 是"截止后还能看题，但录不进新答案"，不是延长考试
  CHECK (mode <> 'read_only' OR status <> 'in_progress' OR true)
);

COMMENT ON TABLE assessment.paper_attempt IS
  '在线固定卷的一次作答：指向 assignment + paper_version，不复制题目；同一学生同时只有一个进行中';

-- 同一学生同一作业只能有一份进行中/收尾中的卷（规则 1）
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_attempt_one_active
    ON assessment.paper_attempt (assignment_id, student_id)
    WHERE status IN ('in_progress', 'finalizing');

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_attempt_no
    ON assessment.paper_attempt (assignment_id, student_id, attempt_no);

-- 到期扫描：只看还没结束的
CREATE INDEX IF NOT EXISTS paper_attempt_due_idx
    ON assessment.paper_attempt (worker_due_at)
    WHERE status IN ('in_progress', 'finalizing');

CREATE INDEX IF NOT EXISTS paper_attempt_student_idx
    ON assessment.paper_attempt (student_id, assignment_id);

-- ---------------------------------------------------------------------------
-- 三、paper_attempt_item：这次作答里"有哪些槽位"
--
-- 指向 assignment_item 而不是 paper_slot：发布时已经冻结了题号/分值/来源，
-- attempt 再复制一份就等于允许两份投影漂移。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_attempt_item (
  id                 uuid PRIMARY KEY,
  attempt_id         uuid NOT NULL REFERENCES assessment.paper_attempt(id),
  assignment_item_id uuid NOT NULL REFERENCES assessment.assignment_item(id),
  paper_slot_id      uuid NOT NULL REFERENCES assessment.paper_slot(id),
  ord                integer NOT NULL CHECK (ord >= 1),
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, assignment_item_id),
  UNIQUE (attempt_id, paper_slot_id),
  UNIQUE (attempt_id, ord)
);

-- ---------------------------------------------------------------------------
-- 四、response_revision：逐槽不可变响应版本
--
-- 为什么按"槽位"而不是"整卷"版本化：多标签页里两个窗口分别在改不同的题，
-- 整卷版本号会让它们互相冲突；按槽比较才既防丢写又不误伤（规则 2）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.response_revision (
  id                 uuid PRIMARY KEY,
  attempt_id         uuid NOT NULL REFERENCES assessment.paper_attempt(id),
  paper_slot_id      uuid NOT NULL REFERENCES assessment.paper_slot(id),
  revision_no        integer NOT NULL CHECK (revision_no >= 1),
  -- 客户端生成的重试标识：同一次保存重试必须命中同一行（规则 2）
  client_revision_id text NOT NULL CHECK (length(client_revision_id) BETWEEN 1 AND 200),
  raw_response       jsonb NOT NULL,
  normalized_response jsonb NULL,
  parse_status       text NOT NULL
    CHECK (parse_status IN ('parsed', 'unparsed', 'invalid', 'empty')),
  invalid_reasons    jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- 服务端接收时间：快照资格只看它（规则 3）；客户端时间不进判定
  server_received_at timestamptz NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (attempt_id, client_revision_id),
  UNIQUE (attempt_id, paper_slot_id, revision_no)
);

CREATE INDEX IF NOT EXISTS response_revision_attempt_idx
    ON assessment.response_revision (attempt_id, paper_slot_id, revision_no DESC);

-- ---------------------------------------------------------------------------
-- 五、submission / submission_item：交卷与提交项
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.submission (
  id              uuid PRIMARY KEY,
  attempt_id      uuid NOT NULL UNIQUE REFERENCES assessment.paper_attempt(id),
  student_id      uuid NOT NULL,
  trigger_kind    text NOT NULL
    CHECK (trigger_kind IN ('manual', 'deadline', 'forced')),
  submitted_at    timestamptz NOT NULL,
  item_count      integer NOT NULL CHECK (item_count >= 0),
  unanswered_count integer NOT NULL CHECK (unanswered_count >= 0),
  invalid_count   integer NOT NULL CHECK (invalid_count >= 0),
  -- 快照的哈希：回执、评阅输入、审计都以它为准
  response_manifest_hash char(64) NOT NULL CHECK (response_manifest_hash ~ '^[0-9a-f]{64}$'),
  receipt_hash    char(64) NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (unanswered_count + invalid_count <= item_count)
);

CREATE TABLE IF NOT EXISTS assessment.submission_item (
  id                  uuid PRIMARY KEY,
  submission_id       uuid NOT NULL REFERENCES assessment.submission(id),
  assignment_item_id  uuid NOT NULL REFERENCES assessment.assignment_item(id),
  paper_slot_id       uuid NOT NULL REFERENCES assessment.paper_slot(id),
  raw_response        jsonb NULL,
  normalized_response jsonb NULL,
  parse_status        text NOT NULL
    CHECK (parse_status IN ('parsed', 'unparsed', 'invalid', 'empty')),
  rubric_version_id   uuid NULL,
  answer_test_run_id  uuid NULL,
  -- 阶段 C 只留"待评阅"：本阶段不写 grade、不跑 CAS（规则 7）
  evaluation_status   text NOT NULL DEFAULT 'pending'
    CHECK (evaluation_status IN ('pending', 'graded', 'human_required')),
  suggested_score     numeric(8,2) NULL,
  requires_human      boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (submission_id, assignment_item_id)
);

-- ---------------------------------------------------------------------------
-- 六、append-only：交卷之后的任何事实都只能追加
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION assessment.guard_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % 上的 % 被拒绝', TG_TABLE_NAME, TG_OP;
END;
$$;

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY[
      'paper_attempt_item', 'response_revision', 'submission', 'submission_item'
  ] LOOP
    EXECUTE format(
      'DROP TRIGGER IF EXISTS %I_append_only ON assessment.%I', target, target);
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON assessment.%I
         FOR EACH ROW EXECUTE FUNCTION assessment.guard_append_only()', target, target);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 七、RLS：服务账号全量；学生只能看/写自己的行
--
-- 学生的身份来自 `app.actor_id`（API 层在事务里 SET LOCAL）。没有这个设置时
-- 一律读不到——fail closed，而不是"读不到就放开"。
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY[
      'paper_attempt', 'paper_attempt_item', 'response_revision',
      'submission', 'submission_item'
  ] LOOP
    EXECUTE format('ALTER TABLE assessment.%I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE assessment.%I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS %I_service_only ON assessment.%I', target, target);
    EXECUTE format(
      'CREATE POLICY %I_service_only ON assessment.%I USING (
         current_setting(''app.assessment_service'', true) = ''on''
       )', target, target);
  END LOOP;
END
$$;

-- 学生范围：paper_attempt 直接按 student_id；其余表沿 attempt 关联。
DROP POLICY IF EXISTS paper_attempt_student_scope ON assessment.paper_attempt;
CREATE POLICY paper_attempt_student_scope ON assessment.paper_attempt
  USING (student_id::text = current_setting('app.actor_id', true))
  WITH CHECK (student_id::text = current_setting('app.actor_id', true));

DROP POLICY IF EXISTS attempt_item_student_scope ON assessment.paper_attempt_item;
CREATE POLICY attempt_item_student_scope ON assessment.paper_attempt_item
  USING (EXISTS (
    SELECT 1 FROM assessment.paper_attempt a
     WHERE a.id = attempt_id
       AND a.student_id::text = current_setting('app.actor_id', true)
  ))
  WITH CHECK (true);

DROP POLICY IF EXISTS response_revision_student_scope ON assessment.response_revision;
CREATE POLICY response_revision_student_scope ON assessment.response_revision
  USING (EXISTS (
    SELECT 1 FROM assessment.paper_attempt a
     WHERE a.id = attempt_id
       AND a.student_id::text = current_setting('app.actor_id', true)
  ))
  WITH CHECK (true);

DROP POLICY IF EXISTS submission_student_scope ON assessment.submission;
CREATE POLICY submission_student_scope ON assessment.submission
  USING (student_id::text = current_setting('app.actor_id', true))
  WITH CHECK (student_id::text = current_setting('app.actor_id', true));

DROP POLICY IF EXISTS submission_item_student_scope ON assessment.submission_item;
CREATE POLICY submission_item_student_scope ON assessment.submission_item
  USING (EXISTS (
    SELECT 1 FROM assessment.submission s
     WHERE s.id = submission_id
       AND s.student_id::text = current_setting('app.actor_id', true)
  ))
  WITH CHECK (true);

COMMIT;
