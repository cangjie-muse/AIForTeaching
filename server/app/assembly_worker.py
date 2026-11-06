"""约束组卷的 worker 入口：领取 `paper-assembly` 任务，执行一次已入队的求解运行。

为什么要走队列而不是在 HTTP 请求里同步跑完：

  * 一次 CP-SAT 求解几十秒起步，1,000 候选/50 槽位会更久，放在请求里必然超时；
  * 改成"后台线程"就失去了重试、心跳与失败可见性——进程重启后连"有没有在跑"都
    查不到；
  * `pipeline_job` 已经有状态机、心跳超时回收、重试上限与进度轮询，直接复用。

**幂等**在这一层是硬要求：worker 会因超时被回收、因部署被重启，同一个
`assembly_run_id` 的 job 可能被领两次。所以 `run_job` 只做三件事：

    1. 从 payload 取 run_id（缺了就是载荷错误，不能猜）；
    2. 调 `AssemblyService.execute(run_id)`——它自己保证"终态不再重解"；
    3. 返回可序列化的小摘要（job.result 会入库并在前端展示，塞整份方案会让行变成
       几 MB，也会把"方案正文"泄露到队列表里）。

求解器不可用时（本机没装 ortools），`execute` 会把运行记成 `failed` 并写诊断，
`run_job` 如实返回 `status=failed`。**不会**抛异常让 worker 一直重试，也不会
伪造一个方案：重试一个必然失败的求解只会把队列拖住。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .assembly.repository import PostgresAssemblyRunRepository
from .assembly.service import AssemblyService
from .assembly.solver_port import default_solver, ortools_version
from .blueprints import BlueprintService, CourseScopeAuthorizer, PostgresBlueprintRepository
from .candidates import CandidateService, PostgresCandidateRepository


class AssemblyJobError(RuntimeError):
    """job 载荷不合法（不是求解失败）。"""


def run_job(job: Any) -> dict[str, Any]:
    payload = dict((job.get("payload") if hasattr(job, "get") else job["payload"]) or {})
    raw_run_id = payload.get("assembly_run_id")
    if not raw_run_id:
        raise AssemblyJobError("paper-assembly 任务缺少 assembly_run_id")
    run_id = UUID(str(raw_run_id))
    service = build_service()
    row = service.execute(run_id=run_id)
    return {
        "assembly_run_id": str(row.id),
        "status": str(row.status),
        "solutions": len(row.solution_manifest),
        # 只有**全部**方案都被证明最优才允许说 True；备选方案不继承最优性。
        "optimal_proven": bool(row.solution_manifest) and all(
            bool(item.get("optimal_proven")) for item in row.solution_manifest),
        "solver": "cp-sat" if ortools_version() else "unavailable",
        "diagnostics_reason": (row.diagnostics or {}).get("reason"),
    }


def build_service() -> AssemblyService:
    """worker 侧装配：真实仓储 + 默认真求解器；不需要落卷端口（那是教师的动作）。

    授权端口用 `CourseScopeAuthorizer` 包一个"服务账号视图"：worker 不是人，但它
    代表的人是运行发起人（`row.actor_id`），所以课程范围仍按发起人判定——worker
    不会因为"是后台任务"就获得跨课程读取的能力。
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


class _WorkerActor:
    """服务账号的授权视图：只有 `id` 与 `roles`，不持有任何人类身份。"""

    id = "service:assembly-worker"
    roles = frozenset({"teacher"})

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return self.id


__all__ = ["AssemblyJobError", "build_service", "run_job"]
