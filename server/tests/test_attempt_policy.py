"""阶段 C 任务 2：作答窗口、策略校验与学生安全投影。

这一层钉的是三件"错了会直接伤到学生或考试公平"的事：

1. **截止只认服务端时间。** 客户端时钟快慢不同，任何"本地时间还没到"的实现都会让
   一部分人多交、一部分人少交。截止那一微秒之后必须拒绝。
2. **只读宽限不延长录入。** 宽限是"截止后还能看题"，不是"还能改答案"。
3. **学生投影是白名单。** 题干、公开资产、本人响应，别的都不能有；`public_assets`
   是自由 dict，所以扫描是递归的——嵌套的 `answer` 同样会被抓出来。

运行：`python server/tests/test_attempt_policy.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.online.contracts import (  # noqa: E402
    AssignmentWindow,
    DeadlineWindow,
    PolicyDenied,
    PolicyTiming,
    assert_student_safe,
    compute_deadline,
    validate_policy,
)

START = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)


def window(*, due_minutes: int = 120, close_minutes: int = 150) -> AssignmentWindow:
    return AssignmentWindow(
        visible_at=START - timedelta(minutes=30),
        due_at=START + timedelta(minutes=due_minutes),
        hard_close_at=START + timedelta(minutes=close_minutes),
    )


def policy(**changes) -> PolicyTiming:
    values = dict(time_limit_seconds=None, grace_seconds=0, late_policy="reject",
                  max_attempts=1)
    values.update(changes)
    return PolicyTiming(**values)


# ---------------------------------------------------------------------------
# 截止计算
# ---------------------------------------------------------------------------

def test_deadline_is_due_at_without_time_limit() -> None:
    result = compute_deadline(START, window(), policy())
    assert result.effective_deadline == window().due_at
    assert result.worker_due_at == window().due_at


def test_inverted_window_is_rejected_at_construction() -> None:
    """due_at > hard_close_at 的窗口根本不该能构造出来（0027 的 CHECK 同义）。

    上一版这里断言的是"硬关闭优先"——那需要先允许一个倒挂窗口存在，而正确的做法
    是**让它根本构造不出来**：倒挂的窗口意味着"截止时间晚于硬关闭"，任何下游
    计算都是在给一个自相矛盾的输入做四则运算。
    """
    try:
        AssignmentWindow(
            visible_at=START,
            due_at=START + timedelta(minutes=120),
            hard_close_at=START + timedelta(minutes=90),
        )
    except ValueError as error:
        assert "hard_close_at" in str(error)
    else:
        raise AssertionError("倒挂窗口必须被 AssignmentWindow 拒绝")


def test_time_limit_can_shorten_the_deadline() -> None:
    result = compute_deadline(START, window(), policy(time_limit_seconds=600))
    assert result.effective_deadline == START + timedelta(minutes=10)


def test_time_limit_cannot_extend_past_due() -> None:
    result = compute_deadline(START, window(due_minutes=30),
                              policy(time_limit_seconds=3600))
    assert result.effective_deadline == START + timedelta(minutes=30)


def test_read_only_grace_moves_worker_not_the_deadline() -> None:
    policy_value = policy(grace_seconds=600, late_policy="read_only_grace")
    assignment = window(due_minutes=120, close_minutes=180)
    result = compute_deadline(START, assignment, policy_value)
    assert result.effective_deadline == assignment.due_at, "宽限不能延长有效截止"
    assert result.worker_due_at == assignment.due_at + timedelta(minutes=10)


def test_worker_due_never_exceeds_hard_close() -> None:
    policy_value = policy(grace_seconds=3600, late_policy="read_only_grace")
    assignment = window(due_minutes=120, close_minutes=130)
    result = compute_deadline(START, assignment, policy_value)
    assert result.worker_due_at == assignment.hard_close_at


def test_accepts_revision_exactly_at_deadline() -> None:
    result = compute_deadline(START, window(), policy())
    assert result.accepts_revision_at(result.effective_deadline) is True


def test_rejects_one_microsecond_after_deadline() -> None:
    result = compute_deadline(START, window(), policy())
    late = result.effective_deadline + timedelta(microseconds=1)
    assert result.accepts_revision_at(late) is False


def test_read_only_is_only_after_the_deadline() -> None:
    result = compute_deadline(START, window(), policy())
    assert result.is_read_only_at(result.effective_deadline) is False
    assert result.is_read_only_at(result.effective_deadline + timedelta(seconds=1)) is True


def test_naive_datetime_is_treated_as_utc() -> None:
    """库里偶尔出现 naive 时间；按 UTC 解释，而不是让比较抛 TypeError。"""
    result = DeadlineWindow(
        effective_deadline=datetime(2026, 11, 7, 3, 0),
        worker_due_at=datetime(2026, 11, 7, 3, 0),
    )
    assert result.accepts_revision_at(datetime(2026, 11, 7, 2, 59, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# 策略校验
# ---------------------------------------------------------------------------

def test_unlimited_attempts_only_for_practice() -> None:
    validate_policy(policy(max_attempts=None), purpose="practice")
    try:
        validate_policy(policy(max_attempts=None), purpose="formal")
    except PolicyDenied as error:
        assert error.code == "invalid_attempt_policy"
        assert "练习" in error.message
    else:
        raise AssertionError("正式卷不限次数必须被拒绝")


def test_zero_duration_timed_policy_is_rejected() -> None:
    try:
        validate_policy(policy(time_limit_seconds=0), purpose="formal")
    except PolicyDenied as error:
        assert "时长" in error.message
    else:
        raise AssertionError("限时策略时长为 0 必须被拒绝")


def test_grace_requires_read_only_grace() -> None:
    try:
        validate_policy(policy(grace_seconds=600, late_policy="reject"), purpose="formal")
    except PolicyDenied as error:
        assert "read_only_grace" in error.message
    else:
        raise AssertionError("非只读宽限策略带宽限时间必须被拒绝")


def test_autosave_intervals_must_be_ordered() -> None:
    try:
        validate_policy(policy(autosave_debounce_ms=9000, autosave_max_interval_ms=5000),
                        purpose="formal")
    except PolicyDenied as error:
        assert "debounce" in error.message
    else:
        raise AssertionError("debounce > max_interval 必须被拒绝")


def test_valid_policy_passes_silently() -> None:
    validate_policy(policy(time_limit_seconds=3600), purpose="formal")


# ---------------------------------------------------------------------------
# 泄露扫描（白名单的执行面）
# ---------------------------------------------------------------------------

def test_scanner_catches_top_level_answer() -> None:
    try:
        assert_student_safe({"answer": "x"})
    except AssertionError as error:
        assert "answer" in str(error)
    else:
        raise AssertionError("顶层 answer 必须被抓出来")


def test_scanner_catches_nested_and_case_variants() -> None:
    for payload in (
        {"assets": [{"Answer": "x"}]},
        {"meta": {"solution": {"steps": []}}},
        {"rubric": {"points": 1}},
        {"answer_test": {"script": "..."}},
    ):
        try:
            assert_student_safe(payload)
        except AssertionError:
            continue
        raise AssertionError(f"嵌套禁止字段没被抓出来：{payload}")


def test_scanner_allows_normal_fields() -> None:
    assert_student_safe({
        "slot_id": str(UUID(int=7)),
        "stem": "求极限",
        "public_assets": [{"kind": "image", "url": "/assets/x.png"}],
        "raw_response": {"text": "我的答案文本"},
        "parse_status": "parsed",
    })


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
    print(f"✓ 作答策略与截止计算测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
