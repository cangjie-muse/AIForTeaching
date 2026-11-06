"""打印作业 API：建草稿、发布终检、严重撤回、读取（阶段 A 任务 8）。

    POST /api/v1/assignments                 建作业草稿（登记卷子与三个时间点）
    POST /api/v1/assignments/{id}/publish     发布终检 + 固定投影 + 写曝光事件
    POST /api/v1/assignments/{id}/withdraw    严重撤回（保留提交读数，作废后续使用）
    GET  /api/v1/assignments/{id}             读作业与其固定投影
    GET  /api/v1/paper-versions/{id}/assignments  这份卷已登记的作业（含草稿）

三个写接口都要 `Idempotency-Key`。这里不能像 Release 冻结那样靠内容寻址：
发布与撤回是**状态迁移**，同一份请求在不同时刻调用意义不同（第一次是发布，
第二次要么是重试、要么是"再发布一次"——后者必须被拒绝）。所以幂等键记录与
状态迁移同事务落库，同键同载荷返回原响应，同键不同载荷 409。

错误码（detail 为 `{code, message}`）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 course_scope_denied
    404 assignment_not_found / course_not_found
    409 assignment_not_published_yet / assignment_state_conflict /
        idempotency_conflict
    422 assignment_not_approved / review_evidence_stale / time_order_invalid /
        source_incomplete / exposure_conflict / assignment_input_invalid /
        withdrawal_reason_required
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .assignments import (
    AssignmentError,
    AssignmentExposureConflictError,
    AssignmentIdempotencyConflictError,
    AssignmentInputError,
    AssignmentNotApprovedError,
    AssignmentNotFoundError,
    AssignmentRow,
    AssignmentService,
    AssignmentSourceIncompleteError,
    AssignmentStateError,
    AssignmentTimeOrderError,
    AssignmentView,
    PostgresAssignmentRepository,
    MAX_IDEMPOTENCY_KEY,
)
from .auth import Actor, require
from .blueprints import UnknownCourseError, ref_uuid
from .papers import PaperScopeAuthorizer

router = APIRouter(prefix="/api/v1", tags=["assignments"])

require_teacher = require("teacher")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class AssignmentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_version_id: UUID
    offering_ref: str = Field(min_length=1, max_length=200)
    visible_at: datetime
    due_at: datetime
    hard_close_at: datetime


class AssignmentWithdrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


@contextmanager
def _assignment_errors() -> Iterator[None]:
    try:
        yield
    except HTTPException:
        raise
    except AssignmentNotFoundError as exc:
        raise _error(404, "assignment_not_found", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except AssignmentIdempotencyConflictError as exc:
        raise _error(409, "idempotency_conflict", str(exc)) from exc
    except AssignmentStateError as exc:
        raise _error(409, "assignment_state_conflict", str(exc)) from exc
    except AssignmentNotApprovedError as exc:
        raise _error(422, "assignment_not_approved", str(exc)) from exc
    except AssignmentExposureConflictError as exc:
        raise _error(422, "exposure_conflict", str(exc), ) from exc
    except AssignmentTimeOrderError as exc:
        raise _error(422, "time_order_invalid", str(exc)) from exc
    except AssignmentSourceIncompleteError as exc:
        raise _error(422, "source_incomplete", str(exc)) from exc
    except AssignmentInputError as exc:
        message = str(exc)
        code = "withdrawal_reason_required" if "理由" in message else "assignment_input_invalid"
        raise _error(400 if code == "idempotency_key_required" else 422, code,
                     message) from exc
    except AssignmentError as exc:
        raise _error(422, "assignment_invalid", str(exc)) from exc


def get_assignment_service(actor: Actor = Depends(require_teacher)) -> AssignmentService:
    return AssignmentService(
        repository=PostgresAssignmentRepository(),
        authorizer=PaperScopeAuthorizer(actor),
    )


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise _error(
            400, "idempotency_key_required",
            "写接口必须带 Idempotency-Key：发布/撤回的重试不能变成第二次状态迁移",
        )
    cleaned = value.strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise _error(400, "idempotency_key_required",
                     f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY} 字符")
    return cleaned


def _actor_uuid(actor: Actor) -> UUID:
    return ref_uuid("account", actor.id)


def _mark(response: Response, view: AssignmentView) -> None:
    if view.replayed:
        response.headers["Idempotent-Replay"] = "true"


@router.post("/assignments", response_model=AssignmentView, status_code=201)
def create_assignment(
    body: AssignmentCreateIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: AssignmentService = Depends(get_assignment_service),
) -> AssignmentView:
    key = _idempotency_key(idempotency_key)
    with _assignment_errors():
        view = service.create_draft(
            actor_id=_actor_uuid(actor),
            paper_version_id=body.paper_version_id,
            offering_ref=body.offering_ref,
            visible_at=body.visible_at,
            due_at=body.due_at,
            hard_close_at=body.hard_close_at,
            idempotency_key=key,
            actor_ref=actor.id,
        )
    if view.replayed:
        response.status_code = 200
    _mark(response, view)
    return view


@router.post("/assignments/{assignment_id}/publish", response_model=AssignmentView)
def publish_assignment(
    assignment_id: UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: AssignmentService = Depends(get_assignment_service),
) -> AssignmentView:
    key = _idempotency_key(idempotency_key)
    with _assignment_errors():
        view = service.publish(
            actor_id=_actor_uuid(actor),
            assignment_id=assignment_id,
            idempotency_key=key,
        )
    _mark(response, view)
    return view


@router.post("/assignments/{assignment_id}/withdraw", response_model=AssignmentView)
def withdraw_assignment(
    assignment_id: UUID,
    body: AssignmentWithdrawIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: AssignmentService = Depends(get_assignment_service),
) -> AssignmentView:
    key = _idempotency_key(idempotency_key)
    with _assignment_errors():
        view = service.withdraw(
            actor_id=_actor_uuid(actor),
            assignment_id=assignment_id,
            reason=body.reason,
            idempotency_key=key,
        )
    _mark(response, view)
    return view


@router.get("/assignments/{assignment_id}", response_model=AssignmentView)
def read_assignment(
    assignment_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: AssignmentService = Depends(get_assignment_service),
) -> AssignmentView:
    with _assignment_errors():
        return service.get(actor_id=_actor_uuid(actor), assignment_id=assignment_id)


@router.get("/paper-versions/{paper_version_id}/assignments",
            response_model=list[AssignmentRow])
def list_paper_assignments(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: AssignmentService = Depends(get_assignment_service),
) -> list[AssignmentRow]:
    """这份卷已经登记过哪些作业——发布面板靠它决定"有没有草稿可发"。

    没有这个读接口，页面只能让教师重复建草稿（同卷同班建两份作业），或者在
    前端自己拼 assignment_id；两者都会把"哪份是真的"变成猜测。
    """
    with _assignment_errors():
        rows = service.list_for_paper(
            actor_id=_actor_uuid(actor), paper_version_id=paper_version_id)
    return list(rows)
