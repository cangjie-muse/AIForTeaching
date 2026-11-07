"""教师确认评分：rubric 逐项决定、追加 grade revision、AI 只能建议（阶段 D 任务 5）。

这一层管的是"分数从哪来"，三个不可越界的点：

1. **每一项都要有教师本人的当前决定。** 预评只是建议：如果某一项没有教师决定，
   确认必须整批失败（`ReviewIncomplete`），并把缺项带出去——跳过它会让总分
   少算一项，而学生与教师都不知道。

2. **确认不发布。** `confirm_grade` 追加一条 `status="confirmed"` 的版本，
   `published_at` 保持为空；发布是任务 6 的独立动作。两件事分开，
   才能做到"教师可以在发布前反复调整而不惊动学生"。

3. **AI 只能建议。** `suggest_groups` 只产生 `status="proposed"` 的建议，
   **不写任何** grade revision、也不写任何 item decision。数据库层另有一道
   （0037 的 append-only 触发器 + RLS），这里再拦一次是"双保险"：
   模型输出永远不能成为分数。

改分的语义接上 0037：追加 `revision_no+1`，指回上一版；旧版本分数原样保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID, uuid4

from pydantic import Field

from ..contracts.common import ContractModel, Score
from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    GradeError as _ContractGradeError,
    GradeRevision,
    SubmissionGradeView,
)


GradingError = _ContractGradeError
"""与 contracts.GradeError 完全同一个类。

