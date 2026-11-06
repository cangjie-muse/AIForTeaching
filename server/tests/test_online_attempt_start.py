"""阶段 C 任务 3：开始作答（重复开始固定 attempt、次数与策略门禁）。

要钉住的都是"学生刷新一下页面会发生什么"：

  * 刷新/换设备/点两次 → **回到同一份卷**，不消耗次数；
  * 换一把新的幂等键再点 → 仍然回到同一份（次数是"交过几次卷"，不是"点过几个按钮"）；
  * 交过 1 次、上限 1 → 第二次开始被拒 `attempt_limit_exceeded`；
  * 作业没发布 / 是打印卷 / 还没到可见时间 / 已撤回 → 分别给不同的拒绝原因；
  * 别人的 attempt → 404（不是 403）。

运行：`python server/tests/test_online_attempt_start.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.online.contracts import (  # noqa: E402
    AttemptLimitExceeded,
    AssignmentWindow,
    PolicyDenied,
    PolicyTiming,
    ResourceNotFound,
    Withdrawn,
)
from app.online.repository import (  # noqa: E402
    AttemptAssignment,
    AttemptItemSeed,
    InMemoryAttemptRepository,
)
from app.online.service import AttemptService  # noqa: E402

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
STUDENT = UUID("5e5e0000-0000-4000-8000-000000000001")
OTHER_STUDENT = UUID("5e5e0000-0000-4000-8000-000000000002")
ASSIGNMENT = UUID("dd000000-0000-4000-8000-000000000001")
PAPER = UUID("ee000000-0000-4000-8000-000000000001")
COURSE = UUID(int=101)
OFFERING = UUID(int=102)
SLOTS = [UUID(f"cc000000-0000-4000-8000-0000000000{index}") for index in ("a1", "b1")]


def policy(**changes) -> PolicyTiming:
    values = dict(time_limit_seconds=None, grace_seconds=600,
                  late_policy="read_only_grace", max_attempts=1)
    values.update(changes)
    return PolicyTiming(**values)


def assignment(**changes) -> AttemptAssignment:
    values = dict(
        id=ASSIGNMENT, course_id=COURSE, course_ref="analysis",
        offering_id=OFFERING, paper_version_id=PAPER, status="published",
        distribution_kind="online",
        visible_at=NOW - timedelta(minutes=30),
        due_at=NOW + timedelta(hours=2),
        hard_close_at=NOW + timedelta(hours=2, minutes=30),
        policy=policy(), purpose="formal",
    )
    values.update(changes)
    return AttemptAssignment(**values)


def build(*, item_count: int = 2, now: datetime = NOW, **changes):
    repo = InMemoryAttemptRepository(assignments=[assignment(**changes)])
    seeds = [AttemptItemSeed(assignment_item_id=UUID(int=100 + index),
                             paper_slot_id=SLOTS[index], ord=index + 1)
             for index in range(item_count)]
    repo.seed_items(ASSIGNMENT, seeds)
    service = AttemptService(repository=repo, clock=lambda: now)
    return service, repo


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 重复开始：固定同一份 attempt
# ---------------------------------------------------------------------------

def test_first_start_creates_an_attempt_with_items() -> None:
    service, repo = build()
    result = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                           idempotency_key="start-1")
    assert result.reused is False
    assert result.attempt_no == 1
    assert result.item_count == 2
    assert len(repo.items_for(result.attempt_id)) == 2


def test_refresh_with_same_key_returns_the_same_attempt() -> None:
    service, _ = build()
    first = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                          idempotency_key="start-1")
    again = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                         idempotency_key="start-1")
    assert again.attempt_id == first.attempt_id
    assert again.reused is True


def test_new_key_still_returns_the_active_attempt() -> None:
    """换一把键（换设备/清缓存）也必须回到同一份卷：次数按交卷算，不按点击算。"""
    service, _ = build()
    first = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                          idempotency_key="start-1")
    second = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                           idempotency_key="start-2")
    assert second.attempt_id == first.attempt_id
    assert second.reused is True
    assert second.attempt_no == 1


def test_deadline_is_computed_on_the_server() -> None:
    service, _ = build()
    result = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                           idempotency_key="start-1")
    assert result.server_deadline_at == NOW + timedelta(hours=2)
    assert result.worker_due_at == NOW + timedelta(hours=2, minutes=10)


def test_time_limit_shortens_the_deadline() -> None:
    service, _ = build(policy=policy(time_limit_seconds=600))
    result = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                           idempotency_key="start-timed")
    assert result.server_deadline_at == NOW + timedelta(minutes=10)


# ---------------------------------------------------------------------------
# 次数与门禁
# ---------------------------------------------------------------------------

def test_second_start_after_submission_respects_the_limit() -> None:
    service, repo = build()
    first = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                          idempotency_key="start-1")
    # 模拟"已交卷"：把 attempt 标成 submitted，让下一次开始变成第 2 次
    row = repo.attempts[first.attempt_id]
    repo.attempts[first.attempt_id] = row.model_copy(
        update={"status": "submitted"})
    error = expect(AttemptLimitExceeded, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-3"))
    assert error.code == "attempt_limit_exceeded"
    assert "上限 1" in error.message


def test_unpublished_assignment_is_rejected() -> None:
    service, _ = build(status="draft")
    error = expect(PolicyDenied, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-x"))
    assert "assignment_not_open" in error.message


def test_print_only_assignment_has_no_online_entry() -> None:
    service, _ = build(distribution_kind="print")
    error = expect(PolicyDenied, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-x"))
    assert "distribution_not_online" in error.message


def test_both_distribution_is_accepted() -> None:
    service, _ = build(distribution_kind="both")
    assert service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                         idempotency_key="start-both").attempt_no == 1


def test_before_visible_time_is_rejected() -> None:
    service, _ = build(visible_at=NOW + timedelta(hours=1))
    error = expect(PolicyDenied, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-x"))
    assert "还没到可见时间" in error.message


def test_withdrawn_assignment_is_rejected() -> None:
    service, _ = build(status="withdrawn")
    error = expect(Withdrawn, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-x"))
    assert error.code == "assignment_withdrawn"


def test_invalid_policy_is_rejected_before_starting() -> None:
    # purpose 是作业属性，不是策略字段——上一版把它塞进 PolicyTiming，被 extra=forbid 拒了。
    service, repo = build(policy=policy(max_attempts=None), purpose="formal")
    error = expect(PolicyDenied, lambda: service.start(
        student_id=STUDENT, assignment_id=ASSIGNMENT, idempotency_key="start-x"))
    assert "练习" in error.message
    assert repo.attempts == {}, "策略不合格时不能留下 attempt"


def test_missing_assignment_is_404() -> None:
    service, _ = build()
    error = expect(ResourceNotFound, lambda: service.start(
        student_id=STUDENT, assignment_id=UUID(int=999), idempotency_key="start-x"))
    assert error.code == "attempt_not_found"


# ---------------------------------------------------------------------------
# 重连与越权
# ---------------------------------------------------------------------------

def test_find_active_reports_none_before_starting() -> None:
    service, _ = build()
    assert service.find_active(student_id=STUDENT, assignment_id=ASSIGNMENT) is None


def test_find_active_returns_the_open_attempt() -> None:
    service, _ = build()
    started = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                            idempotency_key="start-1")
    found = service.find_active(student_id=STUDENT, assignment_id=ASSIGNMENT)
    assert found is not None and found.attempt_id == started.attempt_id


def test_other_students_attempt_is_not_found() -> None:
    service, _ = build()
    started = service.start(student_id=STUDENT, assignment_id=ASSIGNMENT,
                            idempotency_key="start-1")
    error = expect(ResourceNotFound, lambda: service.owner_check(
        attempt_id=started.attempt_id, student_id=OTHER_STUDENT))
    assert error.code == "attempt_not_found"


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
    print(f"✓ 开始作答测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
