"""`AssemblyService`：启动运行、查询回放、选择方案落卷。

三条状态纪律（违反任何一条都会让"没解也能出卷"变可能）：

1. **没有方案就没有卷子。** `INFEASIBLE / TIMEOUT(无解) / FAILED` 的运行没有
   `solution_manifest`，`select` 直接拒绝——不是"按钮置灰"，是服务端不认。
2. **可行 ≠ 最优。** 方案里带着 `solver_status` 与 `optimal_proven`；备选方案
   只标 `FEASIBLE`。这条信息会一路显示到教师界面，不做"统一叫可用"的简化。
3. **终态不改写。** 同一运行只能从 queued/running 走到终态一次；重跑必须新开一行，
   否则回放读到的历史会被覆盖。

授权：启动与查询都先走阶段 A 的 `BlueprintService.get_version`（它用真实的
`course_ref` 判课程范围）。本服务不复制授权规则，也不接受调用方自报范围。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from ..releases import utcnow
from .blueprint import blueprint_snapshot
from .candidate import AssemblyCandidateService, StageACandidateSource
from .contracts import (
    SOLVED_STATUSES,
    AssemblyStartResult,
    BlueprintSnapshot,
    CandidateManifest,
    PaperDraft,
    RunStatus,
    SolutionManifest,
    SolverConfig,
    SolveResult,
    candidate_manifest_from_json,
    solution_from_json,
    solution_hash_matches,
    solution_to_json,
    to_json_value,
    verify_candidate_manifest,
)
from .diagnostics import diagnose_infeasible, issues_to_payload, precheck
from .materialize import AssemblyMaterializer, AssemblyValidationBlocked, MaterializeError
from .repository import AssemblyRunRow


class AssemblyError(RuntimeError):
    """组卷域错误基类（API 层按 code 映射 HTTP 状态）。"""

    code = "assembly_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunNotFound(AssemblyError):
    code = "assembly_run_not_found"


class RunNotSolved(AssemblyError):
    code = "assembly_run_not_solved"


class SolutionIndexInvalid(AssemblyError):
    code = "solution_index_invalid"


class SolutionMismatch(AssemblyError):
    code = "solution_manifest_mismatch"


class IdempotencyConflict(AssemblyError):
    code = "idempotency_conflict"


TERMINAL_STATUSES = frozenset({
    RunStatus.OPTIMAL, RunStatus.FEASIBLE, RunStatus.INFEASIBLE,
    RunStatus.TIMEOUT, RunStatus.FAILED, RunStatus.CANCELLED,
})


def _key(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned or len(cleaned) > 200:
        raise AssemblyError("Idempotency-Key 不能为空且不超过 200 字符")
    return cleaned


def _with(row: AssemblyRunRow, **changes: Any) -> AssemblyRunRow:
    """`AssemblyRunRow` 是 pydantic 模型：用 `model_copy` 而不是 dataclasses.replace。"""
    return row.model_copy(update=changes)


class AssemblyService:
    def __init__(
        self,
        *,
        repository: Any,
        solver: Any,
        blueprints: Any,
        candidates: Any = None,
        candidate_service: AssemblyCandidateService | None = None,
        materializer: AssemblyMaterializer | None = None,
        enqueuer: Callable[[UUID, str], int | None] | None = None,
        solver_config: SolverConfig | None = None,
        solver_name: str = "cp-sat",
        solver_version: str = "",
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.solver = solver
        self.blueprints = blueprints
        self.materializer = materializer
        self.enqueuer = enqueuer
        self.solver_config = solver_config or SolverConfig()
        self.solver_name = solver_name
        self.solver_version = solver_version
        self._clock = clock
        self._new_id = id_factory
        self.candidate_service = candidate_service or AssemblyCandidateService(
            source=StageACandidateSource(candidates=candidates, versions=blueprints),
            clock=clock,
        )

    # -- 启动 ---------------------------------------------------------------
    def start(self, *, actor_id: UUID, blueprint_version_id: UUID,
              idempotency_key: str, enqueue_only: bool = False) -> AssemblyStartResult:
        """启动一次求解；同一 (教师, 键) 重试返回同一运行。

        `enqueue_only=True` 时只建运行行并入队 `paper-assembly` 任务，**不同步求解**：
        1,000 候选/50 槽位的求解会超过任何合理的 HTTP 超时，此时把结论交给 worker。
        入队失败不吞：运行会停在 queued 且没有 job_id，教师看得到"排队中但没人领"，
        比假装成功好。
        """
        key = _key(idempotency_key)
        blueprint = self._load_blueprint(actor_id, blueprint_version_id)
        with self.repository.unit_of_work() as uow:
            replay = uow.find_idempotent(actor_id, key)
            if replay is not None:
                if replay.blueprint_version_id != blueprint_version_id:
                    # 同一把键换了蓝图：这是两个意图，不能合并成一次运行。
                    raise IdempotencyConflict(
                        "同一 Idempotency-Key 被用于不同的蓝图版本")
                return AssemblyStartResult(
                    assembly_run_id=replay.id, status=replay.status, reused=True)

            manifest = self.candidate_service.snapshot(
                actor_id=actor_id, blueprint=blueprint)
            if enqueue_only:
                return self._start_queued(uow, actor_id, blueprint, manifest, key)
            run_id = self._new_id()
            started_at = self._clock()
            row = AssemblyRunRow(
                id=run_id,
                blueprint_version_id=blueprint_version_id,
                actor_id=actor_id,
                idempotency_key=key,
                candidate_manifest=to_json_value(manifest),
                candidate_manifest_hash=manifest.manifest_hash,
                candidate_snapshot_id=self._snapshot_id(manifest),
                solver_name=self.solver_name,
                solver_version=self.solver_version,
                solver_config=to_json_value(self.solver_config),
                status=RunStatus.RUNNING,
                started_at=started_at,
                created_at=started_at,
            )
            row = uow.add(row)
            if row.id != run_id:
                # 并发重试撞上唯一键：另一路已经在跑，直接复用那一行。
                return AssemblyStartResult(
                    assembly_run_id=row.id, status=row.status, reused=True)
            finished = self._solve(blueprint, manifest, row)
            uow.update(finished)
            return AssemblyStartResult(
                assembly_run_id=finished.id, status=finished.status, reused=False)

    def _start_queued(self, uow: Any, actor_id: UUID, blueprint: BlueprintSnapshot,
                      manifest: CandidateManifest, key: str) -> AssemblyStartResult:
        """建 queued 行 + 入队 job；这条路径不碰求解器。"""
        if self.enqueuer is None:
            # 没有队列端口时不能假装"排队中"：那会让教师一直等一个没人领的任务。
            raise AssemblyError(
                "缺少队列端口：enqueue_only 需要能写入 pipeline_job")
        run_id = self._new_id()
        now = self._clock()
        row = AssemblyRunRow(
            id=run_id,
            blueprint_version_id=blueprint.blueprint_version_id,
            actor_id=actor_id,
            idempotency_key=key,
            candidate_manifest=to_json_value(manifest),
            candidate_manifest_hash=manifest.manifest_hash,
            candidate_snapshot_id=self._snapshot_id(manifest),
            solver_name=self.solver_name,
            solver_version=self.solver_version,
            solver_config=to_json_value(self.solver_config),
            status=RunStatus.QUEUED,
            created_at=now,
        )
        stored = uow.add(row)
        if stored.id != run_id:
            return AssemblyStartResult(
                assembly_run_id=stored.id, status=stored.status, reused=True)
        job_id = self.enqueuer(run_id, str(actor_id))
        if job_id is not None:
            stored = uow.update(_with(stored, job_id=job_id))
        return AssemblyStartResult(
            assembly_run_id=stored.id, status=stored.status, reused=False)

    def _solve(self, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
               row: AssemblyRunRow) -> AssemblyRunRow:
        """调求解器并落结果；预检已证无解时不浪费一次求解。"""
        proved = precheck(blueprint, manifest)
        if proved:
            return _with(
                row,
                status=RunStatus.INFEASIBLE,
                solution_manifest=(),
                diagnostics={
                    **diagnose_infeasible(blueprint, manifest),
                    "precheck_codes": [item.code for item in proved],
                },
                completed_at=self._clock(),
            )
        try:
            result = self.solver.solve(blueprint, manifest, self.solver_config)
        except Exception as error:  # noqa: BLE001 — 求解器异常一律落成 FAILED
            # 不能把异常往上抛成 500：教师需要看到"这次运行失败了、为什么"，
            # 而不是一个没有运行记录的崩溃。
            return _with(
                row,
                status=RunStatus.FAILED,
                solution_manifest=(),
                diagnostics={
                    "reason": "solver_error",
                    "detail": f"{type(error).__name__}: {error}",
                },
                completed_at=self._clock(),
            )
        return self._finish(blueprint, manifest, row, result)

    def _finish(self, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
                row: AssemblyRunRow, result: SolveResult) -> AssemblyRunRow:
        solutions = tuple(
            solution_to_json(variant)
            for solution_set in result.solution_sets
            for variant in solution_set.variants
        )
        if result.status in SOLVED_STATUSES and not solutions:
            # 求解器说"有解"却没给方案：这是求解器/适配层的错，不是业务结论。
            raise AssemblyError(
                f"求解器返回 {result.status} 但没有方案：拒绝记录一个空解")
        if result.status in SOLVED_STATUSES:
            issues = precheck(blueprint, manifest)
            diagnostics = {
                **dict(result.diagnostics or {}),
                "precheck_codes": [item.code for item in issues],
                "issues": issues_to_payload(issues),
            }
        else:
            diagnostics = diagnose_infeasible(blueprint, manifest, result.diagnostics)
        return _with(
            row,
            status=result.status,
            solution_manifest=solutions,
            objective_breakdown={
                "variants": [
                    {"variant_label": variant.variant_label,
                     "objective_value": variant.objective_value,
                     "terms": [term.name for term in variant.objective_terms]}
                    for solution_set in result.solution_sets
                    for variant in solution_set.variants
                ],
            },
            diagnostics=diagnostics,
            best_bound=result.best_bound,
            optimality_gap_milli=result.optimality_gap_milli,
            completed_at=self._clock(),
        )

    # -- worker 路径 ---------------------------------------------------------
    def execute(self, *, run_id: UUID) -> AssemblyRunRow:
        """执行一次**已经入队**的运行（worker 调用）。

        幂等：运行已是终态就不再求解，直接返回当前行。worker 会因超时回收、部署重启
        被重复领取同一个 job，重复求解既浪费又可能让结论漂移（换 seed 就是另一份方案）。

        求解输入必须是**运行行里固定的那份候选清单**，不是重新取数：这次运行的语义
        是"用那一刻的候选求解"，重取会让同一个 job 在重试后换一套输入。

        重新取数只用于**再确认一次资格**（题目可能已被撤回），结论是"候选全失效"
        时直接记 INFEASIBLE 并留下被拒原因，而不是拿失效的题硬解。
        """
        with self.repository.unit_of_work() as uow:
            row = uow.get(run_id)
            if row is None:
                raise RunNotFound(f"求解运行不存在：{run_id}")
            if row.status in TERMINAL_STATUSES:
                return row
            blueprint = self._load_blueprint(row.actor_id, row.blueprint_version_id)
            manifest = candidate_manifest_from_json(
                dict(row.candidate_manifest),
                manifest_hash=row.candidate_manifest_hash,
                candidate_snapshot_id=row.candidate_snapshot_id,
            )
            if not verify_candidate_manifest(blueprint, manifest):
                # 库里的清单被改过：宁可失败，也不拿一份说不清来路的题去求解。
                failed = _with(row, status=RunStatus.FAILED, diagnostics={
                    "reason": "candidate_manifest_tampered",
                    "detail": "运行行里的候选清单与记录的 manifest_hash 不一致",
                }, completed_at=self._clock())
                uow.update(failed)
                return failed
            finished = self._solve(blueprint, manifest, row)
            try:
                return uow.update(finished)
            except ValueError:
                # 另一个 worker 先写了终态：以它为准，历史只追加不覆盖。
                return uow.get(run_id) or finished

    # -- 查询 ---------------------------------------------------------------
    def run(self, *, actor_id: UUID, run_id: UUID) -> AssemblyRunRow:
        row = self._load(actor_id, run_id)
        return row

    def latest(self, *, actor_id: UUID, blueprint_version_id: UUID,
               limit: int = 20) -> list[AssemblyRunRow]:
        self._load_blueprint(actor_id, blueprint_version_id)
        with self.repository.unit_of_work() as uow:
            return uow.latest_for_blueprint(blueprint_version_id, limit)

    # -- 选择方案落卷 -------------------------------------------------------
    def select(self, *, actor_id: UUID, run_id: UUID, index: int,
               idempotency_key: str) -> PaperDraft:
        key = _key(idempotency_key)
        row = self._load(actor_id, run_id)
        if row.status not in SOLVED_STATUSES or not row.solution_manifest:
            raise RunNotSolved(
                f"本次运行状态是 {row.status}，没有可选方案；不能生成卷子")
        if not 0 <= index < len(row.solution_manifest):
            raise SolutionIndexInvalid(f"方案序号 {index} 越界（共 {len(row.solution_manifest)} 个）")
        if row.candidate_snapshot_id is None:
            # 建卷必须引用阶段 A 的候选快照（paper_version.candidate_snapshot_id NOT NULL）
            raise MaterializeError("这次运行没有关联候选快照，无法建卷（重跑一次运行）")
        if self.materializer is None:
            raise AssemblyError("缺少落卷端口：拒绝在无卷面服务的情况下建卷")
        blueprint = self._load_blueprint(actor_id, row.blueprint_version_id)
        solution = solution_from_json(row.solution_manifest[index])
        self._verify_solution(row, index, solution)
        try:
            materialized = self.materializer.materialize(
                actor_id=actor_id,
                blueprint=blueprint,
                solution=solution,
                candidate_snapshot_id=row.candidate_snapshot_id,
                assembly_run_id=row.id,
                selected_solution_index=index,
                idempotency_key=f"{key}:paper",
            )
        except AssemblyValidationBlocked as blocked:
            self._record_validation_failure(row, blocked)
            raise
        self._record_validation_ok(row, materialized)
        return materialized.as_draft()

    # -- 内部 ---------------------------------------------------------------
    def _load_blueprint(self, actor_id: UUID, blueprint_version_id: UUID) -> BlueprintSnapshot:
        """读蓝图（授权发生在阶段 A 的服务里）并翻译；翻译失败不当业务成功。"""
        version = self.blueprints.get_version(
            actor_id=actor_id, version_id=blueprint_version_id)
        return blueprint_snapshot(version)

    def _load(self, actor_id: UUID, run_id: UUID) -> AssemblyRunRow:
        with self.repository.unit_of_work() as uow:
            row = uow.get(run_id)
        if row is None:
            raise RunNotFound(f"求解运行不存在：{run_id}")
        # 读别人的运行 = 看别的课程考什么，必须过课程范围判定。
        self._load_blueprint(actor_id, row.blueprint_version_id)
        return row

    @staticmethod
    def _snapshot_id(manifest: CandidateManifest) -> UUID | None:
        return manifest.candidate_snapshot_id

    @staticmethod
    def _verify_solution(row: AssemblyRunRow, index: int,
                         solution: SolutionManifest) -> None:
        """回放完整性：从方案内容重算哈希，必须等于记录值。

        只比对"载荷里写的 manifest_hash"是无效校验——篡改的人会把它一起改掉。
        重算才发现得了：改一道题、改一个分值，哈希就不一样了。
        """
        if not solution_hash_matches(solution):
            raise SolutionMismatch(
                "方案载荷的内容哈希与其记录的 manifest_hash 不一致：方案被改过，拒绝建卷")
        recorded = row.solution_manifest[index].get("manifest_hash")
        if recorded and recorded != solution.manifest_hash:
            raise SolutionMismatch("方案载荷与运行记录里的 manifest_hash 不一致")

    def _record_validation_ok(self, row: AssemblyRunRow, materialized: Any) -> None:
        diagnostics = dict(row.diagnostics)
        diagnostics["validation"] = {
            "valid": True,
            "paper_version_id": str(materialized.paper_version_id),
            "manifest_hash": materialized.manifest_hash,
            "issues": list(materialized.issues),
        }
        # 只写诊断：运行已经解出，不该因为"复验通过"再写一次状态。
        with self.repository.unit_of_work() as uow:
            uow.record_diagnostics(row.id, diagnostics)

    def _record_validation_failure(self, row: AssemblyRunRow,
                                   blocked: AssemblyValidationBlocked) -> None:
        """复验失败：运行标 FAILED（不让它继续被选），卷子留在草稿。"""
        diagnostics = dict(row.diagnostics)
        diagnostics["validation"] = {
            "valid": False,
            "paper_version_id": str(blocked.paper_version_id),
            "issues": list(blocked.issues),
        }
        failed = _with(
            row,
            status=RunStatus.FAILED,
            diagnostics=diagnostics,
            completed_at=self._clock(),
        )
        with self.repository.unit_of_work() as uow:
            uow.update(failed)


__all__ = [
    "AssemblyError",
    "AssemblyService",
    "IdempotencyConflict",
    "RunNotFound",
    "RunNotSolved",
    "SolutionIndexInvalid",
    "SolutionMismatch",
]
