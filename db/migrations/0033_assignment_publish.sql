-- 0033: 打印作业发布所需的追加列与约束（阶段 A 任务 8）
--
-- 0027 已经把 assessment.assignment / assignment_item 建好了，但只够"存一行作业"：
-- 发布终检要冻结审核证据（publish_manifest_hash / review_manifest_hash）、撤回要
-- 留下理由与时间、曝光检查要按题族查（assignment_item 缺 exercise_family_id）。
-- 这些都是本任务才确定下来的字段，所以在 0033 里补齐，不去改已提交的 0027。
--
-- 全部是 ALTER TABLE ... ADD COLUMN IF NOT EXISTS / 幂等约束，重复执行不报错。

BEGIN;

-- ---------------------------------------------------------------------------
-- 一、assignment：发布证据与撤回痕迹
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.assignment
  ADD COLUMN IF NOT EXISTS publish_manifest_hash char(64);

ALTER TABLE assessment.assignment
  ADD COLUMN IF NOT EXISTS review_manifest_hash char(64);

ALTER TABLE assessment.assignment
  ADD COLUMN IF NOT EXISTS withdrawn_at timestamptz;

ALTER TABLE assessment.assignment
  ADD COLUMN IF NOT EXISTS withdrawn_by uuid;

ALTER TABLE assessment.assignment
  ADD COLUMN IF NOT EXISTS withdrawal_reason text;

COMMENT ON COLUMN assessment.assignment.publish_manifest_hash IS
  '发布那一刻卷面的 manifest_hash：事后能证明"发的就是审过的那一版"';
COMMENT ON COLUMN assessment.assignment.review_manifest_hash IS
  '发布时卷子的双审证据哈希；与卷面当前值不一致就说明审核证据已失效';
COMMENT ON COLUMN assessment.assignment.withdrawal_reason IS
  '严重撤回的理由：泄题/错题/时间冲突等，必须非空（CHECK 兜底）';

-- 已发布/已关闭必须有发布时刻；撤回必须有撤回时刻与理由。
-- 用 DO 块是为了幂等：约束不能 ADD CONSTRAINT IF NOT EXISTS。
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'assignment_published_at_required'
      AND conrelid = 'assessment.assignment'::regclass
  ) THEN
    ALTER TABLE assessment.assignment
      ADD CONSTRAINT assignment_published_at_required
      CHECK (status NOT IN ('published', 'closed') OR published_at IS NOT NULL);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'assignment_withdrawn_fields'
      AND conrelid = 'assessment.assignment'::regclass
  ) THEN
    ALTER TABLE assessment.assignment
      ADD CONSTRAINT assignment_withdrawn_fields
      CHECK (
        (status = 'withdrawn') = (withdrawn_at IS NOT NULL)
        AND (status <> 'withdrawn' OR (withdrawal_reason IS NOT NULL
                                       AND length(btrim(withdrawal_reason)) > 0))
      );
  END IF;
END $$;

-- 发布终检按课程串行化 + 作业列表按课程查，这是唯一的两个访问方向
CREATE INDEX IF NOT EXISTS assignment_course_status_idx
    ON assessment.assignment (course_id, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- 二、assignment_item：题族 id
--
-- 曝光判定按"族"而不是按版本：同一道题换了个版本还是同一道题，答案公开过一次
-- 就再也收不回来。0027 的 item 表只钉了 version，缺 family，发布终检与撤回写
-- 曝光事件都要用到它。
-- ---------------------------------------------------------------------------

ALTER TABLE assessment.assignment_item
  ADD COLUMN IF NOT EXISTS exercise_family_id uuid;

COMMENT ON COLUMN assessment.assignment_item.exercise_family_id IS
  '曝光与去重按题族判定（同族=同一道题的不同版本）';

CREATE INDEX IF NOT EXISTS assignment_item_family_idx
    ON assessment.assignment_item (exercise_family_id);

COMMIT;
