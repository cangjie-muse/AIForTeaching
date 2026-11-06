-- 0030 · 手工建卷/编辑：槽位级 Release Context + 草稿变更集（append-only）
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-01-manual-fixed-papers.md  任务 4
--   docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
--     §10.3—§10.5（paper_version / paper_section / paper_slot）、§8.2（撤销本次草稿编辑）、
--     §16（异常与边界：教师手工改坏总分草稿可保存，base revision 冲突禁止静默覆盖）
--
-- 为什么需要这个迁移（0027/0029 一个字都不改）：
--   1. 任务 4 的硬约束是"槽位必须固定来源：release_context_id / source_release_id /
--      exercise_version_id / exercise_family_id"。0027 的 paper_slot 有后三个，没有
--      release_context_id——打印作业项（assignment_item）投影时又确实需要槽位级
--      Release，否则只能从 paper_version 反推。所以补一列（可空，写入路径必填；
--      0027 之前 paper_slot 没有任何写入方，因此不存在需要回填的历史行）。
--   2. "被替换掉的槽位不删除历史来源，记在变更集里"。paper_slot 一行只有一个当前值，
--      替换后旧来源就丢了。这里新增 assessment.paper_edit_event，按
--      (paper_version_id, edit_seq, ord) 追加，存替换/调序/调分的旧值与新值；
--      UPDATE/DELETE 由触发器拒绝——审计事实不能改。
--
-- RLS：paper_edit_event 与 paper_version / paper_section / paper_slot 同口径，
-- 开 FORCE ROW LEVEL SECURITY，课程范围经 paper_version.course_id 判定；
-- 服务账号写路径仍统一走 db.transaction(assessment_service=True)。
--
-- 幂等：CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP TRIGGER IF EXISTS
-- 后再建；不删除、不改写任何既有对象。

BEGIN;

CREATE SCHEMA IF NOT EXISTS assessment;

-- ---------------------------------------------------------------------------
-- 一、paper_slot.release_context_id：槽位级固定来源
--
-- 可空的原因：0027 建表时没有写入方，本迁移之前不可能有行；写入路径（任务 4 起）
-- 对每个已填槽位都写同一 uuid（= paper_version.release_context_id）。不加 NOT NULL
-- 是为了让"先加列、后补数据"的在线迁移在有任何历史行时也不会失败。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_slot
  ADD COLUMN IF NOT EXISTS release_context_id uuid;

COMMENT ON COLUMN assessment.paper_slot.release_context_id IS
  '槽位固定的 Release Context；与 paper_version.release_context_id 一致，写入时由候选快照决定，不接受客户端自选';

-- "这个 Release 下有哪些槽位"是撤回巡检与曝光回放的查询方向
CREATE INDEX IF NOT EXISTS paper_slot_release_context_idx
    ON assessment.paper_slot (release_context_id);

-- 已填槽位必须来源齐全（四元组 + 依赖哈希），空槽位允许全空：
-- 这是 §10.5 的"内容寻址"在数据库层的最后一道防线，防止只有题号的槽位混进来。
ALTER TABLE assessment.paper_slot
  DROP CONSTRAINT IF EXISTS ck_paper_slot_source_complete;
ALTER TABLE assessment.paper_slot
  ADD CONSTRAINT ck_paper_slot_source_complete CHECK (
    exercise_version_id IS NULL
    OR (
      exercise_id IS NOT NULL
      AND exercise_family_id IS NOT NULL
      AND source_release_id IS NOT NULL
      AND release_context_id IS NOT NULL
      AND dependency_manifest_hash IS NOT NULL
    )
  );

-- ---------------------------------------------------------------------------
-- 二、paper_edit_event：草稿编辑的不可变变更集
--
-- edit_seq >= 2：第 1 版是建卷，不是编辑；ord 是同一批操作里的顺序（一次 PATCH
-- 可以同时替换 A1、调 A2 的分、把 A3 上移一位）。
-- previous_* / new_* 同时保留：撤销（按 change_set_id 反向应用）与审计回放都需要
-- "换掉的是哪一版、来自哪个 Release、当时多少分"。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS assessment.paper_edit_event (
  id                             uuid PRIMARY KEY,
  paper_version_id               uuid NOT NULL REFERENCES assessment.paper_version(id),
  change_set_id                  uuid NOT NULL,
  edit_seq                       integer NOT NULL CHECK (edit_seq >= 2),
  ord                            integer NOT NULL CHECK (ord >= 1),
  operation_kind                 text NOT NULL
                                 CHECK (operation_kind IN ('replace','move','score')),
  -- 槽位不删除，所以可以外键；它同时是"这次编辑动的是哪一格"的唯一锚点
  slot_id                        uuid NOT NULL REFERENCES assessment.paper_slot(id),
  reason                         text,
  previous_exercise_id           uuid,
  previous_exercise_version_id   uuid,
  previous_exercise_instance_id  uuid,
  previous_exercise_family_id    uuid,
  previous_source_release_id     uuid,
  previous_release_context_id    uuid,
  new_exercise_id                uuid,
  new_exercise_version_id        uuid,
  new_exercise_instance_id       uuid,
  new_exercise_family_id         uuid,
  new_source_release_id          uuid,
  new_release_context_id         uuid,
  previous_slot_no               integer,
  new_slot_no                    integer,
  previous_score                 numeric(8,2),
  new_score                      numeric(8,2),
  -- 编辑后的整卷 manifest：事件与"这一版卷子"必须可核对
  manifest_hash                  char(64) NOT NULL,
  actor_id                       uuid NOT NULL,
  occurred_at                    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (paper_version_id, edit_seq, ord)
);

COMMENT ON TABLE assessment.paper_edit_event IS
  '草稿编辑的 append-only 变更集：替换/调序/调分的旧来源与新来源都保留，UPDATE/DELETE 被触发器拒绝';

CREATE INDEX IF NOT EXISTS paper_edit_event_version_idx
    ON assessment.paper_edit_event (paper_version_id, edit_seq DESC);
CREATE INDEX IF NOT EXISTS paper_edit_event_change_set_idx
    ON assessment.paper_edit_event (change_set_id);

-- 审计事实不可变：触发器比"应用层约定"可靠——任何绕过服务的 SQL 也改不了历史。
CREATE OR REPLACE FUNCTION assessment.guard_paper_edit_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'paper_edit_event is append-only: % 被拒绝', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS paper_edit_event_guard ON assessment.paper_edit_event;
CREATE TRIGGER paper_edit_event_guard
  BEFORE UPDATE OR DELETE ON assessment.paper_edit_event
  FOR EACH ROW EXECUTE FUNCTION assessment.guard_paper_edit_event();

-- ---------------------------------------------------------------------------
-- 三、RLS：与 paper_* 同口径（服务账号全量，教师/学生按课程范围）
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.paper_edit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment.paper_edit_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS edit_event_course_scope ON assessment.paper_edit_event;
CREATE POLICY edit_event_course_scope ON assessment.paper_edit_event
  USING (
    current_setting('app.assessment_service', true) = 'on'
    OR EXISTS (
      SELECT 1 FROM assessment.paper_version p
      WHERE p.id = paper_version_id
        AND p.course_id::text = ANY (
          string_to_array(current_setting('app.course_ids', true), ',')
        )
    )
  );

COMMIT;
