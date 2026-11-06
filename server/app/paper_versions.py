"""手工固定卷的草稿 API（任务 4）。

路由：
    POST  /api/v1/paper-versions                              建 draft / 克隆已批准卷
    PATCH /api/v1/paper-versions/{id}/slots                   草稿替换 / 调序 / 调分
    POST  /api/v1/paper-versions/{id}/validate                独立整卷校验（只读、可哈希）
    GET   /api/v1/paper-versions/{id}                         读取一个纸卷版本
    GET   /api/v1/paper-versions/{id}/edit-events             读取草稿变更集（append-only）

为什么写接口一定要 `Idempotency-Key`：教师点「保存/替换」时网络超时很常见，客户端
重试会带同一份载荷再来一次。没有幂等键，重试就是"再替换一次/再 +1 个 edit_seq"；
有了它，同键同载荷返回原结果（200 + `Idempotent-Replay: true`），同键不同载荷 409。
幂等记录与纸卷写入在**同一个事务**里落库，进程崩溃不会留下"卷改了、键没记"的空档。

为什么草稿编辑必须带 `base_version=edit_seq`：两个窗口同时打开同一份草稿，后写的
那个人必须知道自己覆盖了谁。过期是 409 `stale_version`（并回传 `current_version`），
不是静默覆盖；approved 卷是 409 `paper_not_draft`，只能克隆成新 paper 身份的新草稿。

替换只接受 `exercise_version_id`：完整来源（source_release_id / exercise_family_id /
exercise_id / dependency_manifest_hash）由服务从建卷时固定的候选快照解析。客户端
不可能伪造"替换来自同一 Release"。

错误码（detail 为 `{code, message}`，stale_version 另带 current_version；401/403 角色
错误沿用 auth 的字符串）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 角色不足（auth.require）/ course_scope_denied
    404 course_not_found / blueprint_version_not_found / candidate_snapshot_not_found /
        paper_not_found
    409 idempotency_conflict / stale_version / paper_not_draft
    422 paper_invalid / snapshot_mismatch / validation_input_mismatch / <规则原因码>：
        invalid_create_mode / base_not_approved / paper_identity_conflict /
        unknown_slot_code / duplicate_slot_selection / candidate_not_in_snapshot /
        candidate_excluded / candidate_not_mandatory / source_incomplete /
        release_context_mismatch / no_operations / slot_not_found /
        unknown_operation / missing_exercise_version_id / score_precision /
        invalid_slot_order
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .blueprints import (
    BlueprintService,
    BlueprintVersionNotFoundError,
    CourseScopeAuthorizer,
    PostgresBlueprintRepository,
    UnknownCourseError,
)
from .candidates import (
    CandidateScopeError,
    CandidateService,
    CandidateSnapshotNotFoundError,
    PostgresCandidateRepository,
)
from .contracts import Score, SlotSelection, ValidationResult
from .papers import (
    MAX_IDEMPOTENCY_KEY,
    EditOperation,
    PaperEditEvent,
    PaperIdempotencyConflictError,
    PaperNotDraftError,
    PaperNotFoundError,
    PaperRuleError,
    PaperScopeAuthorizer,
    PaperService,
    PaperVersionRow,
    PostgresPaperRepository,
    StalePaperVersion,
)
from .validation import (
    PaperValidationService,
    PostgresPaperValidationRepository,
    ValidationInputError,
)

router = APIRouter(prefix="/api/v1", tags=["papers"])

require_teacher = require("teacher")


def _error(status_code: int, code: str, message: str, **extra: object) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


@contextmanager
def _paper_errors() -> Iterator[None]:
    """把域错误映射成稳定的 HTTP 码与机器可读 code。

    顺序有讲究：PaperRuleError / PaperNotDraftError 都是 ValueError 子类，
    具体类型必须先于兜底的 ValueError；否则规则错误会被降级成一个笼统的
    `paper_invalid`，前端就没法根据原因码提示"该改哪一项"。
    """
    try:
        yield
    except HTTPException:
        raise
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except BlueprintVersionNotFoundError as exc:
        raise _error(404, "blueprint_version_not_found", str(exc)) from exc
    except CandidateSnapshotNotFoundError as exc:
        raise _error(404, "candidate_snapshot_not_found", str(exc)) from exc
    except CandidateScopeError as exc:
        raise _error(422, "snapshot_mismatch", str(exc)) from exc
    except ValidationInputError as exc:
        raise _error(422, "validation_input_mismatch", str(exc)) from exc
    except PaperNotFoundError as exc:
        raise _error(404, "paper_not_found", str(exc)) from exc
    except PaperNotDraftError as exc:
        raise _error(409, "paper_not_draft", str(exc)) from exc
    except StalePaperVersion as exc:
        raise _error(
            409, "stale_version", str(exc), current_version=exc.current_version
        ) from exc
    except PaperIdempotencyConflictError as exc:
        raise _error(409, "idempotency_conflict", str(exc)) from exc
    except PaperRuleError as exc:
        raise _error(422, exc.code, str(exc)) from exc
    except ValueError as exc:
        raise _error(422, "paper_invalid", str(exc)) from exc


class SlotSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_code: str = Field(min_length=1, max_length=64)
    exercise_version_id: UUID


class PaperDraftIn(BaseModel):
    """建卷命令：组装（蓝图+快照+选题）或克隆（base_paper_version_id）。

    `paper_id` 允许调用方指定新的纸卷身份（客户端重试时可用同一 id 观察冲突），
    但不接受"已经存在的 paper 身份"：纠错/克隆走 base_paper_version_id，
    同一身份两个同内容版本会撞 0027 的 UNIQUE (manifest_hash, paper_id)。
    """

    model_config = ConfigDict(extra="forbid")

    blueprint_version_id: UUID | None = None
    candidate_snapshot_id: UUID | None = None
    selections: tuple[SlotSelectionIn, ...] = ()
    paper_id: UUID | None = None
    base_paper_version_id: UUID | None = None


class EditOperationIn(BaseModel):
    """一条编辑操作。`kind` 决定哪些字段必填，由服务按原因码拒绝缺项。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["replace", "move", "score"]
    slot_id: UUID
    exercise_version_id: UUID | None = None
    target_slot_no: int | None = Field(default=None, ge=1)
    score: Score | None = None
    reason: str | None = Field(default=None, max_length=500)


