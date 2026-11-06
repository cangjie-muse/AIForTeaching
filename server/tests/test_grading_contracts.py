"""阶段 D：成绩发布门禁、追加式改分与申诉。

要钉住的三件事：

1. **未确认的人工分不能发布**，而且不是"跳过那一项"，是整批拒绝并把阻塞项带出来；
2. **总分不把未确认项当 0**——那会悄悄压低学生成绩；
3. **改分是新增一版**，上一版状态变 `regraded`、指回 `previous_revision_id`，
   学生看到的"当前成绩"永远是最大的**已发布**版本。

运行：`python server/tests/test_grading_contracts.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.grading.contracts import (  # noqa: E402
    Appeal,
    AppealStatus,
    GradeError,
    GradeNotDraft,
    GradePublishBlocked,
    GradeRevision,
    SubmissionGradeView,
    accept_appeal,
    assert_publishable,
    current_for_student,
    publish_checks,
    publish_revision,
    regrade_revision,
    total_of,
)

NOW = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
TEACHER = UUID("5e5e0000-0000-4000-8000-0000000000t1".replace("t", "0"))
STUDENT = UUID("5e5e0000-0000-4000-8000-000000000002")


def item(number: int, *, confirmed: str | None = None, requires_human: bool = False,
         suggested: str | None = None) -> SubmissionGradeView:
    return SubmissionGradeView(
        submission_item_id=UUID(int=100 + number), slot_no=number,
        suggested_score=None if suggested is None else Decimal(suggested),
        confirmed_score=None if confirmed is None else Decimal(confirmed),
        requires_human=requires_human,
        confirmed_by=TEACHER if confirmed else None,
        confirmed_at=NOW if confirmed else None,
    )


# ---------------------------------------------------------------------------
# 发布门禁
# ---------------------------------------------------------------------------

def test_total_uses_confirmed_scores_only() -> None:
    grades = [item(1, confirmed="8.00"), item(2, confirmed="12.00")]
    assert total_of(grades) == Decimal("20.00")


def test_unconfirmed_scores_block_the_total() -> None:
    grades = [item(1, confirmed="8.00"), item(2)]
    try:
        total_of(grades)
    except GradePublishBlocked as error:
        assert "还没确认分数" in str(error)
        assert len(error.blocking) == 1
    else:
        raise AssertionError("未确认项不能被当成 0")


def test_publish_checks_lists_only_blocking_items() -> None:
    grades = [item(1, confirmed="8.00"), item(2, requires_human=True),
              item(3, confirmed="4.00")]
    assert [entry.slot_no for entry in publish_checks(grades)] == [2]


def test_publish_is_all_or_nothing() -> None:
    grades = [item(1, confirmed="8.00"), item(2, requires_human=True)]
    try:
        assert_publishable(grades)
    except GradePublishBlocked as error:
        assert "1 个提交项" in str(error)
        assert error.blocking[0].slot_no == 2
    else:
        raise AssertionError("有一个待人工确认时不能发布")


def test_fully_confirmed_batch_publishes() -> None:
    grades = [item(1, confirmed="8.00"), item(2, confirmed="12.00")]
    assert assert_publishable(grades) == Decimal("20.00")


def test_requires_human_review_does_not_block_once_confirmed() -> None:
    grades = [item(1, requires_human=True, confirmed="9.00")]
    assert assert_publishable(grades) == Decimal("9.00")


# ---------------------------------------------------------------------------
# 版本与发布
# ---------------------------------------------------------------------------

def revision(number: int = 1, *, status: str = "draft", total: str = "20.00",
             previous: UUID | None = None) -> GradeRevision:
    return GradeRevision(
        id=UUID(int=900 + number), submission_id=SUBMISSION, revision_no=number,
        total_score=Decimal(total), status=status,
        published_at=NOW if status == "published" else None,
        published_by=TEACHER if status == "published" else None,
        previous_revision_id=previous, created_at=NOW,
    )


def test_regrade_appends_a_new_revision() -> None:
    base = revision(1, status="published")
    updated = regrade_revision(previous=base, new_total=Decimal("22.50"),
                              actor_id=TEACHER, now=NOW, revision_id=UUID(int=901))
    assert updated.revision_no == 2
    assert updated.previous_revision_id == base.id
    assert updated.status == "draft", "改分先落成草稿，等教师确认发布"
    assert updated.total_score == Decimal("22.50")


def test_regrade_requires_published_or_confirmed_baseline() -> None:
    base = revision(1, status="draft")
    try:
        regrade_revision(previous=base, new_total=Decimal("22.00"),
                         actor_id=TEACHER, now=NOW, revision_id=UUID(int=902))
    except GradeNotDraft as error:
        assert "不能作为改分基线" in str(error)
    else:
        raise AssertionError("草稿版不能作为改分基线")


def test_regrade_links_to_the_appeal() -> None:
    base = revision(1, status="published")
    appeal_id = UUID(int=777)
    updated = regrade_revision(previous=base, new_total=Decimal("23.00"),
                              actor_id=TEACHER, now=NOW, appeal_id=appeal_id,
                              revision_id=UUID(int=903))
    assert updated.appeal_id == appeal_id


def test_only_draft_can_be_published() -> None:
    published = revision(1, status="published")
    try:
        publish_revision(published, actor_id=TEACHER, now=NOW)
    except GradeNotDraft as error:
        assert "只有 draft 能发布" in str(error)
    else:
        raise AssertionError("已发布的版本不能再发布")


def test_publish_records_who_and_when() -> None:
    published = publish_revision(revision(1), actor_id=TEACHER, now=NOW)
    assert published.status == "published"
    assert published.published_at == NOW
    assert published.published_by == TEACHER


def test_student_sees_the_latest_published_revision() -> None:
    versions = [revision(1, status="published"), revision(2, status="draft")]
    assert current_for_student(versions).revision_no == 1, (
        "草稿改分不能提前让学生看到")


def test_withdrawn_revision_is_not_current() -> None:
    versions = [
        revision(1, status="published"),
        revision(2, status="withdrawn", total="0.00"),
    ]
    current = current_for_student(versions)
    assert current is not None and current.revision_no == 1


# ---------------------------------------------------------------------------
# 申诉
# ---------------------------------------------------------------------------

def appeal(status: AppealStatus = AppealStatus.OPEN) -> Appeal:
    return Appeal(id=UUID(int=800), submission_id=SUBMISSION, student_id=STUDENT,
                  reason="第 2 题判分不合理", status=status, created_at=NOW)


def test_accept_appeal_only_from_open() -> None:
    accepted = accept_appeal(appeal(), actor_id=TEACHER, now=NOW + timedelta(hours=1))
    assert accepted.status is AppealStatus.ACCEPTED
    assert accepted.decided_by == TEACHER


def test_cannot_accept_twice() -> None:
    try:
        accept_appeal(appeal(AppealStatus.ACCEPTED), actor_id=TEACHER, now=NOW)
    except GradeError as error:
        assert "只有 open 能受理" in str(error)
    else:
        raise AssertionError("已受理的申诉不能再受理")


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
    print(f"✓ 成绩与申诉测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
