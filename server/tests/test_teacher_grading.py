"""阶段 D 任务 5：教师确认评分、rubric 分项一致、追加版本、AI 只能建议。

要钉住的五条：

  1. 缺任何一项教师决定 → 整批 `ReviewIncomplete`（不跳过、不少算）；
  2. rubric 分项合计必须等于项分，否则 `rubric_score_mismatch`；
  3. 确认**不发布**（`published_at` 为空），发布是下一步的独立动作；
  4. 二次确认追加第 2 版并指回第 1 版，**第 1 版分数原样保留**；
  5. AI 分组建议只有 `proposed`，**不写任何 grade revision、也不写任何决定**。

运行：`python server/tests/test_teacher_grading.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.grading.revisions import (  # noqa: E402
    ConfirmCommand,
    GradeAlreadyPublished,
    GradingError,
    GradingService,
    GroupSuggestion,
    InMemoryGradingRepository,
    ItemDecisionInput,
    ReviewIncomplete,
    RubricCriterionScore,
    RubricScoreMismatch,
    decisions_of_current_revision,
    grade_manifest_hash,
)

NOW = datetime(2026, 11, 7, 5, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
ITEM_A = UUID("dd000000-0000-4000-8000-0000000000a1")
ITEM_B = UUID("dd000000-0000-4000-8000-0000000000b1")
TEACHER = UUID("5e5e0000-0000-4000-8000-0000000000c1")

MAX_SCORES = {ITEM_A: Decimal("5.00"), ITEM_B: Decimal("10.00")}


def decision(number: UUID, score: str, *, criteria=None,
             rationale: str | None = None, sequence: int = 1) -> ItemDecisionInput:
    return ItemDecisionInput(
        submission_item_id=number, score=Decimal(score),
        criterion_scores=tuple(criteria or ()), rationale=rationale, sequence=sequence)


def build(*, advisor=None):
    repo = InMemoryGradingRepository()
    return GradingService(repository=repo, advisor=advisor, clock=lambda: NOW,
                          id_factory=lambda: UUID(int=900 + len(repo.grade_revisions))), repo


def full_command(**changes) -> ConfirmCommand:
    values = dict(
        submission_id=SUBMISSION,
        decisions=(decision(ITEM_A, "5.00"), decision(ITEM_B, "8.00")),
    )
    values.update(changes)
    return ConfirmCommand(**values)


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 完整性
# ---------------------------------------------------------------------------

def test_every_item_needs_a_teacher_decision() -> None:
    service, repo = build()
    error = expect(ReviewIncomplete, lambda: service.confirm_grade(
        full_command(decisions=(decision(ITEM_A, "5.00"),)),
        actor_id=TEACHER, item_max_scores=MAX_SCORES))
    assert ITEM_B in error.missing, "要指出缺的是哪一项，而不是只说「缺项」"


def test_confirmation_appends_a_version_but_does_not_publish() -> None:
    service, repo = build()
    revision = service.confirm_grade(full_command(), actor_id=TEACHER,
                                    item_max_scores=MAX_SCORES)
    assert revision.revision_no == 1
    assert revision.status == "confirmed"
    assert revision.published_at is None, "确认与发布是两件事"
    assert revision.total_score == Decimal("13.00")
    assert {record.decided_by for record in repo.decisions} == {TEACHER}
    assert len(repo.decisions) == 2


def test_score_out_of_range_is_refused() -> None:
    service, _ = build()
    expect(GradingError, lambda: service.confirm_grade(
        full_command(decisions=(decision(ITEM_A, "99.00"),
                                decision(ITEM_B, "8.00"))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES))


def test_rubric_criteria_must_sum_to_the_item_score() -> None:
    service, _ = build()
    criteria = (RubricCriterionScore("step-1", Decimal("2.00")),
                RubricCriterionScore("step-2", Decimal("2.99")))
    error = expect(RubricScoreMismatch, lambda: service.confirm_grade(
        full_command(decisions=(decision(ITEM_A, "5.00", criteria=criteria),
                                decision(ITEM_B, "8.00"))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES))
    assert error.code == "rubric_score_mismatch"


def test_matching_rubric_criteria_are_accepted() -> None:
    service, repo = build()
    criteria = (RubricCriterionScore("step-1", Decimal("2.00")),
                RubricCriterionScore("step-2", Decimal("3.00")))
    revision = service.confirm_grade(
        full_command(decisions=(decision(ITEM_A, "5.00", criteria=criteria),
                                decision(ITEM_B, "8.00"))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES)
    assert revision.total_score == Decimal("13.00")


# ---------------------------------------------------------------------------
# 追加版本
# ---------------------------------------------------------------------------

def test_second_confirmation_appends_and_keeps_the_first() -> None:
    service, repo = build()
    first = service.confirm_grade(full_command(), actor_id=TEACHER,
                                 item_max_scores=MAX_SCORES)
    second = service.confirm_grade(
        full_command(base_revision_no=1, change_reason="复核后调整步骤分",
                     decisions=(decision(ITEM_A, "4.00"), decision(ITEM_B, "8.00"))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES)
    assert second.revision_no == 2
    assert second.previous_revision_id == first.id
    assert first.total_score == Decimal("13.00"), "第一版分数不能被改写"
    assert second.total_score == Decimal("12.00")
    assert second.status == "confirmed"
    assert second.published_at is None


def test_stale_base_revision_is_refused() -> None:
    service, _ = build()
    service.confirm_grade(full_command(), actor_id=TEACHER, item_max_scores=MAX_SCORES)
    error = expect(GradingError, lambda: service.confirm_grade(
        full_command(base_revision_no=7), actor_id=TEACHER, item_max_scores=MAX_SCORES))
    assert "base_revision_stale" in str(error)


def test_published_version_blocks_further_confirmation() -> None:
    service, repo = build()
    revision = service.confirm_grade(full_command(), actor_id=TEACHER,
                                    item_max_scores=MAX_SCORES)
    repo.grade_revisions[0] = revision.model_copy(update={"status": "published"})
    expect(GradeAlreadyPublished, lambda: service.confirm_grade(
        full_command(base_revision_no=1), actor_id=TEACHER, item_max_scores=MAX_SCORES))


def test_only_current_revision_decisions_count() -> None:
    service, repo = build()
    service.confirm_grade(full_command(), actor_id=TEACHER, item_max_scores=MAX_SCORES)
    service.confirm_grade(
        full_command(base_revision_no=1,
                     decisions=(decision(ITEM_A, "4.00"), decision(ITEM_B, "8.00"),
                                decision(ITEM_A, "4.00", sequence=2))),
        actor_id=TEACHER, item_max_scores=MAX_SCORES)
    current = decisions_of_current_revision(repo, submission_id=SUBMISSION)
    assert len(current) == 3, "当前版本的决定包含追加的那一条，不含第 1 版"
    manifest = grade_manifest_hash(repo.grade_revisions[-1], current)
    assert len(manifest) == 64


# ---------------------------------------------------------------------------
# AI 只能建议
# ---------------------------------------------------------------------------

class StubAdvisor:
    def suggest(self, *, submission_id, items):
        return (GroupSuggestion(id=UUID(int=7), submission_id=submission_id,
                                item_ids=tuple(items),
                                proposed_score=Decimal("9.00"),
                                rationale="这两项解法一致，建议同分"),)


def test_ai_suggestions_never_write_grades_or_decisions() -> None:
    service, repo = build(advisor=StubAdvisor())
    suggestions = service.suggest_groups(submission_id=SUBMISSION,
                                         item_ids=[ITEM_A, ITEM_B])
    assert suggestions[0].status == "proposed"
    assert repo.grade_revisions == [], "AI 建议不能产生成绩版本"
    assert repo.decisions == [], "AI 建议不能产生逐项决定"


def test_accepting_a_suggestion_still_requires_teacher_confirmation() -> None:
    service, repo = build(advisor=StubAdvisor())
    suggestion = service.suggest_groups(submission_id=SUBMISSION,
                                        item_ids=[ITEM_A])[0]
    accepted = service.accept_suggestion(suggestion, actor_id=TEACHER,
                                         score=Decimal("5.00"))
    assert accepted.status == "accepted"
    assert repo.grade_revisions == [], (
        "采纳建议只是「教师改主意」，分数仍要经过 confirm_grade")


def test_accepting_twice_is_refused() -> None:
    service, _ = build(advisor=StubAdvisor())
    suggestion = service.suggest_groups(submission_id=SUBMISSION,
                                        item_ids=[ITEM_A])[0]
    service.accept_suggestion(suggestion, actor_id=TEACHER, score=Decimal("5.00"))
    accepted = GroupSuggestion(id=suggestion.id, submission_id=SUBMISSION,
                               item_ids=suggestion.item_ids,
                               proposed_score=Decimal("5.00"),
                               rationale=None, status="accepted")
    expect(GradingError, lambda: service.accept_suggestion(
        accepted, actor_id=TEACHER, score=Decimal("5.00")))


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
    print(f"✓ 教师确认评分测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
