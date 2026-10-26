"""`/api/v1/health` 里 queue.worker_alive 的判活规则。

为什么值得单独测：这条信号用来回答"点了没反应，是模型慢还是没人干"，
判错会把人引到错误的方向。线上已经错过一次：
课件是一个任务跑十几分钟（逐节调模型），用"上次取任务距今多久 < 600 秒"判活，
跑到第 11 分钟就会翻成 worker_alive=false，看起来像 worker 死了，其实它正忙着。

规则：
- 有任务在跑（running > 0）→ 活着，这是最强证据；
- 队列空且没有任务在跑 → None，无从判断，不编结论；
- 有任务排队但没人取（queued > 0、running = 0）→ 看距上次取任务多久。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import db, main  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}")
    else:
        failures.append(name)
        print(f"  ✗ {name}{('：' + detail) if detail else ''}")


def health(queued: int, running: int, since_seconds: float | None,
           stale: int = 0, beat_age: float | None = 5) -> dict:
    """用假数据跑一遍 _queue_health，不动真库。"""

    def fake_query_one(sql: str, params: tuple | dict = ()) -> dict:
        return {
            "queued": queued,
            "running": running,
            "failed_24h": 0,
            "last_claim": (None if since_seconds is None else
                           datetime.now(timezone.utc) - timedelta(seconds=since_seconds)),
            "oldest_age_seconds": 100 if queued else None,
            "since_claim_seconds": since_seconds,
            "stale_running": stale,
            "heartbeat_age_seconds": beat_age,
        }

    original = db.query_one
    db.query_one = fake_query_one  # type: ignore[assignment]
    try:
        return main._queue_health()
    finally:
        db.query_one = original  # type: ignore[assignment]


def main_() -> int:
    print("有任务在跑")
    result = health(queued=0, running=1, since_seconds=4000)
    check("跑了 66 分钟仍在跑也判活着", result["worker_alive"] is True, str(result["worker_alive"]))
    check("running 如实返回", result["running"] == 1)

    print("running 但心跳停了")
    result = health(queued=0, running=2, since_seconds=3000, stale=2, beat_age=2800)
    check("心跳全停就不再算活着", result["worker_alive"] is False, str(result["worker_alive"]))
    check("如实报出僵尸任务数", result["stale_running"] == 2, str(result["stale_running"]))
    check("报出心跳多久没刷", result["worker_heartbeat_age_seconds"] == 2800,
          str(result["worker_heartbeat_age_seconds"]))
    check("两个任务里只有一个僵住 → 仍算活着",
          health(queued=0, running=2, since_seconds=3000, stale=1)["worker_alive"] is True)
    check("僵尸任务数为 0 时状态字段照旧", health(0, 1, 60)["stale_running"] == 0)

    print("有积压、没人取")
    check("刚取过任务 → 活着", health(3, 0, 30)["worker_alive"] is True)
    check("10 分钟内取过 → 活着", health(3, 0, 599)["worker_alive"] is True)
    check("超过 10 分钟没人取 → 判死", health(3, 0, 601)["worker_alive"] is False)
    check("从来没有取过（since 为空）→ 判死",
          health(3, 0, None)["worker_alive"] is False)

    print("队列是空的")
    check("空队列 → None，不编结论", health(0, 0, None)["worker_alive"] is None)
    check("空队列但有历史取过 → None", health(0, 0, 30)["worker_alive"] is None)

    print("字段完整性")
    result = health(2, 1, 45)
    for key in ("queued", "running", "failed_24h", "last_claim",
                "since_last_claim_seconds", "oldest_queued_seconds", "worker_alive"):
        check(f"包含 {key}", key in result)
    check("last_claim 是 ISO 字符串", isinstance(result["last_claim"], str))

    print()
    if failures:
        print(f"✗ {len(failures)} 项未通过：" + "、".join(failures))
        return 1
    print("✓ 队列判活测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
