"""约束组卷 API（阶段 B）：启动运行、查询回放、选择方案落卷。

    POST /paper-blueprint-versions/{id}/assembly-runs        启动一次求解（需幂等键）
    GET  /paper-blueprint-versions/{id}/assembly-runs        该蓝图最近的运行（新→旧）
    GET  /assembly-runs/{run_id}                             读取一次运行（含诊断与方案）
    POST /assembly-runs/{run_id}/solutions/{index}/paper     选方案落草稿卷（需幂等键）

四条路由的语义都围绕"没有方案就没有卷子"：

* 启动接口返回的 `status` 可能是 `infeasible / failed`。**这不是错误码**——一次
  无解的运行是一次成功的运行，教师需要读到原因（`diagnostics.issues`），而不是
  一个 500。所以这些状态用 201 + 状态字段表达；只有请求本身不合法才是 4xx。
* `solutions/{index}/paper` 在没有方案时返回 409 `assembly_run_not_solved`，
  在复验不通过时返回 409 `assembly_validation_blocked` 并附 `issues`。两者都不
  返回一个"看起来成功"的草稿。

为什么不把运行放在 `paper-versions` 下：运行发生在**蓝图**层面（还没卷子），
把它挂在卷子路径下会让人以为必须先有卷子。

为什么 `select` 要幂等键：它实际会建一份卷子。网络超时后的重试若被当成第二次
选择，就会留下两份内容相同、来源相同的草稿。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from . import db
from .assembly.contracts import RunStatus, SolverConfig
from .assembly.diagnostics import candidate_supply, precheck
from .assembly.materialize import AssemblyMaterializer, AssemblyValidationBlocked
from .assembly.repository import AssemblyRunRow, PostgresAssemblyRunRepository
from .assembly.service import (
    AssemblyError,
    AssemblyService,
    IdempotencyConflict,
    RunNotFound,
    RunNotSolved,
    SolutionIndexInvalid,
    SolutionMismatch,
)
from .assembly.solver_port import default_solver, ortools_version
from .assembly.blueprint import BlueprintTranslationError
from .auth import Actor, require
from .blueprints import (
    BlueprintService,
    BlueprintVersionNotFoundError,
    CourseScopeAuthorizer,
    PostgresBlueprintRepository,
    UnknownCourseError,
    ref_uuid,
)
from .candidates import CandidateService, PostgresCandidateRepository
from .papers import PaperScopeAuthorizer, PostgresPaperRepository, PaperService
from .validation import PaperValidationService, PostgresPaperValidationRepository

router = APIRouter(prefix="/api/v1", tags=["assembly"])

require_teacher = require("teacher")
MAX_IDEMPOTENCY_KEY = 200


def _error(status_code: int, code: str, message: str,
           extra: dict[str, Any] | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key:
        raise _error(400, "idempotency_key_required",
                     "写接口必须带 Idempotency-Key 头，重试才不会跑出第二次求解")
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise _error(400, "idempotency_key_required",
                     f"Idempotency-Key 太长（上限 {MAX_IDEMPOTENCY_KEY} 字符）")
    return key


class SolverConfigIn(BaseModel):
    """求解配置：只允许整数，且方案数上限是 3（0034 与规格 §7.3 同一条）。"""

    model_config = ConfigDict(extra="forbid")

    random_seed: int = 20261105
    time_limit_seconds: int = 30
    max_solutions: int = 3


class AssemblyRunOut(BaseModel):
    """一次运行的对外视图。

    `optimal_proven` 与 `solver_status` 必须分别给：UI 要能说出"可行，未证明最优"，
    而不是把所有有方案的状态都显示成"已优化"。
    """

    assembly_run_id: str
    blueprint_version_id: str
    status: str
    solved: bool
    optimal_proven: bool
    candidate_manifest_hash: str
    candidate_snapshot_id: str | None
    solver_name: str
    solver_version: str
    solver_config: dict[str, Any]
    best_bound: int | None
    optimality_gap_milli: int | None
    diagnostics: dict[str, Any]
    objective_breakdown: dict[str, Any]
    solutions: list[dict[str, Any]]
    created_at: str
    started_at: str | None
    completed_at: str | None


def _run_payload(row: AssemblyRunRow) -> AssemblyRunOut:
    return AssemblyRunOut(
        assembly_run_id=str(row.id),
        blueprint_version_id=str(row.blueprint_version_id),
        status=str(row.status),
        solved=row.solved,
        optimal_proven=all(bool(item.get("optimal_proven")) for item in row.solution_manifest),
        candidate_manifest_hash=row.candidate_manifest_hash,
        candidate_snapshot_id=(
            None if row.candidate_snapshot_id is None else str(row.candidate_snapshot_id)),
        solver_name=row.solver_name,
        solver_version=row.solver_version,
        solver_config=dict(row.solver_config),
        best_bound=row.best_bound,
        optimality_gap_milli=row.optimality_gap_milli,
        diagnostics=dict(row.diagnostics),
        objective_breakdown=dict(row.objective_breakdown),
        solutions=list(row.solution_manifest),
        created_at=row.created_at.isoformat(),
        started_at=None if row.started_at is None else row.started_at.isoformat(),
        completed_at=None if row.completed_at is None else row.completed_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# 组合根
# ---------------------------------------------------------------------------

def get_blueprint_service(actor: Actor = Depends(require_teacher)) -> BlueprintService:
    return BlueprintService(
        repository=PostgresBlueprintRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


def get_assembly_candidate_service(
    actor: Actor = Depends(require_teacher),  # noqa: ARG001 - 依赖只为鉴权顺序
) -> CandidateService:
    return CandidateService(
        repository=PostgresCandidateRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


def get_assembly_service(
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    candidates: CandidateService = Depends(get_assembly_candidate_service),
) -> AssemblyService:
    """生产装配：真实仓储 + 阶段 A 的建卷与整卷校验端口。

    求解器走 `default_solver()`：本机没有 ortools 时它返回一个**明确失败**的实现
    （运行记 failed + 诊断写清缺什么），而不是假装解出方案。这条路径在
    `test_assembly_pipeline.py` 里被真实跑过（`UnavailableSolver`）。
    """
    papers = PaperService(
        repository=PostgresPaperRepository(),
        authorizer=PaperScopeAuthorizer(actor),
        blueprints=blueprints,
        candidates=candidates,
    )
    validator = PaperValidationService(
        repository=PostgresPaperValidationRepository(),
        candidates=candidates,
        authorizer=PaperScopeAuthorizer(actor),
    )
    version = ortools_version() or "unavailable"
    return AssemblyService(
        repository=PostgresAssemblyRunRepository(),
        solver=default_solver(),
        blueprints=blueprints,
        candidate_service=_assembly_candidate(blueprints, candidates),
        materializer=AssemblyMaterializer(papers=papers, validator=validator),
        solver_name="cp-sat" if ortools_version() else "unavailable",
        solver_version=version,
    )


def _assembly_candidate(blueprints: BlueprintService, candidates: CandidateService):
    from .assembly.candidate import AssemblyCandidateService, StageACandidateSource

    return AssemblyCandidateService(
        source=StageACandidateSource(candidates=candidates, versions=blueprints))


# ---------------------------------------------------------------------------
# 启动与查询
# ---------------------------------------------------------------------------

def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RunNotFound):
        return _error(404, "assembly_run_not_found", str(exc))
    if isinstance(exc, RunNotSolved):
        return _error(409, "assembly_run_not_solved", str(exc))
    if isinstance(exc, SolutionIndexInvalid):
        return _error(422, "solution_index_invalid", str(exc))
    if isinstance(exc, SolutionMismatch):
        return _error(409, "solution_manifest_mismatch", str(exc))
    if isinstance(exc, IdempotencyConflict):
        return _error(409, "idempotency_conflict", str(exc))
    if isinstance(exc, AssemblyValidationBlocked):
        return _error(409, "assembly_validation_blocked", str(exc),
                      {"paper_version_id": str(exc.paper_version_id),
                       "issues": list(exc.issues)})
    if isinstance(exc, AssemblyError):
        return _error(422, exc.code, str(exc))
    return _error(500, "assembly_failed", f"{type(exc).__name__}: {exc}")


def _map_build_error(exc: Exception) -> HTTPException:
    """启动路径上的非组卷错误（蓝图/候选/翻译）→ 稳定 HTTP 码。"""
    if isinstance(exc, BlueprintVersionNotFoundError):
        return _error(404, "blueprint_version_not_found", str(exc))
    if isinstance(exc, UnknownCourseError):
        return _error(404, "course_not_found", f"课程不存在：{exc}")
    if isinstance(exc, PermissionError):
        return _error(403, "course_scope_denied", str(exc))
    if isinstance(exc, BlueprintTranslationError):
        # 蓝图内容不能翻译成求解器输入属于"这份蓝图本身有问题"，不是服务器故障。
        return _error(422, "blueprint_not_solvable", str(exc))
    return _map_error(exc)


@router.post("/paper-blueprint-versions/{version_id}/assembly-runs", status_code=201)
def start_assembly_run(
    version_id: UUID,
    response: Response,
    body: SolverConfigIn | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: AssemblyService = Depends(get_assembly_service),
) -> dict[str, Any]:
    """启动一次求解。`202` 语义在这里不合适：运行**同步**跑完才返回（worker 化是下一步）。

    无解/失败也是 201：那是一次成功的运行，结论在 `status` 与 `diagnostics` 里。
    把无解写成 4xx 会让前端只能弹出"出错了"，而教师真正需要的是"哪一槽没有候选"。
    """
    key = _key(idempotency_key)
    config = body or SolverConfigIn()
    try:
        solver_config = SolverConfig(
            random_seed=config.random_seed,
            time_limit_seconds=config.time_limit_seconds,
            max_solutions=config.max_solutions,
        )
    except ValueError as exc:
        # 配置非法是**请求**的问题（422），不是服务器故障（500）。这条线要划清：
        # 前端把 422 当"改参数"，把 500 当"重试/找运维"。
        raise _error(422, "solver_config_invalid", str(exc)) from exc
    try:
        service.solver_config = solver_config
        started = service.start(
            actor_id=ref_uuid("account", actor.id),
            blueprint_version_id=version_id,
            idempotency_key=key,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_build_error(exc) from exc
    row = service.run(actor_id=ref_uuid("account", actor.id),
                      run_id=started.assembly_run_id)
    if started.reused:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return {**_run_payload(row).model_dump(mode="json"), "reused": started.reused}


@router.get("/paper-blueprint-versions/{version_id}/assembly-runs")
def list_assembly_runs(
    version_id: UUID,
    limit: int = 20,
    actor: Actor = Depends(require_teacher),
    service: AssemblyService = Depends(get_assembly_service),
) -> list[AssemblyRunOut]:
    if not 1 <= limit <= 100:
        raise _error(422, "limit_out_of_range", "limit 必须在 1..100 之间")
    try:
        rows = service.latest(
            actor_id=ref_uuid("account", actor.id),
            blueprint_version_id=version_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _map_build_error(exc) from exc
    return [_run_payload(row) for row in rows]


@router.get("/assembly-runs/{run_id}", response_model=AssemblyRunOut)
def get_assembly_run(
    run_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: AssemblyService = Depends(get_assembly_service),
) -> AssemblyRunOut:
    try:
        row = service.run(actor_id=ref_uuid("account", actor.id), run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_build_error(exc) from exc
    return _run_payload(row)


@router.get("/paper-blueprint-versions/{version_id}/candidate-supply")
def candidate_supply_for_blueprint(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    candidates: CandidateService = Depends(get_assembly_candidate_service),
) -> dict[str, Any]:
    """每个槽位有多少可选候选 + 预检问题（教师"为什么组不出来"的第一屏）。

    只读：不写运行、不写候选快照。它回答的是"现在这个库里，这份蓝图能凑出多少"，
    与一次具体求解无关——所以可以随便重算。
    """
    actor_id = ref_uuid("account", actor.id)
    try:
        version = blueprints.get_version(actor_id=actor_id, version_id=version_id)
        from .assembly.blueprint import blueprint_snapshot

        blueprint = blueprint_snapshot(version)
        manifest = _assembly_candidate(blueprints, candidates).snapshot(
            actor_id=actor_id, blueprint=blueprint)
    except Exception as exc:  # noqa: BLE001
        raise _map_build_error(exc) from exc
    issues = precheck(blueprint, manifest)
    return {
        "blueprint_version_id": str(version_id),
        "candidate_manifest_hash": manifest.manifest_hash,
        "candidates": len(manifest.candidates),
        "exclusions": [
            {"exercise_version_id": str(item.exercise_version_id), "reason": item.reason}
            for item in manifest.exclusions
        ],
        "supply": candidate_supply(blueprint, manifest),
        "issues": [
            {"code": item.code, "message": item.message, "slot_code": item.slot_code}
            for item in issues
        ],
        "solvable_precheck": not issues,
    }


@router.get("/assembly-runs/{run_id}/comparison")
def compare_solutions(
    run_id: UUID,
    left: int = 0,
    right: int = 1,
    anchors: str = "",
    actor: Actor = Depends(require_teacher),
    service: AssemblyService = Depends(get_assembly_service),
) -> dict[str, Any]:
    """对照一次运行里的两个方案（或将来 A/B 的两卷）。

    只回答结构问题：分值结构是否一致、除锚题外有没有共用题。**不回答"等值"**——
    `statistical_equivalence` 恒为 false，`note` 是固定措辞。这不是没做完，
    而是本系统没有等值设计，不该替教师做出它兑现不了的承诺。

    `anchors` 允许共用题目版本 id（逗号分隔）。没声明锚题时，两方案共用任何题目都
    会被判为重叠——因为"用了同一道题就自动算锚题"等于没有约束。
    """
    from .assembly.comparator import compare_structure, issues_payload
    from .assembly.contracts import solution_from_json

    actor_id = ref_uuid("account", actor.id)
    try:
        row = service.run(actor_id=actor_id, run_id=run_id)
        if not row.solution_manifest:
            raise RunNotSolved(f"这次运行状态是 {row.status}，没有方案可比较")
        for index in (left, right):
            if not 0 <= index < len(row.solution_manifest):
                raise SolutionIndexInvalid(
                    f"方案序号 {index} 越界（共 {len(row.solution_manifest)} 个）")
        blueprint = service._load_blueprint(actor_id, row.blueprint_version_id)  # noqa: SLF001
        anchors = tuple(
            UUID(item) for item in (anchors or "").split(",") if item.strip())
        comparison = compare_structure(
            blueprint,
            solution_from_json(row.solution_manifest[left]),
            solution_from_json(row.solution_manifest[right]),
            anchors=anchors,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_build_error(exc) from exc
    return {
        "assembly_run_id": str(run_id),
        "left": left,
        "right": right,
        "structurally_matched": comparison.structurally_matched,
        "statistical_equivalence": comparison.statistical_equivalence,
        "note": comparison.note,
        "issues": issues_payload(comparison),
        "slots": [
            {
                "slot_code": item.slot_code,
                "section_code": item.section_code,
                "score_hundredths": item.score_hundredths,
                "same_version": item.same_version,
                "same_family": item.same_family,
                "shared_and_allowed": item.shared_and_allowed,
                "difficulty_delta": item.difficulty_delta,
                "seconds_delta": item.seconds_delta,
            }
            for item in comparison.slots
        ],
        "shared_version_ids": [str(item) for item in comparison.shared_version_ids],
        "shared_family_ids": [str(item) for item in comparison.shared_family_ids],
        "anchors": [str(item) for item in comparison.anchors],
        "summary": comparison.extra,
    }


@router.post("/assembly-runs/{run_id}/solutions/{index}/paper", status_code=201)
def materialize_solution(
    run_id: UUID,
    index: int,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: AssemblyService = Depends(get_assembly_service),
) -> dict[str, Any]:
    """把第 `index` 个方案落成草稿卷，并立刻独立复验一次。"""
    key = _key(idempotency_key)
    try:
        draft = service.select(
            actor_id=ref_uuid("account", actor.id),
            run_id=run_id, index=index, idempotency_key=key)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc
    return {
        "paper_version_id": str(draft.paper_version_id),
        "manifest_hash": draft.manifest_hash,
        "status": draft.status,
        # 草稿不因为"来自求解器"就跳过审核：这句话要出现在响应里，前端才好提示。
        "next_step": "草稿卷仍需整卷校验与双人复核，才能提交审核通过",
    }


def _blueprint_mode(version: Any) -> str:
    return str(getattr(version, "assembly_mode", "manual"))


__all__ = [
    "get_assembly_service",
    "router",
]


# 让导入方明确知道求解器可用性（健康检查/运维脚本可用），不静默假装有。
def solver_capability() -> dict[str, Any]:
    installed = ortools_version()
    return {
        "solver": "cp-sat" if installed else "unavailable",
        "ortools_version": installed,
        "requirements_file": "server/requirements-assembly.txt",
        "degraded": installed is None,
    }


def registered_routes() -> list[str]:
    return [f"{sorted(route.methods)[0]} {route.path}" for route in router.routes]


# `db` 与 `RunStatus` 在本模块被用于类型/诊断上下文；显式引用避免"看起来没用到"。
_ = (db, RunStatus)