曾经这里与 contracts 各定义了一个 GradeError：两个同名类导致
`except GradingError` 抓不住发布模块抛出的错误——测试当场就红了。
"""


class ReviewIncomplete(GradingError):
    """有提交项缺少教师本人的当前决定：整批拒绝。"""

    code = "review_incomplete"

    def __init__(self, message: str, *, missing: Sequence[UUID]) -> None:
        super().__init__(message)
        self.missing = tuple(missing)


class RubricScoreMismatch(GradingError):
    code = "rubric_score_mismatch"


class GradeAlreadyPublished(GradingError):
    code = "grade_already_published"


@dataclass(frozen=True)
class RubricCriterionScore:
    criterion_id: str
    score: Decimal


@dataclass(frozen=True)
class ItemDecisionInput:
    """教师对某一项的决定（提交前构造，不在别处悄悄生成）。"""

    submission_item_id: UUID
    score: Decimal
    criterion_scores: tuple[RubricCriterionScore, ...] = ()
    rationale: str | None = None
    sequence: int = 1


@dataclass(frozen=True)
class DecisionRecord:
    """已落库的一条逐项决定（append-only）。

    `id` 必须存在：后一条要指回它（supersedes_id），否则"改主意"的链条断了。
    """

    id: UUID
    submission_item_id: UUID
    grade_revision_id: UUID
    sequence: int
    score: Decimal
    decided_by: UUID
    decided_at: datetime
    rationale: str | None = None
    supersedes_id: UUID | None = None


class InMemoryGradingRepository:
    def __init__(self) -> None:
        self.grade_revisions: list[GradeRevision] = []
        self.decisions: list[DecisionRecord] = []
        self.suggestions: list[dict[str, Any]] = []

    def revisions_for(self, submission_id: UUID) -> tuple[GradeRevision, ...]:
        return tuple(item for item in self.grade_revisions
                     if item.submission_id == submission_id)

    def save_revision(self, revision: GradeRevision) -> GradeRevision:
        self.grade_revisions.append(revision)
        return revision

    def decisions_for(self, submission_id: UUID) -> tuple[DecisionRecord, ...]:
        revision_ids = {item.id for item in self.revisions_for(submission_id)}
        return tuple(item for item in self.decisions
                     if item.grade_revision_id in revision_ids)

    def save_decision(self, record: DecisionRecord) -> None:
        # 与 0037 的 UNIQUE(submission_item_id, decision_seq) 同义：同一项的同号决定
        # 只能有一条，改主意必须换一个序号并指回被取代的那条。
        if any(item.submission_item_id == record.submission_item_id
               and item.sequence == record.sequence for item in self.decisions):
            raise ValueError("uq_item_decision: (提交项, 序号) 重复")
        self.decisions.append(record)

    def last_decision(self, submission_item_id: UUID) -> DecisionRecord | None:
        found = [item for item in self.decisions
                 if item.submission_item_id == submission_item_id]
        return max(found, key=lambda item: item.sequence) if found else None


@dataclass(frozen=True)
class ConfirmCommand:
    submission_id: UUID
    decisions: tuple[ItemDecisionInput, ...]
    base_revision_no: int = 0
    change_reason: str | None = None


@dataclass(frozen=True)
class GroupSuggestion:
    """AI 的建议：只有"建议"状态，永远不能直接进分数。"""

    id: UUID
    submission_id: UUID
    item_ids: tuple[UUID, ...]
    proposed_score: Decimal | None
    rationale: str | None
    status: str = "proposed"   # proposed | accepted | rejected


class GroupingAdvisor:
    """分组建议端口（真实实现来自模型；测试注入桩）。"""

    def suggest(self, *, submission_id: UUID, items: Sequence[UUID]) -> Sequence[GroupSuggestion]:
        return ()


class GradingService:
    def __init__(
        self,
        *,
        repository: InMemoryGradingRepository,
        advisor: GroupingAdvisor | None = None,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.advisor = advisor or GroupingAdvisor()
        self._now = clock
        self._new_id = id_factory

    # -- 确认评分（追加版本，不发布） ------------------------------------
    def confirm_grade(self, command: ConfirmCommand, *, actor_id: UUID,
                      item_max_scores: dict[UUID, Decimal]) -> GradeRevision:
        expected = set(item_max_scores)
        provided = {entry.submission_item_id for entry in command.decisions}
        missing = sorted(str(item) for item in expected - provided)
        if missing:
            raise ReviewIncomplete(
                f"{len(missing)} 个提交项还没有教师确认的决定", missing=expected - provided)

        for entry in command.decisions:
            max_score = item_max_scores.get(entry.submission_item_id, Decimal("0"))
            if entry.score < 0 or entry.score > max_score:
                raise GradingError(
                    f"score_out_of_range: {entry.score} 不在 0..{max_score}")
            if entry.criterion_scores:
                total = sum((criterion.score for criterion in entry.criterion_scores),
                            Decimal("0"))
                if total != entry.score:
                    # rubric 分项与项分不一致：会让"分项表"和"总分"对不上。
                    raise RubricScoreMismatch(
                        f"rubric_score_mismatch: 分项合计 {total} ≠ 项分 {entry.score}")

        revisions = sorted(self.repository.revisions_for(command.submission_id),
                           key=lambda item: item.revision_no)
        if revisions and revisions[-1].status == "published":
            # 已发布的成绩要改，必须先撤回/重评（任务 6/7 的流程），
            # 不能在这里悄悄追加一版让两版都"已发布"。
            raise GradeAlreadyPublished(
                "grade_already_published: 先撤回已发布版本，再确认新版本")
        previous = revisions[-1] if revisions else None
        if previous is not None and command.base_revision_no != previous.revision_no:
            raise GradingError(
                f"base_revision_stale: 客户端基于第 {command.base_revision_no} 版，"
                f"当前是第 {previous.revision_no} 版")

        now = self._now()
        total = sum((entry.score for entry in command.decisions), Decimal("0"))
        if previous is not None and previous.status == "draft":
            # 重评（regrade）产生的草稿版本：教师的确认应当落在**这一版**上，
            # 而不是再追加一版——否则一次申诉会留下两个几乎相同的版本，
            # 回执与申诉号也对不上。
            revision = previous.model_copy(update={
                "total_score": total, "status": "confirmed",
                "reason": command.change_reason or previous.reason,
            })
            self.repository.save_revision(revision)
            self._save_decisions(command, revision, actor_id, now)
            return revision
        revision = GradeRevision(
            id=self._new_id(), submission_id=command.submission_id,
            revision_no=(previous.revision_no + 1) if previous else 1,
            total_score=total, status="confirmed", published_at=None,
            published_by=None, reason=command.change_reason,
            previous_revision_id=previous.id if previous else None, created_at=now,
        )
        self.repository.save_revision(revision)
        self._save_decisions(command, revision, actor_id, now)
        return revision

    def _save_decisions(self, command: ConfirmCommand, revision: GradeRevision,
                        actor_id: UUID, now: datetime) -> None:
        for entry in command.decisions:
            # 序号由服务端确定：同一提交项在第二版里必须用新号（DB 的唯一键是
            # (提交项, 序号)），并指回上一版那条，改分链因此可追溯。
            previous_decision = self.repository.last_decision(entry.submission_item_id)
            sequence = 1 if previous_decision is None else previous_decision.sequence + 1
            self.repository.save_decision(DecisionRecord(
                id=self._new_id(),
                submission_item_id=entry.submission_item_id,
                grade_revision_id=revision.id, sequence=sequence,
                score=entry.score, decided_by=actor_id, decided_at=now,
                rationale=entry.rationale,
                supersedes_id=previous_decision.id if previous_decision else None,
            ))

    # -- AI 分组建议（永远只是建议） -------------------------------------
    def suggest_groups(self, *, submission_id: UUID,
                       item_ids: Sequence[UUID]) -> tuple[GroupSuggestion, ...]:
        if not item_ids:
            return ()
        suggestions = self.advisor.suggest(submission_id=submission_id, items=item_ids)
        out: list[GroupSuggestion] = []
        for suggestion in suggestions:
            # 建议统一标成 proposed，且**不落**任何分数事实。
            out.append(GroupSuggestion(
                id=suggestion.id, submission_id=submission_id,
                item_ids=tuple(suggestion.item_ids),
                proposed_score=suggestion.proposed_score,
                rationale=suggestion.rationale, status="proposed"))
        self.repository.suggestions.extend(
            {"submission_id": submission_id, "status": "proposed"} for _ in out)
        return tuple(out)

    def accept_suggestion(self, suggestion: GroupSuggestion, *,
                          actor_id: UUID, score: Decimal) -> GroupSuggestion:
        """采纳建议 = 教师把建议变成一个**新的待确认决定**，而不是直接写成绩。"""
        if suggestion.status != "proposed":
            raise GradingError(f"suggestion_status_{suggestion.status}")
        return GroupSuggestion(
            id=suggestion.id, submission_id=suggestion.submission_id,
            item_ids=suggestion.item_ids, proposed_score=score,
            rationale=suggestion.rationale, status="accepted")


# ---------------------------------------------------------------------------
# 快照/复验：发布只能读取"当前版本"的决定
# ---------------------------------------------------------------------------

def decisions_of_current_revision(repository: InMemoryGradingRepository,
                                  *, submission_id: UUID) -> tuple[DecisionRecord, ...]:
    revisions = repository.revisions_for(submission_id)
    if not revisions:
        return ()
    current = max(revisions, key=lambda item: item.revision_no)
    return tuple(item for item in repository.decisions
                 if item.grade_revision_id == current.id)


def grade_manifest_hash(revision: GradeRevision,
                        decisions: Iterable[DecisionRecord]) -> str:
    """成绩的规范哈希：总分 + 每个决定，供回执与审计核对。"""
    return canonical_hash({
        "submission": str(revision.submission_id),
        "revision_no": revision.revision_no,
        "total": str(revision.total_score),
        "decisions": sorted(
            [{"item": str(item.submission_item_id), "score": str(item.score),
              "seq": item.sequence} for item in decisions],
            key=lambda entry: entry["item"]),
    })


__all__ = [
    "ConfirmCommand",
    "DecisionRecord",
    "GradingError",
    "GradingService",
    "GradeAlreadyPublished",
    "GroupSuggestion",
    "GroupingAdvisor",
    "InMemoryGradingRepository",
    "ItemDecisionInput",
    "ReviewIncomplete",
    "RubricCriterionScore",
    "RubricScoreMismatch",
    "decisions_of_current_revision",
    "grade_manifest_hash",
]
