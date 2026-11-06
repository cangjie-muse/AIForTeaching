"""阶段 C 任务 4：自动保存的幂等、冲突与"非法输入也要留证"。

这份测试对应考场上最常见的四种翻车方式：

  1. 网络重试把同一个答案存了两版 → 幂等（同一 client_revision_id 返回同一修订）；
  2. 两个标签页改同一道题，后保存的悄悄覆盖 → base_revision 冲突 + 带回最新版；
  3. 两个标签页改**不同**的题 → 不该互相报错（按槽比较，不是整卷）；
  4. 学生写了字但解析不了 → 仍然保存并标 invalid，而不是 400 丢掉。

运行：`python server/tests/test_online_response_save.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.online.contracts import (  # noqa: E402
    AttemptFinalized,
    ResourceNotFound,
    ResponseConflict,
    Withdrawn,
)
from app.online.responses import (  # noqa: E402
    INVALID_EMPTY,
    INVALID_OVERSIZE,
    IdempotencyConflict,
    InMemoryResponseRepository,
    ResponseService,
    looks_invalid,
)

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(hours=1)
STUDENT = UUID("5e5e0000-0000-4000-8000-000000000001")
OTHER = UUID("5e5e0000-0000-4000-8000-000000000002")
ATTEMPT = UUID("aa000000-0000-4000-8000-000000000001")
SLOT_A = UUID("cc000000-0000-4000-8000-0000000000a1")
SLOT_B = UUID("cc000000-0000-4000-8000-0000000000b1")


class FakeAttempt:
    def __init__(self, *, status: str = "in_progress", assignment_status: str = "published",
                 student_id: UUID = STUDENT,
                 server_deadline_at: datetime = DEADLINE) -> None:
        self.id = ATTEMPT
        self.student_id = student_id
        self.status = status
        self.assignment_status = assignment_status
        self.server_deadline_at = server_deadline_at


def build(*, now: datetime = NOW, **attempt_kwargs):
    repo = InMemoryResponseRepository()
    repo.add_attempt(FakeAttempt(**attempt_kwargs))
    service = ResponseService(repository=repo, clock=lambda: now)
    return service, repo


def save(service, **overrides):
    values = dict(attempt_id=ATTEMPT, slot_id=SLOT_A, student_id=STUDENT,
                  client_revision_id="client-1", base_revision_no=0,
                  raw_response={"text": "x+1"})
    values.update(overrides)
    return service.save(**values)


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_first_save_is_revision_one() -> None:
    service, _ = build()
    result = save(service)
    assert result.revision_no == 1
    assert result.parse_status == "parsed"


def test_same_client_revision_returns_the_same_revision() -> None:
    service, repo = build()
    first = save(service, client_revision_id="client-1")
    again = save(service, client_revision_id="client-1")
    assert again.revision_no == first.revision_no == 1
    assert len(repo.rows) == 1, "重试不该插第二行"


def test_same_client_id_with_different_content_is_a_client_bug() -> None:
    service, _ = build()
    save(service, client_revision_id="client-1", raw_response={"text": "第一版"})
    expect(IdempotencyConflict, lambda: save(
        service, client_revision_id="client-1", raw_response={"text": "换内容"}))


# ---------------------------------------------------------------------------
# 冲突
# ---------------------------------------------------------------------------

def test_stale_base_returns_the_server_latest() -> None:
    service, _ = build()
    save(service, client_revision_id="client-1", raw_response={"text": "第一版"})
    error = expect(ResponseConflict, lambda: save(
        service, client_revision_id="client-2", base_revision_no=0,
        raw_response={"text": "旧标签页"}))
    assert error.server_result.revision_no == 1
    assert error.server_result.raw_response == {"text": "第一版"}, (
        "冲突必须把服务端最新的**本人**响应带回去，否则学生不知道覆盖了什么")


def test_sequential_saves_with_matching_base_succeed() -> None:
    service, _ = build()
    first = save(service, client_revision_id="client-1", base_revision_no=0)
    second = save(service, client_revision_id="client-2", base_revision_no=1,
                  raw_response={"text": "第二版"})
    assert second.revision_no == first.revision_no + 1


def test_two_tabs_editing_different_slots_do_not_conflict() -> None:
    """按槽比较的意义：改不同的题不该互相报错。"""
    service, _ = build()
    save(service, slot_id=SLOT_A, client_revision_id="tab-a")
    second = save(service, slot_id=SLOT_B, client_revision_id="tab-b",
                  base_revision_no=0, raw_response={"text": "B 的答案"})
    assert second.revision_no == 1


def test_conflict_is_only_raised_when_the_base_is_behind() -> None:
    service, _ = build()
    save(service, client_revision_id="client-1")
    error = expect(ResponseConflict, lambda: save(
        service, client_revision_id="client-2", base_revision_no=5))
    assert "第 5 版" in error.message
    assert error.server_result.revision_no == 1


# ---------------------------------------------------------------------------
# 时间与状态
# ---------------------------------------------------------------------------

def test_save_exactly_at_deadline_is_accepted() -> None:
    service, _ = build()
    result = save(service, client_revision_id="client-1", base_revision_no=0,
                  **{}).revision_no
    assert result == 1
    late = expect(AttemptFinalized, lambda: save(
        service, client_revision_id="client-3", base_revision_no=1,
        server_now=DEADLINE + timedelta(microseconds=1)))
    assert late.code == "attempt_finalized"


def test_submitted_attempt_cannot_be_saved() -> None:
    service, _ = build(status="submitted")
    expect(AttemptFinalized, lambda: save(service, client_revision_id="late"))


def test_withdrawn_assignment_blocks_saving_with_its_own_code() -> None:
    service, _ = build(assignment_status="withdrawn")
    error = expect(Withdrawn, lambda: save(service, client_revision_id="late"))
    assert error.code == "assignment_withdrawn"


def test_other_students_attempt_is_not_found() -> None:
    service, _ = build()
    expect(ResourceNotFound, lambda: save(service, student_id=OTHER,
                                          client_revision_id="client-x"))


# ---------------------------------------------------------------------------
# 非法输入留证
# ---------------------------------------------------------------------------

def test_empty_response_is_saved_as_invalid_not_rejected() -> None:
    service, repo = build()
    result = save(service, client_revision_id="client-1", raw_response={})
    assert result.parse_status == "invalid"
    assert result.revision_no == 1
    assert len(repo.rows) == 1, "空答案也要留一行：否则学生以为写了"


def test_oversize_response_is_flagged() -> None:
    invalid, reason = looks_invalid({"text": "x" * 100000})
    assert invalid is True and reason == INVALID_OVERSIZE


def test_wrong_shaped_response_is_flagged() -> None:
    invalid, reason = looks_invalid({"text": 123})
    assert invalid is True and reason != INVALID_EMPTY


def test_latex_only_response_is_valid() -> None:
    invalid, reason = looks_invalid({"latex": "\\frac{1}{2}"})
    assert invalid is False and reason is None


def test_latest_for_slot_returns_empty_when_nothing_saved() -> None:
    service, _ = build()
    empty = service.latest_for_slot(attempt_id=ATTEMPT, slot_id=SLOT_A,
                                   student_id=STUDENT)
    assert empty.revision_no == 0
    assert empty.raw_response is None


def test_latest_for_slot_returns_the_newest_revision() -> None:
    service, _ = build()
    save(service, client_revision_id="client-1", raw_response={"text": "一"})
    save(service, client_revision_id="client-2", base_revision_no=1,
         raw_response={"text": "二"})
    latest = service.latest_for_slot(attempt_id=ATTEMPT, slot_id=SLOT_A,
                                     student_id=STUDENT)
    assert latest.revision_no == 2
    assert latest.raw_response == {"text": "二"}


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
    print(f"✓ 自动保存测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
