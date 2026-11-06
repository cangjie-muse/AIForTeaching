"""整卷提交 / 双复核 / 撤回 API（任务 7）。

路由：
    POST /api/v1/paper-versions/{id}/submit-review   草稿 → in_review（冻结证据）
    POST /api/v1/paper-versions/{id}/approve         in_review → approved（双人）
    POST /api/v1/paper-versions/{id}/withdraw        approved → withdrawn
    GET  /api/v1/paper-versions/{id}/review          读取当前生效的审核状态

为什么所有写接口都要 `Idempotency-Key`：复核签字是高风险动作，网络超时后的重试
绝不能变成"第二位复核人"或"第二次撤回"。同键同载荷返回原响应（200 +
`Idempotent-Replay: true`），同键不同载荷 409。幂等记录与状态迁移在同一个事务里
落库，进程崩溃不会留下"卷子批了、键没记"的空档。

为什么 `approve` 的请求体必须带 `manifest_hash`：复核人签的是**某一份具体卷面**，
不是"这个 paper_version 当前是什么"。服务会把它与 `submitted_manifest_hash`、
round.manifest_hash、paper.manifest_hash 四者比对；任何一处不一致都返回
409 `review_manifest_changed`（证据过期），而不是"manifest 变了仍然算通过"。

错误码（detail 为 `{code, message}`；401/403 角色错误沿用 auth 的字符串）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 角色不足 / course_scope_denied / contributor_cannot_review /
        human_reviewer_required
    404 paper_not_found / course_not_found
    409 paper_not_draft / renders_not_ready / review_not_open /
        review_manifest_changed / duplicate_reviewer / paper_not_approved /
        paper_withdrawn / review_round_conflict / idempotency_conflict
    422 paper_invalid（附完整 report 与 report_hash）/ review_input_invalid /
        withdrawal_reason_required
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .blueprints import (
    CourseScopeAuthorizer,
    UnknownCourseError,
    ref_uuid,
)
from .candidates import (
    CandidateScopeError,
    CandidateService,
    PostgresCandidateRepository,
)
from .papers import (
    MAX_IDEMPOTENCY_KEY,
    PaperIdempotencyConflictError,
    PaperNotDraftError,
    PaperNotFoundError,
    PaperScopeAuthorizer,
)
from .paper_review import (
    ContributorReviewError,
    DuplicateReviewerError,
    HumanReviewerRequiredError,
    IdempotencyKeyRequiredError,
    PaperNotApprovedError,
    PaperReviewError,
    PaperReviewNotFoundError,
    PaperReviewService,
    PaperReviewState,
    PaperWithdrawnError,
    PostgresPaperReviewRepository,
    RendersNotReadyError,
    ReviewInputError,
    ReviewManifestChangedError,
    ReviewNotOpenError,
    ReviewRoundConflictError,
    WithdrawalReasonRequiredError,
)
from .validation import (
    PaperValidationBlockedError,
    PaperValidationService,
    PostgresPaperValidationRepository,
    ValidationInputError,
)

router = APIRouter(prefix="/api/v1", tags=["paper-review"])

require_teacher = require("teacher")


def _error(status_code: int, code: str, message: str, **extra: object) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


@contextmanager
def _review_errors() -> Iterator[None]:
    """把域错误映射成稳定的 HTTP 码与机器可读 code。

    具体类型必须先于兜底的 ValueError：`ReviewInputError` 与
    `WithdrawalReasonRequiredError` 都是 ValueError 子类。
    """
    try:
        yield
    except HTTPException:
        raise
    except IdempotencyKeyRequiredError as exc:
        raise _error(400, "idempotency_key_required", str(exc)) from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except (PaperReviewNotFoundError, PaperNotFoundError) as exc:
        raise _error(404, "paper_not_found", str(exc)) from exc
    except PaperNotDraftError as exc:
        raise _error(409, "paper_not_draft", str(exc)) from exc
    except PaperValidationBlockedError as exc:
        raise _error(
            422, "paper_invalid", str(exc),
            report=exc.result.model_dump(mode="json"),
            report_hash=exc.result.report_hash,
        ) from exc
    except RendersNotReadyError as exc:
        raise _error(409, "renders_not_ready", str(exc)) from exc
    except ReviewRoundConflictError as exc:
        raise _error(409, "review_round_conflict", str(exc)) from exc
    except ReviewNotOpenError as exc:
        raise _error(409, "review_not_open", str(exc)) from exc
    except ReviewManifestChangedError as exc:
        raise _error(409, "review_manifest_changed", str(exc)) from exc
    except DuplicateReviewerError as exc:
        raise _error(409, "duplicate_reviewer", str(exc)) from exc
    except ContributorReviewError as exc:
        raise _error(403, "contributor_cannot_review", str(exc)) from exc
    except HumanReviewerRequiredError as exc:
        raise _error(403, "human_reviewer_required", str(exc)) from exc
    except PaperWithdrawnError as exc:
        raise _error(409, "paper_withdrawn", str(exc)) from exc
    except PaperNotApprovedError as exc:
        raise _error(409, "paper_not_approved", str(exc)) from exc
    except WithdrawalReasonRequiredError as exc:
        raise _error(422, "withdrawal_reason_required", str(exc)) from exc
    except ReviewInputError as exc:
        raise _error(422, "review_input_invalid", str(exc)) from exc
    except PaperIdempotencyConflictError as exc:
        raise _error(409, "idempotency_conflict", str(exc)) from exc
    except CandidateScopeError as exc:
        raise _error(422, "snapshot_mismatch", str(exc)) from exc
    except ValidationInputError as exc:
        raise _error(422, "validation_input_mismatch", str(exc)) from exc
    except PaperReviewError as exc:
        raise _error(409, "review_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _error(422, "review_input_invalid", str(exc)) from exc


class ApproveIn(BaseModel):
    """一次复核签署。`manifest_hash` 是"我审的是这一版"的显式声明。"""

    model_config = ConfigDict(extra="forbid")

    manifest_hash: str
    evidence_hash: str
    note: str | None = Field(default=None, max_length=500)


class WithdrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def get_review_service(
    actor: Actor = Depends(require_teacher),
) -> PaperReviewService:
    """生产装配：复核仓储 + 只读整卷校验 + 课程范围授权。"""
    return PaperReviewService(
        repository=PostgresPaperReviewRepository(),
        authorizer=PaperScopeAuthorizer(actor),
        validator=PaperValidationService(
            repository=PostgresPaperValidationRepository(),
            candidates=CandidateService(
                repository=PostgresCandidateRepository(),
                authorizer=CourseScopeAuthorizer(actor),
            ),
            authorizer=PaperScopeAuthorizer(actor),
        ),
    )


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise _error(
            400, "idempotency_key_required",
            "写接口必须带 Idempotency-Key：重试不能变成第二次复核/撤回",
        )
    cleaned = value.strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise _error(
            400, "idempotency_key_required",
            f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY} 字符",
        )
    return cleaned


def _mark(response: Response, state: PaperReviewState) -> None:
    if state.replayed:
        response.headers["Idempotent-Replay"] = "true"


def _actor_uuid(actor: Actor) -> UUID:
    """本仓库账号是 text（`acc-…`），assessment 表是 uuid：走确定性 uuid5 映射。"""
    return ref_uuid("account", actor.id)


@router.post(
    "/paper-versions/{paper_version_id}/submit-review",
    response_model=PaperReviewState,
)
def submit_review(
    paper_version_id: UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: PaperReviewService = Depends(get_review_service),
) -> PaperReviewState:
    key = _idempotency_key(idempotency_key)
    with _review_errors():
        state = service.submit(
            actor_id=_actor_uuid(actor),
            paper_version_id=paper_version_id,
            idempotency_key=key,
        )
    _mark(response, state)
    return state


@router.post(
    "/paper-versions/{paper_version_id}/approve",
    response_model=PaperReviewState,
)
def approve_paper(
    paper_version_id: UUID,
    body: ApproveIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: PaperReviewService = Depends(get_review_service),
) -> PaperReviewState:
    key = _idempotency_key(idempotency_key)
    with _review_errors():
        state = service.approve(
            actor_id=_actor_uuid(actor),
            paper_version_id=paper_version_id,
            manifest_hash=body.manifest_hash,
            evidence_hash=body.evidence_hash,
            note=body.note,
            idempotency_key=key,
        )
    _mark(response, state)
    return state


@router.post(
    "/paper-versions/{paper_version_id}/withdraw",
    response_model=PaperReviewState,
)
def withdraw_paper(
    paper_version_id: UUID,
    body: WithdrawIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: PaperReviewService = Depends(get_review_service),
) -> PaperReviewState:
    key = _idempotency_key(idempotency_key)
    with _review_errors():
        state = service.withdraw(
            actor_id=_actor_uuid(actor),
            paper_version_id=paper_version_id,
            reason=body.reason,
            idempotency_key=key,
        )
    _mark(response, state)
    return state


@router.get(
    "/paper-versions/{paper_version_id}/review",
    response_model=PaperReviewState,
)
def get_paper_review(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: PaperReviewService = Depends(get_review_service),
) -> PaperReviewState:
    with _review_errors():
        return service.get(
            actor_id=_actor_uuid(actor),
            paper_version_id=paper_version_id,
        )
