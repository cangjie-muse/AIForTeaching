"""约束组卷的 worker 入口：领取 `paper-assembly` 任务，执行一次已入队的求解运行。

为什么要走队列而不是在 HTTP 请求里同步跑完：

  * 一次 CP-SAT 求解几十秒起步，1,000 候选/50 槽位会更久，放在请求里必然超时；
  * 改成"后台线程"就失去了重试、心跳与失败可见性——进程重启后连"有没有在跑"都
    查不到；
  * `pipeline_job` 已经有状态机、心跳超时回收、重试上限与进度轮询，直接复用。

**必须写回 job 状态。** 这是本模块最容易漏的一步：`rendering.run_render_job` 会
把 `pipeline_job` 置成 succeeded/failed；如果这里不写，任务会一直停在 `running`，
心跳一篇一篇地刷、stale 回收再把它重排——队列看起来"有任务在跑"，其实什么都没发生。
所以 `run_job` 无论成败都 `_finish_job(...)`，与渲染任务同一约定。

**幂等**是硬要求：worker 会因超时被回收、因部署被重启，同一个 `assembly_run_id` 的
job 可能被领两次。幂等由 `AssemblyService.execute` 保证（终态不再重解），`run_job`
只负责取 run_id、调用、写回、返回小摘要。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from . import db
from .assembly.repository import PostgresAssemblyRunRepository
from .assembly.service import AssemblyService
from .assembly.solver_port import default_solver, ortools_version
from .blueprints import BlueprintService, CourseScopeAuthorizer, PostgresBlueprintRepository
from .candidates import CandidateService, PostgresCandidateRepository


class AssemblyJobError(RuntimeError):
    """job 载荷不合法（不是求解失败）。"""


def run_job(job: Any) -> dict[str, Any]:
    """执行一个 `paper-assembly` job；成功与失败都写回 `pipeline_job`。

    返回值只放小字段：`job.result` 会入库并在前端展示，塞进整份方案会让行变成几 MB，
    也等于把"方案正文"泄露到队列表里。
    """
    job_id = int(job["id"])
    payload = dict((job.get("payload") if hasattr(job, "get") else job["payload"]) or {})
    try:
        raw_run_id = payload.get("assembly_run_id")
        if not raw_run_id:
            raise AssemblyJobError("paper-assembly 任务缺少 assembly_run_id")
        run_id = UUID(str(raw_run_id))
        _step(job_id, 20)
        row = build_service().execute(run_id=run_id)
        summary = {
            "assembly_run_id": str(row.id),
            "status": str(row.status),
            "solutions": len(row.solution_manifest),
            # 只有**全部**方案都被证明最优才允许说 True：备选不继承最优性。
            "optimal_proven": bool(row.solution_manifest) and all(
                bool(item.get("optimal_proven")) for item in row.solution_manifest),
            "solver": "cp-sat" if ortools_version() else "unavailable",
            "diagnostics_reason": (row.diagnostics or {}).get("reason"),
        }
        # 求解结论是 failed/infeasible 时，**job 本身**算成功执行：求解器正常给出了
        # "无解"这个结论。把它记成 job 失败会让 worker 反复重试一个已经确定的结论。
        _finish_job(job_id, "succeeded", result=summary)
        return {"id": job_id, "status": "succeeded", "result": summary, "error": None}
    except Exception as error:  # noqa: BLE001 — 任何失败都要落进任务记录
        message = f"{type(error).__name__}: {error}"
        print(f"[任务 {job_id}] 组卷求解失败：{message}", flush=True)
        _finish_job(job_id, "failed", error=message)
        return {"id": job_id, "status": "failed", "result": None, "error": message}


def build_service() -> AssemblyService:
    """worker 侧装配：真实仓储 + 默认真求解器；不需要落卷端口（那是教师的动作）。

    授权端口用 `CourseScopeAuthorizer` 包一个"服务账号视图"：worker 不是人，但它
    代表的人是运行发起人（`row.actor_id`），课程范围仍按发起人判定——worker 不会
    因为"是后台任务"就获得跨课程读取的能力。
    """
    blueprints = BlueprintService(
        repository=PostgresBlueprintRepository(),
        authorizer=CourseScopeAuthorizer(_WorkerActor()),
    )
    candidates = CandidateService(
        repository=PostgresCandidateRepository(),
        authorizer=CourseScopeAuthorizer(_WorkerActor()),
    )
    return AssemblyService(
        repository=PostgresAssemblyRunRepository(),
        solver=default_solver(),
        blueprints=blueprints,
        candidates=candidates,
        solver_name="cp-sat" if ortools_version() else "unavailable",
        solver_version=ortools_version() or "unavailable",
    )


def _step(job_id: int, progress: int) -> None:
    """进度：前端靠它显示"在跑"，也顺便证明 worker 还活着。"""
    db.execute(
        "UPDATE pipeline_job SET progress = %s, heartbeat_at = now() WHERE id = %s",
        (progress, job_id),
    )


def _finish_job(job_id: int, status: str, *, result: dict | None = None,
                error: str | None = None) -> None:
    db.execute(
        """UPDATE pipeline_job
              SET status = %s, progress = %s, finished_at = now(),
                  result = %s::jsonb, error = %s
            WHERE id = %s""",
        (
            status,
            100 if status == "succeeded" else 0,
            json.dumps(result, ensure_ascii=False, default=str) if result else None,
            error,
            job_id,
        ),
    )


class _WorkerActor:
    """服务账号的授权视图：只有 `id` 与 `roles`，不持有任何人类身份。"""

    id = "service:assembly-worker"
    roles = frozenset({"teacher"})

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return self.id


__all__ = ["AssemblyJobError", "build_service", "run_job"]
