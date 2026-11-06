"""蓝图版本与候选快照 API。

路由：
    POST /paper-blueprints/{blueprint_id}/versions        创建/追加蓝图版本
    GET  /paper-blueprint-versions/{id}                   读取一个蓝图版本
    POST /paper-blueprint-versions/{id}/candidate-snapshots 生成并保存候选清单（任务 3）
    GET  /paper-blueprint-versions/{id}/candidates        读取最近一次候选清单（任务 3）

为什么写接口一定要 Idempotency-Key：教师点「保存」时网络超时很常见，浏览器/脚本
重试会带着同一份载荷再来一次。没有幂等键，重试就是第二版；有了它，同键同载荷返回
原版本（200 + `Idempotent-Replay: true`），同键不同载荷 409。幂等记录与版本插入在
**同一个事务**里写，进程崩溃也不会留下"版本在、键不在"的空档。

候选快照的幂等真源是**内容哈希**而不是那把键：同一份候选清单（同蓝图版本、同
Release、同题目集合）得到同一个 `candidate_manifest_hash`，0027 的
`UNIQUE (blueprint_version_id, candidate_manifest_hash)` 保证只落一行，重试返回
原行并带 `created=false`（HTTP 200）。Idempotency-Key 仍然按要求校验——它是接口
契约（§0.3 规则 3），只是不承担去重职责。

为什么追加版本要 base_version_id：草稿编辑是"读—改—写"，两个窗口同时打开时后写的
那个人必须知道自己覆盖了谁。基线过期就是 409，不是静默覆盖。

课程范围：本仓库 `course.id` 是文本（`analysis`），而 0027 的 assessment 表是 uuid。
路由负责用 `ref_uuid` 把文本 id 映射成确定性 uuid，并把原文一并交给服务写进
`assessment.id_ref`；响应里两种 id 都给，前端不必自己算 uuid5。
候选范围（purpose/course/offering/release_context）**一律从蓝图版本派生**，
路由不收这些参数——否则换个 release_context_id 就绕过了整个门禁。

错误码（detail 为 `{code, message}`，便于前端分支；401/403 角色错误沿用 auth 的字符串）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 角色不足（auth.require）/ course_scope_denied
    404 course_not_found / blueprint_version_not_found / candidate_snapshot_not_found
    409 idempotency_conflict / stale_base_version / base_version_required
    422 blueprint_invalid / release_context_not_found / candidate_scope_invalid
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .blueprints import (
    BaseVersionRequiredError,
    BlueprintService,
    BlueprintValidationError,
    BlueprintVersionNotFoundError,
    BlueprintVersionRow,
    CourseScopeAuthorizer,
    IdempotencyConflictError,
    PostgresBlueprintRepository,
    SCHEMA_VERSION,
    StaleBaseVersionError,
    UnknownCourseError,
    ref_uuid,
)
from .candidates import (
    CandidateService,
    CandidateSnapshotNotFoundError,
    PostgresCandidateRepository,
    ReleaseContextNotFoundError,
)
from .contracts import (
    AssemblyMode,
    BlueprintContent,
    BlueprintSection,
    CandidateScope,
    CandidateSnapshot,
    Purpose,
    ReviewPolicy,
    Score,
)

router = APIRouter(prefix="/api/v1", tags=["paper-blueprints"])

# 同一个依赖对象在两处复用：FastAPI 会缓存同一次请求的结果，鉴权只跑一遍；
# 若各自调用 require("teacher")，会生成两个不同的依赖函数，白白查两次身份。
require_teacher = require("teacher")

MAX_IDEMPOTENCY_KEY = 200


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class BlueprintVersionIn(BaseModel):
    """创建/追加蓝图版本的请求命令。

    `course_ref / offering_ref` 是本仓库的文本 id；`course_id / offering_id` 由
    路由映射成 uuid5 后构造共享契约 `BlueprintContent`——请求体里不接受 uuid，
    否则调用方得自己实现 uuid5，而命名空间一旦算错就是另一个课程。
    """

    model_config = ConfigDict(extra="forbid")

    course_ref: str = Field(min_length=1, max_length=200)
    offering_ref: str | None = Field(default=None, min_length=1, max_length=200)
    release_context_id: UUID
    purpose: Purpose
    total_score: Score
    duration_seconds: int = Field(gt=0)
    sections: tuple[BlueprintSection, ...] = Field(min_length=1)
    hard_constraints: dict[str, object] = {}
    soft_objectives: dict[str, object] = {}
    review_policy: ReviewPolicy
    assembly_mode: AssemblyMode = "manual"
    base_version_id: UUID | None = None

    def to_content(self) -> BlueprintContent:
        return BlueprintContent(
            schema_version=SCHEMA_VERSION,  # type: ignore[arg-type]
            purpose=self.purpose,
            course_id=ref_uuid("course", self.course_ref),
            offering_id=(
                None if self.offering_ref is None
                else ref_uuid("offering", self.offering_ref)
            ),
            release_context_id=self.release_context_id,
            total_score=self.total_score,
            duration_seconds=self.duration_seconds,
            sections=self.sections,
            hard_constraints=self.hard_constraints,
            soft_objectives=self.soft_objectives,
            review_policy=self.review_policy,
            assembly_mode=self.assembly_mode,
        )


def get_blueprint_service(
    actor: Actor = Depends(require_teacher),
) -> BlueprintService:
    """生产服务：PostgreSQL 仓储 + 按班级名单判定的课程范围授权。"""
    return BlueprintService(
        repository=PostgresBlueprintRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


def _require_idempotency_key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key:
        raise _error(400, "idempotency_key_required",
                     "写接口必须带 Idempotency-Key 头，重试才不会创建第二个版本")
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise _error(400, "idempotency_key_required",
                     f"Idempotency-Key 太长（上限 {MAX_IDEMPOTENCY_KEY} 字符）")
    return key


@router.post(
    "/paper-blueprints/{blueprint_id}/versions",
    status_code=201,
    response_model=BlueprintVersionRow,
)
def create_blueprint_version(
    blueprint_id: UUID,
    body: BlueprintVersionIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> BlueprintVersionRow:
    key = _require_idempotency_key(idempotency_key)
    try:
        row = service.create_version(
            actor_id=ref_uuid("account", actor.id),
            content=body.to_content(),
            blueprint_id=blueprint_id,
            base_version_id=body.base_version_id,
            idempotency_key=key,
            course_ref=body.course_ref,
            offering_ref=body.offering_ref,
            actor_ref=actor.id,
        )
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", f"课程不存在：{exc}") from exc
    except BaseVersionRequiredError as exc:
        raise _error(409, "base_version_required", str(exc)) from exc
    except StaleBaseVersionError as exc:
        raise _error(409, "stale_base_version", str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise _error(409, "idempotency_conflict", str(exc)) from exc
    except BlueprintValidationError as exc:
        raise _error(422, "blueprint_invalid", str(exc)) from exc

    if not row.created:
        # 没有新建版本：幂等重放或内容去重都返回 200，而不是骗人的 201。
        response.status_code = 200
    if row.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return row


def get_candidate_service(
    actor: Actor = Depends(require_teacher),
) -> CandidateService:
    """生产候选服务：PostgreSQL 仓储 + 与蓝图同一套课程范围授权。"""
    return CandidateService(
        repository=PostgresCandidateRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


def _load_blueprint_version(
    blueprints: BlueprintService, version_id: UUID, actor: Actor
) -> BlueprintVersionRow:
    """读蓝图版本并统一错误映射；候选路由沿用它，避免两处状态码漂移。"""
    try:
        return blueprints.get_version(
            actor_id=ref_uuid("account", actor.id),
            version_id=version_id,
            actor_ref=actor.id,
        )
    except BlueprintVersionNotFoundError as exc:
        raise _error(404, "blueprint_version_not_found",
                     f"蓝图版本不存在：{version_id}") from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", f"课程不存在：{exc}") from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc


def _candidate_scope(version: BlueprintVersionRow) -> CandidateScope:
    try:
        return CandidateScope.from_version(version)
    except ValueError as exc:
        raise _error(422, "candidate_scope_invalid", str(exc)) from exc


@router.post(
    "/paper-blueprint-versions/{version_id}/candidate-snapshots",
    status_code=201,
    response_model=CandidateSnapshot,
)
def create_candidate_snapshot(
    version_id: UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    service: CandidateService = Depends(get_candidate_service),
) -> CandidateSnapshot:
    """生成候选清单并落库；同内容重试返回原行（200 + created=false）。

    请求体是空的：候选范围完全由蓝图版本决定（见模块 docstring）。这样即使有人
    直接构造请求，也没有"指定别的 Release Context"的入口。
    """
    _require_idempotency_key(idempotency_key)
    version = _load_blueprint_version(blueprints, version_id, actor)
    scoped = _candidate_scope(version)
    try:
        snapshot = service.snapshot(
            actor_id=ref_uuid("account", actor.id), scope=scoped)
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except ReleaseContextNotFoundError as exc:
        # 422 而不是 404：蓝图本身存在，是它引用的 Release 配置错了。
        raise _error(422, "release_context_not_found",
                     f"蓝图引用的 Release Context 不存在：{exc}") from exc
    if not snapshot.created:
        response.status_code = 200
    return snapshot


@router.get(
    "/paper-blueprint-versions/{version_id}/candidates",
    response_model=CandidateSnapshot,
)
def get_candidate_snapshot(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    service: CandidateService = Depends(get_candidate_service),
) -> CandidateSnapshot:
    """读取最近一次候选清单（含逐条拒绝原因码）；没有快照就是 404。"""
    version = _load_blueprint_version(blueprints, version_id, actor)
    scoped = _candidate_scope(version)
    try:
        return service.latest(actor_id=ref_uuid("account", actor.id), scope=scoped)
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except CandidateSnapshotNotFoundError as exc:
        raise _error(404, "candidate_snapshot_not_found",
                     f"该蓝图版本还没有候选快照：{version_id}") from exc


@router.get(
    "/paper-blueprint-versions/{version_id}",
    response_model=BlueprintVersionRow,
)
def get_blueprint_version(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> BlueprintVersionRow:
    return _load_blueprint_version(service, version_id, actor)
