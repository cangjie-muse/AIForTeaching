"""成绩发布、撤回与申诉闭环（阶段 D 任务 6—7）。

发布是把"教师确认过的建议"变成"学生可见的分数"的那一步，因此它比确认更严格：

1. **发布前置校验**：当前版本每个提交项都必须有决定；缺一项就整批拒绝。
   确认时校验过还不够——确认与发布之间可能有人追加了提交项。
2. **幂等**：同一 `Idempotency-Key` 重复发布返回同一结果（`replayed=True`），
   不新增版本、不改发布时间。学生刷新页面/教师连点两次都会用到。
3. **撤回**：已发布版本不能原地改；撤回就是新增一条 `withdrawn` 版本，
   把 `grade.current_published_revision_id` 清空，并留下撤回原因。
   学生看到的"当前成绩"随之变成"无已发布成绩"。
4. **申诉闭环**：open → accepted → regrade → 新的 confirmed 版本（带 appeal_id）
   → 再发布。申诉本身**不改分**；改分只能由教师确认的新版本产生。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    Appeal,
    AppealStatus,
    GradeError,
    GradeRevision,
    accept_appeal,
    assert_publishable,
    publish_checks,
    publish_revision,
    regrade_revision,
    SubmissionGradeView,
)
from .revisions import (
    ConfirmCommand,
    DecisionRecord,
    GradingService,
    GradeAlreadyPublished,
    InMemoryGradingRepository,
    ReviewIncomplete,
)

PUBLISH_SCOPE = "grade-publish"
WITHDRAW_SCOPE = "grade-withdraw"


class PublishIncomplete(ReviewIncomplete):
    code = "grade_publish_incomplete"


class AlreadyWithdrawn(GradeError):
    code = "grade_already_withdrawn"


@dataclass(frozen=True)
class PublishResult:
    revision: GradeRevision
    replayed: bool
    fingerprint: str


@dataclass(frozen=True)
class AppealRecord:
    appeal: Appeal
    regrade_run_id: UUID | None = None


class InMemoryPublishRepository:
    """在 revisions 仓储之上记录发布状态、幂等键与撤回。"""

    def __init__(self, grading: InMemoryGradingRepository | None = None) -> None:
        self.grading = grading or InMemoryGradingRepository()
        self.current_published: dict[UUID, UUID] = {}   # submission -> revision id
        self.idempotency: dict[tuple[str, str, UUID], tuple[str, PublishResult]] = {}
        self.appeals: list[AppealRecord] = []
        self.regrade_runs: list[dict[str, Any]] = []

    # -- 读写（供 GradingService 复用） ----------------------------------
    def revisions_for(self, submission_id: UUID) -> tuple[GradeRevision, ...]:
        return self.grading.revisions_for(submission_id)

    def save_revision(self, revision: GradeRevision) -> GradeRevision:
        return self.grading.save_revision(revision)

    def decisions_for_current(self, submission_id: UUID) -> tuple[DecisionRecord, ...]:
        revisions = self.grading.revisions_for(submission_id)
        if not revisions:
            return ()
        current = max(revisions, key=lambda item: item.revision_no)
        return tuple(item for item in self.grading.decisions
                     if item.grade_revision_id == current.id)

    def remember_idempotent(self, scope: str, key: str, actor: UUID,
                            fingerprint: str, result: PublishResult) -> None:
        self.idempotency[(scope, key, actor)] = (fingerprint, result)

    def find_idempotent(self, scope: str, key: str, actor: UUID) -> tuple[str, PublishResult] | None:
        return self.idempotency.get((scope, key, actor))


class PublishService:
    def __init__(
        self,
        *,
        repository: InMemoryPublishRepository,
        grading: GradingService,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.grading = grading
        self._now = clock
        self._new_id = id_factory

    # -- 发布 -----------------------------------------------------------
    def publish(self, *, submission_id: UUID, actor_id: UUID,
                idempotency_key: str,
                expected_items: Sequence[UUID]) -> PublishResult:
        key = (idempotency_key or "").strip()
        if not key:
            raise GradeError("idempotency_key_required: 发布必须带幂等键")
        fingerprint = canonical_hash({"submission": str(submission_id),
                                     "items": sorted(str(item) for item in expected_items)})
        cached = self.repository.find_idempotent(PUBLISH_SCOPE, key, actor_id)
        if cached is not None:
            stored_fingerprint, result = cached
            if stored_fingerprint != fingerprint:
                raise GradeError("idempotency_conflict: 同一幂等键用于不同请求")
            return PublishResult(revision=result.revision, replayed=True,
                                 fingerprint=fingerprint)

        revisions = sorted(self.repository.revisions_for(submission_id),
                           key=lambda item: item.revision_no)
        if not revisions:
            raise GradeError("no_grade_to_publish: 还没有确认过的成绩版本")
        current = revisions[-1]
        if current.status == "published":
            # 重复发布同一版本：幂等返回（不新增版本）。
            result = PublishResult(revision=current, replayed=True, fingerprint=fingerprint)
            self.repository.remember_idempotent(PUBLISH_SCOPE, key, actor_id,
                                               fingerprint, result)
            self.repository.current_published[submission_id] = current.id
            return result
        if current.status in ("withdrawn", "regraded"):
            raise GradeAlreadyPublished(
                "grade_already_withdrawn: 当前版本已撤回，需先确认新版本再发布")

        decided = {record.submission_item_id
                   for record in self.repository.decisions_for_current(submission_id)}
        missing = sorted(str(item) for item in expected_items if item not in decided)
        if missing:
            raise PublishIncomplete(
                f"{len(missing)} 个提交项在当前版本里没有教师决定，不能发布",
                missing=tuple(UUID(item) for item in missing))

        # contracts.publish_revision 只接受 draft；把"教师确认"当成 draft 前的状态，
        # 发布时先推进到 draft（状态机单点，不在这里复制一套规则）。
        publishable = (current.model_copy(update={"status": "draft"})
                       if current.status == "confirmed" else current)
        published = publish_revision(publishable, actor_id=actor_id, now=self._now())
        self.repository.save_revision(published)
        # 上一版（若有）标成已重评：历史保留、不再是"当前"
        if len(revisions) > 1:
            previous = revisions[-2]
            self.repository.save_revision(previous.model_copy(update={"status": "regraded"}))
        self.repository.current_published[submission_id] = published.id
        result = PublishResult(revision=published, replayed=False, fingerprint=fingerprint)
        self.repository.remember_idempotent(PUBLISH_SCOPE, key, actor_id,
                                           fingerprint, result)
        return result

    # -- 撤回 -----------------------------------------------------------
    def withdraw(self, *, submission_id: UUID, actor_id: UUID,
                 reason: str, idempotency_key: str) -> GradeRevision:
        key = (idempotency_key or "").strip()
        if not key:
            raise GradeError("idempotency_key_required: 撤回必须带幂等键")
        if not (reason or "").strip():
            raise GradeError("withdraw_reason_required: 撤回必须写原因（学生要看到）")
        revisions = sorted(self.repository.revisions_for(submission_id),
                           key=lambda item: item.revision_no)
        if not revisions:
            raise AlreadyWithdrawn("grade_already_withdrawn: 没有可撤回的已发布成绩")
        fingerprint = canonical_hash({"submission": str(submission_id),
                                      "revision": revisions[-1].id, "reason": reason})
        # 幂等优先：同一键重试必须返回同一条撤回结果，而不是先被状态检查挡住。
        cached = self.repository.find_idempotent(WITHDRAW_SCOPE, key, actor_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1].revision
        if revisions[-1].status != "published":
            raise AlreadyWithdrawn("grade_already_withdrawn: 没有可撤回的已发布成绩")
        current = revisions[-1]
        withdrawn = current.model_copy(update={
            "status": "withdrawn", "reason": reason,
            "published_at": None, "published_by": None,
        })
        self.repository.save_revision(withdrawn)
        self.repository.current_published.pop(submission_id, None)
        result = PublishResult(revision=withdrawn, replayed=False,
                               fingerprint=fingerprint)
        self.repository.remember_idempotent(WITHDRAW_SCOPE, key, actor_id,
                                            fingerprint, result)
        return withdrawn

    # -- 申诉与重评 ------------------------------------------------------
    def file_appeal(self, *, submission_id: UUID, student_id: UUID,
                    grade_revision_id: UUID, reason: str) -> AppealRecord:
        if not (reason or "").strip():
            raise GradeError("appeal_reason_required: 申诉必须说明理由")
        if not any(item.id == grade_revision_id
                   for item in self.repository.revisions_for(submission_id)):
            raise GradeError("appeal_grade_not_found: 申诉指向的成绩版本不存在")
        record = AppealRecord(Appeal(
            id=self._new_id(), submission_id=submission_id, student_id=student_id,
            reason=reason, status=AppealStatus.OPEN, created_at=self._now()))
        self.repository.appeals.append(record)
        return record

    def accept_appeal(self, *, appeal_id: UUID, actor_id: UUID) -> AppealRecord:
        for index, record in enumerate(self.repository.appeals):
            if record.appeal.id != appeal_id:
                continue
            accepted = accept_appeal(record.appeal, actor_id=actor_id, now=self._now())
            run_id = self._new_id()
            updated = AppealRecord(appeal=accepted, regrade_run_id=run_id)
            self.repository.appeals[index] = updated
            self.repository.regrade_runs.append({
                "id": run_id, "appeal_id": appeal_id,
                "submission_id": accepted.submission_id, "status": "running",
                "started_at": self._now(),
            })
            return updated
        raise GradeError("appeal_not_found: 没有这条申诉")

    def regrade(self, *, appeal_id: UUID, actor_id: UUID,
                new_total: Decimal, change_reason: str) -> GradeRevision:
        """重评 = 追加新版本（带 appeal_id），**不发布**；发布仍走 publish。"""
        record = next((item for item in self.repository.appeals
                       if item.appeal.id == appeal_id), None)
        if record is None or record.appeal.status is not AppealStatus.ACCEPTED:
            raise GradeError("appeal_not_accepted: 只有已受理的申诉能重评")
        revisions = sorted(self.repository.revisions_for(record.appeal.submission_id),
                           key=lambda item: item.revision_no)
        if not revisions:
            raise GradeError("no_grade_to_regrade: 还没有成绩版本")
        base = revisions[-1]
        updated = regrade_revision(
            previous=base, new_total=new_total, actor_id=actor_id, now=self._now(),
            appeal_id=appeal_id, reason=change_reason,
            revision_id=self._new_id())
        self.repository.save_revision(updated)
        return updated


__all__ = [
    "AlreadyWithdrawn",
    "AppealRecord",
    "InMemoryPublishRepository",
    "PUBLISH_SCOPE",
    "PublishIncomplete",
    "PublishResult",
    "PublishService",
    "WITHDRAW_SCOPE",
]
