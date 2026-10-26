-- 任务心跳：worker 跑任务时定时刷新，用来区分「真的在跑」和「进程早没了」。
--
-- 为什么需要：任务被领走后状态就是 running，只有跑完才会变。worker 被重启、
-- 被杀、或者某次模型调用一直挂着，这一行就永远停在 running —— 前端转圈，
-- 队列里 queued = 0，health 看着还"正常"。2026-10-26 那次两个任务卡 48 分钟
-- 就是这么来的，除了干等没有别的办法。
--
-- heartbeat_at 由 worker 每 15 秒刷一次；超过 STALE 秒没刷就说明跑它的进程
-- 已经不在了，可以安全地重新排队。

ALTER TABLE pipeline_job ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;

-- 查"哪些 running 已经没人管"是每次 worker 启动和巡检都要做的事，给它个索引
CREATE INDEX IF NOT EXISTS pipeline_job_running_heartbeat_idx
    ON pipeline_job (heartbeat_at)
    WHERE status = 'running';
