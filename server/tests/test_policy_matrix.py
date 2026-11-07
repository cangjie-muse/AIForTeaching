"""阶段 D 任务 11：策略矩阵与端到端闭环（组合验收）。

单测各自验证一条规则；这里验证它们**组合在一起**仍然成立：

    练习 → 检查（可见对错） → 学习投影（无成绩）
    正式 → 预评（只建议）   → 教师确认 → 发布 → 学生可见
                                   ↓ 申诉受理 → 重评（草稿） → 再发布

矩阵要钉的六条：

  * 只有 practice 能立即看对错/分数；coursework 与 formal 一律不能；
  * `fail` 在练习与正式路径上都**永远不是 0**；
  * AI 建议在任何路径上都**不写成绩**；
  * 未确认就发布 → 整批拒绝；
  * 撤回后学生看不到"当前成绩"，但历史版本仍可读；
  * 重评草稿在学生端不可见，直到再次发布。

运行：`python server/tests/test_policy_matrix.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.grading.contracts import GradeError  # noqa: E402
from app.grading.publish import (  # noqa: E402
    InMemoryPublishRepository,
    PublishIncomplete,
    PublishService,
)
from app.grading.revisions import (  # noqa: E402
    ConfirmCommand,
    GradingService,
    GroupSuggestion,
    ItemDecisionInput,
)
from app.grading.service import (  # noqa: E402
    GradingService as EvaluationService,
    InMemoryEvaluationRepository,
    SubmissionItemContext,
)
from app.practice.contracts import PracticePolicy, build_feedback, make_result  # noqa: E402
from app.practice.feedback import (  # noqa: E402
    CatalogEntry,
    CheckFeedbackRequest,
    FeedbackCatalog,
    FeedbackService,
    PolicyDenied,
    PracticePolicyState,
)

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
ITEM_A = UUID("dd000000-0000-4000-8000-0000000000a1")
ITEM_B = UUID("dd000000-0000-4000-8000-0000000000b1")
REVISION = UUID("cc000000-0000-4000-8000-0000000000a1")
EXERCISE = UUID("ee000000-0000-4000-8000-0000000000c1")
TEACHER = UUID("5e5e0000-0000-4000-8000-0000000000c1")
STUDENT = UUID("5e5e0000-0000-4000-8000-0000000000d1")
MAX = {ITEM_A: Decimal("5.00"), ITEM_B: Decimal("10.00")}


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 一、策略矩阵：谁能立即看对错
# ---------------------------------------------------------------------------

def test_only_practice_shows_immediate_correctness() -> None:
    from app.practice.contracts import PracticePolicy as Policy

    seen = {}
    for purpose in ("practice", "coursework", "formal"):
        policy = Policy(purpose=purpose, correctness_release="immediate")
        from app.practice.contracts import decide_release
        seen[purpose] = decide_release(policy, attempt_index=0)["correctness"]
    assert seen == {"practice": True, "coursework": False, "formal": False}, seen


def test_only_practice_shows_immediate_score() -> None:
    from app.practice.contracts import decide_release

    scores = {purpose: decide_release(PracticePolicy(purpose=purpose,
                                                    score_release="immediate"),
                                      attempt_index=0)["score"]
              for purpose in ("practice", "coursework", "formal")}
    assert scores == {"practice": True, "coursework": False, "formal": False}


def test_fail_never_becomes_zero_on_any_path() -> None:
    # 练习路径
    practice_result = make_result(revision_id=REVISION, exercise_version_id=EXERCISE,
                                  status="fail", reason="timeout")
    assert practice_result.score is None
    assert practice_result.correct is None
    # 正式预评路径
    evaluation = EvaluationService(repository=InMemoryEvaluationRepository())
    summary = evaluation.evaluate_submission(submission_id=SUBMISSION, items=[
        SubmissionItemContext(submission_item_id=ITEM_A, response_revision_id=REVISION,
                              response_text="答案", parse_status="parsed",
                              response_kind="text", max_score=Decimal("5.00"),
                              answer_test={"kind": "sympy"})])
    assert summary.items[0].suggested_score is None
    assert summary.items[0].requires_human is True


def test_formal_never_releases_solution_to_student() -> None:
    catalog = FeedbackCatalog([CatalogEntry(level=0, kind="solution",
                                            artifact_version_id=UUID(int=3),
                                            download_url="/assets/sol")])
    service = FeedbackService(catalog=catalog)
    # 策略写着开放，但"学生能否看到"仍由 PracticePolicyState 控制；formal 下不提供
    try:
        service.for_check(CheckFeedbackRequest(
            revision=type("R", (), {"revision_id": REVISION, "parse_status": "parsed",
                                    "revision_no": 1})(),
            reveal_solution=True),
            PracticePolicyState(max_hint_level=0, solution_enabled=False))
    except PolicyDenied:
        pass
    else:
        raise AssertionError("formal 路径不应解锁解析")


# ---------------------------------------------------------------------------
# 二、端到端：确认 → 发布 → 学生可见 → 撤回
# ---------------------------------------------------------------------------

def build_chain():
    repository = InMemoryPublishRepository()
    grading = GradingService(repository=repository.grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=800))
    publish = PublishService(repository=repository, grading=grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=700))
    return repository, grading, publish


def confirm(grading, *, item_b: str = "9.00", base: int = 0) -> None:
    grading.confirm_grade(
        ConfirmCommand(submission_id=SUBMISSION, base_revision_no=base,
                       decisions=(ItemDecisionInput(submission_item_id=ITEM_A,
                                                    score=Decimal("5.00")),
                                  ItemDecisionInput(submission_item_id=ITEM_B,
                                                    score=Decimal(item_b)))),
        actor_id=TEACHER, item_max_scores=MAX)


def student_grades(repository) -> list:
    current = repository.current_published.get(SUBMISSION)
    return [item for item in repository.revisions_for(SUBMISSION)
            if item.id == current and item.status == "published"]


def test_full_chain_publish_then_visible_then_withdrawn() -> None:
    repository, grading, publish = build_chain()
    confirm(grading)
    result = publish.publish(submission_id=SUBMISSION, actor_id=TEACHER,
                             idempotency_key="pub-1", expected_items=[ITEM_A, ITEM_B])
    assert result.revision.status == "published"
    assert student_grades(repository)[0].total_score == Decimal("14.00")

    publish.withdraw(submission_id=SUBMISSION, actor_id=TEACHER,
                     reason="评分标准有误", idempotency_key="wd-1")
    assert student_grades(repository) == [], "撤回后学生不再看到当前成绩"
    # 历史版本仍在（可审计，不可展示）
    assert len(repository.revisions_for(SUBMISSION)) >= 2


def test_publish_before_confirmation_is_refused() -> None:
    repository, grading, publish = build_chain()
    expect(GradeError, lambda: publish.publish(
        submission_id=SUBMISSION, actor_id=TEACHER,
        idempotency_key="pub-x", expected_items=[ITEM_A]))


def test_ai_suggestion_cannot_shortcut_the_chain() -> None:
    repository, grading, publish = build_chain()

    class Advisor:
        def suggest(self, *, submission_id, items):
            return (GroupSuggestion(id=UUID(int=6), submission_id=submission_id,
                                    item_ids=tuple(items),
                                    proposed_score=Decimal("9.00"), rationale="同分"),)

    from app.grading.revisions import GradingService as TeacherService

    teacher = TeacherService(repository=repository.grading, advisor=Advisor(),
                             clock=lambda: NOW, id_factory=lambda: UUID(int=900))
    teacher.suggest_groups(submission_id=SUBMISSION, item_ids=[ITEM_A, ITEM_B])
    assert repository.grading.grade_revisions == [], "AI 建议不能产生成绩版本"
    expect(GradeError, lambda: publish.publish(
        submission_id=SUBMISSION, actor_id=TEACHER,
        idempotency_key="pub-ai", expected_items=[ITEM_A, ITEM_B]))


def test_appeal_regrade_requires_confirmation_and_publication() -> None:
    repository, grading, publish = build_chain()
    confirm(grading)
    published = publish.publish(submission_id=SUBMISSION, actor_id=TEACHER,
                                idempotency_key="pub-1",
                                expected_items=[ITEM_A, ITEM_B]).revision
    appeal = publish.file_appeal(submission_id=SUBMISSION, student_id=STUDENT,
                                 grade_revision_id=published.id, reason="第 2 题少给")
    publish.accept_appeal(appeal_id=appeal.appeal.id, actor_id=TEACHER)
    draft = publish.regrade(appeal_id=appeal.appeal.id, actor_id=TEACHER,
                            new_total=Decimal("15.00"), change_reason="补 1 分")
    assert draft.status == "draft"
    assert student_grades(repository)[0].total_score == Decimal("14.00"), (
        "重评草稿不能提前学生可见")
    expect(PublishIncomplete, lambda: publish.publish(
        submission_id=SUBMISSION, actor_id=TEACHER,
        idempotency_key="pub-2", expected_items=[ITEM_A, ITEM_B])), (
        "重评版没有教师决定，发布必须整批拒绝")
    confirm(grading, item_b="10.00", base=draft.revision_no)
    republished = publish.publish(submission_id=SUBMISSION, actor_id=TEACHER,
                                 idempotency_key="pub-3",
                                 expected_items=[ITEM_A, ITEM_B])
    assert republished.revision.status == "published"
    assert student_grades(repository)[0].total_score == Decimal("15.00")


def test_practice_path_never_touches_grades() -> None:
    """练习检查只写学习投影：任何练习都不会产生 grade revision。"""
    from app.practice.service import (
        AttemptView, InMemoryPracticeRepository, PracticeService, UnavailableChecker,
    )

    repo = InMemoryPracticeRepository()
    repo.add_attempt(AttemptView(
        attempt_id=UUID("aa000000-0000-4000-8000-000000000001"), student_id=STUDENT,
        revision_id=REVISION, slot_id=UUID("cc000000-0000-4000-8000-0000000000b1"),
        exercise_version_id=EXERCISE, response_text="x", attempt_status="in_progress",
        has_submission=False))
    practice = PracticeService(repository=repo, checker=UnavailableChecker())
    result, feedback = practice.check(attempt_id=UUID("aa000000-0000-4000-8000-000000000001"),
                                      student_id=STUDENT, policy=PracticePolicy(purpose="practice"))
    assert result.status == "fail"
    assert feedback.message.startswith("系统暂时无法判定")
    assert len(repo.projections) == 1
    assert repo.answer_runs, "检查运行要留证"


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
    print(f"✓ 策略矩阵与端到端闭环测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
