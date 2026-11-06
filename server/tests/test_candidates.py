"""固定 Release Context 候选资格与曝光门禁（server/app/candidates.py、exposure.py、
releases.py）的单元测试。

为什么这一层必须钉死：候选清单是"哪些题可以进卷"的唯一入口。它一旦出错，
错误会被后面每一步忠实地传下去——

  * 审核刚被撤回的题进了正式卷 → 学生拿到一道已被判错的题；
  * 练习卷已经公开过答案的题再次进正式保密卷 → 泄题，且无法通过复核发现；
  * 题目被原地编辑过，但 Release 还固定着旧内容 → 打印出来的题干和审核过的不是同一份；
  * 拒绝只给一个 `False`，教师看到"3 道题不合格"却不知道该改哪一项。

所以这里测的不是"函数返回了什么"，而是四条不变式：

  1. 不合格的候选**永远**进不了 accepted（每条拒绝都有可解释的原因码）；
  2. 原因码是有限枚举，不是布尔、不是自由文本；
  3. 内容寻址版本 id 随内容变化、随身份稳定（不需要 exercise_version 表）；
  4. 快照哈希只由内容决定：重复生成不换哈希，新冻结一个 Release 也不改旧快照。

运行：`python server/tests/test_candidates.py`（仓库惯例：不引入 pytest，顶层仍是
pytest 风格的 test_ 函数）。
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from pydantic import ValidationError  # noqa: E402

from app.blueprints import ref_uuid  # noqa: E402
from app.candidates import (  # noqa: E402
    REASON_MEANINGS,
    CandidateRecord,
    CandidateScope,
    CandidateService,
    InMemoryCandidateRepository,
    ReleaseContextNotFoundError,
    ReleaseScopeInfo,
    candidate_reasons,
)
from app.contracts import (  # noqa: E402
    CandidateEvidence,
    ExposureEvent,
)
from app.exposure import ExposureConflict, ExposureService  # noqa: E402
from app.releases import (  # noqa: E402
    DuplicateReleaseItemError,
    ExerciseSnapshot,
    InMemoryReleaseRepository,
    ReleaseScopeError,
    ReleaseService,
    UnknownExerciseError,
    content_hash,
    exercise_uuid,
    family_id_for,
    version_id_for,
)

MIGRATION_DIR = ROOT / "db" / "migrations"
COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "class-2026-fall"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=7)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def exercise_snapshot(
    ref: str = "ex-1",
    *,
    prompt: str = "证明 $\\lim_{n\\to\\infty} a_n = 0$",
    answer: str = "取 $\\varepsilon>0$，由定义……",
    review_status: str = "teacher-reviewed",
    license: str = "licensed",
    language: str = "zh",
) -> ExerciseSnapshot:
    return ExerciseSnapshot(
        course_ref=COURSE,
        exercise_ref=ref,
        prompt_md=prompt,
        answer_md=answer,
        review_status=review_status,
        license=license,
        language=language,
        difficulty=3,
        content_version=1,
        node_refs=("n-1",),
    )


def decision_for(review_status: str) -> str:
    """本仓库没有 review_decision_item：review_status 就是唯一审核真源。

    映射必须显式写出来，而不是在服务里散落 `in ("teacher-reviewed","published")`：
    - teacher-reviewed / published → approved（人工逐题确认过）
    - retired                      → revoked（已被新版取代/撤回）
    - draft / machine-checked      → pending（还没到人工终态）
    """
    if review_status in {"teacher-reviewed", "published"}:
        return "approved"
    if review_status == "retired":
        return "revoked"
    return "pending"


def make_record(
    ref: str = "ex-1",
    *,
    release_state: str = "included",
    snapshot: ExerciseSnapshot | None = None,
    **flags: object,
) -> CandidateRecord:
    """一条候选记录：证据标志默认全绿，测试只翻转关心的那一项。"""
    snap = snapshot or exercise_snapshot(ref)
    digest = content_hash(snap.prompt_md, snap.answer_md)
    decision = decision_for(snap.review_status)
    booleans: dict[str, object] = {
        "in_context": release_state == "included",
        "license_valid": bool(snap.license) and (snap.license or "").lower() not in {
            "unknown", "unlicensed", "none", "denied",
        },
        "revoked": decision == "revoked",
        "hidden_evaluation": False,
        "private_draft": False,
        "parameterized": False,
        "instance_approved": True,
        "dependencies_complete": True,
        "render_ready": bool((snap.prompt_md or "").strip()),
        "language_pair_valid": (snap.language or "").strip().lower() in {"zh", "en"},
        "purpose_allowed": True,
        "cloud_allowed": True,
    }
    booleans.update(flags)
    evidence = CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=CONTEXT,
        exercise_id=exercise_uuid(COURSE, ref),
        exercise_version_id=version_id_for(COURSE, ref, digest),
        exercise_instance_id=None,
        exercise_family_id=family_id_for(COURSE, ref),
        exercise_kind="proof",
        concept_ids=(ref_uuid("course", "concept-n1"),),
        learning_outcome_ids=(),
        difficulty_label="medium",
        estimated_seconds=600,
        decision_seqs={"exercise": snap.content_version},
        latest_decisions={"exercise": decision},
        dependency_manifest_hash="a" * 64,
        content_hash=digest,
        **booleans,  # type: ignore[arg-type]
    )
    return CandidateRecord(
        evidence=evidence,
        release_state=release_state,  # type: ignore[arg-type]
        review_status=snap.review_status,
        license=snap.license,
        answer_present=bool((snap.answer_md or "").strip()),
    )


def scope(
    *,
    purpose: str = "formal",
    course_ref: str = COURSE,
    offering_ref: str | None = OFFERING,
    release_context_id: UUID = CONTEXT,
) -> CandidateScope:
    return CandidateScope(
        blueprint_version_id=uuid4(),
        purpose=purpose,  # type: ignore[arg-type]
        course_id=ref_uuid("course", course_ref),
        course_ref=course_ref,
        offering_id=None if offering_ref is None else ref_uuid("offering", offering_ref),
        offering_ref=offering_ref,
        release_context_id=release_context_id,
    )


def release_info(
    *,
    status: str = "frozen",
    course_ref: str = COURSE,
    offering_ref: str | None = OFFERING,
    existed: bool = True,
) -> ReleaseScopeInfo | None:
    if not existed:
        return None
    return ReleaseScopeInfo(
        release_context_id=CONTEXT,
        course_id=ref_uuid("course", course_ref),
        course_ref=course_ref,
        offering_id=None if offering_ref is None else ref_uuid("offering", offering_ref),
        offering_ref=offering_ref,
        status=status,  # type: ignore[arg-type]
        item_count=1,
    )


class PermissiveAuthorizer:
    """测试替身：授权端口语义与生产一致（不通过就抛 PermissionError）。"""

    def __init__(self) -> None:
        self.service_accounts: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **facts: object) -> None:
        return None

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


def service(
    records: list[CandidateRecord],
    *,
    events: list[ExposureEvent] | None = None,
    release: ReleaseScopeInfo | None = None,
) -> tuple[CandidateService, InMemoryCandidateRepository]:
    repository = InMemoryCandidateRepository(
        release=release if release is not None else release_info(),
        records=tuple(records),
        events=tuple(events or ()),
    )
    return CandidateService(
        repository=repository,
        authorizer=PermissiveAuthorizer(),
        clock=lambda: NOW,
    ), repository


def snapshot_of(
    records: list[CandidateRecord],
    *,
    events: list[ExposureEvent] | None = None,
    release: ReleaseScopeInfo | None = None,
    purpose: str = "formal",
    course_ref: str = COURSE,
    offering_ref: str | None = OFFERING,
) -> object:
    candidate_service, _ = service(records, events=events, release=release)
    return candidate_service.snapshot(
        actor_id=uuid4(),
        scope=scope(purpose=purpose, course_ref=course_ref, offering_ref=offering_ref),
    )


def expect(exc_type: type[BaseException], fn):  # type: ignore[no-untyped-def]
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"预期抛出 {exc_type.__name__}，但调用成功返回")


# ---------------------------------------------------------------------------
# 1. 内容寻址版本：不需要 exercise_version 表
# ---------------------------------------------------------------------------

def test_content_addressed_version_id_tracks_content_not_row_identity() -> None:
    first = exercise_snapshot("ex-1", answer="答案 A")
    same = exercise_snapshot("ex-1", answer="答案 A")
    edited = exercise_snapshot("ex-1", answer="答案 B")

    digest = content_hash(first.prompt_md, first.answer_md)
    assert digest == content_hash(same.prompt_md, same.answer_md), "同内容应得到同一哈希"
    assert digest != content_hash(edited.prompt_md, edited.answer_md), "改答案必须换哈希"
    assert len(digest) == 64 and re.fullmatch(r"[0-9a-f]{64}", digest)

    version = version_id_for(COURSE, "ex-1", digest)
    assert version == version_id_for(COURSE, "ex-1", digest), "版本 id 必须是确定性的"
    assert version != version_id_for(COURSE, "ex-1", content_hash(edited.prompt_md, edited.answer_md)), (
        "编辑答案后版本 id 没变——等于把两道不同的题当成同一版"
    )
    assert version != version_id_for("analysis-ii", "ex-1", digest), "不同课程的同一题号不能撞版本 id"
    assert version != version_id_for(COURSE, "ex-2", digest), "不同题目的同一内容不能撞版本 id"


def test_family_id_is_stable_across_content_edits() -> None:
    """同族题=同一道稳定题目；编辑换版本，但不该换族（曝光/去重都按族算）。"""
    assert family_id_for(COURSE, "ex-1") == family_id_for(COURSE, "ex-1")
    assert family_id_for(COURSE, "ex-1") != family_id_for(COURSE, "ex-2")
    assert family_id_for(COURSE, "ex-1") != family_id_for("analysis-ii", "ex-1")
    # 版本 id 与族 id 不能相等：否则"同族不同版本"会被误判成"同一版本"
    digest = content_hash("题干", "答案")
    assert family_id_for(COURSE, "ex-1") != version_id_for(COURSE, "ex-1", digest)


# ---------------------------------------------------------------------------
# 2. 冻结 Release：把当前内容钉成不可变清单
# ---------------------------------------------------------------------------

def release_service(*exercises: ExerciseSnapshot, **kwargs: object) -> tuple[ReleaseService, InMemoryReleaseRepository]:
    repository = InMemoryReleaseRepository(exercises or (exercise_snapshot(),))
    return ReleaseService(repository=repository, clock=lambda: NOW), repository


def test_freeze_pins_the_content_addressed_version() -> None:
    service_, _ = release_service()
    context = service_.freeze(
        actor_id=uuid4(), course_ref=COURSE, purpose="formal",
        exercise_refs=("ex-1",), offering_ref=OFFERING,
    )
    assert context.status == "frozen"
    assert context.item_count == 1
    items = service_.items(context.id)
    snapshot = exercise_snapshot()
    assert items[0].exercise_version_id == version_id_for(
        COURSE, "ex-1", content_hash(snapshot.prompt_md, snapshot.answer_md))
    assert items[0].exercise_family_id == family_id_for(COURSE, "ex-1")
    assert items[0].content_hash == content_hash(snapshot.prompt_md, snapshot.answer_md)
    assert items[0].state == "included"


def test_freezing_after_an_edit_pins_a_new_version_but_the_same_family() -> None:
    service_, repository = release_service(exercise_snapshot(answer="旧答案"))
    first = service_.freeze(actor_id=uuid4(), course_ref=COURSE, purpose="formal",
                            exercise_refs=("ex-1",), offering_ref=OFFERING)
    repository.upsert_exercise(exercise_snapshot(answer="新答案"))
    second = service_.freeze(actor_id=uuid4(), course_ref=COURSE, purpose="formal",
                             exercise_refs=("ex-1",), offering_ref=OFFERING)
    old = service_.items(first.id)[0]
    new = service_.items(second.id)[0]
    assert old.exercise_version_id != new.exercise_version_id, "编辑后版本 id 没变"
    assert old.exercise_family_id == new.exercise_family_id, "编辑不该换族（曝光按族算）"


def test_release_context_hash_is_deterministic() -> None:
    service_, _ = release_service()
    first = service_.freeze(actor_id=uuid4(), course_ref=COURSE, purpose="formal",
                            exercise_refs=("ex-1",), offering_ref=OFFERING)
    second = service_.freeze(actor_id=uuid4(), course_ref=COURSE, purpose="formal",
                             exercise_refs=("ex-1",), offering_ref=OFFERING)
    assert first.content_hash == second.content_hash, "同内容两次冻结得到了不同清单哈希"
    assert first.id != second.id, "两次冻结应是两个 Release 事实"


def test_freeze_rejects_unknown_exercises_and_duplicate_refs() -> None:
    service_, _ = release_service()
    expect(UnknownExerciseError, lambda: service_.freeze(
        actor_id=uuid4(), course_ref=COURSE, purpose="formal",
        exercise_refs=("nope",), offering_ref=OFFERING))
    expect(DuplicateReleaseItemError, lambda: service_.freeze(
        actor_id=uuid4(), course_ref=COURSE, purpose="formal",
        exercise_refs=("ex-1", "ex-1"), offering_ref=OFFERING))


def test_base_release_must_come_from_the_same_course() -> None:
    other = exercise_snapshot()
    other = other.model_copy(update={"course_ref": "analysis-ii"})
    service_, _ = release_service(exercise_snapshot(), other)
    foreign = service_.freeze(actor_id=uuid4(), course_ref="analysis-ii", purpose="formal",
                              exercise_refs=("ex-1",))
    expect(ReleaseScopeError, lambda: service_.freeze(
        actor_id=uuid4(), course_ref=COURSE, purpose="formal",
        exercise_refs=("ex-1",), offering_ref=OFFERING,
        base_release_id=foreign.id))


# ---------------------------------------------------------------------------
# 3. 资格矩阵：每一项不合格都给出原因码
# ---------------------------------------------------------------------------

def _rejected_reasons(**kwargs: object) -> tuple[str, ...]:
    snapshot = snapshot_of([make_record(**kwargs)])  # type: ignore[arg-type]
    assert snapshot.accepted == (), "不合格候选进了 accepted"
    assert len(snapshot.rejected) == 1
    return snapshot.rejected[0].reasons


def test_clean_candidate_is_accepted() -> None:
    snapshot = snapshot_of([make_record()])
    assert snapshot.accepted and not snapshot.rejected
    record = snapshot.accepted[0]
    assert record.review_status == "teacher-reviewed"
    assert record.license == "licensed"
    assert record.evidence.content_hash == content_hash(
        exercise_snapshot().prompt_md, exercise_snapshot().answer_md
    )


def test_outside_release_context_is_rejected_with_reason_code() -> None:
    reasons = _rejected_reasons(release_state="missing")
    assert "outside_release_context" in reasons, reasons


def test_excluded_item_is_not_a_candidate() -> None:
    reasons = _rejected_reasons(release_state="excluded")
    assert "outside_release_context" in reasons, reasons


def test_drifted_content_is_rejected_not_silently_accepted() -> None:
    """Release 固定的是旧内容、题目已被原地编辑：必须拒绝，而不是拿新内容去印审核过的卷。"""
    reasons = _rejected_reasons(release_state="drifted")
    assert "content_drift" in reasons, reasons


def test_withdrawn_release_item_is_rejected() -> None:
    reasons = _rejected_reasons(release_state="withdrawn")
    assert "release_withdrawn" in reasons, reasons


def test_revoked_review_decision_is_rejected() -> None:
    reasons = _rejected_reasons(snapshot=exercise_snapshot(review_status="retired"))
    assert "revoked" in reasons, reasons


def test_private_draft_and_machine_checked_are_not_approved() -> None:
    for status in ("draft", "machine-checked"):
        reasons = _rejected_reasons(snapshot=exercise_snapshot(review_status=status))
        assert "review_not_approved" in reasons, (status, reasons)
        # 原因码是给教师看的：必须能查到含义
        assert all(code in REASON_MEANINGS for code in reasons)


def test_missing_answer_blocks_candidacy() -> None:
    reasons = _rejected_reasons(snapshot=exercise_snapshot(answer=""))
    assert "answer_missing" in reasons, reasons


def test_license_missing_and_invalid_are_distinguished() -> None:
    missing = _rejected_reasons(snapshot=exercise_snapshot(license=None))
    assert "license_missing" in missing, missing
    invalid = _rejected_reasons(snapshot=exercise_snapshot(license="unknown"))
    assert "license_invalid" in invalid, invalid
    assert "license_invalid" in REASON_MEANINGS


def test_language_pair_invalid_is_rejected() -> None:
    reasons = _rejected_reasons(snapshot=exercise_snapshot(language=""))
    assert "language_pair_invalid" in reasons, reasons


def test_render_not_ready_is_rejected() -> None:
    reasons = _rejected_reasons(render_ready=False)
    assert "render_not_ready" in reasons, reasons


def test_reserved_dimensions_gate_when_the_flag_is_set() -> None:
    """隐藏评测集 / 私人草稿 / 参数化实例 / 依赖不完整：本仓库没有这些概念，
    但契约层必须留门——一旦上游真的标了，候选不能因为"本仓库没这个概念"就漏过去。"""
    cases = {
        "hidden_evaluation": {"hidden_evaluation": True},
        "private_draft": {"private_draft": True},
        "missing_approved_instance": {"parameterized": True, "instance_approved": False},
        "dependencies_incomplete": {"dependencies_complete": False},
        "purpose_not_allowed": {"purpose_allowed": False},
    }
    for expected, flags in cases.items():
        snapshot = snapshot_of([make_record(**flags)])
        assert snapshot.accepted == (), f"{expected}: 不合格候选进了 accepted"
        assert expected in snapshot.rejected[0].reasons, (expected, snapshot.rejected[0].reasons)


def test_reasons_are_always_known_codes_never_booleans() -> None:
    snapshot = snapshot_of(
        [make_record(snapshot=exercise_snapshot(review_status="draft", answer="", license=None))]
    )
    reasons = snapshot.rejected[0].reasons
    assert len(reasons) >= 3, f"多条问题应全部列出，实际 {reasons}"
    assert all(isinstance(code, str) and code in REASON_MEANINGS for code in reasons)
    assert all(not isinstance(code, bool) for code in reasons)
    dumped = snapshot.model_dump(mode="json")
    assert isinstance(dumped["rejected"][0]["reasons"], list)


# ---------------------------------------------------------------------------
# 4. Release 级别的前置门（整卷共性问题）
# ---------------------------------------------------------------------------

def test_formal_paper_requires_a_frozen_release_context() -> None:
    candidate_service, _ = service([make_record()], release=release_info(status="open"))
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="formal"))
    assert snapshot.accepted == ()
    assert "release_not_frozen" in snapshot.rejected[0].reasons


def test_practice_paper_accepts_an_open_release_context() -> None:
    candidate_service, _ = service([make_record()], release=release_info(status="open"))
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="practice"))
    assert snapshot.accepted, snapshot.rejected


def test_release_from_another_course_is_rejected() -> None:
    candidate_service, _ = service([make_record()], release=release_info(course_ref="analysis-ii"))
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope())
    assert snapshot.accepted == ()
    assert "scope_mismatch" in snapshot.rejected[0].reasons


def test_release_from_another_offering_is_rejected() -> None:
    candidate_service, _ = service(
        [make_record()], release=release_info(offering_ref="class-other")
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope())
    assert snapshot.accepted == ()
    assert "scope_mismatch" in snapshot.rejected[0].reasons


def test_missing_release_context_fails_closed() -> None:
    """Release 不存在时是错误，不是"候选为空"——否则教师只会看到"候选不足"。"""
    repository = InMemoryCandidateRepository(records=(make_record(),))
    candidate_service = CandidateService(
        repository=repository, authorizer=PermissiveAuthorizer(), clock=lambda: NOW
    )
    expect(ReleaseContextNotFoundError, lambda: candidate_service.snapshot(
        actor_id=uuid4(), scope=scope()))


# ---------------------------------------------------------------------------
# 5. 曝光门禁：练习公开答案 vs 正式保密卷
# ---------------------------------------------------------------------------

def exposure_event(
    *,
    kind: str,
    family_exercise: str = "ex-1",
    offering_ref: str | None = OFFERING,
    visible_from: datetime = NOW,
) -> ExposureEvent:
    snapshot = exercise_snapshot(family_exercise)
    return ExposureEvent(
        exercise_version_id=version_id_for(
            COURSE, family_exercise, content_hash(snapshot.prompt_md, snapshot.answer_md)),
        exercise_instance_id=None,
        exercise_family_id=family_id_for(COURSE, family_exercise),
        scope_offering_id=None if offering_ref is None else ref_uuid("offering", offering_ref),
        event_kind=kind,  # type: ignore[arg-type]
        visible_from=visible_from,
        occurred_at=visible_from,
    )


def test_practice_answer_release_blocks_a_formal_paper() -> None:
    candidate_service, _ = service(
        [make_record()], events=[exposure_event(kind="practice_answer_released")]
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="formal"))
    assert snapshot.accepted == ()
    assert "exposure_conflict" in snapshot.rejected[0].reasons


def test_practice_answer_release_does_not_block_a_practice_paper() -> None:
    candidate_service, _ = service(
        [make_record()], events=[exposure_event(kind="practice_answer_released")]
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="practice"))
    assert snapshot.accepted, snapshot.rejected


def test_future_exposure_event_does_not_block_yet() -> None:
    candidate_service, _ = service(
        [make_record()],
        events=[exposure_event(kind="practice_answer_released", visible_from=FUTURE)],
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="formal"))
    assert snapshot.accepted, "还没到可见时间的曝光事件被当成了已经泄题"


def test_cross_offering_secure_assignment_blocks_a_formal_paper() -> None:
    candidate_service, _ = service(
        [make_record()],
        events=[exposure_event(kind="secure_assigned", offering_ref="class-other")],
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="formal"))
    assert snapshot.accepted == ()
    assert "exposure_conflict" in snapshot.rejected[0].reasons


def test_same_offering_secure_assignment_does_not_block() -> None:
    candidate_service, _ = service(
        [make_record()], events=[exposure_event(kind="secure_assigned")]
    )
    snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose="formal"))
    assert snapshot.accepted, snapshot.rejected


def test_withdrawn_family_blocks_every_purpose() -> None:
    for purpose in ("formal", "coursework", "practice"):
        candidate_service, _ = service(
            [make_record()], events=[exposure_event(kind="withdrawn", offering_ref=None)]
        )
        snapshot = candidate_service.snapshot(actor_id=uuid4(), scope=scope(purpose=purpose))
        assert snapshot.accepted == (), f"{purpose}: 已撤回题进入了候选"
        assert "exposure_conflict" in snapshot.rejected[0].reasons


def test_exposure_service_reports_the_event_kind_in_the_message() -> None:
    record = make_record()
    event = exposure_event(kind="practice_answer_released")
    error = expect(ExposureConflict, lambda: ExposureService.assert_no_conflict(
        "formal", OFFERING_ID, (record.evidence,), (event,), NOW))
    assert "practice_answer_released" in str(error)


def test_exposure_service_is_shared_by_precheck_and_final_check() -> None:
    """候选预检与发布终检必须是同一个判定，否则两处规则漂移就会放走泄题题。"""
    from inspect import getsource

    from app import candidates as candidate_module

    source = getsource(candidate_module)
    assert "ExposureService.assert_no_conflict" in source, (
        "候选快照没有调用共享的曝光判定，预检与发布终检会各写一套规则"
    )


# ---------------------------------------------------------------------------
# 6. 快照：不可变清单 + 稳定哈希 + 不含答案正文
# ---------------------------------------------------------------------------

def test_snapshot_is_content_keyed_and_idempotent() -> None:
    candidate_service, repository = service([make_record()])
    scoped = scope()
    first = candidate_service.snapshot(actor_id=uuid4(), scope=scoped)
    second = candidate_service.snapshot(actor_id=uuid4(), scope=scoped)
    assert first.created is True
    assert second.created is False, "同一输入重复生成快照时不该产生第二行"
    assert second.id == first.id
    assert second.candidate_manifest_hash == first.candidate_manifest_hash
    assert len(repository.snapshots) == 1


def test_snapshot_hash_is_stable_when_another_release_is_frozen() -> None:
    """新冻结一个 Release 不该改变旧快照的哈希——否则"审核过的候选清单"会天天失效。"""
    scoped = scope()
    candidate_service, repository = service([make_record()])
    before = candidate_service.snapshot(actor_id=uuid4(), scope=scoped)

    # 模拟随后又冻结了一个 Release（同课程、多一道题），旧 context 的条目不变
    other_context = uuid4()
    repository.seed_release(release_info(), context_id=other_context)
    after = candidate_service.snapshot(actor_id=uuid4(), scope=scoped)

    assert after.candidate_manifest_hash == before.candidate_manifest_hash
    assert after.id == before.id


def test_manifest_covers_decision_seq_license_and_review_status() -> None:
    """计划书要求清单覆盖审核 decision_seq、许可与曝光状态；这些必须真的进哈希。"""
    record = make_record()
    snapshot = snapshot_of([record])
    manifest = snapshot.manifest
    accepted = manifest["accepted"][0]
    assert accepted["evidence"]["decision_seqs"]["exercise"] == 1
    assert accepted["evidence"]["latest_decisions"]["exercise"] == "approved"
    assert accepted["review_status"] == "teacher-reviewed"
    assert accepted["license"] == "licensed"
    assert "exposure_events" in manifest
    assert manifest["scope"]["purpose"] == "formal"


def test_snapshot_payload_contains_no_answer_text() -> None:
    record = make_record(snapshot=exercise_snapshot(answer="唯一答案字符串-42"))
    snapshot = snapshot_of([record])
    dumped = str(snapshot.model_dump(mode="json"))
    assert "唯一答案字符串-42" not in dumped, "候选快照泄露了答案正文（只允许哈希与标志）"
    assert "证明" not in dumped, "候选快照泄露了题干正文"


def test_rejected_entries_carry_identity_for_explaining_to_the_teacher() -> None:
    snapshot = snapshot_of([make_record(release_state="missing")])
    rejected = snapshot.rejected[0]
    assert rejected.exercise_id == exercise_uuid(COURSE, "ex-1")
    assert rejected.exercise_family_id == family_id_for(COURSE, "ex-1")
    assert rejected.review_status == "teacher-reviewed"
    assert rejected.reasons


def test_candidate_reasons_helper_is_pure_and_total() -> None:
    """`candidate_reasons` 供路由/前端复用；同一记录不能给出两套结论。"""
    record = make_record(snapshot=exercise_snapshot(review_status="draft"))
    first = candidate_reasons(record)
    assert first == candidate_reasons(record)
    assert "review_not_approved" in first


def test_scope_rejects_unknown_purpose() -> None:
    try:
        scope(purpose="exam")
    except ValidationError:
        pass
    else:
        raise AssertionError("CandidateScope 接受了未定义的 purpose")


# ---------------------------------------------------------------------------
# 7. 迁移与 SQL 的静态断言（本机没有可连的 PostgreSQL）
# ---------------------------------------------------------------------------

def migrations() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(MIGRATION_DIR.glob("*.sql"))
    }


def test_minimal_release_context_migration_exists_and_is_additive() -> None:
    files = migrations()
    names = sorted(files)
    index = names.index("0028_assessment_identity_bridge.sql")
    assert index + 1 < len(names), "0028 之后没有新增迁移"
    name = names[index + 1]
    assert name.startswith("0029_"), f"任务 3 的迁移应编号 0029，实际 {name}"
    flat = " ".join(files[name].lower().split())
    assert "create table if not exists assessment.release_context" in flat, (
        "缺少最小 Release Context 表：没有它，'属于固定 Release Context' 这个硬门槛"
        "只能由调用方传一个布尔值，等于假装 Release 体系已存在"
    )
    assert "create table if not exists assessment.release_item" in flat
    assert "unique (release_context_id, exercise_version_id)" in flat, (
        "release_item 缺少 (context, version) 唯一键：同一 Release 可以固定同一版本两次"
    )
    assert "unique (release_context_id, exercise_id)" in flat, (
        "release_item 缺少 (context, exercise) 唯一键：同一 Release 可以固定同一道题的两个版本"
    )
    assert "create table if not exists assessment.exercise_version" not in flat, (
        "内容寻址版本已经能承担版本身份，不应再建 exercise_version 表"
    )
    for forbidden in ("drop table", "drop column", "delete from", "truncate"):
        assert forbidden not in flat, f"迁移里出现禁止操作：{forbidden}"


def test_sql_never_references_tables_that_do_not_exist() -> None:
    """任务书红线：不许写 `SELECT ... FROM release` 这种指向不存在表的查询。

    做法：从迁移里收集所有真实建过的表，再把服务层 SQL 里出现的 FROM/JOIN/UPDATE/
    INSERT 目标逐个比对。CTE 名单独收集，不算表。
    """
    created: set[str] = set()
    for text in migrations().values():
        for match in re.finditer(
            r"create table(?: if not exists)?\s+([\w.]+)", text, re.IGNORECASE
        ):
            created.add(match.group(1).lower())
    assert "assessment.release_item" in created
    assert "release" not in created and "release_context" not in created, (
        "本仓库 public 里没有 release 表；若有，测试夹具需要同步更新"
    )

    for module in ("candidates.py", "releases.py", "exposure.py"):
        source = (ROOT / "server" / "app" / module).read_text(encoding="utf-8")
        # 只看 SQL 字符串（三引号块）：`from .exposure import ...` 是 Python 语法，
        # 不参与"引用不存在的表"的判断。
        sql_blocks = re.findall(r'"""(.*?)"""', source, re.DOTALL)
        ctes = {
            match.group(1).lower()
            for block in sql_blocks
            for match in re.finditer(r"(\w+)\s+as\s*\(", block, re.IGNORECASE)
        }
        referenced: set[str] = set()
        for block in sql_blocks:
            referenced |= {
                match.group(2).lower()
                for match in re.finditer(
                    r"\b(from|join|into|update)\s+([\w.]+)", block, re.IGNORECASE
                )
            }
        unknown = {
            name for name in referenced
            if name not in created and name not in ctes
        }
        assert not unknown, (
            f"{module} 的 SQL 引用了迁移里不存在的表：{sorted(unknown)}"
        )


def exercise_columns() -> set[str]:
    """`exercise` 的真实列集合（建表 + 后续 ADD COLUMN）。"""
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE exercise \((.*?)\n\);", schema, re.DOTALL)
    assert match, "db/schema.sql 里找不到 exercise 建表语句"
    columns: set[str] = set()
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        head = stripped.split()[0].lower()
        if head in {"primary", "foreign", "unique", "check", "constraint", "exclude"}:
            continue
        columns.add(head)
    for alter in re.finditer(
        r"ALTER TABLE exercise\s+(.*?);", schema, re.DOTALL | re.IGNORECASE
    ):
        for added in re.finditer(r"ADD COLUMN\s+(\w+)", alter.group(1), re.IGNORECASE):
            columns.add(added.group(1).lower())
    return columns


def test_sql_only_uses_columns_that_exist_on_exercise() -> None:
    """列名写错与新表一样致命：假 cursor 不会报错，真库会直接 `column does not exist`。

    `exercise.language` 是后续迁移 ADD COLUMN 加上去的（建表语句里没有），所以列集合
    必须同时收集建表与 ALTER —— 否则这条测试会误报，反而教人放宽它。
    """
    columns = exercise_columns()
    assert {"prompt_md", "answer_md", "review_status", "license", "language"} <= columns
    for module in ("candidates.py", "releases.py"):
        source = (ROOT / "server" / "app" / module).read_text(encoding="utf-8")
        sql_blocks = re.findall(r'"""(.*?)"""', source, re.DOTALL)
        referenced = {
            match.group(1).lower()
            for block in sql_blocks
            for match in re.finditer(r"\be\.(\w+)\b", block)
        }
        unknown = referenced - columns
        assert not unknown, (
            f"{module} 的 SQL 用了 exercise 上不存在的列：{sorted(unknown)}"
        )


# ---------------------------------------------------------------------------
# 脚本入口
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
        except AssertionError as exc:
            failures.append(name)
            print(f"  ✗ {name}：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 脚本模式下把异常也记成失败
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 候选资格与曝光测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
