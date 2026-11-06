"""阶段 C 任务 5/6：交卷快照、幂等回执、到期收卷与竞态。

要钉住的是"这一次交卷到底冻结了什么"：

  * 截止之后才到达的修订**不进快照**（但事实仍留在 response_revision 里）；
  * 没作答 ≠ 作答为空：前者 `unanswered`，后者 `invalid(empty)`；
  * 交卷幂等：同一 attempt 第二次交卷返回**同一份回执**（含同一 receipt_hash）；
  * 手动交卷与到期 worker 同时发生时，只产生一份快照，后到的只拿到回执；
  * 撤回的作业：收卷仍然发生（冻结事实），但不再允许继续作答。

运行：`python server/tests/test_online_finalize.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.online.contracts import AttemptFinalized, ResourceNotFound  # noqa: E402
from app.online.repository import AttemptItemSeed  # noqa: E402
from app.online.responses import ResponseRow  # noqa: E402
from app.online.submissions import (  # noqa: E402
    DeadlineWorker,
    InMemorySubmissionRepository,
    SubmissionService,
)

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(hours=1)
STUDENT = UUID("5e5e0000-0000-4000-8000-000000000001")
OTHER = UUID("5e5e0000-0000-4000-8000-000000000002")
ATTEMPT = UUID("aa000000-0000-4000-8000-000000000001")
SLOT_A = UUID("cc000000-0000-4000-8000-0000000000a1")
SLOT_B = UUID("cc000000-0000-4000-8000-0000000000b1")


class FakeAttempt:
    def __init__(self, *, status: str = "in_progress",
                 assignment_status: str = "published",
                 student_id: UUID = STUDENT,
                 deadline: datetime = DEADLINE,
                 worker_due: datetime | None = None) -> None:
        self.id = ATTEMPT
        self.student_id = student_id
        self.status = status
        self.assignment_status = assignment_status
        self.server_deadline_at = deadline
        self.worker_due_at = worker_due or (deadline + timedelta(minutes=10))
        self.closed = None

    def close(self, moment: datetime, reason: str) -> None:
        self.status = "submitted"
        self.closed = (moment, reason)


def seed_items() -> tuple[AttemptItemSeed, ...]:
    return (
        AttemptItemSeed(assignment_item_id=UUID(int=101), paper_slot_id=SLOT_A, ord=1),
        AttemptItemSeed(assignment_item_id=UUID(int=102), paper_slot_id=SLOT_B, ord=2),
    )


def revision(*, slot: UUID, revision_no: int, received: datetime, text: str,
             parse_status: str = "parsed") -> ResponseRow:
    return ResponseRow(
        id=UUID(int=500 + revision_no + (1 if slot == SLOT_B else 0)),
        attempt_id=ATTEMPT, slot_id=slot, revision_no=revision_no,
        client_revision_id=f"c-{slot}-{revision_no}", raw_response={"text": text},
        parse_status=parse_status, invalid_reasons=(),
        server_received_at=received,
    )


def build(*, attempt: FakeAttempt | None = None, revisions=()):
    repo = InMemorySubmissionRepository()
    repo.add_attempt(attempt or FakeAttempt(), items=seed_items())
    for row in revisions:
        repo.add_response(row)
    service = SubmissionService(repository=repo, clock=lambda: NOW)
    return service, repo


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 快照内容
# ---------------------------------------------------------------------------

def test_snapshot_takes_latest_revision_per_slot() -> None:
    service, _ = build(revisions=(
        revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"),
        revision(slot=SLOT_A, revision_no=2, received=NOW + timedelta(minutes=5), text="二"),
        revision(slot=SLOT_B, revision_no=1, received=NOW, text="B"),
    ))
    receipt = service.finalize(attempt_id=ATTEMPT, student_id=STUDENT)
    assert receipt.item_count == 2
    assert receipt.unanswered_count == 0
    record = service.repository.submissions[ATTEMPT]
    by_slot = {item.slot_id: item for item in record.items}
    assert by_slot[SLOT_A].revision_no == 2
    assert by_slot[SLOT_A].raw_response == {"text": "二"}


def test_revisions_after_the_deadline_are_excluded_from_the_snapshot() -> None:
    service, repo = build(attempt=FakeAttempt(deadline=DEADLINE), revisions=(
        revision(slot=SLOT_A, revision_no=1, received=DEADLINE, text="截止那一刻"),
        revision(slot=SLOT_B, revision_no=1, received=DEADLINE + timedelta(seconds=1),
                 text="迟到的"),
    ))
    receipt = service.finalize(attempt_id=ATTEMPT)
    record = repo.submissions[ATTEMPT]
    by_slot = {item.slot_id: item for item in record.items}
    assert by_slot[SLOT_A].raw_response == {"text": "截止那一刻"}
    assert by_slot[SLOT_B].unanswered is True, "迟到的修订不能进快照"
    assert receipt.unanswered_count == 1
    # 事实仍留在修订里（不进快照不等于删除）
    assert any(row.slot_id == SLOT_B for row in repo.response_rows)


def test_unanswered_and_invalid_are_counted_separately() -> None:
    service, _ = build(revisions=(
        revision(slot=SLOT_A, revision_no=1, received=NOW, text="", parse_status="invalid"),
    ))
    receipt = service.finalize(attempt_id=ATTEMPT)
    assert receipt.item_count == 2
    assert receipt.invalid_count == 1, "写了空白 → invalid"
    assert receipt.unanswered_count == 1, "完全没写 → unanswered"


def test_manifest_and_receipt_hashes_are_stable() -> None:
    revisions = (
        revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"),
        revision(slot=SLOT_B, revision_no=1, received=NOW, text="二"),
    )
    first_service, _ = build(revisions=revisions)
    first = first_service.finalize(attempt_id=ATTEMPT, trigger_kind="manual")
    second_service, _ = build(revisions=revisions)
    second = second_service.finalize(attempt_id=ATTEMPT, trigger_kind="manual")
    assert first.response_manifest_hash == second.response_manifest_hash
    assert first.receipt_hash == second.receipt_hash
    assert len(first.response_manifest_hash) == 64


def test_trigger_kind_changes_the_receipt_hash() -> None:
    revisions = (revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"),)
    manual_service, _ = build(revisions=revisions)
    manual = manual_service.finalize(attempt_id=ATTEMPT, trigger_kind="manual")
    deadline_service, _ = build(revisions=revisions)
    deadline = deadline_service.finalize(attempt_id=ATTEMPT, trigger_kind="deadline")
    assert manual.response_manifest_hash == deadline.response_manifest_hash
    assert manual.receipt_hash != deadline.receipt_hash, (
        "触发方式要进回执哈希：否则申诉时分不清是手动交的还是到点收的")


# ---------------------------------------------------------------------------
# 幂等与竞态
# ---------------------------------------------------------------------------

def test_second_finalize_returns_the_same_receipt() -> None:
    service, repo = build(revisions=(
        revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"),))
    first = service.finalize(attempt_id=ATTEMPT, trigger_kind="manual")
    again = service.finalize(attempt_id=ATTEMPT, trigger_kind="deadline")
    assert again.submission_id == first.submission_id
    assert again.receipt_hash == first.receipt_hash
    assert len(repo.submissions) == 1, "不能产生第二份快照"


def test_deadline_worker_finalizes_due_attempts() -> None:
    attempt = FakeAttempt(worker_due=NOW - timedelta(seconds=1))
    repo = InMemorySubmissionRepository()
    repo.add_attempt(attempt, items=seed_items())
    repo.add_response(revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"))
    service = SubmissionService(repository=repo, clock=lambda: NOW)
    worker = DeadlineWorker(submissions=service, repository=repo)
    report = worker.finalize_due(now=NOW)
    assert report["finalized"] == [str(ATTEMPT)]
    assert report["replayed"] == []
    assert service.repository.submissions[ATTEMPT].trigger_kind == "deadline"


def test_deadline_worker_is_idempotent_when_manual_came_first() -> None:
    repo = InMemorySubmissionRepository()
    repo.add_attempt(FakeAttempt(worker_due=NOW - timedelta(seconds=1)),
                     items=seed_items())
    service = SubmissionService(repository=repo, clock=lambda: NOW)
    manual = service.finalize(attempt_id=ATTEMPT, student_id=STUDENT,
                              trigger_kind="manual")
    worker = DeadlineWorker(submissions=service, repository=repo)
    report = worker.finalize_due(now=NOW)
    # 交卷把 attempt 置成终态：worker 的 due 查询看不到它，因此什么都不做。
    assert report == {"finalized": [], "replayed": []}, report
    assert repo.submissions[ATTEMPT].receipt_hash == manual.receipt_hash
    assert service.repository.lock_attempt(ATTEMPT).status == "submitted"
    assert repo.due_attempts(NOW) == ()


def test_worker_and_manual_race_produces_one_snapshot() -> None:
    """worker 先收，学生随后手点交卷：拿到同一份回执，触发方式不被改写。"""
    repo = InMemorySubmissionRepository()
    repo.add_attempt(FakeAttempt(worker_due=NOW - timedelta(seconds=1)),
                     items=seed_items())
    repo.add_response(revision(slot=SLOT_A, revision_no=1, received=NOW, text="一"))
    service = SubmissionService(repository=repo, clock=lambda: NOW)
    assert DeadlineWorker(submissions=service, repository=repo).finalize_due(
        now=NOW)["finalized"] == [str(ATTEMPT)]
    manual = service.finalize(attempt_id=ATTEMPT, student_id=STUDENT,
                              trigger_kind="manual")
    assert len(repo.submissions) == 1, "只能有一份快照"
    assert repo.submissions[ATTEMPT].trigger_kind == "deadline", (
        "先写的人是谁就是谁：后到的不能改写触发方式")
    assert manual.receipt_hash == repo.submissions[ATTEMPT].receipt_hash


def test_deadline_worker_skips_attempts_not_due_yet() -> None:
    repo = InMemorySubmissionRepository()
    repo.add_attempt(FakeAttempt(worker_due=NOW + timedelta(minutes=30)),
                     items=seed_items())
    service = SubmissionService(repository=repo, clock=lambda: NOW)
    report = DeadlineWorker(submissions=service, repository=repo).finalize_due(now=NOW)
    assert report == {"finalized": [], "replayed": []}


# ---------------------------------------------------------------------------
# 越权与终态
# ---------------------------------------------------------------------------

def test_other_student_cannot_finalize() -> None:
    service, _ = build()
    expect(ResourceNotFound, lambda: service.finalize(
        attempt_id=ATTEMPT, student_id=OTHER))


def test_missing_attempt_is_404() -> None:
    service, _ = build()
    expect(ResourceNotFound, lambda: service.finalize(attempt_id=UUID(int=999)))


def test_already_closed_without_submission_is_refused() -> None:
    service, _ = build(attempt=FakeAttempt(status="closed"))
    expect(AttemptFinalized, lambda: service.finalize(attempt_id=ATTEMPT))


def test_withdrawn_assignment_can_still_be_finalized() -> None:
    """撤回只冻结事实、不再允许作答；收卷本身仍要发生。"""
    service, repo = build(attempt=FakeAttempt(assignment_status="withdrawn"),
                          revisions=(revision(slot=SLOT_A, revision_no=1,
                                              received=NOW, text="一"),))
    receipt = service.finalize(attempt_id=ATTEMPT, trigger_kind="forced")
    assert receipt.item_count == 2
    assert repo.submissions[ATTEMPT].trigger_kind == "forced"


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
    print(f"✓ 交卷与到期收卷测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
