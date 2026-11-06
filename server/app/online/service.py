"""`AttemptService.start`：开始作答（阶段 C 任务 3 的核心规则）。

设计 §11.1 与 §0.3 规则 1 要求的三件事，都在这里：

1. **重复开始返回原 attempt。** 学生刷新页面、换设备、点两次"开始"，必须回到
   同一份进行中的卷；**新幂等键也不消耗次数**——次数是"交过几次卷"，不是"点过几次按钮"。
2. **先校验策略再开考。** 策略不合理（不限次数配正式卷、限时时长为 0……）就拒绝，
   而不是开了考再在保存阶段发现。
3. **服务端算截止。** 客户端时钟不参与；`server_deadline_at` 与 `worker_due_at`
   都由 `compute_deadline` 给出。

没有做的（刻意）：这里不写 `response_revision`、不写 `submission`——那是任务 4、5。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    AttemptLimitExceeded,
    AttemptStatus,
    DeadlineWindow,
    PolicyDenied,
    ResourceNotFound,
    Withdrawn,
    compute_deadline,
    validate_policy,
)
from .repository import AttemptItemSeed, AttemptRow

START_SCOPE = "attempt-start"


@dataclass(frozen=True)
class StartResult:
    attempt_id: UUID
    status: AttemptStatus
    attempt_no: int
    server_deadline_at: datetime
    worker_due_at: datetime
    reused: bool
    item_count: int


def _clean_key(value: str | None) -> str:
    text = (value or "").strip()
    if not text or len(text) > 200:
        raise PolicyDenied("idempotency_key_invalid: 幂等键不能为空且不超过 200 字符")
    return text


class AttemptService:
    def __init__(
        self,
        *,
        repository: Any,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self._now = clock
        self._new_id = id_factory

    # -- 开始 ------------------------------------------------------------
    def start(self, *, student_id: UUID, assignment_id: UUID,
              idempotency_key: str | None = None) -> StartResult:
        key = _clean_key(idempotency_key)
        now = self._now()
        with self.repository.unit_of_work() as uow:
            assignment = uow.assignment(assignment_id)
            if assignment is None:
                raise ResourceNotFound(f"作业不存在：{assignment_id}")
            if assignment.status == "withdrawn":
                raise Withdrawn("作业已撤回：不能开始作答")
            if assignment.status not in ("scheduled", "published"):
                raise PolicyDenied(
                    "assignment_not_open: 作业还没发布，不能开始作答")
            if not assignment.accepts_online:
                raise PolicyDenied(
                    "distribution_not_online: 这份作业是打印卷，没有在线作答入口")
            if now < assignment.visible_at:
                raise PolicyDenied("assignment_not_open: 还没到可见时间")

            # 1) 幂等重放：同一把键在同一学生的同一次开考请求上返回原 attempt。
            replay = uow.find_idempotent(START_SCOPE, key, student_id)
            if replay is not None:
                return StartResult(
                    attempt_id=replay.id, status=replay.status,
                    attempt_no=replay.attempt_no,
                    server_deadline_at=replay.server_deadline_at,
                    worker_due_at=replay.worker_due_at, reused=True,
                    item_count=len(uow.items_for(replay.id)),
                )

            # 2) 已有进行中/收尾中的 attempt：直接返回它（规则 1，不消耗次数）。
            active = uow.active_attempt(assignment_id, student_id)
            if active is not None:
                return StartResult(
                    attempt_id=active.id, status=active.status,
                    attempt_no=active.attempt_no,
                    server_deadline_at=active.server_deadline_at,
                    worker_due_at=active.worker_due_at, reused=True,
                    item_count=len(uow.items_for(active.id)),
                )

            # 3) 策略体检：不合格拒绝开考，而不是开考后在保存阶段才炸。
            validate_policy(assignment.policy, purpose=assignment.purpose)
            history = uow.attempts_for(assignment_id, student_id)
            max_attempts = assignment.policy.max_attempts
            if max_attempts is not None and len(history) >= max_attempts:
                raise AttemptLimitExceeded(
                    f"attempt_limit_exceeded: 已用 {len(history)} 次，"
                    f"上限 {max_attempts} 次")

            # 4) 服务端算截止，再落 attempt 与逐槽行。
            window = compute_deadline(now, assignment.window, assignment.policy)
            row = AttemptRow(
                id=self._new_id(),
                assignment_id=assignment.id,
                paper_version_id=assignment.paper_version_id,
                student_id=student_id,
                attempt_no=len(history) + 1,
                status="in_progress",
                started_at=now,
                server_deadline_at=window.effective_deadline,
                worker_due_at=window.worker_due_at,
            )
            uow.add(row)
            seeds: list[AttemptItemSeed] = list(uow.assignment_items(assignment.id))
            uow.add_items(row.id, seeds)
            uow.remember_idempotent(
                START_SCOPE, key, student_id,
                canonical_hash({"assignment_id": str(assignment.id),
                                "student_id": str(student_id)}),
                row,
            )
            return StartResult(
                attempt_id=row.id, status=row.status, attempt_no=row.attempt_no,
                server_deadline_at=row.server_deadline_at,
                worker_due_at=row.worker_due_at, reused=False,
                item_count=len(seeds),
            )

    # -- 重连 ------------------------------------------------------------
    def find_active(self, *, student_id: UUID, assignment_id: UUID) -> StartResult | None:
        """页面重连时先问"我有没有正在考的卷"；没有就返回 None（前端给开始按钮）。"""
        with self.repository.unit_of_work() as uow:
            active = uow.active_attempt(assignment_id, student_id)
            if active is None:
                return None
            return StartResult(
                attempt_id=active.id, status=active.status,
                attempt_no=active.attempt_no,
                server_deadline_at=active.server_deadline_at,
                worker_due_at=active.worker_due_at, reused=True,
                item_count=len(uow.items_for(active.id)),
            )

    # -- 读 -------------------------------------------------------------
    def owner_check(self, *, attempt_id: UUID, student_id: UUID) -> Any:
        """越权读到的是 404 而不是 403：不告诉试探者"这个 attempt 存在"。"""
        with self.repository.unit_of_work() as uow:
            row = uow.get(attempt_id)
            if row is None or row.student_id != student_id:
                raise ResourceNotFound(f"attempt 不存在或不属于本人：{attempt_id}")
            return row


__all__ = ["AttemptService", "DeadlineWindow", "StartResult", "START_SCOPE"]
