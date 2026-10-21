-- 0008：整本书生成——按章拆子任务，失败只重跑那一章
--
-- 0007 让"一章"能跑。老师真正要的是上传一个 PDF 就完事，所以这一版把一本书
-- 拆成每章一个子任务：父任务负责切章与汇总进度，子任务各跑一次模型调用。
--
-- 关键约束：同一本书的同一章，不能因为重跑父任务就再建一个子任务——
-- 那会把同一章的钱和审核工作量翻倍。用 (parent_id, chapter_key) 唯一索引顶住，
-- 重跑父任务只会把失败的子任务重新排队。
--
-- 为什么把 chapter_key 存成列而不是塞进 payload：唯一索引要能建在列上，
-- 而且运维查"这一章跑了几次"时不用解 JSON。

BEGIN;

ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book'));

ALTER TABLE pipeline_job
  ADD COLUMN chapter_key   text,   -- ch-3 / part-2，父任务内唯一
  ADD COLUMN chapter_title text;   -- 章标题，展示用

CREATE UNIQUE INDEX pipeline_job_chapter_uniq
  ON pipeline_job (parent_id, chapter_key)
  WHERE parent_id IS NOT NULL AND chapter_key IS NOT NULL;

CREATE INDEX pipeline_job_parent_idx ON pipeline_job (parent_id);

COMMIT;
