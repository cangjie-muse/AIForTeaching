"""阶段 A 任务 8：打印作业的固定投影、发布终检与严重撤回（服务层）。

三个必须钉死的事实，别的地方都测不到：

1. **固定投影**：发布后把卷面换掉（教师克隆新版本、改题），已发布作业的每一题
   仍然指向发布那一刻的 release / version / family / 分值。学生手上那张纸不会
   因为教师改天改卷而"追不到是哪一版"。
2. **发布终检是硬门槛**：卷子不是 approved、审核证据与卷面 manifest 对不上、
   时间倒挂、槽位缺来源、曝光冲突——任何一条都拒绝发布，而不是"先发出去再说"。
3. **严重撤回会烧掉题族**：撤回写 `withdrawn` 曝光事件，曝光规则里 withdrawn 是
   无条件拦截项。撤回意味着"这题不能再进任何卷"，不是"换个用途还能用"。

本文件用内存仓储跑规则；SQL、触发器、advisory lock 由
`scripts/verify-migrations.cjs` 与 test_assignments 的静态断言覆盖（本仓库 Python
测试不连库）。运行：`python server/tests/test_assignments.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assignments import (  # noqa: E402
    AssignmentExposureConflictError,
    AssignmentIdempotencyConflictError,
    AssignmentInputError,
    AssignmentNotApprovedError,
    AssignmentNotFoundError,
    AssignmentService,
    AssignmentSourceIncompleteError,
    AssignmentStateError,
    AssignmentTimeOrderError,
    InMemoryAssignmentRepository,
)
from app.blueprints import ref_uuid  # noqa: E402
from app.contracts import (  # noqa: E402
    ExposureEvent,
    PaperSectionView,
    PaperSlotView,
    canonical_hash,
)
from app.papers import PAPER_READ_ACTION, PAPER_WRITE_ACTION, PaperVersionRow  # noqa: E402

COURSE_REF = "analysis"
OFFERING_REF = "class-2026-01"
# 课程/班级 uuid 必须走同一套 uuid5 映射：与生产里 CourseScopeAuthorizer / 蓝图 /
# Release 用的是同一个命名空间，自己在测试里拍一个 uuid 只会测到"另一个班"。
COURSE_ID = ref_uuid("course", COURSE_REF)
OFFERING_ID = ref_uuid("offering", OFFERING_REF)
RELEASE_ID = UUID("33333333-3333-5333-8333-333333333333")
SOURCE_RELEASE = UUID("44444444-4444-5444-8444-444444444444")
PAPER_ID = UUID("55555555-5555-5555-8555-555555555555")
PAPER_VERSION_ID = UUID("66666666-6666-5666-8666-666666666666")
SNAPSHOT_ID = UUID("77777777-7777-5777-8777-777777777777")
ACTOR = UUID("88888888-8888-5888-8888-888888888888")
MANIFEST = canonical_hash({"paper": "assignment-fixture"})
REVIEW_MANIFEST = canonical_hash({"review": "two-approvals"})

VISIBLE = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc)


class StubAuthorizer:
    def __init__(self) -> None:
        self.denied = False
        self.calls: list[tuple[str, dict]] = []

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        self.calls.append((action, dict(scope)))
        if self.denied:
            raise PermissionError("course scope denied")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return True


def make_slot(no: int, score: str, *, code: str | None = None,
              family: UUID | None = None, **overrides: object) -> PaperSlotView:
    family = family or UUID(int=900 + no)
    values = dict(
        id=uuid4(),
        section_code="A",
        code=code or f"A{no}",
        slot_no=no,
        exercise_id=UUID(int=700 + no),
        exercise_version_id=UUID(int=800 + no),
        exercise_instance_id=None,
        exercise_family_id=family,
        source_release_id=SOURCE_RELEASE,
        release_context_id=RELEASE_ID,
        score=Decimal(score),
        estimated_seconds=600,
        display_config={},
        dependency_manifest_hash=canonical_hash({"dep": no}),
        content_hash=canonical_hash({"slot": no}),
    )
    values.update(overrides)
    return PaperSlotView(**values)


def make_paper(*, status: str = "approved", paper_kind: str = "formal",
               submitted: str | None = MANIFEST,
               review_manifest: str | None = REVIEW_MANIFEST,
               slots: tuple[PaperSlotView, ...] | None = None) -> PaperVersionRow:
    slots = slots if slots is not None else (
        make_slot(1, "40.00"),
        make_slot(2, "60.00"),
    )
    section = PaperSectionView(
        code="A", title="一、计算题", instruction="写出步骤",
        sort_order=1, section_score=Decimal("100.00"), slots=slots,
        content_hash=canonical_hash({"section": "A"}),
    )
    return PaperVersionRow(
        id=PAPER_VERSION_ID,
        paper_id=PAPER_ID,
        version_no=1,
        candidate_snapshot_id=SNAPSHOT_ID,
        blueprint_version_id=UUID(int=555),
        release_context_id=RELEASE_ID,
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        purpose=paper_kind,
        paper_kind=paper_kind,
        status=status,
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        edit_seq=3,
        manifest_hash=MANIFEST,
        submitted_manifest_hash=submitted,
        review_manifest_hash=review_manifest,
        sections=(section,),
        created_by=ACTOR,
        created_at=VISIBLE,
    )


def build(*, paper: PaperVersionRow | None = None,
          events: tuple[ExposureEvent, ...] = (),
          authorizer: StubAuthorizer | None = None) -> tuple[
              AssignmentService, InMemoryAssignmentRepository, StubAuthorizer]:
    paper = paper or make_paper()
    repo = InMemoryAssignmentRepository(
        papers=[paper], courses=[COURSE_REF], events=events)
    # 课程文本 id 的反向映射：服务靠 id_ref 把 uuid 还原成 course_ref
    repo.refs[("course", COURSE_ID)] = COURSE_REF
    repo.refs[("offering", OFFERING_ID)] = OFFERING_REF
    authorizer = authorizer or StubAuthorizer()
    # 固定时钟：曝光判定按"发布那一刻"看 visible_from，用真实时钟会让
    # 2026-11-10 开考的卷子在今天（写测试时）永远看不到冲突事件——测试要通过
    # 伪造时间才能测到规则，而不是靠"跑测试的日期刚好合适"。
    service = AssignmentService(
        repository=repo, authorizer=authorizer, clock=lambda: VISIBLE)
    return service, repo, authorizer


def draft(service: AssignmentService, *, key: str = "create-1") -> UUID:
    view = service.create_draft(
        actor_id=ACTOR,
        paper_version_id=PAPER_VERSION_ID,
        offering_ref=OFFERING_REF,
        visible_at=VISIBLE,
        due_at=DUE,
        hard_close_at=CLOSE,
        idempotency_key=key,
    )
    return view.assignment.id


# ---------------------------------------------------------------------------
# 一、建草稿
# ---------------------------------------------------------------------------

def test_draft_requires_an_approved_paper() -> None:
    service, _, _ = build(paper=make_paper(status="in_review"))
    try:
        draft(service)
    except AssignmentNotApprovedError as error:
        assert "in_review" in str(error)
        return
    raise AssertionError("未审核通过的卷子被发布成了作业")


def test_draft_requires_review_evidence() -> None:
    service, _, _ = build(paper=make_paper(review_manifest=None))
    try:
        draft(service)
    except AssignmentNotApprovedError as error:
        assert "review_manifest_hash" in str(error)
        return
    raise AssertionError("没有双审证据的卷子被接受")


def test_draft_rejects_time_order_violation() -> None:
    service, _, _ = build()
    try:
        service.create_draft(
            actor_id=ACTOR, paper_version_id=PAPER_VERSION_ID,
            offering_ref=OFFERING_REF,
            visible_at=DUE, due_at=VISIBLE, hard_close_at=CLOSE,
            idempotency_key="bad-times",
        )
    except AssignmentTimeOrderError:
        return
    raise AssertionError("due_at 早于 visible_at 仍被接受")


def test_draft_requires_idempotency_key() -> None:
    service, _, _ = build()
    try:
        service.create_draft(
            actor_id=ACTOR, paper_version_id=PAPER_VERSION_ID,
            offering_ref=OFFERING_REF, visible_at=VISIBLE, due_at=DUE,
            hard_close_at=CLOSE, idempotency_key="   ",
        )
    except AssignmentInputError:
        return
    raise AssertionError("空 Idempotency-Key 被接受")


def test_draft_replays_on_same_key_and_conflicts_on_different_payload() -> None:
    service, repo, _ = build()
    first = service.create_draft(
        actor_id=ACTOR, paper_version_id=PAPER_VERSION_ID,
        offering_ref=OFFERING_REF, visible_at=VISIBLE, due_at=DUE,
        hard_close_at=CLOSE, idempotency_key="same-key",
    )
    again = service.create_draft(
        actor_id=ACTOR, paper_version_id=PAPER_VERSION_ID,
        offering_ref=OFFERING_REF, visible_at=VISIBLE, due_at=DUE,
        hard_close_at=CLOSE, idempotency_key="same-key",
    )
    assert again.assignment.id == first.assignment.id
    assert again.replayed is True and again.created is False
    assert len(repo.rows) == 1

    try:
        service.create_draft(
            actor_id=ACTOR, paper_version_id=PAPER_VERSION_ID,
            offering_ref=OFFERING_REF, visible_at=VISIBLE,
            due_at=DUE + timedelta(hours=1),
            hard_close_at=CLOSE + timedelta(hours=3),
            idempotency_key="same-key",
        )
    except AssignmentIdempotencyConflictError:
        return
    raise AssertionError("同键不同载荷被当成重放")


def test_draft_is_denied_without_course_scope() -> None:
    authorizer = StubAuthorizer()
    authorizer.denied = True
    service, _, _ = build(authorizer=authorizer)
    try:
        draft(service)
    except PermissionError:
        return
    raise AssertionError("无课程范围的账号建出了作业")


# ---------------------------------------------------------------------------
# 二、发布终检
# ---------------------------------------------------------------------------

def test_publish_pins_full_projection_and_scores() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    view = service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                           idempotency_key="publish-1")

    assert view.assignment.status == "published"
    assert view.assignment.published_at is not None
    assert view.assignment.publish_manifest_hash == MANIFEST
    assert view.assignment.review_manifest_hash == REVIEW_MANIFEST

    items = repo.items[assignment_id]
    assert len(items) == 2
    assert [item.ord for item in items] == [1, 2]
    assert sum(item.score for item in items) == Decimal("100.00")
    for item in items:
        assert item.release_context_id == RELEASE_ID
        assert item.source_release_id == SOURCE_RELEASE
        assert item.exercise_version_id is not None
        assert item.exercise_family_id is not None
        assert len(item.content_hash) == 64


def test_published_items_do_not_follow_later_paper_edits() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    published = service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                                idempotency_key="publish-1")
    before = {(item.paper_slot_id, item.exercise_version_id)
              for item in published.items}

    # 教师随后改了卷：换题、换版本、改分。
    edited_slots = (make_slot(1, "50.00", code="A1"), make_slot(2, "50.00", code="A2"))
    repo.papers[PAPER_VERSION_ID] = make_paper(slots=edited_slots)

    stored = service.get(actor_id=ACTOR, assignment_id=assignment_id)
    after = {(item.paper_slot_id, item.exercise_version_id) for item in stored.items}
    assert after == before, "已发布作业跟着后来的改卷变了"
    assert sum(item.score for item in stored.items) == Decimal("100.00")


def test_publish_rejects_stale_review_evidence() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    other = canonical_hash({"review": "someone-else-approved"})
    repo.papers[PAPER_VERSION_ID] = make_paper(review_manifest=other)
    try:
        service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                        idempotency_key="publish-stale")
    except AssignmentNotApprovedError as error:
        assert "审核证据已失效" in str(error)
        return
    raise AssertionError("审核证据换过之后仍然发布成功")


def test_publish_rejects_submitted_manifest_drift() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    repo.papers[PAPER_VERSION_ID] = make_paper(
        submitted=canonical_hash({"paper": "edited-after-review"}))
    try:
        service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                        idempotency_key="publish-drift")
    except AssignmentNotApprovedError:
        return
    raise AssertionError("卷面 manifest 与提交时不一致仍然发布成功")


def test_publish_rejects_slot_without_full_sources() -> None:
    broken = make_slot(2, "60.00", release_context_id=None)
    service, _, _ = build(paper=make_paper(slots=(make_slot(1, "40.00"), broken)))
    assignment_id = draft(service)
    try:
        service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                        idempotency_key="publish-broken")
    except AssignmentSourceIncompleteError as error:
        assert "release_context_id" in str(error)
        return
    raise AssertionError("缺来源的槽位被发布成了作业项")


def test_publish_blocks_on_exposure_conflict() -> None:
    conflict = ExposureEvent(
        exercise_version_id=UUID(int=801),
        exercise_family_id=UUID(int=901),
        scope_offering_id=None,
        event_kind="practice_answer_released",
        visible_from=VISIBLE - timedelta(days=1),
        occurred_at=VISIBLE - timedelta(days=1),
    )
    service, _, _ = build(events=(conflict,))
    assignment_id = draft(service)
    try:
        service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                        idempotency_key="publish-conflict")
    except AssignmentExposureConflictError as error:
        assert error.code == "practice_answer_released"
        return
    raise AssertionError("练习卷已公开答案的题被放进了正式保密卷")


def test_publish_writes_secure_assigned_events_for_formal_papers() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                    idempotency_key="publish-events")
    events = [event for event in repo.events if event.event_kind == "secure_assigned"]
    assert len(events) == 2
    for event in events:
        assert event.scope_offering_id == OFFERING_ID
        assert event.visible_from == VISIBLE, "曝光窗口应取 visible_at，不是 now"


def test_publish_is_replay_safe_and_refuses_second_publish() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                    idempotency_key="publish-once")
    events_after_first = len(repo.events)

    replay = service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                             idempotency_key="publish-once")
    assert replay.replayed is True
    assert len(repo.events) == events_after_first, "重放又写了一遍曝光事件"
    assert len(repo.items[assignment_id]) == 2

    try:
        service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                        idempotency_key="publish-twice")
    except AssignmentStateError as error:
        assert "published" in str(error)
        return
    raise AssertionError("已发布的作业被第二次发布")


def test_publish_rejects_unknown_assignment() -> None:
    service, _, _ = build()
    try:
        service.publish(actor_id=ACTOR, assignment_id=uuid4(),
                        idempotency_key="publish-missing")
    except AssignmentNotFoundError:
        return
    raise AssertionError("不存在的作业被发布")


# ---------------------------------------------------------------------------
# 三、严重撤回
# ---------------------------------------------------------------------------

def test_withdraw_marks_status_and_burns_the_family() -> None:
    service, repo, _ = build()
    assignment_id = draft(service)
    service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                    idempotency_key="publish-1")
    view = service.withdraw(actor_id=ACTOR, assignment_id=assignment_id,
                            reason="第 2 题泄题，学生已在群里看到答案",
                            idempotency_key="withdraw-1")

    assert view.assignment.status == "withdrawn"
    assert view.assignment.withdrawn_at is not None
    assert "泄题" in (view.assignment.withdrawal_reason or "")
    withdrawn = [event for event in repo.events if event.event_kind == "withdrawn"]
    assert len(withdrawn) == 2
    assert {event.exercise_family_id for event in withdrawn} == {
        UUID(int=901), UUID(int=902)}
    # 撤回之后，这些族再也不能进新卷——曝光规则里 withdrawn 无条件拦截。
    from app.exposure import ExposureConflict, ExposureService

    class Target:
        exercise_version_id = UUID(int=801)
        exercise_family_id = UUID(int=901)

    try:
        ExposureService.assert_no_conflict(
            "formal", OFFERING_ID, [Target()], repo.events, CLOSE)
    except ExposureConflict as error:
        assert error.code == "withdrawn"
        return
    raise AssertionError("撤回过的题族还能再进卷")


def test_withdraw_requires_reason_and_published_state() -> None:
    service, _, _ = build()
    assignment_id = draft(service)
    try:
        service.withdraw(actor_id=ACTOR, assignment_id=assignment_id,
                         reason="  ", idempotency_key="withdraw-empty")
    except AssignmentInputError:
        return
    finally:
        pass
    raise AssertionError("空理由的撤回被接受")


def test_withdraw_rejects_draft_and_double_withdraw() -> None:
    service, _, _ = build()
    assignment_id = draft(service)
    try:
        service.withdraw(actor_id=ACTOR, assignment_id=assignment_id,
                         reason="还没发布就想撤回", idempotency_key="w-1")
    except AssignmentStateError:
        return
    raise AssertionError("草稿状态可以撤回")


def test_withdraw_twice_is_rejected() -> None:
    service, _, _ = build()
    assignment_id = draft(service)
    service.publish(actor_id=ACTOR, assignment_id=assignment_id,
                    idempotency_key="publish-1")
    service.withdraw(actor_id=ACTOR, assignment_id=assignment_id,
                     reason="泄题", idempotency_key="w-1")
    try:
        service.withdraw(actor_id=ACTOR, assignment_id=assignment_id,
                         reason="再撤一次", idempotency_key="w-2")
    except AssignmentStateError as error:
        assert "撤回" in str(error)
        return
    raise AssertionError("同一作业被撤回两次")


# ---------------------------------------------------------------------------
# 四、读与静态断言
# ---------------------------------------------------------------------------

def test_get_requires_read_scope() -> None:
    service, _, authorizer = build()
    assignment_id = draft(service)
    service.get(actor_id=ACTOR, assignment_id=assignment_id)
    last_action = authorizer.calls[-1][0]
    assert last_action == PAPER_READ_ACTION
    assert authorizer.calls[0][0] == PAPER_WRITE_ACTION


def test_postgres_repository_uses_advisory_lock_per_course() -> None:
    source = (ROOT / "server" / "app" / "assignments.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in source
    assert "assignment-publish:" in source, "锁键必须是课程粒度，不能是作业粒度"
    assert "assessment.assignment_item" in source
    assert "assessment.item_exposure_event" in source


def test_migration_0033_is_additive_only() -> None:
    migrations = sorted((ROOT / "db" / "migrations").glob("0033_*.sql"))
    assert len(migrations) == 1, f"缺少唯一的 0033 迁移：{migrations}"
    text = migrations[0].read_text(encoding="utf-8").lower()
    for forbidden in ("drop table", "drop column", "truncate", "delete from"):
        assert forbidden not in text, f"0033 出现破坏性操作：{forbidden}"
    assert "exercise_family_id" in text
    assert "withdrawal_reason" in text


# ---------------------------------------------------------------------------
# 手动收集
# ---------------------------------------------------------------------------

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
    print(f"✓ 打印作业测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