class PaperEditIn(BaseModel):
    """草稿编辑命令：`base_version` 就是调用方读到的 `edit_seq`。"""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    operations: tuple[EditOperationIn, ...] = Field(min_length=1)


def get_paper_service(actor: Actor = Depends(require_teacher)) -> PaperService:
    """生产装配：纸卷仓储 + 蓝图/候选服务（各自带课程范围授权）。"""
    return PaperService(
        repository=PostgresPaperRepository(),
        authorizer=PaperScopeAuthorizer(actor),
        blueprints=BlueprintService(
            repository=PostgresBlueprintRepository(),
            authorizer=CourseScopeAuthorizer(actor),
        ),
        candidates=CandidateService(
            repository=PostgresCandidateRepository(),
            authorizer=CourseScopeAuthorizer(actor),
        ),
    )


def get_validation_service(
    actor: Actor = Depends(require_teacher),
) -> PaperValidationService:
    """生产装配：只读卷面仓储 + 只读候选预检 + 课程范围授权。

    校验接口是只读的，所以**不需要** Idempotency-Key；重复调用只会重复计算同一结论
    （`report_hash` 相同）。候选服务用 `preview` 而不是 `snapshot`：校验器不能为了
    读结论往 `candidate_snapshot` 里写新行。
    """
    return PaperValidationService(
        repository=PostgresPaperValidationRepository(),
        candidates=CandidateService(
            repository=PostgresCandidateRepository(),
            authorizer=CourseScopeAuthorizer(actor),
        ),
        authorizer=PaperScopeAuthorizer(actor),
    )


