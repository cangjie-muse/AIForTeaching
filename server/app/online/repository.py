"""在线作答的仓储与内存实现（阶段 C 任务 3）。

为什么要单独的端口而不是直接读 `assessment.assignment`：
   * 开考要一次性拿到"作业窗口 + 分发方式 + 策略 + 卷面题目"四组事实，
     分散在多个模块里读会让"读到一半作业被撤回"变成可能；
   * 内存实现让 `AttemptService` 的规则可以在没有数据库的机器上被真实测试
     （本仓库的既有约定：领域逻辑跑内存仓储，SQL 由迁移真引擎断言覆盖）。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Sequence
from uuid import UUID

from ..contracts.common import ContractModel
from .contracts import AssignmentWindow, AttemptStatus, PolicyTiming


@dataclass(frozen=True)
class AttemptAssignment:
    """开考需要的作业事实（只读投影，不复制阶段 A 的整行）。"""

    id: UUID
    course_id: UUID
    course_ref: str
    offering_id: UUID
    paper_version_id: UUID
    status: str
    distribution_kind: str
    visible_at: datetime
    due_at: datetime
    hard_close_at: datetime
    policy: PolicyTiming
    purpose: str = "formal"

    @property
    def window(self) -> AssignmentWindow:
        return AssignmentWindow(visible_at=self.visible_at, due_at=self.due_at,
                                hard_close_at=self.hard_close_at)

    @property
    def accepts_online(self) -> bool:
        return self.distribution_kind in ("online", "both")

    @property
    def is_live(self) -> bool:
        return self.status in ("scheduled", "published", "closed")


class AttemptItemSeed(ContractModel):
    """开考时要落进 `paper_attempt_item` 的一行（来自 assignment_item 的冻结投影）。"""

    assignment_item_id: UUID
    paper_slot_id: UUID
    ord: int


class AttemptRow(ContractModel):
    id: UUID
    assignment_id: UUID
    paper_version_id: UUID
    student_id: UUID
    attempt_no: int
    status: AttemptStatus
    started_at: datetime
    server_deadline_at: datetime
    worker_due_at: datetime
    created: bool = True


class InMemoryAttemptRepository:
    """内存仓储：语义与 0036 的部分唯一索引、append-only 一致。"""

    def __init__(self, *, assignments: Sequence[AttemptAssignment] = ()) -> None:
        self.assignments: dict[UUID, AttemptAssignment] = {
            item.id: item for item in assignments}
        self.items: dict[UUID, tuple[AttemptItemSeed, ...]] = {}
        self.attempts: dict[UUID, AttemptRow] = {}
        self.attempt_items: dict[UUID, tuple[AttemptItemSeed, ...]] = {}
        self.idempotency: dict[tuple[str, str, UUID], tuple[str, AttemptRow]] = {}

    def seed_items(self, assignment_id: UUID, items: Sequence[AttemptItemSeed]) -> None:
        self.items[assignment_id] = tuple(items)

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryAttemptRepository"]:
        yield self

    # -- 读 ---------------------------------------------------------------
    def assignment(self, assignment_id: UUID) -> AttemptAssignment | None:
        return self.assignments.get(assignment_id)

    def assignment_items(self, assignment_id: UUID) -> tuple[AttemptItemSeed, ...]:
        return self.items.get(assignment_id, ())

    def active_attempt(self, assignment_id: UUID, student_id: UUID) -> AttemptRow | None:
        """同一学生同一作业的进行中 attempt（0036 的部分唯一索引同义）。"""
        for row in self.attempts.values():
            if (row.assignment_id == assignment_id and row.student_id == student_id
                    and row.status in ("in_progress", "finalizing")):
                return row
        return None

    def attempts_for(self, assignment_id: UUID, student_id: UUID) -> tuple[AttemptRow, ...]:
        return tuple(row for row in self.attempts.values()
                     if row.assignment_id == assignment_id
                     and row.student_id == student_id)

    def get(self, attempt_id: UUID) -> AttemptRow | None:
        return self.attempts.get(attempt_id)

    def find_idempotent(self, scope: str, key: str, actor_id: UUID):
        record = self.idempotency.get((scope, key, actor_id))
        return record[1] if record else None

    # -- 写 ---------------------------------------------------------------
    def add(self, row: AttemptRow) -> AttemptRow:
        if self.active_attempt(row.assignment_id, row.student_id) is not None:
            raise ValueError("uq_paper_attempt_one_active: 已有进行中的 attempt")
        self.attempts[row.id] = row
        return row

    def add_items(self, attempt_id: UUID, items: Sequence[AttemptItemSeed]) -> None:
        self.attempt_items[attempt_id] = tuple(items)

    def items_for(self, attempt_id: UUID) -> tuple[AttemptItemSeed, ...]:
        return self.attempt_items.get(attempt_id, ())

    def remember_idempotent(self, scope: str, key: str, actor_id: UUID,
                            fingerprint: str, row: AttemptRow) -> None:
        self.idempotency[(scope, key, actor_id)] = (fingerprint, row)


__all__ = [
    "AttemptAssignment",
    "AttemptItemSeed",
    "AttemptRow",
    "InMemoryAttemptRepository",
]
