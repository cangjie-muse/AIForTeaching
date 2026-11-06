"""在线作答（阶段 C）契约：作答窗口、策略校验、学生安全投影的 DTO。

三条规则写进类型里，而不是只写进注释：

1. **服务端时间唯一权威。** `DeadlineWindow.accepts_revision_at()` 只比较服务端
   收到的时刻与有效截止时刻。客户端时钟一律不参与——否则"手机慢 3 分钟"就能多交一次。
2. **只读宽限不延长录入。** `read_only_grace` 只让学生的页面在截止后还能看题；
   有效截止（`effective_deadline`）仍等于 `min(due_at, hard_close_at, 开始+时长)`。
3. **学生投影是白名单。** `StudentSlotDTO` 只有题干、公开资产、输入方式、本人响应；
   没有任何字段能装下答案/解析/量规。`assert_student_safe()` 把这条做成运行时可检的断言，
   而不是靠"记得别加字段"。

与计划书的差异：
  * 计划的 `PolicyTiming(False, None, ...)` 用 `duration_seconds`；本仓库数据库列是
    `attempt_policy_version.time_limit_seconds`（0027），这里用列名，避免两套叫法。
  * 计划的 `AttemptService` 在任务 3 才写；本模块只放契约与纯函数，好让任务 2 的
    测试不需要数据库也能跑。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal, Sequence
from uuid import UUID

from pydantic import Field, model_validator

from ..contracts.common import ContractModel

# 与 0027 的 CHECK 逐字对应（late_policy / behavior / conflict_policy）
LatePolicy = Literal["reject", "read_only_grace", "auto_submit"]
ConflictPolicy = Literal["per_slot", "whole_attempt"]
AttemptStatus = Literal["in_progress", "finalizing", "submitted", "closed"]
ParseStatus = Literal["parsed", "unparsed", "invalid", "empty"]

# 学生投影里**永远不允许**出现的字段名。扫描是递归的：嵌套在 public_assets 里
# 的 answer 也会被抓出来（那才是最容易被忽略的泄露路径）。
FORBIDDEN_FIELDS = ("answer", "solution", "rubric", "answer_test")


class AttemptError(Exception):
    """作答域错误基类：API 层按 code 映射 HTTP 状态。"""

    code = "attempt_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PolicyDenied(AttemptError):
    code = "invalid_attempt_policy"


class ResourceNotFound(AttemptError):
    """越权访问与不存在给同一个 404：不告诉试探者"这个 attempt 存在但你不是本人"。"""

    code = "attempt_not_found"


class ResponseConflict(AttemptError):
    code = "response_conflict"


class AttemptFinalized(AttemptError):
    code = "attempt_finalized"


class Withdrawn(AttemptError):
    code = "assignment_withdrawn"


class AttemptLimitExceeded(AttemptError):
    code = "attempt_limit_exceeded"


class AssignmentWindow(ContractModel):
    """作业的三个时间点；顺序是硬约束（0027 的 CHECK 同义）。"""

    visible_at: datetime
    due_at: datetime
    hard_close_at: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "AssignmentWindow":
        if not (self.visible_at <= self.due_at <= self.hard_close_at):
            raise ValueError("visible_at <= due_at <= hard_close_at 必须成立")
        return self


class PolicyTiming(ContractModel):
    """一次作答的策略投影（只取与截止/保存有关的列）。

    `max_attempts=None` 表示"不限次数"，按设计只允许出现在练习策略上
    （`validate_policy()` 会拒绝其它组合）。
    """

    time_limit_seconds: int | None = Field(default=None, ge=0)
    grace_seconds: int = Field(default=0, ge=0)
    late_policy: LatePolicy = "reject"
    max_attempts: int | None = Field(default=None, ge=1)
    behavior: Literal["deferred", "adaptive_practice"] = "deferred"
    conflict_policy: ConflictPolicy = "per_slot"
    autosave_debounce_ms: int = Field(default=800, gt=0)
    autosave_max_interval_ms: int = Field(default=5000, gt=0)

    @property
    def time_limited(self) -> bool:
        return self.time_limit_seconds is not None


def validate_policy(policy: PolicyTiming, *, purpose: str) -> None:
    """start 之前的策略体检；不合格抛 `PolicyDenied("invalid_attempt_policy")`。

    为什么不改阶段 A 的策略行：策略版本是**审核过的证据**（0036 的 attempt 引用它）。
    发现它不合理时，正确做法是拒绝开考并提示管理员改配置，而不是在运行时悄悄替换
    一份没审过的策略。
    """
    if policy.max_attempts is None and purpose != "practice":
        raise PolicyDenied(
            "invalid_attempt_policy: 不限次数只允许练习卷（max_attempts 为空）")
    if policy.max_attempts is not None and policy.max_attempts < 1:
        raise PolicyDenied("invalid_attempt_policy: max_attempts 必须 >= 1")
    if policy.time_limit_seconds is not None and policy.time_limit_seconds <= 0:
        raise PolicyDenied("invalid_attempt_policy: 限时策略的时长必须为正")
    if policy.grace_seconds > 0 and policy.late_policy != "read_only_grace":
        # 只有"截止后只读"才需要宽限；reject/auto_submit 配宽限会让"截止"名存实亡。
        raise PolicyDenied(
            "invalid_attempt_policy: grace_seconds 只允许与 read_only_grace 搭配")
    if not 0 < policy.autosave_debounce_ms <= policy.autosave_max_interval_ms:
        raise PolicyDenied(
            "invalid_attempt_policy: 保存间隔需满足 0 < debounce <= max_interval")


class DeadlineWindow(ContractModel):
    """一次作答的有效截止与 worker 到期时刻。"""

    effective_deadline: datetime
    worker_due_at: datetime

    def accepts_revision_at(self, server_received_at: datetime) -> bool:
        """快照资格只看这个：服务端收到时刻 <= 有效截止。截止那一毫秒仍算数内。"""
        return _as_utc(server_received_at) <= _as_utc(self.effective_deadline)

    def is_read_only_at(self, server_now: datetime) -> bool:
        """截止之后到 worker 到期之前：还能看题，但录不进新答案。"""
        return _as_utc(server_now) > _as_utc(self.effective_deadline)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def compute_deadline(started_at: datetime, assignment: AssignmentWindow,
                     policy: PolicyTiming) -> DeadlineWindow:
    """算出有效截止与 worker 到期时刻（服务端时间，纯函数、可单测）。

    有效截止 = min(due_at, hard_close_at, 开始 + 限时)。宽限只影响 worker 何时来处理，
    不影响"还能不能交新答案"。
    """
    deadline = min(_as_utc(assignment.due_at), _as_utc(assignment.hard_close_at))
    if policy.time_limited:
        deadline = min(deadline, _as_utc(started_at) + timedelta(
            seconds=int(policy.time_limit_seconds or 0)))
    grace = policy.grace_seconds if policy.late_policy == "read_only_grace" else 0
    worker_due = min(deadline + timedelta(seconds=grace),
                     _as_utc(assignment.hard_close_at))
    return DeadlineWindow(effective_deadline=deadline, worker_due_at=worker_due)


# ---------------------------------------------------------------------------
# 学生投影 DTO（白名单）
# ---------------------------------------------------------------------------

class StudentSlotDTO(ContractModel):
    """学生看到的一个槽位。

    没有 `exercise_id` / `difficulty_label` / `review_status` 这类内部字段，
    也没有任何形如 answer/rubric 的键——学生卷只需要题干、公开资产、输入方式与本人答案。
    """

    slot_id: UUID
    slot_no: int = Field(ge=1)
    stem: str
    public_assets: tuple[dict[str, Any], ...] = ()
    input_kind: str = Field(min_length=1, max_length=40)
    raw_response: dict[str, Any] | None = None
    parse_status: ParseStatus = "empty"
    revision_no: int = Field(default=0, ge=0)


class SubmissionReceiptDTO(ContractModel):
    submission_id: UUID
    attempt_id: UUID
    finalized_at: datetime
    item_count: int = Field(ge=0)
    unanswered_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    response_manifest_hash: str = Field(min_length=64, max_length=64)
    receipt_hash: str = Field(min_length=64, max_length=64)
    submission_reason: str = Field(min_length=1, max_length=40)


class StudentAttemptDTO(ContractModel):
    """重连时返回给学生的整卷状态。

    已交卷的 attempt 即使作业被撤回也返回 receipt + 空 slots（不回传题干）：
    撤回只是停止继续作答，不删除既有事实（§0.3 规则 6）。
    """

    attempt_id: UUID
    assignment_id: UUID
    paper_version_id: UUID
    status: AttemptStatus
    assignment_status: str = Field(min_length=1, max_length=40)
    attempt_no: int = Field(ge=1)
    started_at: datetime
    server_now: datetime
    server_deadline_at: datetime
    worker_due_at: datetime
    save_debounce_ms: int = Field(gt=0)
    max_save_interval_ms: int = Field(gt=0)
    late_policy: LatePolicy
    slots: tuple[StudentSlotDTO, ...] = ()
    submission_receipt: SubmissionReceiptDTO | None = None


class SaveResponseResult(ContractModel):
    """一次保存的结果（幂等重放会返回同样的 revision_no）。"""

    attempt_id: UUID
    slot_id: UUID
    client_revision_id: str = Field(min_length=1, max_length=200)
    revision_no: int = Field(ge=0)
    server_received_at: datetime
    raw_response: dict[str, Any] | None = None
    parse_status: ParseStatus = "empty"

    @classmethod
    def empty(cls, *, attempt_id: UUID, slot_id: UUID) -> "SaveResponseResult":
        """尚无任何服务端修订（revision_no=0）：只用于初始 base 冲突的回应。"""
        now = datetime.now(timezone.utc)
        return cls(attempt_id=attempt_id, slot_id=slot_id, client_revision_id="",
                   revision_no=0, server_received_at=now, raw_response=None,
                   parse_status="empty")


class AttemptActor(ContractModel):
    """谁在请求作答。`is_worker` 表示到期收卷的后台任务。"""

    student_id: UUID | None = None
    is_worker: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> "AttemptActor":
        if self.is_worker and self.student_id is not None:
            raise ValueError("worker 不代表任何学生")
        if not self.is_worker and self.student_id is None:
            raise ValueError("学生操作必须带 student_id")
        return self


# ---------------------------------------------------------------------------
# 泄露扫描：把"白名单"变成可执行断言
# ---------------------------------------------------------------------------

def assert_student_safe(payload: Any, *, where: str = "payload") -> None:
    """递归扫描键名；出现 `answer/solution/rubric/answer_test` 就抛 AssertionError。

    为什么要在生产路径上也调它：DTO 是白名单，但 `public_assets` 是自由 dict，
    上游完全可能塞一个 `{"answer": ...}` 进去。一次 O(n) 扫描换来"学生卷里不可能有答案"
    这句话可以被测试和运行时同时证明。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            name = str(key).strip().lower()
            if name in FORBIDDEN_FIELDS or name.startswith("answer_test"):
                raise AssertionError(f"{where}.{key} 出现在学生投影里（禁止字段）")
            assert_student_safe(value, where=f"{where}.{key}")
    elif isinstance(payload, (list, tuple, set, frozenset)):
        for index, value in enumerate(payload):
            assert_student_safe(value, where=f"{where}[{index}]")
    return None


