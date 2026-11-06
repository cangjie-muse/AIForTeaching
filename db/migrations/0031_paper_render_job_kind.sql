-- 0031: 让 pipeline_job 认识 paper-render
--
-- 打印卷的三类产物（学生卷 / 答案卷 / 量规卷）走同一套队列：入队 → worker 领取 →
-- 心跳 → 重试 → 落 artifact。不新起第二条流水线，是因为「任务可观测」这件事已经
-- 在 pipeline_job 上做完了：状态机、心跳超时回收（JOB_STALE_SECONDS）、重试上限
-- （JOB_MAX_ATTEMPTS）、前端进度轮询，抄一遍就多一处会腐烂的实现。
--
-- 与 0007 / 0008 / 0024 / 0025 同样的做法：先 DROP IF EXISTS 再加，重复执行不报错。
-- kind 的合法集合必须与 server/app/generation.py 的 claim_next() 白名单一致，
-- 否则任务能入队却永远没人领。

ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book','grade-homework',
                  'courseware-generate','courseware-revise','paper-render'));
