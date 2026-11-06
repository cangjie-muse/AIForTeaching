"""交卷（阶段 C 任务 5）与到期收卷（任务 6）：不可变提交项与幂等回执。

三件事必须一次做对，否则只能靠事后补数据：

1. **交卷先锁 attempt。** 手动交卷与到期 worker 可能同时触发；先锁再判"有没有
   submission"，保证一次作答**只有一份**交卷记录、一份快照。
2. **快照只看截止前的修订。** 资格是 `server_received_at <= server_deadline_at`；
   交卷之后、甚至截止之后的一瞬间到达的修订都不能进快照（§0.3 规则 3）。
3. **回执由内容决定。** `response_manifest_hash` 覆盖每个槽位的"槽位 + 修订号 +
   响应哈希"，`receipt_hash` 再覆盖计数与触发方式。同一份答案重试交卷必须得到
   同一回执——学生才能拿它去申诉。

与计划书的差异：计划的 `submission_item` 要写 `answer_test_run_id`（阶段 D 才有），
本阶段留空并保持 `evaluation_status='pending'`，**不提前实现阶段 D 的评分**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Literal
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    AttemptFinalized,
    ResourceNotFound,
    SubmissionReceiptDTO,
)

TriggerKind = Literal["manual", "deadline", "forced"]


@dataclass(frozen=True)
class SubmissionItem:
    """一个槽位在快照里的样子（不可变）。

    `raw_response` 允许为 None：**没作答**与**作答为空**是两件事，前者是 unanswered，
    后者是 invalid(empty)。混为一谈会让"未答"和"答了空白"在统计里变成同一个数。
    """

    slot_id: UUID
    assignment_item_id: UUID
    revision_no: int
    raw_response: dict[str, Any] | None
    parse_status: str
    response_hash: str
    unanswered: bool


@dataclass(frozen=True)
class SubmissionRecord:
    id: UUID
    attempt_id: UUID
    student_id: UUID
    trigger_kind: TriggerKind
    submitted_at: datetime
    item_count: int
    unanswered_count: int
    invalid_count: int
    response_manifest_hash: str
    receipt_hash: str
    items: tuple[SubmissionItem, ...]

    def to_receipt(self, *, replayed: bool = False) -> SubmissionReceiptDTO:
        return SubmissionReceiptDTO(
            submission_id=self.id, attempt_id=self.attempt_id,
            finalized_at=self.submitted_at, item_count=self.item_count,
            unanswered_count=self.unanswered_count,
            invalid_count=self.invalid_count,
            response_manifest_hash=self.response_manifest_hash,
            receipt_hash=self.receipt_hash, submission_reason=self.trigger_kind,
        )

    @property
    def replayed(self) -> bool:  # pragma: no cover - 由服务层覆盖
        return False


class InMemorySubmissionRepository:
    """语义与 0036 的 `submission.attempt_id UNIQUE` + append-only 一致。"""

    def __init__(self) -> None:
        self.attempts: dict[UUID, Any] = {}
        self.response_rows: list[Any] = []
        self.attempt_items: dict[UUID, tuple[Any, ...]] = {}
        self.submissions: dict[UUID, SubmissionRecord] = {}

    # -- 夹具 ------------------------------------------------------------
    def add_attempt(self, row: Any, *, items: Iterable[Any] = ()) -> None:
        self.attempts[row.id] = row
        self.attempt_items[row.id] = tuple(items)

    def add_response(self, row: Any) -> None:
        self.response_rows.append(row)

    # -- 端口 ------------------------------------------------------------
    def lock_attempt(self, attempt_id: UUID) -> Any | None:
        return self.attempts.get(attempt_id)

    def items_for(self, attempt_id: UUID) -> tuple[Any, ...]:
        return self.attempt_items.get(attempt_id, ())

    def revisions_for(self, attempt_id: UUID) -> tuple[Any, ...]:
        return tuple(row for row in self.response_rows if row.attempt_id == attempt_id)

    def submission_for(self, attempt_id: UUID) -> SubmissionRecord | None:
        return self.submissions.get(attempt_id)

    def save_submission(self, record: SubmissionRecord) -> SubmissionRecord:
        existing = self.submissions.get(record.attempt_id)
        if existing is not None:
            raise ValueError("uq_submission_attempt: 一次作答只能交一次卷")
        self.submissions[record.attempt_id] = record
        return record

    def due_attempts(self, now: datetime) -> tuple[Any, ...]:
        return tuple(
            row for row in self.attempts.values()
            if row.status in ("in_progress", "finalizing")
            and row.worker_due_at <= now
        )


class SubmissionService:
    def __init__(
        self,
        *,
        repository: InMemorySubmissionRepository,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self._now = clock
        self._new_id = id_factory

    # -- 交卷 -----------------------------------------------------------
    def finalize(self, *, attempt_id: UUID, student_id: UUID | None = None,
                 trigger_kind: TriggerKind = "manual",
                 now: datetime | None = None) -> SubmissionReceiptDTO:
        """交卷；已有 submission 时返回同一回执（幂等，不重复建快照）。"""
        moment = now or self._now()
        attempt = self.repository.lock_attempt(attempt_id)
        if attempt is None:
            raise ResourceNotFound(f"attempt 不存在：{attempt_id}")
        if student_id is not None and attempt.student_id != student_id:
            raise ResourceNotFound(f"attempt 不存在或不属于本人：{attempt_id}")

        existing = self.repository.submission_for(attempt_id)
        if existing is not None:
            return existing.to_receipt()

        if attempt.status not in ("in_progress", "finalizing"):
            # 只有"已撤回"能走到这里还能收卷；其它终态说明状态被外力改过。
            if getattr(attempt, "assignment_status", "") != "withdrawn":
                raise AttemptFinalized(
                    f"attempt 已是 {attempt.status}，不能再交卷")

        snapshot = self._snapshot(attempt)
        manifest_hash = canonical_hash([
            {"slot": str(item.slot_id), "revision_no": item.revision_no,
             "response_hash": item.response_hash}
            for item in snapshot.items
        ])
        receipt_hash = canonical_hash({
            "attempt_id": str(attempt.id), "item_count": len(snapshot.items),
            "unanswered": snapshot.unanswered_count,
            "invalid": snapshot.invalid_count,
            "manifest": manifest_hash,
            "trigger": trigger_kind,
        })
        record = SubmissionRecord(
            id=self._new_id(), attempt_id=attempt.id, student_id=attempt.student_id,
            trigger_kind=trigger_kind, submitted_at=moment,
            item_count=len(snapshot.items),
            unanswered_count=snapshot.unanswered_count,
            invalid_count=snapshot.invalid_count,
            response_manifest_hash=manifest_hash, receipt_hash=receipt_hash,
            items=snapshot.items,
        )
        self.repository.save_submission(record)
        attempt.close(moment, trigger_kind) if hasattr(attempt, "close") else None
        return record.to_receipt()

    # -- 快照 -----------------------------------------------------------
    def _snapshot(self, attempt: Any) -> "_Snapshot":
        """按槽取"截止前最后一条修订"；截止之后到达的修订不进快照。"""
        deadline = attempt.server_deadline_at
        latest: dict[UUID, Any] = {}
        for row in self.repository.revisions_for(attempt.id):
            if row.server_received_at > deadline:
                continue  # 迟到的修订：事实保留，但不进这次快照
            current = latest.get(row.slot_id)
            if current is None or row.revision_no > current.revision_no:
                latest[row.slot_id] = row

        items: list[SubmissionItem] = []
        unanswered = invalid = 0
        for seed in self.repository.items_for(attempt.id):
            row = latest.get(seed.paper_slot_id)
            if row is None:
                unanswered += 1
                items.append(SubmissionItem(
                    slot_id=seed.paper_slot_id,
                    assignment_item_id=seed.assignment_item_id,
                    revision_no=0, raw_response=None, parse_status="empty",
                    response_hash=canonical_hash({"unanswered": str(seed.paper_slot_id)}),
                    unanswered=True,
                ))
                continue
            if row.parse_status == "invalid":
                invalid += 1
            items.append(SubmissionItem(
                slot_id=row.slot_id,
                assignment_item_id=seed.assignment_item_id,
                revision_no=row.revision_no,
                raw_response=row.raw_response,
                parse_status=row.parse_status,
                response_hash=canonical_hash({"slot": str(row.slot_id),
                                              "raw": row.raw_response}),
                unanswered=False,
            ))
        return _Snapshot(items=tuple(items), unanswered_count=unanswered,
                         invalid_count=invalid)


@dataclass(frozen=True)
class _Snapshot:
    items: tuple[SubmissionItem, ...]
    unanswered_count: int
    invalid_count: int


class DeadlineWorker:
    """到期收卷：把 `worker_due_at <= now` 的进行中作答按 `deadline` 收掉。

    与手动交卷共用 `SubmissionService.finalize`：谁先到谁写，后到的看到已有
    submission 就返回同一回执。这样"学生手快 + worker 也在收"不会产生两份快照。
    """

    def __init__(self, *, submissions: SubmissionService,
                 repository: InMemorySubmissionRepository) -> None:
        self.submissions = submissions
        self.repository = repository

    def finalize_due(self, *, now: datetime, batch_size: int = 100) -> dict[str, list[str]]:
        finalized: list[str] = []
        replayed: list[str] = []
        for attempt in list(self.repository.due_attempts(now))[:batch_size]:
            before = self.repository.submission_for(attempt.id) is not None
            receipt = self.submissions.finalize(
                attempt_id=attempt.id, trigger_kind="deadline", now=now)
            (replayed if before else finalized).append(str(receipt.attempt_id))
        return {"finalized": finalized, "replayed": replayed}


__all__ = [
    "DeadlineWorker",
    "InMemorySubmissionRepository",
    "SubmissionItem",
    "SubmissionRecord",
    "SubmissionService",
    "TriggerKind",
]
