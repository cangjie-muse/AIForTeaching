"""独立整卷校验器（server/app/validation.py）的测试。

为什么这一层必须钉死：校验器会同时被三处使用——草稿自检、提交审核前的门禁、任务 7
的双审证据。它一旦"复用建卷时的中间结果"，就会退化成"把组卷算法自己的答案再抄一遍"：

  * 有人绕过服务直接改库（改分值、改来源、改 manifest_hash）→ 校验器必须能发现；
  * 教师调分后总分不再等于 100 → 草稿可保存，但 `valid` 必须为 False 并给出差几分；
  * 题目在快照之后被撤回 / 同族题已经公开过答案 → 必须阻断，不能因为快照里是合格的
    就继续放行；
  * 同一个输入重复校验必须得到同一个 `report_hash`，否则双审两个人拿到的证据对不上。

因此这里测的是不变式，而不是实现细节：

  1. `valid` 只由 `severity="error"` 的 issue 决定；warning 不阻断；
  2. 所有分值用 Decimal 精确比较，`99.99 != 100.00`，不存在容差；
  3. 每个 issue 都带 `code / severity / slot_no / message`，缺口能定位到槽位与差额；
  4. `report_hash` 只由"结论内容"决定：同输入同哈希，换时间不换哈希；
  5. 校验器只读：仓储端口没有写方法，SQL 里没有 INSERT/UPDATE/DELETE；
  6. 复核：曝光冲突必须复用 `ExposureService`，不在校验器里另写一套。

运行：`python server/tests/test_paper_validation.py`（仓库惯例：不引入 pytest，
顶层仍是 pytest 风格的 `test_` 函数，`run_tests()` 手动收集）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import paper_versions as api  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402
from app.candidates import (  # noqa: E402
    CandidatePreview,
    CandidateScope,
    ReleaseScopeInfo,
    build_manifest,
    manifest_hash,
)
from app.contracts import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    CandidateEvidence,
    CandidateRecord,
    CandidateSnapshot,
    PaperSectionView,
    PaperSlotView,
    PaperView,
    RejectedCandidate,
    ValidationIssue,
    ExposureEvent,
    canonical_hash,
)
from app.papers import (  # noqa: E402
    PaperNotFoundError,
    paper_manifest,
    section_content_hash,
    slot_content_hash,
)
from app.releases import content_hash, exercise_uuid, family_id_for, version_id_for  # noqa: E402
from app.validation import (  # noqa: E402
    REQUIRED_SOURCE_FIELDS,
    InMemoryPaperValidationRepository,
    PaperValidationBlockedError,
    PaperValidationService,
    ValidationInputError,
    ValidationSubject,
    report_payload,
)

MIGRATION_DIR = ROOT / "db" / "migrations"
VALIDATION_SOURCE = ROOT / "server" / "app" / "validation.py"

COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "class-2026-fall"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
OTHER_CONTEXT = UUID("22222222-2222-5222-8222-222222222222")
BP_ID = UUID("33333333-3333-5333-8333-333333333333")
ACTOR_REF = "acc-teacher"
ACTOR_ID = ref_uuid("account", ACTOR_REF)
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}


# ---------------------------------------------------------------------------
# 契约夹具：蓝图 / 候选 / 快照 / 卷面
# ---------------------------------------------------------------------------

def bp_slot(code: str, score: str, **overrides: object) -> BlueprintSlot:
    payload: dict[str, object] = {"code": code, "score": Decimal(score)}
    payload.update(overrides)
    return BlueprintSlot(**payload)  # type: ignore[arg-type]


def blueprint(
    *,
    sections: tuple[BlueprintSection, ...] | None = None,
    total_score: str = "100.00",
    duration_seconds: int = 7200,
    hard_constraints: dict[str, object] | None = None,
    soft_objectives: dict[str, object] | None = None,
    purpose: str = "formal",
) -> BlueprintContent:
    if sections is None:
        sections = (
            BlueprintSection(
                code="A",
                title="基础",
                instruction="写出完整证明",
                slots=(bp_slot("A1", "40.00"), bp_slot("A2", "60.00")),
            ),
        )
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose=purpose,  # type: ignore[arg-type]
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        total_score=Decimal(total_score),
        duration_seconds=duration_seconds,
        sections=sections,
        hard_constraints=hard_constraints or {},
        soft_objectives=soft_objectives or {},
        review_policy="formal_dual_review",
        assembly_mode="manual",
    )


def make_record(
    ref: str,
    *,
    kind: str = "proof",
    concepts: tuple[UUID, ...] = (),
    outcomes: tuple[UUID, ...] = (),
    estimated_seconds: int | None = 600,
    decision: str = "approved",
    **flags: object,
) -> CandidateRecord:
    prompt = f"证明第 {ref} 题"
    answer = "取 $\\varepsilon>0$，由定义……"
    digest = content_hash(prompt, answer)
    booleans: dict[str, object] = {
        "in_context": True,
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
        exercise_kind=kind,
        concept_ids=concepts,
        learning_outcome_ids=outcomes,
        difficulty_label="medium",
        estimated_seconds=estimated_seconds,
        decision_seqs={"exercise": 1},
        latest_decisions={"exercise": decision},
        dependency_manifest_hash=canonical_hash({"ref": ref, "digest": digest}),
        content_hash=digest,
        **booleans,  # type: ignore[arg-type]
    )
    return CandidateRecord(
        evidence=evidence,
        release_state="included",
        review_status="teacher-reviewed",
        license="licensed",
        answer_present=True,
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


def make_snapshot(
    content: BlueprintContent,
    records: tuple[CandidateRecord, ...],
    *,
    blueprint_version_id: UUID = BP_ID,
    purpose: str | None = None,
    snapshot_id: UUID | None = None,
    manifest_hash_override: str | None = None,
) -> CandidateSnapshot:
    """用公开的规范清单构造快照：哈希可由 `manifest_hash` 独立复核。"""
    scope = CandidateScope(
        blueprint_version_id=blueprint_version_id,
        purpose=(purpose or content.purpose),  # type: ignore[arg-type]
        course_id=COURSE_ID,
        course_ref=COURSE,
        offering_id=OFFERING_ID,
        offering_ref=OFFERING,
        release_context_id=CONTEXT,
    )
    manifest = build_manifest(
        scope=scope,
        release=release_info(),
        accepted=records,
        rejected=(),
        events=(),
        as_of=NOW,
    )
    return CandidateSnapshot(
        id=snapshot_id or uuid4(),
        blueprint_version_id=blueprint_version_id,
        release_context_id=CONTEXT,
        purpose=(purpose or content.purpose),  # type: ignore[arg-type]
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        accepted=records,
        rejected=(),
        candidate_manifest_hash=manifest_hash_override or manifest_hash(manifest),
        created_at=NOW,
        manifest=manifest,
    )


def filled(record: CandidateRecord, section_code: str, code: str, no: int,
           score: str) -> PaperSlotView:
    evidence = record.evidence
    slot = PaperSlotView(
        id=uuid4(),
        section_code=section_code,
        code=code,
        slot_no=no,
        exercise_id=evidence.exercise_id,
        exercise_version_id=evidence.exercise_version_id,
        exercise_instance_id=evidence.exercise_instance_id,
        exercise_family_id=evidence.exercise_family_id,
        source_release_id=evidence.source_release_id,
        release_context_id=evidence.release_context_id,
        score=Decimal(score),
        estimated_seconds=evidence.estimated_seconds,
        display_config={"language_pair": "fixed", "option_order": "fixed"},
        dependency_manifest_hash=evidence.dependency_manifest_hash,
        content_hash=None,
    )
    return slot.model_copy(update={"content_hash": slot_content_hash(slot)})


def empty(section_code: str, code: str, no: int, score: str,
          *, estimated_seconds: int | None = None) -> PaperSlotView:
    slot = PaperSlotView(
        id=uuid4(),
        section_code=section_code,
        code=code,
        slot_no=no,
        score=Decimal(score),
        estimated_seconds=estimated_seconds,
        display_config={"language_pair": "fixed", "option_order": "fixed"},
        release_context_id=CONTEXT,
        content_hash=None,
    )
    return slot.model_copy(update={"content_hash": slot_content_hash(slot)})


def section(code: str, slots: tuple[PaperSlotView, ...], *,
            sort_order: int = 1, title: str = "基础") -> PaperSectionView:
    view = PaperSectionView(
        code=code,
        title=title,
        instruction="写出完整证明",
        sort_order=sort_order,
        section_score=sum((slot.score for slot in slots), Decimal("0.00")),
        slots=slots,
        content_hash=None,
    )
    return view.model_copy(update={"content_hash": section_content_hash(view)})


def paper(
    sections: tuple[PaperSectionView, ...],
    *,
    total_score: str = "100.00",
    duration_seconds: int = 7200,
    status: str = "draft",
    purpose: str = "formal",
    paper_id: UUID | None = None,
    blueprint_version_id: UUID = BP_ID,
) -> PaperView:
    view = PaperView(
        id=paper_id or uuid4(),
        blueprint_version_id=blueprint_version_id,
        release_context_id=CONTEXT,
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        purpose=purpose,  # type: ignore[arg-type]
        paper_kind=purpose,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        total_score=Decimal(total_score),
        duration_seconds=duration_seconds,
        edit_seq=1,
        manifest_hash="",
        review_manifest_hash=None,
        sections=sections,
    )
    return view.model_copy(update={"manifest_hash": paper_manifest(view)})


def default_sections(
    records: tuple[CandidateRecord, ...],
    scores: tuple[str, str] = ("40.00", "60.00"),
) -> tuple[PaperSectionView, ...]:
    return (
        section("A", (
            filled(records[0], "A", "A1", 1, scores[0]),
            filled(records[1], "A", "A2", 2, scores[1]),
        )),
    )


def subject_for(
    *,
    content: BlueprintContent | None = None,
    records: tuple[CandidateRecord, ...] | None = None,
    snapshot: CandidateSnapshot | None = None,
    paper_view: PaperView | None = None,
    blueprint_hash: str | None = None,
) -> ValidationSubject:
    content = content or blueprint()
    records = records if records is not None else (make_record("ex-1"), make_record("ex-2"))
    snapshot = snapshot or make_snapshot(content, records)
    paper_view = paper_view or paper(default_sections(records))
    return ValidationSubject(
        paper=paper_view,
        blueprint=content,
        blueprint_content_hash=(
            canonical_hash(content) if blueprint_hash is None else blueprint_hash
        ),
        snapshot=snapshot,
        course_ref=COURSE,
        offering_ref=OFFERING,
    )


def preview_for(
    subject: ValidationSubject,
    *,
    accepted: tuple[CandidateRecord, ...] | None = None,
    rejected: tuple[RejectedCandidate, ...] = (),
    events: tuple[ExposureEvent, ...] = (),
    release: ReleaseScopeInfo | None = None,
) -> CandidatePreview:
    return CandidatePreview(
        release=release if release is not None else release_info(),
        accepted=subject.snapshot.accepted if accepted is None else accepted,
        rejected=rejected,
        exposure_events=events,
    )


def run(subject: ValidationSubject, *, preview: CandidatePreview | None = None,
        now: datetime = NOW):
    return PaperValidationService.evaluate(
        subject,
        preview=preview if preview is not None else preview_for(subject),
        now=now,
    )


def codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {issue.code for issue in result.issues}


def issue(result, code: str):  # type: ignore[no-untyped-def]
    for item in result.issues:
        if item.code == code:
            return item
    raise AssertionError(f"没有 {code}；实际：{sorted(codes(result))}")


# ---------------------------------------------------------------------------
# 一、干净的卷 + 确定性哈希
# ---------------------------------------------------------------------------

def test_clean_paper_is_valid_and_report_hash_is_reproducible() -> None:
    subject = subject_for()
    first = run(subject)
    second = run(subject)

    assert first.valid is True
    assert first.issues == ()
    assert first.error_count == 0 and first.warning_count == 0
    assert first.manifest_hash == first.recomputed_manifest_hash
    assert first.manifest_hash == subject.paper.manifest_hash
    assert re.fullmatch(r"[0-9a-f]{64}", first.report_hash)
    assert first.report_hash == second.report_hash
    assert report_payload(first) == report_payload(second)
    assert canonical_hash(report_payload(first)) == first.report_hash
    # 结果里不能有时间：否则同内容隔一分钟校验就换一个哈希，双审证据对不上。
    dumped = json.dumps(first.model_dump(mode="json"), ensure_ascii=False)
    assert "2026-11-06" not in dumped


def test_score_arithmetic_is_exact_and_reports_the_shortfall() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    # 只改 A2：section_score 仍是 99.99（精确和），总分字段仍是 100.00。
    sections = default_sections(records, scores=("40.00", "59.99"))
    subject = subject_for(records=records, paper_view=paper(sections))
    result = run(subject)

    assert result.valid is False
    assert {"total_score_mismatch", "blueprint_section_score_mismatch"} <= codes(result)
    total = issue(result, "total_score_mismatch")
    assert total.severity == "error"
    assert total.detail["expected"] == "100.00"
    assert total.detail["actual"] == "99.99"
    # 差额是 Decimal 精确差，不是浮点近似。
    assert total.detail["delta"] == "0.01"
    assert "99.99" in total.message and "100.00" in total.message

    # 分节蓝图目标 100.00 与实际 99.99 的缺口同样可定位到分节。
    section_gap = issue(result, "blueprint_section_score_mismatch")
    assert section_gap.section_code == "A"
    assert section_gap.detail["delta"] == "0.01"

    # 槽位级差异只是 warning：教师可以在分节内重新分配分值。
    per_slot = issue(result, "blueprint_slot_score_mismatch")
    assert per_slot.severity == "warning"
    assert per_slot.slot_no == 2
    assert per_slot.detail["delta"] == "0.01"

    # 99.99 + 0.01 必须精确回到 100.00，证明没有二进制浮点参与。
    assert Decimal("99.99") + Decimal("0.01") == Decimal("100.00")


def test_unfilled_slot_lists_the_missing_slot_and_its_score() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    sections = (
        section("A", (
            filled(records[0], "A", "A1", 1, "40.00"),
            empty("A", "A2", 2, "60.00"),
        )),
    )
    subject = subject_for(records=records, paper_view=paper(sections))
    result = run(subject)

    assert result.valid is False
    gap = issue(result, "slot_unfilled")
    assert gap.severity == "error"
    assert gap.slot_no == 2
    assert gap.section_code == "A"
    assert gap.detail["missing_score"] == "60.00"
    assert "A2" in gap.message


def test_duplicate_exercise_and_family_are_blocking() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    sections = (
        section("A", (
            filled(records[0], "A", "A1", 1, "40.00"),
            filled(records[0], "A", "A2", 2, "60.00"),  # 同一道题出现两次
        )),
    )
    subject = subject_for(records=records, paper_view=paper(sections))
    result = run(subject)

    assert result.valid is False
    assert {"duplicate_exercise", "exercise_family_conflict"} <= codes(result)
    duplicate = issue(result, "duplicate_exercise")
    assert duplicate.severity == "error"
    assert duplicate.detail["occurrences"] == "1,2"
    assert issue(result, "exercise_family_conflict").slot_no == 2


# ---------------------------------------------------------------------------
# 二、蓝图覆盖：题型 / 概念 / 学习目标 / 必选 / 排除
# ---------------------------------------------------------------------------

def test_blueprint_coverage_reports_missing_concepts_outcomes_and_kind() -> None:
    concept = uuid4()
    outcome = uuid4()
    content = blueprint(sections=(
        BlueprintSection(
            code="A", title="基础", instruction="写出完整证明",
            slots=(
                bp_slot("A1", "40.00", required_concept_ids=(concept,),
                        learning_outcome_ids=(outcome,), exercise_kinds=("proof",)),
                bp_slot("A2", "60.00"),
            ),
        ),
    ))
    records = (
        make_record("ex-1", kind="calculation", concepts=(), outcomes=()),
        make_record("ex-2"),
    )
    subject = subject_for(content=content, records=records)
    result = run(subject)

    assert result.valid is False
    assert {"concept_coverage_missing", "outcome_coverage_missing",
            "exercise_kind_mismatch"} <= codes(result)
    concept_gap = issue(result, "concept_coverage_missing")
    assert concept_gap.slot_no == 1
    assert str(concept) in concept_gap.detail["missing"]
    assert "proof" in issue(result, "exercise_kind_mismatch").message


def test_missing_exercise_kind_is_unknown_not_a_silent_pass() -> None:
    # 本仓库 DB source 没有题型列，生产候选的 kind 恒为 "unknown"。
    # 缺数据不能被当成"满足蓝图题型"：蓝图一旦要求题型，就按硬约束阻断；
    # 但也不要伪装成"题型错误"——原因码要能让教师知道是缺数据。
    content = blueprint(sections=(
        BlueprintSection(
            code="A", title="基础", instruction="写出完整证明",
            slots=(
                bp_slot("A1", "40.00", exercise_kinds=("proof",)),
                bp_slot("A2", "60.00"),
            ),
        ),
    ))
    records = (make_record("ex-1", kind="unknown"), make_record("ex-2"))
    result = run(subject_for(content=content, records=records))

    assert result.valid is False
    assert "exercise_kind_unknown" in codes(result)
    assert issue(result, "exercise_kind_unknown").severity == "error"
    assert "exercise_kind_mismatch" not in codes(result)


def test_mandatory_and_excluded_rules_are_enforced_per_slot() -> None:
    records = (make_record("ex-1"), make_record("ex-2"), make_record("ex-3"))
    ex3 = records[2].evidence.exercise_version_id
    ex2 = records[1].evidence.exercise_version_id
    content = blueprint(sections=(
        BlueprintSection(
            code="A", title="基础", instruction="写出完整证明",
            slots=(
                bp_slot("A1", "40.00"),
                bp_slot("A2", "60.00",
                        mandatory_exercise_version_ids=(ex3,),
                        excluded_exercise_version_ids=(ex2,)),
            ),
        ),
    ))
    subject = subject_for(content=content, records=records)
    result = run(subject)

    assert result.valid is False
    assert {"mandatory_exercise_missing", "excluded_exercise",
            "slot_not_mandatory"} <= codes(result)
    assert issue(result, "excluded_exercise").slot_no == 2
    assert issue(result, "mandatory_exercise_missing").detail["missing_version"] == str(ex3)


# ---------------------------------------------------------------------------
# 三、时长：缺失不是 0
# ---------------------------------------------------------------------------

def test_duration_required_treats_missing_seconds_as_unknown_not_zero() -> None:
    content = blueprint(hard_constraints={
        "duration_required": True,
        "duration_max_seconds": 1000,
    })
    records = (make_record("ex-1"), make_record("ex-2", estimated_seconds=None))
    sections = (
        section("A", (
            filled(records[0], "A", "A1", 1, "40.00"),
            filled(records[1], "A", "A2", 2, "60.00"),
        )),
    )
    result = run(subject_for(content=content, records=records,
                             paper_view=paper(sections)))

    assert result.valid is False
    assert "estimated_seconds_missing" in codes(result)
    # 缺失时不得虚构总时长结论：不能报"超时"。
    assert "duration_exceeded" not in codes(result)


def test_duration_exceeded_and_unknown_warning_are_distinct() -> None:
    content = blueprint(hard_constraints={"duration_max_seconds": 1000})
    records = (make_record("ex-1"), make_record("ex-2"))
    exceeded = run(subject_for(content=content, records=records))
    assert "duration_exceeded" in codes(exceeded)
    assert issue(exceeded, "duration_exceeded").detail["actual"] == "1200"
    assert issue(exceeded, "duration_exceeded").detail["maximum"] == "1000"

    unknown = run(subject_for(
        content=blueprint(),
        records=(make_record("ex-1"), make_record("ex-2", estimated_seconds=None)),
        paper_view=paper((section("A", (
            filled(make_record("ex-1", estimated_seconds=None), "A", "A1", 1, "40.00"),
            filled(make_record("ex-2", estimated_seconds=None), "A", "A2", 2, "60.00"),
        )),)),
    ))
    assert issue(unknown, "estimated_seconds_unknown").severity == "warning"
    assert unknown.valid is True


# ---------------------------------------------------------------------------
# 四、当前资格 / 曝光：快照合格不等于现在还合格
# ---------------------------------------------------------------------------

def test_current_revocation_and_released_answer_block_validation() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    subject = subject_for(records=records)
    revoked = RejectedCandidate(
        exercise_version_id=records[0].evidence.exercise_version_id,
        exercise_id=records[0].evidence.exercise_id,
        exercise_family_id=records[0].evidence.exercise_family_id,
        review_status="retired",
        reasons=("revoked",),
    )
    released = ExposureEvent(
        exercise_version_id=records[1].evidence.exercise_version_id,
        exercise_instance_id=None,
        exercise_family_id=records[1].evidence.exercise_family_id,
        scope_offering_id=OFFERING_ID,
        event_kind="practice_answer_released",
        visible_from=NOW - timedelta(days=1),
        occurred_at=NOW - timedelta(days=1),
    )
    preview = preview_for(
        subject,
        accepted=(records[1],),
        rejected=(revoked,),
        events=(released,),
    )
    result = run(subject, preview=preview)

    assert result.valid is False
    ineligible = issue(result, "candidate_ineligible")
    assert ineligible.slot_no == 1
    assert "revoked" in ineligible.detail["reasons"]
    conflict = issue(result, "exposure_conflict")
    assert conflict.severity == "error"
    assert "practice_answer_released" in conflict.message


def test_release_not_frozen_blocks_a_formal_paper() -> None:
    subject = subject_for()
    result = run(subject, preview=preview_for(
        subject, release=release_info(status="open")))

    assert result.valid is False
    issue_item = issue(result, "release_scope_mismatch")
    assert issue_item.detail["reasons"] == "release_not_frozen"


# ---------------------------------------------------------------------------
# 五、来源完整性 / 快照固定 / manifest 防篡改
# ---------------------------------------------------------------------------

def test_source_integrity_requires_all_six_fields_and_the_paper_release() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    a1 = filled(records[0], "A", "A1", 1, "40.00").model_copy(
        update={"source_release_id": None, "content_hash": None})
    a1 = a1.model_copy(update={"content_hash": slot_content_hash(a1)})
    a2 = filled(records[1], "A", "A2", 2, "60.00").model_copy(
        update={"release_context_id": OTHER_CONTEXT, "content_hash": None})
    a2 = a2.model_copy(update={"content_hash": slot_content_hash(a2)})
    result = run(subject_for(records=records,
                             paper_view=paper((section("A", (a1, a2)),))))

    assert result.valid is False
    missing = issue(result, "source_incomplete")
    assert missing.slot_no == 1
    assert "source_release_id" in missing.detail["missing"].split(",")
    assert "release_context_mismatch" in codes(result)
    # 槽位来源与快照证据不一致：不能只信槽位自己写的值。
    mismatch = next(
        item for item in result.issues
        if item.code == "candidate_snapshot_evidence_mismatch" and item.slot_no == 2
    )
    assert mismatch.slot_no == 2
    assert "release_context_id" in mismatch.detail["fields"]


def test_slot_from_another_snapshot_is_rejected() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    stranger = make_record("ex-stranger")
    a2 = filled(stranger, "A", "A2", 2, "60.00")
    subject = subject_for(records=records,
                          paper_view=paper((section("A", (
                              filled(records[0], "A", "A1", 1, "40.00"), a2)),)))
    result = run(subject)

    assert "candidate_not_in_snapshot" in codes(result)
    assert issue(result, "candidate_not_in_snapshot").slot_no == 2


def test_manifest_tamper_is_detected_after_direct_database_edit() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    original = paper(default_sections(records))
    # 直接改库：分值从 60 改成 30，但 manifest_hash / section_score / content_hash
    # 都还是篡改前的值（这些列不会自动重算）。
    section_view = original.sections[0]
    tampered_slot = section_view.slots[1].model_copy(update={"score": Decimal("30.00")})
    tampered_section = section_view.model_copy(
        update={"slots": (section_view.slots[0], tampered_slot)})
    tampered = original.model_copy(update={"sections": (tampered_section,)})
    result = run(subject_for(records=records, paper_view=tampered))

    assert result.valid is False
    assert "manifest_hash_mismatch" in codes(result)
    assert result.recomputed_manifest_hash != tampered.manifest_hash
    assert "slot_content_hash_mismatch" in codes(result)
    assert "section_score_mismatch" in codes(result)


def test_tampered_content_hash_is_detected_even_if_section_hash_is_forged() -> None:
    """同时伪造槽位哈希、分节哈希和 manifest，仍要靠"从字段重算"发现。

    这是比"只改分值"更阴的篡改：攻击者把 `paper_slot.content_hash` 换成任意值，
    再用它重算分节哈希与 manifest，让三层哈希互相自洽。校验器不接受"哈希说自己
    对"，只接受"槽位字段重算出来的哈希"。
    """
    records = (make_record("ex-1"), make_record("ex-2"))
    original = paper(default_sections(records))
    section_view = original.sections[0]
    bad_slot = section_view.slots[1].model_copy(update={"content_hash": "0" * 64})
    bad_section = section_view.model_copy(update={"slots": (section_view.slots[0], bad_slot)})
    bad_section = bad_section.model_copy(
        update={"content_hash": section_content_hash(bad_section)})
    # 先按篡改后的内容重算 manifest，让 manifest_hash 是"自洽"的；
    # 只有重新计算槽位内容哈希才能发现这一处篡改。
    forged = original.model_copy(update={"sections": (bad_section,)})
    forged = forged.model_copy(update={"manifest_hash": paper_manifest(forged)})
    result = run(subject_for(records=records, paper_view=forged))

    assert result.valid is False
    assert "slot_content_hash_mismatch" in codes(result)
    # 重算 manifest 用的是重算后的槽位哈希，所以自洽的伪造 manifest 也会对不上。
    assert "manifest_hash_mismatch" in codes(result)
    assert result.recomputed_manifest_hash != forged.manifest_hash


def test_blueprint_and_snapshot_hashes_are_recomputed() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    subject = subject_for(records=records)
    assert run(subject).valid is True

    wrong_blueprint = ValidationSubject(
        paper=subject.paper,
        blueprint=subject.blueprint,
        blueprint_content_hash="0" * 64,
        snapshot=subject.snapshot,
        course_ref=COURSE,
        offering_ref=OFFERING,
    )
    assert "blueprint_content_hash_mismatch" in codes(run(wrong_blueprint))

    tampered_snapshot = subject.snapshot.model_copy(
        update={"candidate_manifest_hash": "0" * 64})
    wrong_snapshot = ValidationSubject(
        paper=subject.paper,
        blueprint=subject.blueprint,
        blueprint_content_hash=subject.blueprint_content_hash,
        snapshot=tampered_snapshot,
        course_ref=COURSE,
        offering_ref=OFFERING,
    )
    assert "candidate_snapshot_hash_mismatch" in codes(run(wrong_snapshot))


def test_snapshot_from_another_blueprint_is_rejected() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    content = blueprint()
    snapshot = make_snapshot(content, records, blueprint_version_id=uuid4())
    subject = subject_for(content=content, records=records, snapshot=snapshot)
    result = run(subject)

    assert "candidate_snapshot_scope_mismatch" in codes(result)
    assert "blueprint_version_id" in issue(
        result, "candidate_snapshot_scope_mismatch").detail["fields"]


# ---------------------------------------------------------------------------
# 六、report_hash 的规范化语义
# ---------------------------------------------------------------------------

def test_report_hash_ignores_the_clock_but_tracks_the_verdict() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    subject = subject_for(records=records)
    later = NOW + timedelta(minutes=30)
    # 未来才可见的曝光事件：两个时刻都还看不见，结论必须相同。
    future = ExposureEvent(
        exercise_version_id=records[1].evidence.exercise_version_id,
        exercise_instance_id=None,
        exercise_family_id=records[1].evidence.exercise_family_id,
        scope_offering_id=OFFERING_ID,
        event_kind="practice_answer_released",
        visible_from=NOW + timedelta(hours=2),
        occurred_at=NOW,
    )
    preview = preview_for(subject, events=(future,))
    assert run(subject, preview=preview, now=NOW).report_hash == \
        run(subject, preview=preview, now=later).report_hash

    # 结论变了（出现 exposure_conflict），报告哈希必须变。
    visible = future.model_copy(update={"visible_from": NOW - timedelta(minutes=1)})
    changed = run(subject, preview=preview_for(subject, events=(visible,)), now=later)
    assert changed.report_hash != run(subject, preview=preview).report_hash


def test_issue_shape_and_order_are_machine_readable() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    sections = (
        section("A", (
            empty("A", "A1", 1, "40.00"),
            filled(records[1], "A", "A2", 2, "59.99"),
        )),
    )
    result = run(subject_for(records=records, paper_view=paper(sections)))

    required = {"code", "severity", "slot_no", "message"}
    assert required <= set(ValidationIssue.model_fields)
    for item in result.issues:
        assert item.code and item.message
        assert item.severity in {"error", "warning"}
        assert item.slot_no is None or item.slot_no >= 1
    ranks = [0 if item.severity == "error" else 1 for item in result.issues]
    assert ranks == sorted(ranks), "错误必须排在警告之前，顺序才可稳定复现"
    assert result.error_count + result.warning_count == len(result.issues)


# ---------------------------------------------------------------------------
# 七、只读与装配
# ---------------------------------------------------------------------------

def test_service_rereads_the_persisted_rows_on_every_call() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    subject = subject_for(records=records)
    repo = InMemoryPaperValidationRepository(subject)
    service = PaperValidationService(
        repository=repo,
        candidates=SimpleNamespace(
            preview=lambda **_: preview_for(subject),
        ),
        authorizer=SimpleNamespace(
            require=lambda *args, **kwargs: None,
            is_human=lambda actor_id: True,
        ),
        clock=lambda: NOW,
    )
    first = service.validate(actor_id=ACTOR_ID, paper_version_id=subject.paper.id)
    assert first.valid is True

    # 模拟"有人绕过服务直接改库"：仓储里的卷面变了，第二次校验必须重新读。
    tampered_section = subject.paper.sections[0].model_copy(update={"section_score": Decimal("1.00")})
    repo.subject = ValidationSubject(
        paper=subject.paper.model_copy(update={"sections": (tampered_section,)}),
        blueprint=subject.blueprint,
        blueprint_content_hash=subject.blueprint_content_hash,
        snapshot=subject.snapshot,
        course_ref=COURSE,
        offering_ref=OFFERING,
    )
    second = service.validate(actor_id=ACTOR_ID, paper_version_id=subject.paper.id)
    assert second.valid is False
    assert "section_score_mismatch" in codes(second)

    # 只读端口：没有 add/save/update 这类写方法。
    for name in ("add", "save", "save_edit", "update", "delete"):
        assert not hasattr(repo, name)


def test_assert_valid_raises_with_the_full_issue_report() -> None:
    records = (make_record("ex-1"), make_record("ex-2"))
    sections = default_sections(records, scores=("40.00", "59.99"))
    subject = subject_for(records=records, paper_view=paper(sections))
    repo = InMemoryPaperValidationRepository(subject)
    service = PaperValidationService(
        repository=repo,
        candidates=SimpleNamespace(preview=lambda **_: preview_for(subject)),
        authorizer=SimpleNamespace(require=lambda *args, **kwargs: None,
                                   is_human=lambda actor_id: True),
        clock=lambda: NOW,
    )
    try:
        service.assert_valid(actor_id=ACTOR_ID, paper_version_id=subject.paper.id)
    except PaperValidationBlockedError as exc:
        assert exc.result.valid is False
        assert "total_score_mismatch" in codes(exc.result)
    else:
        raise AssertionError("无效卷没有被门禁拦下")

    missing = uuid4()
    try:
        service.validate(actor_id=ACTOR_ID, paper_version_id=missing)
    except PaperNotFoundError:
        pass
    else:
        raise AssertionError("不存在的卷没有抛 PaperNotFoundError")


def test_validation_module_is_read_only_and_needs_no_new_migration() -> None:
    source = VALIDATION_SOURCE.read_text(encoding="utf-8")
    assert "db.transaction(assessment_service=True)" in source
    for table in (
        "assessment.paper_version",
        "assessment.paper_section",
        "assessment.paper_slot",
        "assessment.paper_blueprint_version",
        "assessment.candidate_snapshot",
        "assessment.id_ref",
    ):
        assert table in source, f"读取清单缺 {table}"
    for forbidden in ("INSERT INTO", "UPDATE assessment", "DELETE FROM",
                      "ALTER TABLE", "CREATE TABLE"):
        assert forbidden not in source, f"校验器不应写库：{forbidden}"
    # 校验器自己不建表：0031 是渲染任务类型（任务 6），与校验无关。
    # 这里检查的是「没有任何迁移为校验报告建结构」，而不是「迁移数量停在某个数字」——
    # 后者会在每个后续任务里误报，且它本来也不是任务 5 的不变量。
    #
    # 0032 给 review_round 加了 validation_report_hash：那是任务 7 的**复核证据
    # 冻结**（只存一个 SHA-256），不是把 issue 明细/报告正文落库。因此这里禁的是
    # 报告结构（表、issue 行、报告载荷），允许这个哈希列。
    migrations = sorted(path.name for path in MIGRATION_DIR.glob("*.sql"))
    later = [name for name in migrations if name > "0030"]
    for name in later:
        text = (MIGRATION_DIR / name).read_text(encoding="utf-8").lower()
        for forbidden in ("validation_issue", "paper_validation", "report_payload"):
            assert forbidden not in text, f"{name} 为校验报告建了结构：{forbidden}"
        for line in text.splitlines():
            if "create table" in line and "validation" in line:
                raise AssertionError(f"{name} 为校验报告建了结构：{line.strip()}")


def test_required_source_fields_are_the_six_fixed_fields() -> None:
    assert REQUIRED_SOURCE_FIELDS == (
        "exercise_id",
        "exercise_version_id",
        "exercise_family_id",
        "source_release_id",
        "release_context_id",
        "dependency_manifest_hash",
    )


# ---------------------------------------------------------------------------
# 八、HTTP 契约
# ---------------------------------------------------------------------------

class FakeValidationService:
    def __init__(self, result, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def validate(self, *, actor_id, paper_version_id):  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        if paper_version_id != self.result.paper_version_id:
            raise PaperNotFoundError(str(paper_version_id))
        return self.result


def http_client(service) -> TestClient:  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_validation_service] = lambda: service
    return TestClient(app)


def test_validation_route_returns_the_report_without_idempotency_key() -> None:
    result = run(subject_for())
    with http_client(FakeValidationService(result)) as http:
        response = http.post(
            f"/api/v1/paper-versions/{result.paper_version_id}/validate",
            headers=TEACHER_HEADERS,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["issues"] == []
    assert body["report_hash"] == result.report_hash
    assert body["manifest_hash"] == result.manifest_hash
    assert body["recomputed_manifest_hash"] == result.recomputed_manifest_hash


def test_validation_route_permissions_and_errors() -> None:
    result = run(subject_for())
    with http_client(FakeValidationService(result)) as http:
        student = http.post(
            f"/api/v1/paper-versions/{result.paper_version_id}/validate",
            headers={"X-Actor": "stu-1", "X-Roles": "student"},
        )
        anonymous = http.post(
            f"/api/v1/paper-versions/{result.paper_version_id}/validate")
    assert student.status_code == 403, student.text
    assert anonymous.status_code == 401, anonymous.text

    with http_client(FakeValidationService(result, error=PaperNotFoundError("nope"))) as http:
        missing = http.post(
            f"/api/v1/paper-versions/{uuid4()}/validate",
            headers=TEACHER_HEADERS,
        )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "paper_not_found"

    with http_client(FakeValidationService(result,
                                           error=ValidationInputError("bad scope"))) as http:
        invalid = http.post(
            f"/api/v1/paper-versions/{result.paper_version_id}/validate",
            headers=TEACHER_HEADERS,
        )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "validation_input_mismatch"


def test_shared_material_and_mutual_leak_groups_block() -> None:
    """蓝图硬约束分组：同一组里两道题同时进卷必须报，而不是只看单题资格。"""
    records = (make_record("ex-1"), make_record("ex-2"))
    versions = [str(record.evidence.exercise_version_id) for record in records]
    content = blueprint(hard_constraints={
        "conflict_groups": [versions],
        "leak_groups": [versions],
    })
    result = run(subject_for(content=content, records=records))

    assert result.valid is False
    assert "shared_material_conflict" in codes(result)
    assert "mutual_answer_leak" in codes(result)
    assert issue(result, "shared_material_conflict").detail["versions"] == ",".join(
        sorted(versions))


def test_dependency_manifest_change_after_snapshot_blocks() -> None:
    """快照之后题目依赖变了：槽位固定的依赖清单哈希与当前证据不一致。"""
    records = (make_record("ex-1"), make_record("ex-2"))
    current_evidence = records[0].evidence.model_copy(
        update={"dependency_manifest_hash": canonical_hash({"changed": True})})
    current = (
        records[0].model_copy(update={"evidence": current_evidence}),
        records[1],
    )
    subject = subject_for(records=records)
    result = run(subject, preview=preview_for(subject, accepted=current))

    assert result.valid is False
    mismatch = issue(result, "dependency_manifest_mismatch")
    assert mismatch.slot_no == 1
    assert mismatch.detail["stored"] != mismatch.detail["current"]


def test_blueprint_scope_and_purpose_must_match_the_paper() -> None:
    """蓝图被换成了另一门课/另一个用途：不能只比 slot 覆盖，身份也要对齐。"""
    content = blueprint().model_copy(update={
        "purpose": "practice",
        "course_id": uuid4(),
    })
    result = run(subject_for(content=content))

    assert result.valid is False
    assert "blueprint_purpose_mismatch" in codes(result)
    assert "blueprint_scope_mismatch" in codes(result)
    assert "course_id" in issue(result, "blueprint_scope_mismatch").detail["fields"]


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
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 独立整卷校验器测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