def _idempotency_key(value: str | None) -> str:
    """写接口必须带 Idempotency-Key；空白视同缺失（400，而不是 422）。"""
    if value is None or not value.strip():
        raise _error(
            400, "idempotency_key_required",
            "写接口必须带 Idempotency-Key：重试不能变成第二次建卷/第二次编辑",
        )
    cleaned = value.strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise _error(
            400, "idempotency_key_required",
            f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY} 字符",
        )
    return cleaned


def _mark(response: Response, row: PaperVersionRow) -> None:
    if row.replayed:
        response.headers["Idempotent-Replay"] = "true"


@router.post("/paper-versions", response_model=PaperVersionRow)
def create_paper_version(
    body: PaperDraftIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: PaperService = Depends(get_paper_service),
) -> PaperVersionRow:
    key = _idempotency_key(idempotency_key)
    selections = tuple(
        SlotSelection(
            slot_code=selection.slot_code,
            exercise_version_id=selection.exercise_version_id,
        )
        for selection in body.selections
    )
    with _paper_errors():
        row = service.create_manual_draft(
            actor_id=_actor_uuid(actor),
            actor_ref=actor.id,
            blueprint_version_id=body.blueprint_version_id,
            candidate_snapshot_id=body.candidate_snapshot_id,
            selections=selections,
            paper_id=body.paper_id,
            base_paper_version_id=body.base_paper_version_id,
            idempotency_key=key,
        )
    _mark(response, row)
    response.status_code = 201 if row.created else 200
    return row


@router.patch("/paper-versions/{paper_version_id}/slots", response_model=PaperVersionRow)
def edit_paper_slots(
    paper_version_id: UUID,
    body: PaperEditIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: PaperService = Depends(get_paper_service),
) -> PaperVersionRow:
    key = _idempotency_key(idempotency_key)
    operations = tuple(
        EditOperation(
            kind=operation.kind,
            slot_id=operation.slot_id,
            exercise_version_id=operation.exercise_version_id,
            target_slot_no=operation.target_slot_no,
            score=operation.score,
            reason=operation.reason,
        )
        for operation in body.operations
    )
    with _paper_errors():
        row = service.edit_draft(
            actor_id=_actor_uuid(actor),
            actor_ref=actor.id,
            paper_version_id=paper_version_id,
            base_version=body.base_version,
            operations=operations,
            idempotency_key=key,
        )
    _mark(response, row)
    return row


@router.get("/paper-versions/{paper_version_id}", response_model=PaperVersionRow)
def get_paper_version(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: PaperService = Depends(get_paper_service),
) -> PaperVersionRow:
    with _paper_errors():
        return service.get_version(
            actor_id=_actor_uuid(actor),
            actor_ref=actor.id,
            paper_version_id=paper_version_id,
        )


@router.post(
    "/paper-versions/{paper_version_id}/validate",
    response_model=ValidationResult,
)
def validate_paper_version(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: PaperValidationService = Depends(get_validation_service),
) -> ValidationResult:
    """独立整卷校验：只读、确定性、可哈希。

    草稿自检、提交审核门禁、任务 7 双审证据都调这一个入口。`valid=false` 时
    issues 已按"错误优先 + 分节/槽位顺序"排好，前端不需要自己排序或解析 message。
    """
    with _paper_errors():
        return service.validate(
            actor_id=_actor_uuid(actor),
            paper_version_id=paper_version_id,
        )


@router.get(
    "/paper-versions/{paper_version_id}/edit-events",
    response_model=list[PaperEditEvent],
)
def list_paper_edit_events(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: PaperService = Depends(get_paper_service),
) -> list[PaperEditEvent]:
    with _paper_errors():
        events = service.edit_events(
            actor_id=_actor_uuid(actor),
            actor_ref=actor.id,
            paper_version_id=paper_version_id,
        )
    return list(events)


def _actor_uuid(actor: Actor) -> UUID:
    """本仓库账号是 text（`acc-…`），assessment 表是 uuid：走确定性 uuid5 映射。"""
    from .blueprints import ref_uuid

    return ref_uuid("account", actor.id)
