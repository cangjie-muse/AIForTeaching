-- 0024：作业拍照批改
--
-- 原来的结构里，submission 必须挂在一个 assignment 上（教师布置、题号固定）。
-- 但现实里学生最常见的动作是"我把今天的作业拍下来，让它看看对不对"——没有布置、
-- 没有题号表，题目是拍完才被识别出来的。所以这一版把 submission 放开成两种来源：
--
--   有 assignment  ：班级作业，题号由 assignment_item.ord 决定（原样不动）
--   没有 assignment：自由作业，course_id 必填，题目由识别结果决定
--
-- 为什么不另建一张 homework 表：批改状态机、AI 初评分与教师确认分、审计全都在
-- submission/submission_item 上，另起一张表等于把"AI 初评不进成绩"这条规则抄两遍，
-- 早晚有一遍会失效。
--
-- 另外：作业批改是分钟级任务（一页一次视觉模型调用），必须走 pipeline_job，
-- 所以 kind 里补 'grade-homework'；视觉模型要能单独配一个端点，model_endpoint.kind
-- 里补 'vision'。

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. 任务类型：作业批改
-- -----------------------------------------------------------------------------
ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book','grade-homework'));

-- -----------------------------------------------------------------------------
-- 2. 模型端点：视觉
-- -----------------------------------------------------------------------------
-- 视觉能力要能单独指向某个模型：即使同一个供应商既给文本又给视觉，
-- 两件事的出错方式完全不同（转写错 vs 判分错），配置上也该分得开。
-- 没有单独配时，调用层回落到对话端点。
ALTER TABLE model_endpoint DROP CONSTRAINT IF EXISTS model_endpoint_kind_check;
ALTER TABLE model_endpoint
  ADD CONSTRAINT model_endpoint_kind_check
  CHECK (kind IN ('chat','embedding','rerank','vision'));

-- -----------------------------------------------------------------------------
-- 3. submission 放开：允许"学生自己拍照交上来的作业"
-- -----------------------------------------------------------------------------
ALTER TABLE submission ALTER COLUMN assignment_id DROP NOT NULL;
ALTER TABLE submission
  ADD COLUMN course_id  text REFERENCES course(id) ON DELETE CASCADE,
  ADD COLUMN title      text,                  -- 学生自己起的名字，可空
  ADD COLUMN page_count int NOT NULL DEFAULT 0,
  ADD COLUMN model      text,                  -- 这次批改用的视觉/文本模型
  ADD COLUMN error      text,                  -- 批改失败原因，学生能看到
  ADD COLUMN job_id     bigint;                -- 对应的 pipeline_job

-- 原来的唯一约束要求 (assignment_id, student_id) 全表唯一；
-- 自由作业 assignment_id 是 NULL，多条记录会被当成同一个 NULL 而互相冲突。
ALTER TABLE submission DROP CONSTRAINT IF EXISTS submission_assignment_id_student_id_key;
CREATE UNIQUE INDEX submission_assignment_student_uniq
  ON submission (assignment_id, student_id)
  WHERE assignment_id IS NOT NULL;
CREATE INDEX submission_student_idx ON submission (student_id, submitted_at DESC);
CREATE INDEX submission_course_idx  ON submission (course_id, submitted_at DESC);

-- 两种来源至少要有一个：既没布置也没课程，这批作业没法判分，也没处显示
ALTER TABLE submission ADD CONSTRAINT submission_needs_target
  CHECK (assignment_id IS NOT NULL OR course_id IS NOT NULL);

-- 批改失败要能说出来。原状态机只有"提交/已初评/教师确认/退回"，
-- 失败只能停在 submitted，学生看到的是"一直在批改中"。
ALTER TABLE submission DROP CONSTRAINT IF EXISTS submission_status_check;
ALTER TABLE submission
  ADD CONSTRAINT submission_status_check
  CHECK (status IN ('submitted','grading','ai-graded','teacher-confirmed','returned','failed'));

-- -----------------------------------------------------------------------------
-- 4. 作业页面图
-- -----------------------------------------------------------------------------
-- 一张图一行，而不是把图塞进 submission_item：一页纸上常有好几道题，
-- 图片属于页面，题目属于页面上的条目，两者是 1:N。
CREATE TABLE submission_page (
  submission_id uuid NOT NULL REFERENCES submission(id) ON DELETE CASCADE,
  page_no       int  NOT NULL CHECK (page_no BETWEEN 1 AND 20),
  sha256        text NOT NULL,
  bytes         int  NOT NULL,
  mime          text NOT NULL,
  transcript    jsonb,                        -- 视觉模型对这一页的原始输出，留证用
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (submission_id, page_no)
);

COMMENT ON COLUMN submission_page.transcript IS
  '视觉模型的原始输出。判分有争议时要能回看"当初识别成了什么"，只存最终分数是查不清的。';

-- -----------------------------------------------------------------------------
-- 5. submission_item 补"自由作业"需要的列
-- -----------------------------------------------------------------------------
-- 有 assignment 时题号来自 assignment_item；自由作业没有那张表，
-- 题号、题干、学生作答、匹配到的题库题都得自己记。
ALTER TABLE submission_item
  ADD COLUMN exercise_id      text,           -- 匹配到的题库题；空表示没匹配上
  ADD COLUMN page_no          int,
  ADD COLUMN question_no      text,           -- 卷面上的题号，如 "3(2)"
  ADD COLUMN question_md      text,           -- 识别出的题干
  ADD COLUMN student_answer_md text,          -- 识别出的学生作答（含步骤）
  ADD COLUMN confidence       numeric(3,2),   -- 模型自评置信度，低于 0.6 提醒教师看
  ADD COLUMN auto_comment     text;           -- AI 点评（一段话，指明第一处问题）

COMMENT ON COLUMN submission_item.confidence IS
  '模型自评置信度。它不是准确率，只用来排序——教师复核时先看低置信的那几题。';

COMMIT;
