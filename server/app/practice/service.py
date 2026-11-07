"""练习逐题检查服务（阶段 D 任务 2/3 的服务层）。

这一层把三件事连起来，并且每次都必须**同时**成立：

    检查结果（true/false/fail） → 绑定到具体 revision → 落成学习投影（不产生成绩）

关键规则：

1. **只检查显式动作，不检查自动保存。** 学生每敲一个字都跑一次检查既慢又没有意义；
   只有学生点"检查"才检查，而且要回传它检查的 revision。
2. **绑定 revision：检查是"这一版答案"的结论。** 之后的自动保存会让结论过期，
   反馈里必须带 revision_id，页面据此判断"这条反馈是不是我当前答案的"。
3. **练习不写成绩。** 这条服务只写检查运行与学习投影；正式卷走评分路径，
   绝不共享（否则一次练习就能改学期成绩）。
4. **判不出来就是 fail。** 检查器抛异常、超时、题目不支持 → `fail` + 原因，
   并且不带分数（契约层已禁止 `fail` 带分数）。

检查器是可替换的端口：本机没有 sympy 时默认注入 `UnavailableChecker`，
一切检查返回 `unsupported_exercise`，而不是假装判对了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    CheckReason,
    CheckResult,
    CheckVerdict,
    PracticeFeedback,
    PracticePolicy,
    build_feedback,
    make_result,
)

PRACTICE_ONLY = "practice_only"
FORMAL_PATH = "formal_attempt_uses_grading"


class CheckerUnavailable(RuntimeError):
    pass


class CheckerPort(Protocol):
    name: str
    version: str

    def check(self, *, prompt: str, response: str,
              exercise_version_id: UUID) -> tuple[CheckVerdict, CheckReason,
                                                  dict[str, Any] | None, int]:
        """返回 (结论, 原因, 反例, 耗时毫秒)。**不允许**返回分数。"""
        ...


class UnavailableChecker:
    """没有可用检查器时的兜底：一律 fail(unsupported_exercise)。

    这个类存在的意义：让"本机没装 sympy"表现为**判不出来**，
    而不是"判错"或"没检查也当对"。
    """

    name = "unavailable"
    version = "0"

    def check(self, *, prompt: str, response: str, exercise_version_id: UUID):
        return ("fail", "unsupported_exercise", None, 0)


class TextEqualsChecker:
    """最朴素的检查器：忽略空白后完全一致 → true，否则 false。

    只用来自测服务链路与做降级检查；它不是数学判等（不解释等价变形）。
    名字会写进结果，教师看到 `checker=text-equals` 就知道这个结论的强度。
    """

    name = "text-equals"
    version = "1"

    def __init__(self, *, expected: Callable[[UUID], str]) -> None:
        self._expected = expected

    def check(self, *, prompt: str, response: str, exercise_version_id: UUID):
        expected = self._expected(exercise_version_id) or ""
        left = "".join(response.split())
        right = "".join(expected.split())
        if not left:
            return ("fail", "empty_response", None, 0)
        if left == right:
            return ("true", "cas_true", None, 1)
        return ("false", "cas_false", {"expected": expected}, 1)


@dataclass(frozen=True)
class AttemptView:
    """检查需要的作答事实（不复制阶段 C 的整行）。"""

    attempt_id: UUID
    student_id: UUID
    revision_id: UUID
    slot_id: UUID
    exercise_version_id: UUID
    response_text: str
    attempt_status: str
    has_submission: bool


@dataclass(frozen=True)
class LearningProjection:
    id: UUID
    exercise_version_id: UUID
    source_response_revision_id: UUID
    paper_attempt_id: UUID
    input_status: str
    outcome: CheckVerdict
    engine_version: str


class InMemoryPracticeRepository:
    def __init__(self) -> None:
        self.attempts: dict[UUID, AttemptView] = {}
        self.answer_runs: list[dict[str, Any]] = []
        self.projections: list[LearningProjection] = []

    def add_attempt(self, view: AttemptView) -> None:
        self.attempts[view.attempt_id] = view

    def attempt(self, attempt_id: UUID) -> AttemptView | None:
        return self.attempts.get(attempt_id)

    def existing_run(self, revision_id: UUID) -> dict[str, Any] | None:
        for run in self.answer_runs:
            if run["revision_id"] == revision_id:
                return run
        return None

    def save_run(self, *, revision_id: UUID, result: CheckResult,
                 input_hash: str) -> None:
        if self.existing_run(revision_id) is not None:
            raise ValueError("uq_answer_run: 同一 revision 只允许一次检查")
        self.answer_runs.append({"revision_id": revision_id, "result": result,
                                 "input_hash": input_hash})

    def save_projection(self, projection: LearningProjection) -> None:
        if any(item.source_response_revision_id == projection.source_response_revision_id
               for item in self.projections):
            raise ValueError("uq_learning_attempt_source: 投影已存在")
        self.projections.append(projection)


class PracticeService:
    def __init__(
        self,
        *,
        repository: InMemoryPracticeRepository,
        checker: CheckerPort | None = None,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.checker = checker or UnavailableChecker()
        self._now = clock
        self._new_id = id_factory

    def check(self, *, attempt_id: UUID, student_id: UUID,
              policy: PracticePolicy, hint: str | None = None,
              solution: str | None = None,
              answer_released: bool = False) -> tuple[CheckResult, PracticeFeedback]:
        view = self.repository.attempt(attempt_id)
        if view is None or view.student_id != student_id:
            raise PermissionError("attempt 不存在或不属于本人")
        if view.has_submission or view.attempt_status == "submitted":
            # 正式卷评分走另一条路径；这里拒绝是为了不让练习路悄悄改分。
            raise PermissionError(FORMAL_PATH)

        existing = self.repository.existing_run(view.revision_id)
        if existing is not None:
            # 同一 revision 重复点"检查"：返回第一次的结论（幂等，不重复跑检查器）。
            result = existing["result"]
        else:
            result = self._run_checker(view)
            self.repository.save_run(
                revision_id=view.revision_id, result=result,
                input_hash=canonical_hash({"revision": str(view.revision_id),
                                           "text": view.response_text}))
        self._project(view, result)
        feedback = build_feedback(
            result, hint=hint, solution=solution, answer_released=answer_released)
        return result, feedback

    def _run_checker(self, view: AttemptView) -> CheckResult:
        try:
            verdict, reason, counterexample, elapsed = self.checker.check(
                prompt="", response=view.response_text,
                exercise_version_id=view.exercise_version_id)
        except CheckerUnavailable:
            verdict, reason, counterexample, elapsed = (
                "fail", "unsupported_exercise", None, 0)
        except Exception:  # noqa: BLE001 — 检查器炸了不等于学生答错
            verdict, reason, counterexample, elapsed = (
                "fail", "checker_error", None, 0)
        return make_result(
            revision_id=view.revision_id,
            exercise_version_id=view.exercise_version_id,
            status=verdict, reason=reason, counterexample=counterexample,
            checker=getattr(self.checker, "name", "unknown"),
            elapsed_ms=max(0, int(elapsed)),
        )

    def _project(self, view: AttemptView, result: CheckResult) -> None:
        existing = [item for item in self.repository.projections
                    if item.source_response_revision_id == view.revision_id]
        if existing:
            return  # 幂等：同一 revision 只投影一次
        self.repository.save_projection(LearningProjection(
            id=self._new_id(),
            exercise_version_id=view.exercise_version_id,
            source_response_revision_id=view.revision_id,
            paper_attempt_id=view.attempt_id,
            input_status="parsed" if view.response_text.strip() else "empty",
            outcome=result.status,
            engine_version=f"{result.checker}/{getattr(self.checker, 'version', '0')}",
        ))


def outcome_of_runs(runs: list[dict[str, Any]]) -> dict[str, int]:
    """学习统计口径：三态各算各的，`fail` 不能并进"错误"。"""
    counts = {"true": 0, "false": 0, "fail": 0}
    for run in runs:
        counts[run["result"].status] += 1
    return counts


__all__ = [
    "AttemptView",
    "CheckerPort",
    "CheckerUnavailable",
    "FORMAL_PATH",
    "InMemoryPracticeRepository",
    "LearningProjection",
    "PRACTICE_ONLY",
    "PracticeService",
    "TextEqualsChecker",
    "UnavailableChecker",
    "outcome_of_runs",
]
