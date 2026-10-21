-- 0007：教材上传 → 排队 → 生成资源包（第一章垂直切片）
--
-- 0002 已经把 pipeline_job 建好了（queued/running/succeeded/failed、progress、attempts、idem_key），
-- 但 kind 里没有 "generate"，也没有地方记"这个任务是哪门课、哪本教材的"。
-- 这一版补上，并把 PDF 抽出来的文本落到 textbook 上，避免每次生成都重新抽一遍。
--
-- 不做的事：不给 pipeline_job.course_id 加外键。课程表是导入产物，重建课程时
-- 外键会让历史任务连带删除——任务日志的价值恰恰在于课程被删之后还能看出发生过什么。

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. 生成任务
-- -----------------------------------------------------------------------------
ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr','generate'));

ALTER TABLE pipeline_job
  ADD COLUMN course_id    text,   -- 目标课程；为空表示"新建课程"
  ADD COLUMN textbook_id  text,   -- 输入教材
  ADD COLUMN requested_by text,   -- 谁点的生成，出了问题能找到人
  ADD COLUMN parent_id    bigint REFERENCES pipeline_job(id) ON DELETE SET NULL;

COMMENT ON COLUMN pipeline_job.parent_id IS
  '子任务指向父任务。整本书的生成会拆成每章一个子任务，失败只重跑那一章。';

CREATE INDEX pipeline_job_course_idx   ON pipeline_job (course_id, created_at DESC);
CREATE INDEX pipeline_job_textbook_idx ON pipeline_job (textbook_id);

-- -----------------------------------------------------------------------------
-- 2. 教材文本（抽一次就存下来）
-- -----------------------------------------------------------------------------
ALTER TABLE textbook
  ADD COLUMN text_path   text,    -- 抽取出的 Markdown，带 <!-- page N --> 页标记
  ADD COLUMN text_chars  int,
  ADD COLUMN extracted_by text,   -- 抽取引擎：pypdf / pdftotext / 手工
  ADD COLUMN extracted_at timestamptz;

COMMENT ON COLUMN textbook.text_path IS
  '抽取后的纯文本。PDF 解析库不是到处都装得上，抽好的文本要能复用，不能每次重抽。';

COMMIT;
