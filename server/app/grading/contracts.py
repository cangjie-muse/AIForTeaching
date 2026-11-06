"""成绩与人工评分（阶段 D）：追加版本、发布门禁与申诉闭环。

三条不可违反的规则（设计 §12.3、§0.3 规则 5/6）：

1. **成绩只能追加。** 改分不是 UPDATE，而是新增一条 `GradeRevision`；
   历史版本永远可读（申诉、复核、审计都要靠它）。
2. **未确认的人工分不能发布。** 发布门禁是"该提交的每个提交项都已有人工确认的分数"；
   只要有一项 `requires_human` 还没确认，发布就整批拒绝（不是"跳过那一项"）。
3. **练习不写正式成绩。** 练习检查结果只进反馈，不进 `grade`——否则一次练习就能
   改掉学期成绩。

本模块只做纯逻辑与状态机；落库由阶段 D 任务 1 的迁移与仓储负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Sequence
from uuid import UUID

from pydantic import Field

from ..contracts.common import ContractModel, Score, validate_score

GradeStatus = Literal["draft", "confirmed", "published", "withdrawn", "regraded"]


class GradeError(ValueError):
    code = "grade_error"


class GradeNotDraft(GradeError):
    code = "grade_not_draft"


class GradePublishBlocked(GradeError):
    code = "grade_publish_blocked"

    def __init__(self, message: str, *, blocking: Sequence["SubmissionGradeView"]) -> None:
        super().__init__(message)
        self.blocking = tuple(blocking)


class SubmissionGradeView(ContractModel):
    """一个提交项的评分现状（教师确认后的分数才作数）。"""

    submission_item_id: UUID
    slot_no: int = Field(ge=1)
    suggested_score: Decimal | None = None
    confirmed_score: Decimal | None = None
    requires_human: bool = False
    confirmed_by: UUID | None = None
    confirmed_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.confirmed_score is not None

    @property
    def blocking(self) -> bool:
        return self.requires_human and self.confirmed_score is None


class GradeRevision(ContractModel):
    """一条不可变的成绩版本。

    `revision_no` 单调递增；`previous_revision_id` 指回上一版——改分链因此可追溯。
    学生看到的"当前成绩"永远是 `revision_no` 最大的那条已发布版本。
    """

    id: UUID
    submission_id: UUID
    revision_no: int = Field(ge=1)
    total_score: Score = Field(ge=0)
    status: GradeStatus
    published_at: datetime | None = None
    published_by: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)
    previous_revision_id: UUID | None = None
    # 申诉触发的改分要能指回申诉单
    appeal_id: UUID | None = None
    created_at: datetime


def total_of(grades: Sequence[SubmissionGradeView]) -> Decimal:
    """总分 = 已确认分数的和；未确认的项**不加 0**，而是抛出——加 0 会悄悄压低总分。"""
    unconfirmed = [item for item in grades if not item.ready]
    if unconfirmed:
        raise GradePublishBlocked(
            "有提交项还没确认分数，不能计算总分", blocking=unconfirmed)
    return sum((item.confirmed_score or Decimal("0") for item in grades),
               Decimal("0"))


def publish_checks(grades: Sequence[SubmissionGradeView]) -> list[SubmissionGradeView]:
    """返回阻塞发布的项（空 = 可以发布）。"""
    return [item for item in grades if item.blocking]


def assert_publishable(grades: Sequence[SubmissionGradeView]) -> Decimal:
    """发布门禁：任何一项待人工确认 → 整批拒绝，并把阻塞项带出去。"""
    blocking = publish_checks(grades)
    if blocking:
        raise GradePublishBlocked(
            f"还有 {len(blocking)} 个提交项需要人工确认，不能发布成绩",
            blocking=blocking)
    return total_of(grades)


class AppealStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CLOSED = "closed"


@dataclass(frozen=True)
class Appeal:
    id: UUID
    submission_id: UUID
    student_id: UUID
    reason: str
    status: AppealStatus
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: UUID | None = None


def accept_appeal(appeal: Appeal, *, actor_id: UUID, now: datetime) -> Appeal:
    """受理申诉：只是开了一个"可以重新评分"的入口，**不改成绩**。"""
    if appeal.status is not AppealStatus.OPEN:
        raise GradeError(f"申诉状态是 {appeal.status}，只有 open 能受理")
    return Appeal(id=appeal.id, submission_id=appeal.submission_id,
                  student_id=appeal.student_id, reason=appeal.reason,
                  status=AppealStatus.ACCEPTED, created_at=appeal.created_at,
                  decided_at=now, decided_by=actor_id)


def regrade_revision(*, previous: GradeRevision, new_total: Decimal,
                     actor_id: UUID, now: datetime, appeal_id: UUID | None = None,
                     reason: str | None = None,
                     revision_id: UUID) -> GradeRevision:
    """改分：新增一版，**不覆盖**上一版；上一版状态标成 `regraded`。"""
    validate_score(new_total)
    if previous.status not in ("published", "confirmed", "regraded"):
        raise GradeNotDraft(f"上一版状态是 {previous.status}，不能作为改分基线")
    return GradeRevision(
        id=revision_id, submission_id=previous.submission_id,
        revision_no=previous.revision_no + 1, total_score=new_total,
        status="draft", published_at=None, published_by=None,
        reason=reason or "regrade", previous_revision_id=previous.id,
        appeal_id=appeal_id, created_at=now,
    )


def publish_revision(revision: GradeRevision, *, actor_id: UUID,
                     now: datetime) -> GradeRevision:
    """发布一版成绩：只有 draft 能发布，发布时刻与发布人必须记下来。"""
    if revision.status != "draft":
        raise GradeNotDraft(f"成绩版本状态是 {revision.status}，只有 draft 能发布")
    return revision.model_copy(update={
        "status": "published", "published_at": now, "published_by": actor_id})


def current_for_student(revisions: Sequence[GradeRevision]) -> GradeRevision | None:
    """学生看到的当前成绩 = 最大的已发布版本（撤回的版本不算）。"""
    published = [item for item in revisions if item.status == "published"]
    return max(published, key=lambda item: item.revision_no, default=None)


__all__ = [
    "Appeal",
    "AppealStatus",
    "GradeError",
    "GradeNotDraft",
    "GradePublishBlocked",
    "GradeRevision",
    "GradeStatus",
    "SubmissionGradeView",
    "accept_appeal",
    "assert_publishable",
    "current_for_student",
    "publish_checks",
    "publish_revision",
    "regrade_revision",
    "total_of",
]