class SlotRow(ContractModel):
    """投影仓储返回的一行（**只有**这些列）：题干已清理、资产只保留公开的。"""

    slot_id: UUID
    slot_no: int = Field(ge=1)
    stem: str
    public_assets: tuple[dict[str, Any], ...] = ()
    input_kind: str = "text"
    raw_response: dict[str, Any] | None = None
    parse_status: ParseStatus = "empty"
    revision_no: int = Field(default=0, ge=0)


def build_student_attempt(*, attempt_id: UUID, assignment_id: UUID,
                          paper_version_id: UUID, status: AttemptStatus,
                          assignment_status: str, attempt_no: int,
                          started_at: datetime, server_now: datetime,
                          window: DeadlineWindow, policy: PolicyTiming,
                          slots: Sequence[SlotRow],
                          receipt: SubmissionReceiptDTO | None = None,
                          include_slots: bool = True) -> StudentAttemptDTO:
    """组装学生投影；调用方拿到的 DTO 保证不含禁止字段。"""
    dto = StudentAttemptDTO(
        attempt_id=attempt_id,
        assignment_id=assignment_id,
        paper_version_id=paper_version_id,
        status=status,
        assignment_status=assignment_status,
        attempt_no=attempt_no,
        started_at=started_at,
        server_now=server_now,
        server_deadline_at=window.effective_deadline,
        worker_due_at=window.worker_due_at,
        save_debounce_ms=policy.autosave_debounce_ms,
        max_save_interval_ms=policy.autosave_max_interval_ms,
        late_policy=policy.late_policy,
        slots=tuple(
            StudentSlotDTO(
                slot_id=row.slot_id, slot_no=row.slot_no, stem=row.stem,
                public_assets=tuple(row.public_assets), input_kind=row.input_kind,
                raw_response=row.raw_response, parse_status=row.parse_status,
                revision_no=row.revision_no,
            )
            for row in (slots if include_slots else ())
        ),
        submission_receipt=receipt,
    )
    assert_student_safe(dto.model_dump(mode="json"), where="student_attempt")
    return dto


__all__ = [
    "AssignmentWindow",
    "AttemptActor",
    "AttemptError",
    "AttemptFinalized",
    "AttemptLimitExceeded",
    "AttemptStatus",
    "DeadlineWindow",
    "FORBIDDEN_FIELDS",
    "LatePolicy",
    "PolicyDenied",
    "PolicyTiming",
    "ResourceNotFound",
    "ResponseConflict",
    "SaveResponseResult",
    "SlotRow",
    "StudentAttemptDTO",
    "StudentSlotDTO",
    "SubmissionReceiptDTO",
    "Withdrawn",
    "assert_student_safe",
    "build_student_attempt",
    "compute_deadline",
    "validate_policy",
]
