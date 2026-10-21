#!/usr/bin/env python3
"""生成任务 worker：领取 pipeline_job，调用模型，写资源包，入库。

为什么要独立进程而不是在 HTTP 请求里跑：一章就是一次模型调用（几十秒到几分钟），
整本书十章更久。塞进请求里必然超时，然后被迫改成"先返回、后台线程跑"，
再然后就没有重试、没有进度、不知道卡在哪。

用法：
    python scripts/run_worker.py --once      # 跑一个任务就退出（测试/手动）
    python scripts/run_worker.py             # 常驻，每 5 秒看一眼队列
    python scripts/run_worker.py --job 12    # 只跑指定任务

领取用 `FOR UPDATE SKIP LOCKED`：开两个 worker 不会抢同一个任务。
失败不退出：错误写进 job.error，继续下一个——否则一个坏任务会卡住整个队列。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import db, generation  # noqa: E402


def describe(job: dict) -> str:
    if job["status"] == "succeeded":
        result = job.get("result") or {}
        return (f"成功：{result.get('nodes', 0)} 个知识点、"
                f"{result.get('exercises', 0)} 道题 → {result.get('pack_dir')}")
    return f"失败：{job.get('error')}"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成任务 worker")
    parser.add_argument("--once", action="store_true", help="跑一个任务就退出")
    parser.add_argument("--job", type=int, default=None, help="只跑指定任务 id")
    parser.add_argument("--interval", type=float, default=5.0, help="空闲时的轮询间隔（秒）")
    args = parser.parse_args()

    os.environ.setdefault("APP_ENV", "dev")
    db.pool.open()

    if args.job:
        job = generation.run_job(args.job)
        print(f"[任务 {job['id']}] {describe(job)}")
        return 0 if job["status"] == "succeeded" else 1

    while True:
        job = generation.process_once()
        if job:
            print(f"[任务 {job['id']}] {describe(job)}", flush=True)
        elif args.once:
            print("队列里没有待处理任务。")
            return 0
        else:
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
