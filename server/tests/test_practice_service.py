"""阶段 D 任务 2：练习检查绑定 revision、幂等、只投影不评分。

要钉住的都是"这条反馈到底属于哪一版答案"的问题：

  * 同一 revision 重复检查 → 返回第一次结论（不重复跑检查器）；
  * 检查器炸了 / 不可用 → `fail`（判不出来），而不是错或对；
  * 正式卷（已有 submission）→ **拒绝走练习路径**；
  * 每次检查都落一条学习投影，且带来源 revision 与 attempt（0037 的约束）；
  * 反馈的 revision_id 必须等于被检查的那一版。

运行：`python server/tests/test_practice_service.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.practice.contracts import PracticePolicy  # noqa: E402
from app.practice.service import (  # noqa: E402
    FORMAL_PATH,
    AttemptView,
    CheckerUnavailable,
    InMemoryPracticeRepository,
    PracticeService,
    TextEqualsChecker,
    UnavailableChecker,
    outcome_of_runs,
)

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)
STUDENT = UUID("5e5e0000-0000-4000-8000-000000000001")
OTHER = UUID("5e5e0000-0000-4000-8000-000000000002")
ATTEMPT = UUID("aa000000-0000-4000-8000-000000000001")
REVISION = UUID("cc000000-0000-4000-8000-0000000000a1")
SLOT = UUID("cc000000-0000-4000-8000-0000000000b1")
EXERCISE = UUID("ee000000-0000-4000-8000-0000000000c1")
POLICY = PracticePolicy(purpose="practice")


def view(*, text: str = "x^3+C", status: str = "in_progress",
         has_submission: bool = False, student: UUID = STUDENT) -> AttemptView:
    return AttemptView(
        attempt_id=ATTEMPT, student_id=student, revision_id=REVISION, slot_id=SLOT,
        exercise_version_id=EXERCISE, response_text=text,
        attempt_status=status, has_submission=has_submission)


def build(*, text: str = "x^3+C", expected: str = "x^3+C", **overrides):
    repo = InMemoryPracticeRepository()
    repo.add_attempt(view(text=text, **overrides))
    checker = TextEqualsChecker(expected=lambda _: expected)
    return PracticeService(repository=repo, checker=checker), repo


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 三态
# ---------------------------------------------------------------------------

def test_matching_answer_is_true_without_score_guess() -> None:
    service, repo = build(text="x^3 + C", expected="x^3+C")
    result, feedback = service.check(attempt_id=ATTEMPT, student_id=STUDENT,
                                     policy=POLICY)
    assert result.status == "true"
    assert result.reason == "cas_true"
    assert feedback.status == "true"
    assert len(repo.answer_runs) == 1


def test_wrong_answer_is_false_with_counterexample() -> None:
    service, _ = build(text="x^2", expected="x^3+C")
    result, feedback = service.check(attempt_id=ATTEMPT, student_id=STUDENT,
                                     policy=POLICY)
    assert result.status == "false"
    assert feedback.counterexample == {"expected": "x^3+C"}, (
        "错也要给反例，否则学生不知道错在哪")


def test_blank_answer_is_fail_not_false() -> None:
    service, _ = build(text="   ")
    result, _ = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert result.status == "fail"
    assert result.reason == "empty_response"
    assert result.score is None


# ---------------------------------------------------------------------------
# 检查器不可用 / 崩溃
# ---------------------------------------------------------------------------

def test_unavailable_checker_reports_unsupported() -> None:
    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo, checker=UnavailableChecker())
    result, feedback = service.check(attempt_id=ATTEMPT, student_id=STUDENT,
                                     policy=POLICY)
    assert result.status == "fail"
    assert result.reason == "unsupported_exercise"
    assert "不影响你的成绩" in feedback.message


def test_checker_crash_is_fail_not_false() -> None:
    class Exploding:
        name = "exploding"
        version = "1"

        def check(self, **_):
            raise RuntimeError("boom")

    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo, checker=Exploding())
    result, _ = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert result.status == "fail", "检查器炸了不等于学生答错"
    assert result.reason == "checker_error"


def test_checker_unavailable_exception_path() -> None:
    class Missing:
        name = "missing"
        version = "0"

        def check(self, **_):
            raise CheckerUnavailable("no cas")

    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo, checker=Missing())
    result, _ = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert result.status == "fail"
    assert result.reason == "unsupported_exercise"


# ---------------------------------------------------------------------------
# 幂等与绑定
# ---------------------------------------------------------------------------

def test_recheck_same_revision_reuses_the_first_result() -> None:
    service, repo = build()
    first, _ = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    second, _ = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert first.revision_id == second.revision_id == REVISION
    assert len(repo.answer_runs) == 1, "重复检查不该重复落运行记录"
    assert len(repo.projections) == 1, "同一 revision 只投影一次"


def test_feedback_points_to_the_checked_revision() -> None:
    service, _ = build()
    _, feedback = service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert feedback.revision_id == REVISION


def test_new_revision_gets_its_own_run() -> None:
    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo,
                              checker=TextEqualsChecker(expected=lambda _: "a"))
    service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    newer = view()
    newer = AttemptView(**{**newer.__dict__,
                           "revision_id": UUID("cc000000-0000-4000-8000-0000000000a2")})
    repo.add_attempt(newer)
    service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert len(repo.projections) == 2, "新 revision 应产生新投影"


# ---------------------------------------------------------------------------
# 边界：正式卷与越权
# ---------------------------------------------------------------------------

def test_submitted_attempt_is_refused_on_the_practice_path() -> None:
    service, _ = build(has_submission=True)
    error = expect(PermissionError, lambda: service.check(
        attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY))
    assert FORMAL_PATH in str(error)


def test_other_students_attempt_is_refused() -> None:
    service, _ = build()
    expect(PermissionError, lambda: service.check(
        attempt_id=ATTEMPT, student_id=OTHER, policy=POLICY))


def test_projection_records_its_source() -> None:
    service, repo = build()
    service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    projection = repo.projections[0]
    assert projection.source_response_revision_id == REVISION, (
        "0037 的部分唯一索引要求投影带来源，服务层必须一起写")
    assert projection.paper_attempt_id == ATTEMPT
    assert projection.outcome == "true"


def test_fail_projection_uses_its_own_outcome() -> None:
    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo, checker=UnavailableChecker())
    service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    assert repo.projections[0].outcome == "fail"


def test_stats_keep_fail_separate_from_false() -> None:
    repo = InMemoryPracticeRepository()
    repo.add_attempt(view())
    service = PracticeService(repository=repo, checker=UnavailableChecker())
    service.check(attempt_id=ATTEMPT, student_id=STUDENT, policy=POLICY)
    counts = outcome_of_runs(repo.answer_runs)
    assert counts == {"true": 0, "false": 0, "fail": 1}, (
        "统计里 fail 不能并进 false，否则教师会以为学生答错了")


def test_solution_is_not_released_without_permission() -> None:
    service, _ = build()
    _, feedback = service.check(attempt_id=ATTEMPT, student_id=STUDENT,
                                policy=POLICY, solution="完整解答")
    assert feedback.solution is None


def run_tests() -> int:
    tests = [
        (name, value) for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as error:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(error).__name__}: {error}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 练习检查服务测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
