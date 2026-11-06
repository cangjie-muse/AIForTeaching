"""阶段 D 核心规则：`true / false / fail` 三态、反馈白名单、练习与正式卷的边界。

这三条是考试系统里最容易糊过去、也最容易伤到学生的地方：

  * 把"判不出来"打成错（学生背锅）；
  * 反馈顺手带上完整答案（正式卷泄题）；
  * 正式卷也"立即显示对错"（等于开卷）。

运行：`python server/tests/test_practice_contracts.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.practice.contracts import (  # noqa: E402
    CheckResult,
    PracticePolicy,
    build_feedback,
    decide_release,
    make_result,
)

REVISION = UUID("cc000000-0000-4000-8000-0000000000a1")
EXERCISE = UUID("ee000000-0000-4000-8000-0000000000b1")


def result(**changes) -> CheckResult:
    values = dict(revision_id=REVISION, exercise_version_id=EXERCISE,
                  status="true", reason="cas_true", score=1.0)
    values.update(changes)
    return make_result(**values)


# ---------------------------------------------------------------------------
# 三态
# ---------------------------------------------------------------------------

def test_true_projects_to_true() -> None:
    assert result().correct is True


def test_false_projects_to_false() -> None:
    assert result(status="false", reason="cas_false", score=0.0).correct is False


def test_fail_projects_to_none_not_false() -> None:
    """判不出来必须是 None：布尔化会把它压成"错"。"""
    failed = make_result(revision_id=REVISION, exercise_version_id=EXERCISE,
                        status="fail", reason="timeout")
    assert failed.correct is None
    assert failed.score is None


def test_fail_cannot_carry_a_score() -> None:
    try:
        make_result(revision_id=REVISION, exercise_version_id=EXERCISE,
                    status="fail", reason="checker_error", score=0.0)
    except ValueError as error:
        assert "判不出来" in str(error)
    else:
        raise AssertionError("fail 带分数必须被拒绝（否则成绩单会出现假的 0 分）")


def test_fail_reasons_are_distinguishable() -> None:
    for reason in ("timeout", "syntax_error", "unsupported_exercise", "checker_error"):
        failed = make_result(revision_id=REVISION, exercise_version_id=EXERCISE,
                            status="fail", reason=reason)
        assert failed.reason == reason


def test_result_records_checker_and_elapsed_time() -> None:
    checked = result(checker="sympy", elapsed_ms=120)
    assert checked.checker == "sympy"
    assert checked.elapsed_ms == 120, "出问题时要能说清是谁判的、花了多久"


# ---------------------------------------------------------------------------
# 反馈白名单
# ---------------------------------------------------------------------------

def test_feedback_does_not_leak_solution_by_default() -> None:
    feedback = build_feedback(result(), solution="完整解答在这里")
    assert feedback.solution is None, "答案没发布时，解析不能顺手带出去"


def test_feedback_carries_solution_only_when_released() -> None:
    feedback = build_feedback(result(), solution="完整解答", answer_released=True)
    assert feedback.solution == "完整解答"


def test_fail_feedback_says_it_will_not_count() -> None:
    failed = make_result(revision_id=REVISION, exercise_version_id=EXERCISE,
                        status="fail", reason="timeout")
    assert "不影响你的成绩" in build_feedback(failed).message


def test_counterexample_only_attached_on_false() -> None:
    assert build_feedback(result(status="true")).counterexample is None
    wrong = result(status="false", reason="cas_false", counterexample={"x": 0})
    assert build_feedback(wrong).counterexample == {"x": 0}


def test_feedback_points_to_the_checked_revision() -> None:
    assert build_feedback(result()).revision_id == REVISION, (
        "反馈必须绑定它检查的那一版，否则自动保存后学生会看到对不上的结论")


# ---------------------------------------------------------------------------
# 策略矩阵
# ---------------------------------------------------------------------------

def test_practice_can_show_immediate_correctness() -> None:
    policy = PracticePolicy(purpose="practice")
    assert policy.allows_immediate_feedback() is True
    assert decide_release(policy, attempt_index=0)["correctness"] is True


def test_formal_never_shows_immediate_correctness() -> None:
    policy = PracticePolicy(purpose="formal", correctness_release="immediate")
    assert policy.allows_immediate_feedback() is False
    assert decide_release(policy, attempt_index=0)["correctness"] is False, (
        "正式卷即使策略里写了 immediate，也不能立即给对错（开卷）")


def test_hint_release_after_attempt() -> None:
    policy = PracticePolicy(purpose="practice", hint_release="after_attempt")
    assert decide_release(policy, attempt_index=0)["hint"] is False
    assert decide_release(policy, attempt_index=1)["hint"] is True


def test_solution_release_is_never_implied() -> None:
    policy = PracticePolicy(purpose="practice", hint_release="immediate",
                            correctness_release="immediate")
    assert decide_release(policy, attempt_index=0)["solution"] is False, (
        "解析要单独按「答案是否已发布」判定，不能由提示策略捎带")


def test_coursework_does_not_show_scores_immediately() -> None:
    policy = PracticePolicy(purpose="coursework", score_release="immediate")
    assert decide_release(policy, attempt_index=0)["score"] is False


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
    print(f"✓ 练习三态与策略矩阵测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
