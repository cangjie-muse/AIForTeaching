"""手工固定卷：建卷、替换、调序、调分（server/app/papers.py）的单元测试。

为什么这一层必须钉死：草稿编辑是"同一份卷子被反复改"的唯一入口。它一旦出错，
错误会以三种方式传到学生手里——

  * 槽位只记题号不记来源 → 打印出来的题和审核过的版本不是同一份，无法回放；
  * base_version 不校验 → 两个窗口同时编辑，后写的静默覆盖前写的分值和题；
  * 替换用"当前最新版本"而不是**建卷时固定的候选快照** → 换一个 Release 就能把
    未审、已撤回、跨班的题塞进卷子，而所有门禁都显示通过。

所以这里测的是不变式而不是函数返回值：

  1. 每个已填槽位都固定 `release_context_id / source_release_id /
     exercise_version_id / exercise_family_id / dependency_manifest_hash`，
     这些值只能来自建卷时的 candidate snapshot；
  2. 每次编辑 `edit_seq = base_version + 1`，manifest 重算，旧审核哈希清空；
     `base_version` 过期一律 `StalePaperVersion`，绝不静默覆盖；
  3. 只有 draft 可编辑；approved 只能克隆成**新 paper 身份**的新草稿；
  4. 被替换掉的槽位旧来源不删除历史，写进 append-only 变更集（paper_edit_event）；
  5. 分值全程 Decimal、两位小数，草稿允许总分错误的中间状态；
  6. 没有可连 PostgreSQL 时，SQL 由静态断言覆盖（事务作用域、乐观锁条件、
     append-only 触发器、numeric(8,2)）。

运行：`python server/tests/test_papers.py`（仓库惯例：不引入 pytest，顶层仍是
pytest 风格的 test_ 函数，`run_tests()` 手动收集）。
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from pydantic import ValidationError  # noqa: E402

from app.blueprints import (  # noqa: E402
    BlueprintService,
    InMemoryBlueprintRepository,
    ref_uuid,
)
from app.candidates import (  # noqa: E402
    CandidateService,
    InMemoryCandidateRepository,
    ReleaseScopeInfo,
)
from app.contracts import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    CandidateEvidence,
    CandidateRecord,
    CandidateScope,
    PaperSectionView,
    PaperSlotView,
    PaperView,
    SlotSelection,
    canonical_hash,
)
from app.papers import (  # noqa: E402
    PAPER_READ_ACTION,
    PAPER_WRITE_ACTION,
    EditOperation,
    InMemoryPaperRepository,
    PaperIdempotencyConflictError,
    PaperNotDraftError,
    PaperService,
    PaperRuleError,
    StalePaperVersion,
    paper_manifest,
    slot_source_missing,
)
from app.releases import content_hash, exercise_uuid, family_id_for, version_id_for  # noqa: E402

MIGRATION_DIR = ROOT / "db" / "migrations"
MIGRATION_0030 = MIGRATION_DIR / "0030_paper_editing.sql"
SCHEMA_SQL = ROOT / "db" / "schema.sql"
CONTRACT_SCHEMA = ROOT / "packages" / "contracts" / "assessment" / "paper.schema.json"

COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "class-2026-fall"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
ACTOR_REF = "acc-teacher"
ACTOR_ID = ref_uuid("account", ACTOR_REF)
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class FakeAuthorizer:
    """与生产 CourseScopeAuthorizer 同端口的测试替身。

    同时接受 blueprint 与 paper 两组动作：卷子建在蓝图之上，生产者两个动作都要过。
    """

    def __init__(self, courses: set[str] | None = None,
                 teachers: set[str] | None = None) -> None:
        self.courses = {"analysis"} if courses is None else courses
        self.teachers = {"acc-teacher"} if teachers is None else teachers

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        allowed = {
            "paper_blueprint.write", "paper_blueprint.read",
            PAPER_WRITE_ACTION, PAPER_READ_ACTION,
        }
        if action not in allowed:
            raise PermissionError(action)
        course_ref = scope.get("course_ref")
        if course_ref is None:
            raise PermissionError("缺少课程范围")
        if course_ref not in self.courses:
            raise PermissionError(f"course not in scope: {course_ref}")
        actor_ref = scope.get("actor_ref")
        # 候选服务的授权端口不传 actor_ref（它只判课程范围）；这里只在带身份时校验，
        # 否则会把"没传"误判成"不是这门课的老师"。
        if actor_ref is not None and actor_ref not in self.teachers:
            raise PermissionError(f"teacher has no class in course: {course_ref}")

    def is_human(self, actor_id: UUID) -> bool:
        return True


def slot(code: str, score: str, **overrides: object) -> BlueprintSlot:
    payload: dict[str, object] = {"code": code, "score": Decimal(score)}
    payload.update(overrides)
    return BlueprintSlot(**payload)  # type: ignore[arg-type]


def blueprint_content(**overrides: object) -> BlueprintContent:
    payload: dict[str, object] = {
        "schema_version": "paper-blueprint/v1",
        "purpose": "formal",
        "course_id": COURSE_ID,
        "offering_id": OFFERING_ID,
        "release_context_id": CONTEXT,
        "total_score": Decimal("100.00"),
        "duration_seconds": 7200,
        "sections": (
            BlueprintSection(
                code="A",
                title="基础",
                instruction="写出完整证明",
                slots=(slot("A1", "40.00"), slot("A2", "60.00")),
            ),
        ),
        "hard_constraints": {},
        "soft_objectives": {},
        "review_policy": "formal_dual_review",
        "assembly_mode": "manual",
    }
    payload.update(overrides)
    return BlueprintContent(**payload)  # type: ignore[arg-type]


def make_record(
    ref: str = "ex-1",
    *,
    release_state: str = "included",
    prompt: str | None = None,
    answer: str = "取 $\\varepsilon>0$，由定义……",
    **flags: object,
) -> CandidateRecord:
    """一条候选记录：证据标志默认全绿，测试只翻转关心的那一项。"""
    prompt_md = prompt if prompt is not None else f"证明第 {ref} 题"
    digest = content_hash(prompt_md, answer)
    booleans: dict[str, object] = {
        "in_context": release_state == "included",
        "license_valid": True,
        "revoked": False,
        "hidden_evaluation": False,
        "private_draft": False,
        "parameterized": False,
        "instance_approved": True,
        "dependencies_complete": True,
        "render_ready": True,
        "language_pair_valid": True,
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
        concept_ids=(),
        learning_outcome_ids=(),
        difficulty_label="medium",
        estimated_seconds=600,
        decision_seqs={"exercise": 1},
        latest_decisions={"exercise": "approved"},
        dependency_manifest_hash=canonical_hash({"ref": ref, "digest": digest}),
        content_hash=digest,
        **booleans,  # type: ignore[arg-type]
    )
    return CandidateRecord(
        evidence=evidence,
        release_state=release_state,  # type: ignore[arg-type]
        review_status="teacher-reviewed",
        license="licensed",
        answer_present=bool(answer.strip()),
    )


def release_info(*, status: str = "frozen") -> ReleaseScopeInfo:
    return ReleaseScopeInfo(
        release_context_id=CONTEXT,
        course_id=COURSE_ID,
        course_ref=COURSE,
        offering_id=OFFERING_ID,
        offering_ref=OFFERING,
        status=status,  # type: ignore[arg-type]
        item_count=2,
    )


def factory(
    *,
    content: BlueprintContent | None = None,
    records: tuple[CandidateRecord, ...] | None = None,
) -> SimpleNamespace:
    """一个可用的「蓝图版本 + 候选快照 + 纸卷服务」组合。"""
    authorizer = FakeAuthorizer()
    blueprint_repo = InMemoryBlueprintRepository()
    blueprints = BlueprintService(repository=blueprint_repo, authorizer=authorizer)
    version = blueprints.create_version(
        actor_id=ACTOR_ID,
        content=content or blueprint_content(),
        blueprint_id=uuid4(),
        base_version_id=None,
        idempotency_key="paper-fixture-blueprint",
        course_ref=COURSE,
        offering_ref=OFFERING,
        actor_ref=ACTOR_REF,
    )
    candidate_repo = InMemoryCandidateRepository(
        release=release_info(),
        records=records if records is not None else (make_record("ex-1"), make_record("ex-2")),
    )
    candidates = CandidateService(repository=candidate_repo, authorizer=authorizer)
    scoped = CandidateScope.from_version(version)
    snapshot = candidates.snapshot(actor_id=ACTOR_ID, scope=scoped)
    paper_repo = InMemoryPaperRepository()
    service = PaperService(
        repository=paper_repo,
        authorizer=authorizer,
        blueprints=blueprints,
        candidates=candidates,
        clock=lambda: NOW,
    )
    return SimpleNamespace(
        service=service,
        repo=paper_repo,
        version=version,
        snapshot=snapshot,
        authorizer=authorizer,
        blueprints=blueprints,
        candidates=candidates,
        content=content or blueprint_content(),
    )


def record_for(snapshot, ref: str):
    for record in snapshot.accepted:
        if record.evidence.exercise_id == exercise_uuid(COURSE, ref):
            return record
    raise AssertionError(f"候选快照里没有 {ref}：{[str(r.evidence.exercise_version_id) for r in snapshot.accepted]}")


def choose(snapshot, code: str, ref: str) -> SlotSelection:
    return SlotSelection(
        slot_code=code,
        exercise_version_id=record_for(snapshot, ref).evidence.exercise_version_id,
    )


def create_draft(f, *, selections=None, key: str = "create-1", **overrides) -> object:
    if selections is None:
        selections = (choose(f.snapshot, "A1", "ex-1"), choose(f.snapshot, "A2", "ex-2"))
    payload = {
        "actor_id": ACTOR_ID,
        "actor_ref": ACTOR_REF,
        "blueprint_version_id": f.version.id,
        "candidate_snapshot_id": f.snapshot.id,
        "selections": selections,
        "idempotency_key": key,
    }
    payload.update(overrides)
    return f.service.create_manual_draft(**payload)  # type: ignore[arg-type]


def edit_draft(f, row, *, operations, base_version: int | None = None,
               key: str = "edit-1", **overrides) -> object:
    return f.service.edit_draft(
        actor_id=ACTOR_ID,
        actor_ref=ACTOR_REF,
        paper_version_id=row.id,
        base_version=row.edit_seq if base_version is None else base_version,
        operations=operations,
        idempotency_key=key,
        **overrides,
    )


@contextmanager
def raises_rule(code: str):
    try:
        yield
    except PaperRuleError as exc:
        assert exc.code == code, f"预期原因码 {code}，实际 {exc.code}：{exc}"
        return
    raise AssertionError(f"预期 PaperRuleError({code})，但没有抛错")


# ---------------------------------------------------------------------------
# 建卷
# ---------------------------------------------------------------------------

def test_manual_draft_tracks_candidate_identity_and_edit_sequence() -> None:
    f = factory()
    row = create_draft(f)

    assert row.status == "draft"
    assert row.edit_seq == 1
    assert row.version_no == 1
    assert row.paper_id != row.id
    assert row.candidate_snapshot_id == f.snapshot.id
    assert [s.code for s in row.sections] == ["A"]
    assert [s.slot_no for s in row.sections[0].slots] == [1, 2]

    first = row.sections[0].slots[0]
    chosen = record_for(f.snapshot, "ex-1").evidence
    assert first.code == "A1"
    assert first.exercise_id == chosen.exercise_id
    assert first.exercise_version_id == chosen.exercise_version_id
    assert first.exercise_instance_id == chosen.exercise_instance_id
    assert first.exercise_family_id == chosen.exercise_family_id
    assert first.source_release_id == chosen.source_release_id
    assert first.release_context_id == CONTEXT
    assert first.dependency_manifest_hash == chosen.dependency_manifest_hash
    assert first.score == Decimal("40.00")
    assert first.content_hash is not None and len(first.content_hash) == 64
    assert row.sections[0].section_score == Decimal("100.00")
    assert row.total_score == Decimal("100.00")
    assert row.manifest_hash == paper_manifest(row)

    edited = edit_draft(
        f, row,
        operations=(EditOperation(kind="score", slot_id=first.id, score=Decimal("45.50")),),
        key="edit-score",
    )
    assert edited.edit_seq == 2
    assert edited.sections[0].slots[0].score == Decimal("45.50")
    assert edited.sections[0].section_score == Decimal("105.50")
    # 草稿允许总分错误的中间状态：满分仍是蓝图目标，等整卷校验器（任务 5）报差异
    assert edited.total_score == Decimal("100.00")
    assert edited.manifest_hash != row.manifest_hash

    try:
        edit_draft(f, edited, operations=(), base_version=1, key="edit-stale")
    except StalePaperVersion as exc:
        assert exc.current_version == 2
    else:
        raise AssertionError("过期 base_version 没有抛 StalePaperVersion")


def test_non_draft_and_cross_snapshot_replacement_are_rejected() -> None:
    f = factory()
    row = create_draft(f)
    # 用**另一个版本 id** 存 approved 副本：同一行 id 被覆盖的话，后面针对原 draft
    # 的断言会读到 approved，测的就不是"approved 不可原位改"而是"根本没改到它"。
    approved = row.model_copy(update={"id": uuid4(), "status": "approved"})
    f.repo.versions[approved.id] = approved

    try:
        edit_draft(f, approved, operations=(), key="edit-approved")
    except PaperNotDraftError as exc:
        assert "draft" in str(exc)
    else:
        raise AssertionError("approved 卷仍然被允许原位编辑")

    with raises_rule("candidate_not_in_snapshot"):
        edit_draft(
            f, row,
            operations=(EditOperation(
                kind="replace",
                slot_id=row.sections[0].slots[0].id,
                exercise_version_id=uuid4(),
            ),),
            key="edit-stranger",
        )


def test_selection_outside_the_pinned_snapshot_is_rejected() -> None:
    f = factory()
    stranger = SlotSelection(slot_code="A1", exercise_version_id=uuid4())
    with raises_rule("candidate_not_in_snapshot"):
        create_draft(f, selections=(stranger,), key="create-stranger")


def test_unknown_slot_code_and_duplicate_selection_are_rejected() -> None:
    f = factory()
    with raises_rule("unknown_slot_code"):
        create_draft(
            f,
            selections=(SlotSelection(slot_code="Z9", exercise_version_id=record_for(f.snapshot, "ex-1").evidence.exercise_version_id),),
            key="create-unknown-slot",
        )
    with raises_rule("duplicate_slot_selection"):
        create_draft(
            f,
            selections=(
                choose(f.snapshot, "A1", "ex-1"),
                choose(f.snapshot, "A1", "ex-2"),
            ),
            key="create-duplicate",
        )


def test_unfilled_slot_is_an_explicit_gap_not_a_number_only_slot() -> None:
    """draft 允许空槽位（§16 无候选先显示缺口），但已填槽位必须来源齐全。"""
    f = factory()
    row = create_draft(f, selections=(choose(f.snapshot, "A1", "ex-1"),), key="create-gap")
    gap = row.sections[0].slots[1]
    assert gap.code == "A2"
    assert gap.exercise_id is None
    assert gap.exercise_version_id is None
    assert gap.exercise_family_id is None
    assert gap.source_release_id is None
    assert slot_source_missing(gap) == ()

    partial = gap.model_copy(update={"exercise_version_id": uuid4(), "exercise_id": uuid4()})
    assert "source_release_id" in slot_source_missing(partial)
    assert "exercise_family_id" in slot_source_missing(partial)

    filled = edit_draft(
        f, row,
        operations=(EditOperation(
            kind="replace", slot_id=gap.id,
            exercise_version_id=record_for(f.snapshot, "ex-2").evidence.exercise_version_id,
        ),),
        key="edit-fill-gap",
    )
    assert filled.sections[0].slots[1].source_release_id == CONTEXT


def test_blueprint_exclusion_and_mandatory_lists_gate_slot_choices() -> None:
    excluded = blueprint_content(sections=(
        BlueprintSection(
            code="A", title="基础", instruction="",
            slots=(
                slot("A1", "40.00",
                     excluded_exercise_version_ids=(record_version("ex-1"),)),
                slot("A2", "60.00", mandatory_exercise_version_ids=(record_version("ex-2"),)),
            ),
        ),
    ))
    f = factory(content=excluded)
    with raises_rule("candidate_excluded"):
        create_draft(f, selections=(choose(f.snapshot, "A1", "ex-1"),), key="create-excluded")
    with raises_rule("candidate_not_mandatory"):
        create_draft(f, selections=(choose(f.snapshot, "A2", "ex-1"),), key="create-mandatory")


def record_version(ref: str) -> UUID:
    return version_id_for(COURSE, ref, content_hash(f"证明第 {ref} 题", "取 $\\varepsilon>0$，由定义……"))


# ---------------------------------------------------------------------------
# 替换 / 调序 / 调分
# ---------------------------------------------------------------------------

def test_replace_switches_source_and_keeps_history_in_change_set() -> None:
    f = factory()
    row = create_draft(f)
    before = row.sections[0].slots[0]
    replacement = record_for(f.snapshot, "ex-2").evidence

    edited = edit_draft(
        f, row,
        operations=(EditOperation(
            kind="replace", slot_id=before.id,
            exercise_version_id=replacement.exercise_version_id,
            reason="换一道同难度题",
        ),),
        key="edit-replace",
    )
    after = edited.sections[0].slots[0]
    assert after.id == before.id  # 槽位身份不变
    assert after.exercise_id == replacement.exercise_id
    assert after.exercise_version_id == replacement.exercise_version_id
    assert after.exercise_family_id == replacement.exercise_family_id
    assert after.source_release_id == replacement.source_release_id
    assert after.dependency_manifest_hash == replacement.dependency_manifest_hash
    assert after.score == before.score  # 替换不动分值，调分是独立操作
    assert edited.edit_seq == 2
    # 同一个候选快照：替换不能把 draft 换到另一个 Release
    assert edited.candidate_snapshot_id == row.candidate_snapshot_id

    events = f.repo.list_edit_events(row.id)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "replace"
    assert event.slot_id == before.id
    assert event.previous_exercise_version_id == before.exercise_version_id
    assert event.new_exercise_version_id == replacement.exercise_version_id
    assert event.previous_source_release_id == before.source_release_id
    assert event.new_source_release_id == replacement.source_release_id
    assert event.reason == "换一道同难度题"
    assert event.manifest_hash == edited.manifest_hash
    assert event.edit_seq == 2


def test_move_reorders_within_section_and_renumbers() -> None:
    f = factory()
    row = create_draft(f)
    first, second = row.sections[0].slots
    moved = edit_draft(
        f, row,
        operations=(EditOperation(kind="move", slot_id=first.id, target_slot_no=2),),
        key="edit-move",
    )
    assert [s.code for s in moved.sections[0].slots] == ["A2", "A1"]
    assert [s.slot_no for s in moved.sections[0].slots] == [1, 2]
    assert moved.sections[0].slots[1].id == first.id
    assert moved.sections[0].slots[0].id == second.id
    assert moved.manifest_hash != row.manifest_hash
    assert moved.sections[0].slots[0].content_hash != second.content_hash

    with raises_rule("invalid_slot_order"):
        edit_draft(
            f, moved,
            operations=(EditOperation(kind="move", slot_id=first.id, target_slot_no=3),),
            key="edit-move-bad",
        )


def test_score_must_be_non_negative_two_place_decimal() -> None:
    f = factory()
    row = create_draft(f)
    slot_id = row.sections[0].slots[0].id
    with raises_rule("score_precision"):
        edit_draft(f, row, operations=(EditOperation(
            kind="score", slot_id=slot_id, score=Decimal("1.005")),), key="s1")
    with raises_rule("score_precision"):
        edit_draft(f, row, operations=(EditOperation(
            kind="score", slot_id=slot_id, score=Decimal("-1.00")),), key="s2")
    with raises_rule("unknown_operation"):
        edit_draft(f, row, operations=(EditOperation(
            kind="delete", slot_id=slot_id),), key="s3")
    with raises_rule("slot_not_found"):
        edit_draft(f, row, operations=(EditOperation(
            kind="score", slot_id=uuid4(), score=Decimal("1.00")),), key="s4")


def test_edit_clears_review_hashes_and_requires_open_operations() -> None:
    f = factory()
    row = create_draft(f)
    sealed = row.model_copy(update={
        "submitted_manifest_hash": "b" * 64,
        "review_manifest_hash": "c" * 64,
    })
    f.repo.versions[sealed.id] = sealed
    edited = edit_draft(
        f, sealed,
        operations=(EditOperation(
            kind="score", slot_id=sealed.sections[0].slots[0].id, score=Decimal("41.00")),),
        key="edit-clear",
    )
    assert edited.submitted_manifest_hash is None
    assert edited.review_manifest_hash is None

    with raises_rule("no_operations"):
        edit_draft(f, edited, operations=(), key="edit-empty")


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_idempotent_create_replay_returns_the_same_paper() -> None:
    f = factory()
    first = create_draft(f, key="create-replay")
    assert first.created is True
    second = create_draft(f, key="create-replay")
    assert second.created is False
    assert second.replayed is True
    assert second.id == first.id
    assert len(f.repo.versions) == 1

    with _expect_raise(PaperIdempotencyConflictError):
        create_draft(f, key="create-replay", selections=(choose(f.snapshot, "A1", "ex-2"),))


def test_idempotent_edit_replay_does_not_apply_twice() -> None:
    f = factory()
    row = create_draft(f)
    slot_id = row.sections[0].slots[0].id
    operation = EditOperation(kind="score", slot_id=slot_id, score=Decimal("45.50"))
    first = edit_draft(f, row, operations=(operation,), key="edit-replay")
    assert first.edit_seq == 2
    second = edit_draft(f, row, operations=(operation,), base_version=1, key="edit-replay")
    assert second.edit_seq == 2
    assert second.replayed is True
    assert second.id == first.id
    assert len(f.repo.versions) == 1


@contextmanager
def _expect_raise(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"预期 {exc_type.__name__}，但没有抛错")


# ---------------------------------------------------------------------------
# 克隆：approved 只能变成新 paper 身份的新草稿
# ---------------------------------------------------------------------------

def test_approved_version_can_only_be_cloned_as_a_new_paper_identity() -> None:
    f = factory()
    row = create_draft(f)
    approved = row.model_copy(update={"status": "approved", "review_manifest_hash": "b" * 64})
    f.repo.versions[approved.id] = approved

    cloned = f.service.create_manual_draft(
        actor_id=ACTOR_ID,
        actor_ref=ACTOR_REF,
        base_paper_version_id=approved.id,
        idempotency_key="clone-1",
    )
    assert cloned.status == "draft"
    assert cloned.edit_seq == 1
    assert cloned.version_no == 1
    assert cloned.paper_id != approved.paper_id
    assert cloned.base_paper_version_id == approved.id
    assert cloned.candidate_snapshot_id == approved.candidate_snapshot_id
    assert cloned.review_manifest_hash is None
    assert [s.code for s in cloned.sections] == [s.code for s in approved.sections]
    assert [s.code for s in cloned.sections[0].slots] == ["A1", "A2"]
    assert cloned.sections[0].slots[0].id != approved.sections[0].slots[0].id
    # manifest 是内容清单：id 变了、内容没变，哈希不动
    assert cloned.manifest_hash == approved.manifest_hash

    with raises_rule("paper_identity_conflict"):
        f.service.create_manual_draft(
            actor_id=ACTOR_ID, actor_ref=ACTOR_REF,
            base_paper_version_id=approved.id, paper_id=approved.paper_id,
            idempotency_key="clone-same-identity",
        )
    with raises_rule("base_not_approved"):
        # 另建一份仍是 draft 的卷（同一个 factory 的新 paper 身份），用它证明
        # "draft 不能被当成已批准历史来克隆"。
        draft = create_draft(f, key="clone-draft-source")
        f.service.create_manual_draft(
            actor_id=ACTOR_ID, actor_ref=ACTOR_REF,
            base_paper_version_id=draft.id, idempotency_key="clone-draft",
        )


def test_clone_never_changes_the_original_approved_row() -> None:
    f = factory()
    row = create_draft(f)
    approved = row.model_copy(update={"status": "approved"})
    f.repo.versions[approved.id] = approved
    f.service.create_manual_draft(
        actor_id=ACTOR_ID, actor_ref=ACTOR_REF,
        base_paper_version_id=approved.id, idempotency_key="clone-2",
    )
    assert f.repo.versions[approved.id].status == "approved"
    assert f.repo.versions[approved.id].manifest_hash == approved.manifest_hash


# ---------------------------------------------------------------------------
# manifest 与序列化
# ---------------------------------------------------------------------------

def test_manifest_covers_content_but_ignores_identity_and_edit_metadata() -> None:
    f = factory()
    row = create_draft(f)
    same_content = row.model_copy(update={
        "edit_seq": 9,
        "created_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        "created_by": uuid4(),
        "review_manifest_hash": "d" * 64,
        "submitted_manifest_hash": "e" * 64,
    })
    assert paper_manifest(row) == paper_manifest(same_content)
    assert paper_manifest(row) == canonical_hash(paper_manifest_payload(row))


def paper_manifest_payload(row):
    from app.papers import manifest_payload
    return manifest_payload(row)


def test_scores_serialize_as_two_place_strings_and_reject_float() -> None:
    f = factory()
    row = create_draft(f)
    body = row.model_dump(mode="json")
    assert body["total_score"] == "100.00"
    assert body["sections"][0]["slots"][0]["score"] == "40.00"
    assert body["sections"][0]["section_score"] == "100.00"
    try:
        PaperView(**{**body, "total_score": 100.0})
    except ValidationError:
        return
    raise AssertionError("PaperView 接受了二进制浮点总分")


# ---------------------------------------------------------------------------
# SQL / 迁移静态断言（本机没有可连的 PostgreSQL）
# ---------------------------------------------------------------------------

def test_migration_0030_is_additive_and_follows_0029() -> None:
    names = sorted(p.name for p in MIGRATION_DIR.glob("*.sql"))
    assert MIGRATION_0030.is_file(), "缺少 db/migrations/0030_paper_editing.sql"
    assert names.index(MIGRATION_0030.name) == names.index("0029_minimal_release_context.sql") + 1
    text = MIGRATION_0030.read_text(encoding="utf-8").lower()
    assert "alter table assessment.paper_slot add column if not exists release_context_id uuid" in re.sub(r"\s+", " ", text)
    assert "create table if not exists assessment.paper_edit_event" in re.sub(r"\s+", " ", text)
    assert "begin;" in text and "commit;" in text


def test_edit_event_table_is_append_only_and_scoped() -> None:
    text = re.sub(r"\s+", " ", MIGRATION_0030.read_text(encoding="utf-8").lower())
    assert "create trigger paper_edit_event_guard" in text
    assert "before update or delete" in text
    assert "paper_edit_event is append-only" in text
    assert "alter table assessment.paper_edit_event enable row level security" in text
    assert "alter table assessment.paper_edit_event force row level security" in text
    assert "previous_exercise_version_id" in text and "new_exercise_version_id" in text


def test_edit_event_scores_use_numeric_8_2() -> None:
    text = re.sub(r"\s+", " ", MIGRATION_0030.read_text(encoding="utf-8").lower())
    assert "previous_score numeric(8,2)" in text
    assert "new_score numeric(8,2)" in text


def test_generated_schema_contains_the_paper_editing_objects() -> None:
    text = SCHEMA_SQL.read_text(encoding="utf-8").lower()
    assert "create table if not exists assessment.paper_edit_event" in text, (
        "db/schema.sql 没有重新生成（改完迁移要跑 python db/migrate.py --concat）"
    )
    assert "release_context_id uuid" in text


def test_postgres_repository_uses_assessment_transaction_and_optimistic_lock() -> None:
    source = (ROOT / "server" / "app" / "papers.py").read_text(encoding="utf-8")
    assert "db.transaction(assessment_service=True)" in source, (
        "paper_* 表是 FORCE RLS：读写必须带 assessment_service 事务作用域"
    )
    assert "AND edit_seq = %s" in source and "AND status = 'draft'" in source, (
        "编辑必须用单条 UPDATE 的 edit_seq + status 条件做乐观锁"
    )
    assert "paper_edit_event" in source
    assert "SET status = 'invalidated'" in source, "编辑必须让仍开放的审核轮次失效"
    # 新建/克隆时槽位 id 必须用服务生成的那个：否则响应里的 id 与库里的不同，
    # 客户端拿返回值去 PATCH 会得到 slot_not_found。
    assert "or slot_view.id" in source


def test_sql_references_only_known_assessment_tables() -> None:
    source = (ROOT / "server" / "app" / "papers.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r"assessment\.([a-z_]+)", source))
    known = {
        "paper_version", "paper_section", "paper_slot", "paper_edit_event",
        "paper_review_round", "paper_blueprint_version", "candidate_snapshot",
        "idempotency_record", "id_ref",
    }
    assert referenced <= known, f"引用了不存在的 assessment 表：{sorted(referenced - known)}"


def test_contract_schema_matches_the_shared_paper_view() -> None:
    import json

    assert CONTRACT_SCHEMA.is_file(), f"缺少 {CONTRACT_SCHEMA.relative_to(ROOT)}"
    schema = json.loads(CONTRACT_SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(PaperView.model_fields)
    slot_props = set(schema["$defs"]["slot"]["properties"])
    assert slot_props == set(PaperSlotView.model_fields)
    section_props = set(schema["$defs"]["section"]["properties"])
    assert section_props == set(PaperSectionView.model_fields)
    assert schema["$defs"]["slot"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# 运行器
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
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 手工固定卷测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
