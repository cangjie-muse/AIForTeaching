-- 0035 · 让 pipeline_job 认识 paper-assembly（约束组卷的求解运行）
--
-- 对应文档：
--   docs/superpowers/plans/2026-11-05-paper-02-constrained-assembly.md  任务 6
--   （"贯通 AssemblyService、API 与幂等 worker"）
--
-- 为什么求解要走 job 队列而不在请求里同步跑完：
--   一次 CP-SAT 求解在这台机器上几十秒起步，1,000 候选/50 槽位会更久。放在请求里
--   必然超时，然后被迫改成"后台线程"——再然后就没有重试、没有心跳、没有进度，
--   进程重启后连"有没有在跑"都查不到。`pipeline_job` 已经有状态机、心跳超时回收
--   （JOB_STALE_SECONDS）、重试上限（JOB_MAX_ATTEMPTS）与前端进度轮询，复制一份
--   就是再造一条迟早会腐烂的流水线。
--
-- 与 0031（paper-render）同一手法：先 DROP IF EXISTS 再加，重复执行不报错。
-- kind 的合法集合必须与 server/app/generation.py 的 claim_next() 名单一致，
-- 否则任务能入队却永远没人领——那比直接报错更难发现。
--
-- 本迁移只扩一个 CHECK，不动任何数据、不建表。

ALTER TABLE pipeline_job DROP CONSTRAINT IF EXISTS pipeline_job_kind_check;
ALTER TABLE pipeline_job
  ADD CONSTRAINT pipeline_job_kind_check
  CHECK (kind IN ('upload','probe','extract','chunk','anchor','embed','ocr',
                  'generate','generate-book','grade-homework',
                  'courseware-generate','courseware-revise','paper-render',
                  'paper-assembly'));
