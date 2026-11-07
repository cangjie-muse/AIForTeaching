"""阶段 D 任务 6—7：成绩发布、幂等、撤回与申诉闭环。

要钉住的七条：

  1. 幂等：同一键重复发布返回同一版本（`replayed=True`），不新增版本；
  2. 发布前**再校验**每个提交项都有决定（确认与发布之间可能加项）；
  3. 重复发布同一版本幂等返回；
  4. 撤回：新增 `withdrawn` 版本 + 原因，`current_published` 清空，
     学生随之看到"无已发布成绩"；
  5. 撤回必须写原因（学生要看）；
  6. 申诉：open → accepted → regrade 追加新版本（带 appeal_id），**不发布**；
  7. 只有已受理的申诉能重评；重评后的新版本仍需 publish 才可见。

运行：`python server/tests/test_publish_and_appeal.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.grading.publish import (  # noqa: E402
    AlreadyWithdrawn,
    InMemoryPublishRepository,
    PublishIncomplete,
    PublishService,
)
from app.grading.revisions import (  # noqa: E402
    ConfirmCommand,
    GradeAlreadyPublished,
    GradingError,
    GradingService,
    ItemDecisionInput,
)

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
ITEM_A = UUID("dd000000-0000-4000-8000-0000000000a1")
ITEM_B = UUID("dd000000-0000-4000-8000-0000000000b1")
TEACHER = UUID("5e5e0000-0000-4000-8000-0000000000c1")
STUDENT = UUID("5e5e0000-0000-4000-8000-0000000000d1")
MAX_SCORES = {ITEM_A: Decimal("5.00"), ITEM_B: Decimal("10.00")}


def decision(item: UUID, score: str) -> ItemDecisionInput:
    return ItemDecisionInput(submission_item_id=item, score=Decimal(score))


def build(*, confirmed: bool = True):
    repo = InMemoryPublishRepository()
    grading = GradingService(repository=repo.grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=800 + len(repo.grading.grade_revisions)))
    if confirmed:
        grading.confirm_grade(
            ConfirmCommand(submission_id=SUBMISSION,
                           decisions=(decision(ITEM_A, "5.00"), decision(ITEM_B, "9.00"))),
            actor_id=TEACHER, item_max_scores=MAX_SCORES)
    service = PublishService(repository=repo, grading=grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=700 + len(repo.appeals)))
    return service, repo, grading


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


def publish(service, key: str = "pub-1", items=(ITEM_A, ITEM_B)):
    return service.publish(submission_id=SUBMISSION, actor_id=TEACHER,
                           idempotency_key=key, expected_items=list(items))


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------

def test_publish_marks_the_revision_published() -> None:
    service, repo, _ = build()
    result = publish(service)
    assert result.revision.status == "published"
    assert result.revision.published_at is not None
    assert result.revision.published_by == TEACHER
    assert result.replayed is False
    assert repo.current_published[SUBMISSION] == result.revision.id


def test_republishing_with_the_same_key_is_idempotent() -> None:
    service, repo, _ = build()
    first = publish(service, "pub-1")
    second = publish(service, "pub-1")
    assert second.replayed is True
    assert second.revision.id == first.revision.id
    # 版本序列是 confirmed → published；重放不该再追加第三条。
    assert len(repo.grading.grade_revisions) == 2, "重放不该产生新版本"


def test_publishing_twice_with_different_keys_returns_the_same_published_version() -> None:
    service, repo, _ = build()
    first = publish(service, "pub-1")
    second = publish(service, "pub-2")
    assert second.replayed is True, "已发布的版本重复发布应按幂等返回，而不是报错"
    assert second.revision.id == first.revision.id
    assert len(repo.grading.grade_revisions) == 2


def test_same_key_with_different_request_is_a_conflict() -> None:
    service, _, _ = build()
    publish(service, "pub-1")
    error = expect(GradingError, lambda: publish(service, "pub-1", items=(ITEM_A,)))
    assert "idempotency_conflict" in str(error)


def test_publish_requires_every_item_to_have_a_decision() -> None:
    service, _, _ = build(confirmed=False)
    expect(GradingError, lambda: publish(service))


def test_publish_refuses_when_a_new_item_appeared_after_confirmation() -> None:
    """确认之后追加了提交项：发布必须整批拒绝，不能少算一项。"""
    service, _, grading = build()
    extra = UUID("dd000000-0000-4000-8000-0000000000c1")
    error = expect(PublishIncomplete, lambda: publish(
        service, items=(ITEM_A, ITEM_B, extra)))
    assert extra in error.missing, "要指出缺的是哪一项"


def test_withdrawn_version_cannot_be_published_again_directly() -> None:
    service, _, _ = build()
    publish(service, "pub-1")
    service.withdraw(submission_id=SUBMISSION, actor_id=TEACHER,
                     reason="发现评分标准错误", idempotency_key="wd-1")
    expect(GradeAlreadyPublished, lambda: publish(service, "pub-3"))


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------

def test_withdraw_creates_a_withdrawn_version_and_clears_current() -> None:
    service, repo, _ = build()
    publish(service, "pub-1")
    withdrawn = service.withdraw(submission_id=SUBMISSION, actor_id=TEACHER,
                                 reason="评分标准写错，撤回重评", idempotency_key="wd-1")
    assert withdrawn.status == "withdrawn"
    assert withdrawn.reason == "评分标准写错，撤回重评"
    assert SUBMISSION not in repo.current_published, "撤回后学生不应再看到已发布成绩"
    # 版本序列：confirmed → published → withdrawn。撤回是新增一条，不是改写旧的。
    assert len(repo.grading.grade_revisions) == 3, "历史版本要保留"


def test_withdraw_requires_a_reason() -> None:
    service, _, _ = build()
    publish(service, "pub-1")
    expect(GradingError, lambda: service.withdraw(
        submission_id=SUBMISSION, actor_id=TEACHER, reason="  ",
        idempotency_key="wd-1"))


def test_withdraw_without_published_version_is_refused() -> None:
    service, _, _ = build()
    expect(AlreadyWithdrawn, lambda: service.withdraw(
        submission_id=SUBMISSION, actor_id=TEACHER, reason="没有发布过",
        idempotency_key="wd-1"))


def test_withdraw_is_idempotent_by_key() -> None:
    service, repo, _ = build()
    publish(service, "pub-1")
    first = service.withdraw(submission_id=SUBMISSION, actor_id=TEACHER,
                             reason="撤回原因", idempotency_key="wd-1")
    second = service.withdraw(submission_id=SUBMISSION, actor_id=TEACHER,
                              reason="撤回原因", idempotency_key="wd-1")
    assert first.id == second.id
    assert len(repo.grading.grade_revisions) == 3, "重复撤回不该追加新版本"


# ---------------------------------------------------------------------------
# 申诉闭环
# ---------------------------------------------------------------------------

def test_appeal_requires_reason_and_existing_revision() -> None:
    service, _, _ = build()
    published = publish(service, "pub-1").revision
    expect(GradingError, lambda: service.file_appeal(
        submission_id=SUBMISSION, student_id=STUDENT,
        grade_revision_id=published.id, reason=" "))
    expect(GradingError, lambda: service.file_appeal(
        submission_id=SUBMISSION, student_id=STUDENT,
        grade_revision_id=UUID(int=1), reason="第 2 题判分不合理"))


def test_full_appeal_regrade_publish_cycle() -> None:
    service, repo, grading = build()
    first = publish(service, "pub-1").revision
    appeal = service.file_appeal(submission_id=SUBMISSION, student_id=STUDENT,
                                 grade_revision_id=first.id,
                                 reason="第 2 题步骤分给少了")
    accepted = service.accept_appeal(appeal_id=appeal.appeal.id, actor_id=TEACHER)
    assert accepted.regrade_run_id is not None

    regraded = service.regrade(appeal_id=appeal.appeal.id, actor_id=TEACHER,
                               new_total=Decimal("15.00"),
                               change_reason="复核后补 2 分")
    assert regraded.revision_no == first.revision_no + 1
    assert regraded.appeal_id == appeal.appeal.id
    assert regraded.status == "draft", "重评版本先落草稿，发布是独立动作"
    assert repo.grading.grade_revisions[0].total_score == Decimal("14.00"), (
        "原版本分数不能因重评被改写")

    # 重评版本是 draft，必须重新有教师决定才能发布（不能沿用旧版本的决定）。
    grading.confirm_grade(
        ConfirmCommand(submission_id=SUBMISSION, base_revision_no=regraded.revision_no,
                       change_reason="申诉复核确认",
                       decisions=(decision(ITEM_A, "5.00"), decision(ITEM_B, "10.00"))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES)
    published = publish(service, "pub-2")
    assert published.revision.id == regraded.id
    assert published.revision.status == "published"
    assert repo.current_published[SUBMISSION] == regraded.id


def test_regrade_requires_an_accepted_appeal() -> None:
    service, _, _ = build()
    published = publish(service, "pub-1").revision
    appeal = service.file_appeal(submission_id=SUBMISSION, student_id=STUDENT,
                                 grade_revision_id=published.id, reason="理由")
    expect(GradingError, lambda: service.regrade(
        appeal_id=appeal.appeal.id, actor_id=TEACHER,
        new_total=Decimal("15.00"), change_reason="还没受理"))


def test_appeal_cannot_be_accepted_twice() -> None:
    service, _, _ = build()
    published = publish(service, "pub-1").revision
    appeal = service.file_appeal(submission_id=SUBMISSION, student_id=STUDENT,
                                 grade_revision_id=published.id, reason="理由")
    service.accept_appeal(appeal_id=appeal.appeal.id, actor_id=TEACHER)
    expect(GradingError, lambda: service.accept_appeal(
        appeal_id=appeal.appeal.id, actor_id=TEACHER))


def test_appeal_for_unknown_id_is_refused() -> None:
    service, _, _ = build()
    expect(GradingError, lambda: service.accept_appeal(
        appeal_id=UUID(int=999), actor_id=TEACHER))


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
    print(f"✓ 成绩发布、撤回与申诉测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
