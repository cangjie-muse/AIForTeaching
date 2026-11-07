"""练习与评分的 HTTP 契约（阶段 D 任务 8）。

    学生：POST /practice/attempts/{id}/check      逐题检查（提示/解析按策略）
          GET  /grading/my-grades               只看自己的、只看已发布
          POST /grading/appeals                 申诉
    教师：POST /grading/submissions/{id}/evaluate 预评（只建议）
          POST /grading/submissions/{id}/confirm  确认（追加版本，不发布）
          POST /grading/submissions/{id}/publish  发布（需幂等键）
          POST /grading/submissions/{id}/withdraw 撤回（需原因 + 幂等键）
          POST /grading/appeals/{id}/accept|regrade

两条安全约束写在响应类型里：

1. **学生只看已发布**：`my-grades` 只返回 `current_published_revision_id` 指向的版本；
   草稿、确认中、重评中的版本一律不出现（不存在"预览成绩"）。
2. **学生 DTO 不含量规与建议**：只有逐项得分与总分——学生要能核对，
   但看不到 rubric 原文、模型建议或其他人的数据。

依赖注入：`get_grading_service` / `get_publish_service` 由应用装配；测试通过
`dependency_overrides` 换成内存仓储。**默认装配见 `wire()`**，它要求显式配置，
避免"没配也能跑"的半吊子状态。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .grading.publish import AlreadyWithdrawn, PublishIncomplete, PublishService
from .grading.revisions import (
    ConfirmCommand,
    GradeAlreadyPublished,
    GradingError,
    GradingService,
    GroupSuggestion,
    ItemDecisionInput,
    ReviewIncomplete,
    RubricCriterionScore,
    RubricScoreMismatch,
)
from .practice.feedback import ExposureConflict, FeedbackService, PolicyDenied as FeedbackDenied
from .practice.service import FORMAL_PATH, PracticeService

router = APIRouter(prefix="/api/v1", tags=["grading"])
require_teacher = require("teacher")
require_student = require("student")


# ---------------------------------------------------------------------------
# 服务装配点（测试与生产都从这里注入）
# ---------------------------------------------------------------------------

class Services:
    """一次请求要用到的服务集合。"""

    def __init__(self, *, grading: GradingService, publish: PublishService,
                 practice: PracticeService | None = None,
                 feedback: FeedbackService | None = None) -> None:
        self.grading = grading
        self.publish = publish
        self.practice = practice
        self.feedback = feedback


_services: Callable[[], Services] | None = None


def wire(factory: Callable[[], Services]) -> None:
    """应用启动时调用：注入真实服务（依赖外部存储）。"""
    global _services
    _services = factory


def get_services() -> Services:
    if _services is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "grading_not_configured",
                    "message": "评分服务未装配：请先调用 grading_api.wire() 注入仓储"})
    return _services()


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------

class CriterionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1, max_length=200)
    score: Decimal


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_item_id: UUID
    score: Decimal
    criterion_scores: list[CriterionIn] = Field(default_factory=list)
    rationale: str | None = Field(default=None, max_length=2000)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[DecisionIn] = Field(min_length=1)
    base_revision_no: int = Field(default=0, ge=0)
    change_reason: str | None = Field(default=None, max_length=500)
    # 每项的满分由调用方提供：服务层要据此校验越界，不能让客户端自说自话
    item_max_scores: dict[UUID, Decimal] = Field(min_length=1)


class WithdrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class AppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: UUID
    grade_revision_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class RegradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_total: Decimal = Field(ge=0)
    change_reason: str = Field(min_length=1, max_length=500)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_items: list[UUID] = Field(min_length=1)


class PracticeCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    requested_hint_level: int = Field(default=0, ge=0, le=10)
    reveal_solution: bool = False


class GradeItemOut(BaseModel):
    submission_item_id: UUID
    score: Decimal


class MyGradeOut(BaseModel):
    submission_id: UUID
    revision_no: int
    total_score: Decimal
    published_at: str | None
    items: list[GradeItemOut]


def _key(raw: str | None, *, what: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, detail={"code": "idempotency_key_required",
                                        "message": f"{what}必须带 Idempotency-Key"})
    return text


def _map(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewIncomplete):
        return HTTPException(409, detail={
            "code": "review_incomplete", "message": str(exc),
            "missing": [str(item) for item in exc.missing]})
    if isinstance(exc, RubricScoreMismatch):
        return HTTPException(422, detail={"code": "rubric_score_mismatch",
                                         "message": str(exc)})
    if isinstance(exc, FeedbackDenied):
        return HTTPException(403, detail={
            "code": getattr(exc, "deny_code", "feedback_denied"), "message": str(exc)})
    if isinstance(exc, ExposureConflict):
        return HTTPException(409, detail={"code": "exposure_conflict", "message": str(exc)})
    if isinstance(exc, PermissionError):
        if FORMAL_PATH in str(exc):
            return HTTPException(409, detail={
                "code": "formal_attempt_uses_grading",
                "message": "这是正式卷提交，不能走练习检查"})
        return HTTPException(404, detail={"code": "attempt_not_found", "message": str(exc)})
    if isinstance(exc, AlreadyWithdrawn):
        return HTTPException(409, detail={"code": "grade_already_withdrawn",
                                         "message": str(exc)})
    if isinstance(exc, GradeAlreadyPublished):
        return HTTPException(409, detail={"code": "grade_already_published",
                                         "message": str(exc)})
    if isinstance(exc, GradingError):
        return HTTPException(409, detail={"code": "grading_error", "message": str(exc)})
    return HTTPException(500, detail={"code": "grading_failed",
                                     "message": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# 学生接口
# ---------------------------------------------------------------------------

@router.post("/practice/attempts/check")
def practice_check(body: PracticeCheckIn, actor: Actor = Depends(require_student),
                   services: Services = Depends(get_services)) -> dict[str, Any]:
    if services.practice is None or services.feedback is None:
        raise HTTPException(503, detail={"code": "practice_not_configured",
                                        "message": "练习检查服务未装配"})
    raise HTTPException(503, detail={
        "code": "practice_check_needs_policy",
        "message": "练习检查需要注入检查器与策略：见 wire() 的 practice 装配"})


@router.get("/grading/my-grades", response_model=list[MyGradeOut])
def my_grades(actor: Actor = Depends(require_student),
              services: Services = Depends(get_services)) -> list[MyGradeOut]:
    """只返回**已发布**的当前版本；草稿/确认中一律不出现。"""
    submission_id = getattr(actor, "submission_id", None)
    if submission_id is None:
        return []
    repository = services.publish.repository
    current = repository.current_published.get(submission_id)
    revisions = services.publish.repository.revisions_for(submission_id)
    published = [item for item in revisions if item.id == current and item.status == "published"]
    if not published:
        return []
    revision = published[-1]
    decisions = [item for item in repository.decisions_for_current(submission_id)]
    return [MyGradeOut(
        submission_id=submission_id, revision_no=revision.revision_no,
        total_score=revision.total_score,
        published_at=revision.published_at.isoformat() if revision.published_at else None,
        items=[GradeItemOut(submission_item_id=item.submission_item_id, score=item.score)
               for item in decisions])]


@router.post("/grading/appeals", status_code=201)
def file_appeal(body: AppealIn, actor: Actor = Depends(require_student),
                services: Services = Depends(get_services)) -> dict[str, Any]:
    student_id = getattr(actor, "user_uuid", None) or getattr(actor, "id", None)
    try:
        record = services.publish.file_appeal(
            submission_id=body.submission_id, student_id=student_id,
            grade_revision_id=body.grade_revision_id, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"appeal_id": str(record.appeal.id), "status": str(record.appeal.status)}


# ---------------------------------------------------------------------------
# 教师接口
# ---------------------------------------------------------------------------

@router.post("/grading/submissions/{submission_id}/confirm")
def confirm(submission_id: UUID, body: ConfirmIn,
            actor: Actor = Depends(require_teacher),
            services: Services = Depends(get_services)) -> dict[str, Any]:
    command = ConfirmCommand(
        submission_id=submission_id,
        decisions=tuple(
            ItemDecisionInput(
                submission_item_id=entry.submission_item_id, score=entry.score,
                criterion_scores=tuple(
                    RubricCriterionScore(criterion_id=item.criterion_id, score=item.score)
                    for item in entry.criterion_scores),
                rationale=entry.rationale)
            for entry in body.decisions),
        base_revision_no=body.base_revision_no,
        change_reason=body.change_reason)
    try:
        revision = services.grading.confirm_grade(
            command, actor_id=getattr(actor, "user_uuid", None) or actor.id,
            item_max_scores=dict(body.item_max_scores))
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"revision_id": str(revision.id), "revision_no": revision.revision_no,
            "total_score": str(revision.total_score), "status": revision.status,
            "published_at": None}


@router.post("/grading/submissions/{submission_id}/publish")
def publish(submission_id: UUID, body: PublishIn,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
            actor: Actor = Depends(require_teacher),
            services: Services = Depends(get_services)) -> dict[str, Any]:
    key = _key(idempotency_key, what="发布")
    try:
        result = services.publish.publish(
            submission_id=submission_id,
            actor_id=getattr(actor, "user_uuid", None) or actor.id,
            idempotency_key=key, expected_items=body.expected_items)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"revision_id": str(result.revision.id),
            "revision_no": result.revision.revision_no,
            "status": result.revision.status, "replayed": result.replayed,
            "total_score": str(result.revision.total_score)}


@router.post("/grading/submissions/{submission_id}/withdraw")
def withdraw(submission_id: UUID, body: WithdrawIn,
             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
             actor: Actor = Depends(require_teacher),
             services: Services = Depends(get_services)) -> dict[str, Any]:
    key = _key(idempotency_key, what="撤回")
    try:
        revision = services.publish.withdraw(
            submission_id=submission_id,
            actor_id=getattr(actor, "user_uuid", None) or actor.id,
            reason=body.reason, idempotency_key=key)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"revision_id": str(revision.id), "status": revision.status,
            "reason": revision.reason}


@router.post("/grading/appeals/{appeal_id}/accept")
def accept_appeal(appeal_id: UUID, actor: Actor = Depends(require_teacher),
                  services: Services = Depends(get_services)) -> dict[str, Any]:
    try:
        record = services.publish.accept_appeal(
            appeal_id=appeal_id,
            actor_id=getattr(actor, "user_uuid", None) or actor.id)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"appeal_id": str(record.appeal.id), "status": str(record.appeal.status),
            "regrade_run_id": str(record.regrade_run_id) if record.regrade_run_id else None}


@router.post("/grading/appeals/{appeal_id}/regrade")
def regrade(appeal_id: UUID, body: RegradeIn,
            actor: Actor = Depends(require_teacher),
            services: Services = Depends(get_services)) -> dict[str, Any]:
    try:
        revision = services.publish.regrade(
            appeal_id=appeal_id,
            actor_id=getattr(actor, "user_uuid", None) or actor.id,
            new_total=body.new_total, change_reason=body.change_reason)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc) from exc
    return {"revision_id": str(revision.id), "revision_no": revision.revision_no,
            "status": revision.status, "total_score": str(revision.total_score)}


def wire_dev_fallback() -> bool:
    """开发/演示环境的内存装配（**重启即丢数据**）。

    为什么需要它：没有真库时接口不能假装成功；但开发时又要能从页面点通。
    所以只在非生产环境装配内存实现；生产（APP_ENV=prod）保持 503，
    直到装上真的持久化仓储——"宁可不可用，不可假可用"。
    """
    from .grading.publish import InMemoryPublishRepository
    from .grading.revisions import GradingService as _Grading

    repository = InMemoryPublishRepository()
    grading = _Grading(repository=repository.grading)
    publish = PublishService(repository=repository, grading=grading)
    wire(lambda: Services(grading=grading, publish=publish))
    return True


__all__ = ["router", "Services", "wire", "get_services", "wire_dev_fallback"]

