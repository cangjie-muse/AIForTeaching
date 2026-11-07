"""阶段 D 任务 3：提示分级、解析解锁条件、旧链接重检与曝光冲突。

要钉住的四条：

  1. 越级提示直接拒绝（不是"给到上限就算了"）；
  2. 解析要同时满足"策略开放"与"回答可解析"；
  3. 旧链接每次打开都重新决策——策略已收窄就不能再下载；
  4. 保密正式卷冲突时**拿不到 URL**，且被拒路径**不写**"已释放"曝光事件。

运行：`python server/tests/test_feedback_policy.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.practice.feedback import (  # noqa: E402
    CatalogEntry,
    CheckFeedbackRequest,
    ExposureConflict,
    ExposureRecord,
    ExposureService,
    FeedbackArtifact,
    FeedbackCatalog,
    FeedbackService,
    PolicyDenied,
    PracticePolicyState,
    RevisionState,
)

REVISION = UUID("cc000000-0000-4000-8000-0000000000a1")
EXERCISE = UUID("ee000000-0000-4000-8000-0000000000c1")
HINT_1 = UUID("ab000000-0000-4000-8000-000000000001")
HINT_2 = UUID("ab000000-0000-4000-8000-000000000002")
HINT_3 = UUID("ab000000-0000-4000-8000-000000000003")
SOLUTION = UUID("ab000000-0000-4000-8000-000000000004")


def catalog(*, include_unapproved: bool = False) -> FeedbackCatalog:
    entries = [
        CatalogEntry(level=1, kind="hint", artifact_version_id=HINT_1,
                     download_url="/assets/hint-1"),
        CatalogEntry(level=2, kind="hint", artifact_version_id=HINT_2,
                     download_url="/assets/hint-2"),
        CatalogEntry(level=3, kind="hint", artifact_version_id=HINT_3,
                     download_url="/assets/hint-3"),
        CatalogEntry(level=0, kind="solution", artifact_version_id=SOLUTION,
                     download_url="/assets/solution"),
    ]
    if include_unapproved:
        entries.append(CatalogEntry(level=1, kind="hint", artifact_version_id=UUID(int=9),
                                    approved=False, download_url="/assets/draft-hint"))
    return FeedbackCatalog(entries)


def revision(status: str = "parsed", number: int = 2) -> RevisionState:
    return RevisionState(revision_id=REVISION, parse_status=status, revision_no=number)


def policy(**changes) -> PracticePolicyState:
    values = dict(max_hint_level=2, solution_enabled=True,
                  solution_requires_valid_response=True)
    values.update(changes)
    return PracticePolicyState(**values)


def service(*, exposure: ExposureService | None = None,
            include_unapproved: bool = False) -> FeedbackService:
    return FeedbackService(catalog=catalog(include_unapproved=include_unapproved),
                           exposure=exposure)


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 提示分级
# ---------------------------------------------------------------------------

def test_hint_levels_are_returned_in_order_up_to_the_request() -> None:
    selection = service().for_check(
        CheckFeedbackRequest(revision=revision(), requested_hint_level=2), policy())
    assert selection.levels() == [1, 2]


def test_unapproved_artifact_is_never_selected() -> None:
    selection = service(include_unapproved=True).for_check(
        CheckFeedbackRequest(revision=revision(), requested_hint_level=1), policy())
    assert [item.artifact_version_id for item in selection.hints] == [HINT_1], (
        "未审核的草稿提示绝不能被选中")


def test_over_level_request_is_denied_not_clamped() -> None:
    error = expect(PolicyDenied, lambda: service().for_check(
        CheckFeedbackRequest(revision=revision(), requested_hint_level=3), policy()))
    assert error.deny_code == "hint_level_exceeded"
    assert "上限 2" in str(error), "拒绝时要说清楚上限，而不是悄悄截断"


def test_zero_hint_level_returns_no_hints() -> None:
    selection = service().for_check(
        CheckFeedbackRequest(revision=revision(), requested_hint_level=0), policy())
    assert selection.hints == ()


# ---------------------------------------------------------------------------
# 解析解锁
# ---------------------------------------------------------------------------

def test_solution_requires_policy_and_valid_response() -> None:
    selection = service().for_check(
        CheckFeedbackRequest(revision=revision(), requested_hint_level=1,
                             reveal_solution=True), policy())
    assert selection.solution is not None
    assert selection.solution.contains_solution is True


def test_solution_denied_when_policy_disabled() -> None:
    error = expect(PolicyDenied, lambda: service().for_check(
        CheckFeedbackRequest(revision=revision(), reveal_solution=True),
        policy(solution_enabled=False)))
    assert error.deny_code == "solution_disabled"


def test_invalid_response_cannot_unlock_solution() -> None:
    error = expect(PolicyDenied, lambda: service().for_check(
        CheckFeedbackRequest(revision=revision("invalid"), reveal_solution=True), policy()))
    assert error.deny_code == "solution_requires_valid_response"


def test_empty_response_cannot_unlock_solution() -> None:
    expect(PolicyDenied, lambda: service().for_check(
        CheckFeedbackRequest(revision=revision("empty"), reveal_solution=True), policy()))


def test_solution_can_be_allowed_without_valid_response_when_policy_says_so() -> None:
    selection = service().for_check(
        CheckFeedbackRequest(revision=revision("invalid"), reveal_solution=True),
        policy(solution_requires_valid_response=False))
    assert selection.solution is not None


# ---------------------------------------------------------------------------
# 旧链接重检
# ---------------------------------------------------------------------------

def test_old_link_is_rechecked_against_current_policy() -> None:
    """策略发布后被收窄：旧链接不能再下载（签名有效不等于现在允许）。"""
    error = expect(PolicyDenied, lambda: service().open_artifact(
        artifact_version_id=SOLUTION, allowed_versions=[SOLUTION],
        policy=policy(solution_enabled=False), revision=revision(),
        latest_revision_no=2, contains_solution=True,
        artifact=FeedbackArtifact(artifact_version_id=SOLUTION, level=0, kind="solution",
                                  download_url="/assets/solution",
                                  contains_solution=True)))
    assert error.deny_code == "solution_disabled"


def test_open_reports_that_the_revision_is_superseded() -> None:
    result = service().open_artifact(
        artifact_version_id=HINT_1, allowed_versions=[HINT_1], policy=policy(),
        revision=revision("parsed", 2), latest_revision_no=4, contains_solution=False,
        artifact=FeedbackArtifact(artifact_version_id=HINT_1, level=1, kind="hint",
                                  download_url="/assets/hint-1"))
    assert result.superseded is True, "学生改过答案后，旧提示要标成「基于旧版」"
    assert result.revision_no == 2 and result.latest_revision_no == 4


def test_artifact_from_another_exercise_is_denied() -> None:
    error = expect(PolicyDenied, lambda: service().open_artifact(
        artifact_version_id=UUID(int=42), allowed_versions=[HINT_1], policy=policy(),
        revision=revision(), latest_revision_no=2, contains_solution=False,
        artifact=FeedbackArtifact(artifact_version_id=UUID(int=42), level=1,
                                  download_url="/assets/other")))
    assert error.deny_code == "artifact_mismatch"


# ---------------------------------------------------------------------------
# 曝光冲突
# ---------------------------------------------------------------------------

def test_exposure_conflict_blocks_solution_url() -> None:
    exposure = ExposureService([ExposureRecord(exercise_version_id=SOLUTION,
                                              secure_formal_pending=True)])
    error = expect(ExposureConflict, lambda: service(exposure=exposure).open_artifact(
        artifact_version_id=SOLUTION, allowed_versions=[SOLUTION], policy=policy(),
        revision=revision(), latest_revision_no=2, contains_solution=True,
        artifact=FeedbackArtifact(artifact_version_id=SOLUTION, level=0,
                                  kind="solution", download_url="/assets/solution",
                                  contains_solution=True)))
    assert error.code == "exposure_conflict"


def test_denied_path_does_not_record_release() -> None:
    exposure = ExposureService([ExposureRecord(exercise_version_id=SOLUTION,
                                              secure_formal_pending=True)])
    expect(ExposureConflict, lambda: service(exposure=exposure).open_artifact(
        artifact_version_id=SOLUTION, allowed_versions=[SOLUTION], policy=policy(),
        revision=revision(), latest_revision_no=2, contains_solution=True,
        artifact=FeedbackArtifact(artifact_version_id=SOLUTION, level=0,
                                  kind="solution", download_url="/assets/solution",
                                  contains_solution=True)))
    assert all(not record.released_practice for record in exposure.records), (
        "被拒绝的路径不能写「已释放」事件，否则下次就会误以为已经公开")


def test_approved_path_records_release() -> None:
    exposure = ExposureService()
    service(exposure=exposure).open_artifact(
        artifact_version_id=SOLUTION, allowed_versions=[SOLUTION], policy=policy(),
        revision=revision(), latest_revision_no=2, contains_solution=True,
        artifact=FeedbackArtifact(artifact_version_id=SOLUTION, level=0,
                                  kind="solution", download_url="/assets/solution",
                                  contains_solution=True))
    assert any(record.released_practice for record in exposure.records)


def test_hint_open_does_not_mark_solution_released() -> None:
    exposure = ExposureService()
    service(exposure=exposure).open_artifact(
        artifact_version_id=HINT_1, allowed_versions=[HINT_1], policy=policy(),
        revision=revision(), latest_revision_no=2, contains_solution=False,
        artifact=FeedbackArtifact(artifact_version_id=HINT_1, level=1, kind="hint",
                                  download_url="/assets/hint-1"))
    assert exposure.records == (), "提示不是解析，不能记成「解析已释放」"


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
    print(f"✓ 反馈策略与曝光测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
