"""在线作答 HTTP 契约（阶段 C 任务 7）。

    GET  /api/v1/assignments/{assignment_id}/attempt        我有没有正在考的卷
    POST /api/v1/assignments/{assignment_id}/attempts       开始（需 Idempotency-Key）
    GET  /api/v1/attempts/{attempt_id}                       重连：整卷 + 本人最新响应
    PATCH /api/v1/attempts/{attempt_id}/slots/{slot_id}      保存一个槽位
    POST /api/v1/attempts/{attempt_id}/finalize              交卷（需 Idempotency-Key）

错误语义（§15.3 的统一结构 `{code, message}`，前端按 code 分支）：

    404 attempt_not_found        不存在或不属于本人（不区分，避免探测）
    409 response_conflict        客户端 base 过期，响应体带 `server_result`
    409 attempt_finalized        已交卷/已截止，不能再写
    409 assignment_withdrawn     作业撤回
    409 idempotency_conflict     同一 client_revision_id 换了内容
    422 solver/策略类问题统一用 invalid_attempt_policy / distribution_not_online

学生只能看见自己的行：`student_id` 一律由认证身份推导，请求体不接受学生自报。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .blueprints import ref_uuid
from .online.contracts import (
    AttemptError,
    AttemptFinalized,
    PolicyDenied,
    ResourceNotFound,
    ResponseConflict,
    SaveResponseResult,
    Withdrawn,
    build_student_attempt,
    compute_deadline,
)
from .online.postgres import PostgresOnlineRepository
from .online.responses import IdempotencyConflict, ResponseService
from .online.service import AttemptService
from .online.submissions import DeadlineWorker, SubmissionService

router = APIRouter(prefix="/api/v1", tags=["online-attempts"])

require_student = require("student")
MAX_KEY = 200


def _error(status: int, code: str, message: str,
           extra: dict[str, Any] | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


def _map(exc: Exception) -> HTTPException:
    if isinstance(exc, ResponseConflict):
        result = getattr(exc, "server_result", None)
        payload = result.model_dump(mode="json") if isinstance(result, SaveResponseResult) else None
        return _error(409, "response_conflict", exc.message, {"server_result": payload})
    if isinstance(exc, AttemptFinalized):
        return _error(409, "attempt_finalized", exc.message)
    if isinstance(exc, Withdrawn):
        return _error(409, "assignment_withdrawn", exc.message)
    if isinstance(exc, IdempotencyConflict):
        return _error(409, "idempotency_conflict", str(exc))
    if isinstance(exc, ResourceNotFound):
        return _error(404, "attempt_not_found", exc.message)
    if isinstance(exc, PolicyDenied):
        code = exc.message.split(":", 1)[0].strip() or "invalid_attempt_policy"
        return _error(422, code, exc.message)
    if isinstance(exc, AttemptError):
        return _error(409, exc.code, exc.message)
    return _error(500, "attempt_failed", f"{type(exc).__name__}: {exc}")


def _key(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        raise _error(400, "idempotency_key_required", "写接口必须带 Idempotency-Key")
    if len(text) > MAX_KEY:
        raise _error(400, "idempotency_key_required", "Idempotency-Key 太长")
    return text


class SaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_revision_id: str = Field(min_length=1, max_length=200)
    base_revision_no: int = Field(ge=0)
    raw_response: dict[str, Any] | None = None


def _student_id(actor: Actor) -> UUID:
    return ref_uuid("account", actor.id)


def _services():
    """每次请求建一套服务（仓储自己开事务）；测试通过 dependency_overrides 替换。"""
    repo = PostgresOnlineRepository()
    return repo, AttemptService(repository=repo), ResponseService(repository=repo)


def get_attempt_service(actor: Actor = Depends(require_student)) -> AttemptService:
    return AttemptService(repository=PostgresOnlineRepository())


def get_response_service(actor: Actor = Depends(require_student)) -> ResponseService:
    return ResponseService(repository=PostgresOnlineRepository())


@router.get("/assignments/{assignment_id}/attempt")
def current_attempt(
    assignment_id: UUID,
    actor: Actor = Depends(require_student),
    service: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    """页面重连时先问"我有没有正在考的卷"；没有就 204，前端给"开始"按钮。"""
    found = service.find_active(student_id=_student_id(actor),
                                assignment_id=assignment_id)
    if found is None:
        from fastapi import Response

        return Response(status_code=204)  # type: ignore[return-value]
    return {"attempt_id": str(found.attempt_id), "status": str(found.status),
            "attempt_no": found.attempt_no,
            "server_deadline_at": found.server_deadline_at.isoformat(),
            "worker_due_at": found.worker_due_at.isoformat(),
            "item_count": found.item_count}


@router.post("/assignments/{assignment_id}/attempts", status_code=201)
def start_attempt(
    assignment_id: UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_student),
    service: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    key = _key(idempotency_key)
    try:
        result = service.start(student_id=_student_id(actor),
                               assignment_id=assignment_id, idempotency_key=key)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"attempt_id": str(result.attempt_id), "status": str(result.status),
            "attempt_no": result.attempt_no, "reused": result.reused,
            "server_deadline_at": result.server_deadline_at.isoformat(),
            "worker_due_at": result.worker_due_at.isoformat(),
            "item_count": result.item_count}


@router.get("/attempts/{attempt_id}")
def get_attempt(
    attempt_id: UUID,
    actor: Actor = Depends(require_student),
    service: AttemptService = Depends(get_attempt_service),
) -> dict[str, Any]:
    """重连：返回整卷 + 本人最新响应；**不含任何答案字段**（构建时递归校验）。"""
    student_id = _student_id(actor)
    repo = PostgresOnlineRepository()
    try:
        row = service.owner_check(attempt_id=attempt_id, student_id=student_id)
        assignment = repo.assignment(row.assignment_id) if row else None
        if assignment is None:
            raise ResourceNotFound(f"作业不存在：{attempt_id}")
        window = compute_deadline(row.started_at, assignment.window, assignment.policy)
        include_slots = row.status in ("in_progress", "finalizing")
        dto = build_student_attempt(
            attempt_id=row.id, assignment_id=row.assignment_id,
            paper_version_id=row.paper_version_id, status=row.status,
            assignment_status=assignment.status, attempt_no=row.attempt_no,
            started_at=row.started_at, server_now=service._now(),  # noqa: SLF001
            window=window, policy=assignment.policy,
            slots=repo.student_slots(row.id), include_slots=include_slots,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return dto.model_dump(mode="json")


@router.patch("/attempts/{attempt_id}/slots/{slot_id}")
def save_slot(
    attempt_id: UUID,
    slot_id: UUID,
    body: SaveIn,
    actor: Actor = Depends(require_student),
    service: ResponseService = Depends(get_response_service),
) -> dict[str, Any]:
    try:
        result = service.save(
            attempt_id=attempt_id, slot_id=slot_id, student_id=_student_id(actor),
            client_revision_id=body.client_revision_id,
            base_revision_no=body.base_revision_no, raw_response=body.raw_response)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return result.model_dump(mode="json")


@router.post("/attempts/{attempt_id}/finalize")
def finalize_attempt(
    attempt_id: UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_student),
) -> dict[str, Any]:
    """交卷：已有 submission 时返回同一回执（幂等；学生可重复点）。"""
    _key(idempotency_key)
    service = SubmissionService(repository=PostgresOnlineRepository())
    try:
        receipt = service.finalize(attempt_id=attempt_id,
                                   student_id=_student_id(actor),
                                   trigger_kind="manual")
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return receipt.model_dump(mode="json")


def deadline_worker() -> DeadlineWorker:
    """到期收卷给 worker 用（`scripts/run_worker.py` 可按需要调用）。"""
    repo = PostgresOnlineRepository()
    return DeadlineWorker(submissions=SubmissionService(repository=repo),
                          repository=repo)


__all__ = ["deadline_worker", "get_attempt_service", "get_response_service", "router"]
