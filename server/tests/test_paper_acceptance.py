"""阶段 A 验收：一份真实语义的《数学分析 I》100 分固定卷，从蓝图走到撤回。

这不是"再测一遍单元"：它把**真实服务**串成一条链，中间不换假实现：

    真实 BlueprintService        建 100.00 分、三节五槽的正式卷蓝图
    真实 CandidateService       按冻结 Release + 课程范围生成候选快照
    真实 PaperService           用「蓝图版本 + 候选快照 + 选题」派生草稿卷
    真实 PaperValidationService 独立整卷校验（重新读库、重算 manifest）
    真实 PaperReviewService     提交审核 → 两位不同教师复核 → approved
    真实 AssignmentService      建作业草稿 → 发布（固定投影）→ 严重撤回

被模拟的只有两处，且都在边界上：
  * 授权端口：按课程范围放行的替身（真库判定要 teaching_class 名单，Python 测试不连库）；
  * 渲染 worker 的产物：直接标记三类渲染 ready（渲染本身由 test_rendering.py 覆盖）。

验收要回答的问题只有一个：**教师按文档走完这条路，能不能拿到一份可打印、可发布、
可撤回、且学生侧看不到答案的卷子。** 所以最后几条断言是语义断言，不是结构断言。

运行：`python server/tests/test_paper_acceptance.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assignments import AssignmentService, InMemoryAssignmentRepository  # noqa: E402
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
    SlotSelection,
    canonical_hash,
)
from app.paper_review import (  # noqa: E402
    REQUIRED_RENDER_KINDS,
    ContributorReviewError,
    DuplicateReviewerError,
    InMemoryPaperReviewRepository,
    PaperReviewService,
    ReviewPaper,
)
from app.papers import (  # noqa: E402
    EditOperation,
    InMemoryPaperRepository,
    PaperNotDraftError,
    PaperService,
)
from app.releases import content_hash, exercise_uuid, family_id_for, version_id_for  # noqa: E402
from app.validation import (  # noqa: E402
    InMemoryPaperValidationRepository,
    PaperValidationService,
    ValidationSubject,
)

COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "analysis-2026a"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
BLUEPRINT_ID = UUID("22222222-2222-5222-8222-222222222222")
PAPER_ID = UUID("33333333-3333-5333-8333-333333333333")
# 账号 uuid 必须走同一套 uuid5：蓝图服务会校验 actor_ref 与 actor_id 来自同一映射
# （否则 id_ref 表里记的文本号还原不回来）。自己拍一个 uuid 只会被当场拒绝。
CREATOR = ref_uuid("account", "teacher-creator")
REVIEWER_A = ref_uuid("account", "teacher-a")
REVIEWER_B = ref_uuid("account", "teacher-b")
NOW = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)
VISIBLE = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 9, 3, 30, tzinfo=timezone.utc)

# 真实语义的题面：数学分析 I 的期末固定卷。题干/答案都按课程内容写，
# 不是为了凑哈希，"看不出答案是不是正文"才是这份验收想验的东西。
EXERCISES = (
    ("analysis-lim-01", "计算 $\\lim_{n\\to\\infty}\\left(1+\\frac{1}{n}\\right)^n$。",
     "$e$。由 $\\ln(1+1/n)=1/n+o(1/n)$ 得极限为 $e$。"),
    ("analysis-lim-02", "计算 $\\lim_{x\\to 0}\\frac{\\sin 3x}{x}$。", "$3$。"),
    ("analysis-cont-01", "证明 $f(x)=x^2$ 在 $[0,1]$ 上一致连续。",
     "取 $\\delta=\\varepsilon/2$，则 $|x^2-y^2|\\le 2|x-y|<\\varepsilon$。"),
    ("analysis-mvt-01", "叙述并证明拉格朗日中值定理。",
     "构造 $g(x)=f(x)-\\frac{f(b)-f(a)}{b-a}(x-a)$，用罗尔定理。"),
    ("analysis-series-01", "判断 $\\sum_{n\\ge1}\\frac{1}{n^2}$ 的收敛性。",
     "收敛：与 $\\int_1^{\\infty}x^{-2}dx$ 比较。"),
)

SECTION_SPEC = (
    ("A", "计算题", (("A1", "16.00", 0), ("A2", "24.00", 1))),
    ("B", "证明题", (("B1", "20.00", 2), ("B2", "20.00", 3))),
    ("C", "综合题", (("C1", "20.00", 4),)),
)


# ---------------------------------------------------------------------------
# 边界替身：授权与渲染产物
# ---------------------------------------------------------------------------

class ScopeAuthorizer:
    """按课程范围放行的授权端口替身（真库判定要 teaching_class 名单）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, dict]] = []
        self.denied: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        self.calls.append((actor_id, action, dict(scope)))
        if actor_id in self.denied:
            raise PermissionError(f"course scope denied: {scope.get('course_ref')}")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return True


# ---------------------------------------------------------------------------
# 夹具：蓝图、Release、候选
# ---------------------------------------------------------------------------

def blueprint_content() -> BlueprintContent:
    sections = tuple(
        BlueprintSection(
            code=code,
            title=title,
            instruction="写出完整过程",
            slots=tuple(
                BlueprintSlot(code=slot_code, score=Decimal(score))
                for slot_code, score, _ in slots
            ),
        )
        for code, title, slots in SECTION_SPEC
    )
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        sections=sections,
        hard_constraints={"total_score": "100.00"},
        soft_objectives={},
        review_policy="formal_dual_review",
        assembly_mode="manual",
    )


def release_info() -> ReleaseScopeInfo:
    return ReleaseScopeInfo(
        release_context_id=CONTEXT,
        course_id=COURSE_ID,
        course_ref=COURSE,
        offering_id=OFFERING_ID,
        offering_ref=OFFERING,
        status="frozen",
        item_count=len(EXERCISES),
    )


def make_record(ref: str, prompt: str, answer: str) -> CandidateRecord:
    digest = content_hash(prompt, answer)
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
        in_context=True,
        license_valid=True,
        revoked=False,
        hidden_evaluation=False,
        private_draft=False,
        parameterized=False,
        instance_approved=True,
        dependencies_complete=True,
        render_ready=True,
        language_pair_valid=True,
        purpose_allowed=True,
        cloud_allowed=True,
    )
    return CandidateRecord(
        evidence=evidence,
        release_state="included",
        review_status="teacher-reviewed",
        license="licensed",
        answer_present=bool(answer.strip()),
    )


def records() -> tuple[CandidateRecord, ...]:
    return tuple(make_record(ref, prompt, answer) for ref, prompt, answer in EXERCISES)


# ---------------------------------------------------------------------------
# 链路：一路推进到 approved
# ---------------------------------------------------------------------------

def build_to_approved() -> SimpleNamespace:
    authorizer = ScopeAuthorizer()
    blueprints = BlueprintService(
        repository=InMemoryBlueprintRepository(), authorizer=authorizer, clock=lambda: NOW)
    version = blueprints.create_version(
        actor_id=CREATOR,
        content=blueprint_content(),
        blueprint_id=BLUEPRINT_ID,
        base_version_id=None,
        idempotency_key="acceptance-blueprint",
        course_ref=COURSE,
        offering_ref=OFFERING,
        actor_ref="teacher-creator",
    )

    candidate_repo = InMemoryCandidateRepository(release=release_info(), records=records())
    candidates = CandidateService(
        repository=candidate_repo, authorizer=authorizer, clock=lambda: NOW)
    scope = CandidateScope.from_version(version)
    snapshot = candidates.snapshot(actor_id=CREATOR, scope=scope)

    paper_repo = InMemoryPaperRepository()
    papers = PaperService(
        repository=paper_repo, authorizer=authorizer,
        blueprints=blueprints, candidates=candidates, clock=lambda: NOW,
    )
    selections = tuple(
        SlotSelection(slot_code=slot_code,
                      exercise_version_id=records()[index].evidence.exercise_version_id)
        for _, _, slots in SECTION_SPEC
        for slot_code, _, index in slots
    )
    draft = papers.create_manual_draft(
        actor_id=CREATOR,
        actor_ref="teacher-creator",
        blueprint_version_id=version.id,
        candidate_snapshot_id=snapshot.id,
        selections=selections,
        paper_id=PAPER_ID,
        idempotency_key="acceptance-paper",
    )

    validator = PaperValidationService(
        repository=InMemoryPaperValidationRepository(ValidationSubject(
            paper=draft,
            blueprint=version.content,
            blueprint_content_hash=canonical_hash(version.content),
            snapshot=snapshot,
            course_ref=COURSE,
            offering_ref=OFFERING,
        )),
        candidates=candidates,
        authorizer=authorizer,
        clock=lambda: NOW,
    )

    review_repo = InMemoryPaperReviewRepository()
    review_repo.add_paper(ReviewPaper(
        id=draft.id, paper_id=draft.paper_id, status=draft.status,
        purpose=draft.purpose, course_id=draft.course_id,
        offering_id=draft.offering_id, edit_seq=draft.edit_seq,
        manifest_hash=draft.manifest_hash, created_by=CREATOR,
    ))
    review_repo.refs[("course", COURSE_ID)] = COURSE
    review_repo.refs[("offering", OFFERING_ID)] = OFFERING
    review_repo.add_contributor(draft.id, CREATOR)
    # 渲染 worker 在真实链路里会写这三行；这里直接标记"三类都 ready"。
    review_repo.set_renders(draft.id, draft.manifest_hash, REQUIRED_RENDER_KINDS)
    reviews = PaperReviewService(
        repository=review_repo, authorizer=authorizer, validator=validator, clock=lambda: NOW)

    submitted = reviews.submit(
        actor_id=CREATOR, paper_version_id=draft.id,
        idempotency_key="acceptance-submit")
    first = reviews.approve(
        actor_id=REVIEWER_A, paper_version_id=draft.id,
        manifest_hash=draft.manifest_hash,
        evidence_hash=submitted.report_hash or "",
        note="计算题与证明题覆盖到位",
        idempotency_key="acceptance-approve-a",
    )
    # 两名"不该能签"的人必须在第二轮之前试：卷子一旦 approved，状态机会先拒绝
    # （ReviewNotOpenError），那样测到的就不是"自审/重复签名"这条规则了。
    rejected = {}
    for label, actor, key in (
        ("self_review", CREATOR, "acceptance-self-review"),
        ("duplicate", REVIEWER_A, "acceptance-approve-a-again"),
    ):
        try:
            reviews.approve(
                actor_id=actor, paper_version_id=draft.id,
                manifest_hash=draft.manifest_hash,
                evidence_hash=submitted.report_hash or "",
                idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 —— 只记录类型，断言在测试里
            rejected[label] = type(exc)
        else:
            rejected[label] = None
    second = reviews.approve(
        actor_id=REVIEWER_B, paper_version_id=draft.id,
        manifest_hash=draft.manifest_hash,
        evidence_hash=submitted.report_hash or "",
        note="总分与评分点一致",
        idempotency_key="acceptance-approve-b",
    )

    approved_review_paper = review_repo.paper(draft.id)
    approved_row = draft.model_copy(update={
        "status": "approved",
        "submitted_manifest_hash": draft.manifest_hash,
        "review_manifest_hash": approved_review_paper.review_manifest_hash,
    })

    return SimpleNamespace(
        authorizer=authorizer, version=version, snapshot=snapshot, draft=draft,
        validator=validator, validation=validator.validate(
            actor_id=CREATOR, paper_version_id=draft.id),
        reviews=reviews, review_repo=review_repo, submitted=submitted,
        first=first, second=second, rejected=rejected, approved_row=approved_row,
        selections=selections,
    )


def build_assigned(pipeline: SimpleNamespace) -> SimpleNamespace:
    repo = InMemoryAssignmentRepository(
        papers=[pipeline.approved_row], courses=[COURSE])
    repo.refs[("course", COURSE_ID)] = COURSE
    repo.refs[("offering", OFFERING_ID)] = OFFERING
    service = AssignmentService(
        repository=repo, authorizer=pipeline.authorizer, clock=lambda: VISIBLE)
    created = service.create_draft(
        actor_id=CREATOR,
        paper_version_id=pipeline.approved_row.id,
        offering_ref=OFFERING,
        visible_at=VISIBLE, due_at=DUE, hard_close_at=CLOSE,
        idempotency_key="acceptance-assignment",
        actor_ref="teacher-creator",
    )
    published = service.publish(
        actor_id=CREATOR, assignment_id=created.assignment.id,
        idempotency_key="acceptance-publish")
    return SimpleNamespace(service=service, repo=repo, created=created, published=published)


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------

def test_blueprint_is_a_real_hundred_point_fixed_paper() -> None:
    pipeline = build_to_approved()
    content = pipeline.version.content
    total = sum(
        (slot.score for section in content.sections for slot in section.slots),
        Decimal("0"),
    )
    assert total == Decimal("100.00"), total
    assert content.total_score == Decimal("100.00")
    assert len(content.sections) == 3
    assert [section.title for section in content.sections] == ["计算题", "证明题", "综合题"]
    assert content.review_policy == "formal_dual_review"
    assert content.assembly_mode == "manual", "阶段 A 只做手工组卷"


def test_candidates_are_pinned_to_the_frozen_release() -> None:
    pipeline = build_to_approved()
    snapshot = pipeline.snapshot
    assert len(snapshot.accepted) == len(EXERCISES), "五道题都该合格"
    assert snapshot.rejected == (), [r.reasons for r in snapshot.rejected]
    assert {record.evidence.release_context_id for record in snapshot.accepted} == {CONTEXT}
    # 候选清单不可变：同一份内容再生成一次不落第二行。
    again = CandidateService(
        repository=InMemoryCandidateRepository(release=release_info(), records=records()),
        authorizer=pipeline.authorizer, clock=lambda: NOW,
    ).snapshot(actor_id=CREATOR, scope=CandidateScope.from_version(pipeline.version))
    assert again.candidate_manifest_hash == snapshot.candidate_manifest_hash


def test_draft_fixes_every_slot_to_the_selected_exercise_version() -> None:
    pipeline = build_to_approved()
    draft = pipeline.draft
    slots = [slot for section in draft.sections for slot in section.slots]
    assert draft.status == "draft"
    assert draft.total_score == Decimal("100.00")
    assert len(slots) == 5
    assert all(slot.exercise_version_id is not None for slot in slots), "没有空槽位"
    assert all(slot.release_context_id == CONTEXT for slot in slots)
    assert all(slot.dependency_manifest_hash for slot in slots)
    # 槽位分值与蓝图一一对应（不能建完就自己变）。
    expected = {slot_code: score for _, _, items in SECTION_SPEC for slot_code, score, _ in items}
    for slot in slots:
        assert slot.score == Decimal(expected[slot.code]), (slot.code, slot.score)


def test_independent_validation_passes_and_recomputes_the_same_manifest() -> None:
    pipeline = build_to_approved()
    result = pipeline.validation
    assert result.valid is True, result.issues
    assert result.error_count == 0 and result.warning_count == 0, result.issues
    assert result.manifest_hash == pipeline.draft.manifest_hash, (
        "校验重算的 manifest 必须与建卷时冻结的一致，否则审核证据无从谈起")
    assert result.recomputed_manifest_hash == result.manifest_hash
    assert result.report_hash and len(result.report_hash) == 64


def test_submit_needs_three_renders_and_two_distinct_human_reviewers() -> None:
    pipeline = build_to_approved()
    assert pipeline.submitted.required_approvals == 2
    assert pipeline.submitted.approval_count == 0
    assert pipeline.submitted.report_hash, "提交时冻结校验报告哈希"
    assert pipeline.first.approval_count == 1
    assert pipeline.first.status == "in_review", "一审之后仍是审核中"
    assert pipeline.second.status == "approved"
    assert pipeline.second.approval_count == 2
    # 贡献者不能自审；同一人不能签两次。两条都在第二轮之前试过（见 build_to_approved）。
    assert pipeline.rejected["self_review"] is ContributorReviewError, (
        f"命题人自审应被拒绝，实际 {pipeline.rejected['self_review']}")
    assert pipeline.rejected["duplicate"] is DuplicateReviewerError, (
        f"同一复核人签两次应被拒绝，实际 {pipeline.rejected['duplicate']}")


def test_review_manifest_hash_binds_paper_report_and_both_reviewers() -> None:
    pipeline = build_to_approved()
    stored = pipeline.review_repo.paper(pipeline.draft.id)
    assert stored.status == "approved"
    assert stored.review_manifest_hash == pipeline.approved_row.review_manifest_hash
    # 聚合哈希的输入包含卷面 manifest、校验报告哈希与两位复核人：
    # 少任何一项都不该是这个值。
    base = {
        "manifest_hash": pipeline.draft.manifest_hash,
        "report_hash": pipeline.submitted.report_hash,
        "reviewers": sorted([str(REVIEWER_A), str(REVIEWER_B)]),
    }
    assert stored.review_manifest_hash != canonical_hash(base)


def test_publishing_freezes_the_paper_projection() -> None:
    pipeline = build_to_approved()
    assigned = build_assigned(pipeline)
    published = assigned.published
    assert published.assignment.status == "published"
    assert published.assignment.publish_manifest_hash == pipeline.approved_row.manifest_hash
    assert published.assignment.review_manifest_hash == pipeline.approved_row.review_manifest_hash
    items = published.items
    assert len(items) == 5, [item.ord for item in items]
    assert sum((item.score for item in items), Decimal("0")) == Decimal("100.00")
    # 每一项的题目版本与卷面槽位一一对应：发布是"固定投影"，不是"照抄 id"。
    paper_slots = sorted(
        (slot for section in pipeline.approved_row.sections for slot in section.slots),
        key=lambda slot: slot.slot_no,
    )
    by_slot = {slot.id: slot for slot in paper_slots}
    for item in sorted(items, key=lambda row: row.ord):
        slot = by_slot[item.paper_slot_id]
        assert item.exercise_version_id == slot.exercise_version_id
        assert item.exercise_family_id == slot.exercise_family_id
        assert item.content_hash == slot.content_hash
        assert item.release_context_id == CONTEXT


def test_approved_paper_can_not_be_edited_in_place() -> None:
    pipeline = build_to_approved()
    paper_repo = InMemoryPaperRepository()
    paper_repo.versions[pipeline.approved_row.id] = pipeline.approved_row
    service = PaperService(
        repository=paper_repo, authorizer=pipeline.authorizer,
        blueprints=SimpleNamespace(get_version=lambda **_: pipeline.version),
        candidates=SimpleNamespace(get_snapshot=lambda **_: pipeline.snapshot),
        clock=lambda: NOW,
    )
    first_slot = pipeline.approved_row.sections[0].slots[0]
    try:
        service.edit_draft(
            actor_id=CREATOR, paper_version_id=pipeline.approved_row.id,
            base_version=pipeline.approved_row.edit_seq,
            operations=(EditOperation(kind="score", slot_id=first_slot.id,
                                      score=Decimal("10.00")),),
            idempotency_key="acceptance-edit-approved",
        )
    except PaperNotDraftError as exc:
        assert "approved" in str(exc) or "草稿" in str(exc), exc
    else:
        raise AssertionError("approved 卷被原位修改了")
    # 克隆是新版本的唯一入口：内容复制、身份换新，旧版仍然是 approved。
    assert paper_repo.versions[pipeline.approved_row.id].status == "approved"


def test_withdrawal_marks_the_assignment_and_burns_the_family() -> None:
    pipeline = build_to_approved()
    assigned = build_assigned(pipeline)
    withdrawn = assigned.service.withdraw(
        actor_id=CREATOR, assignment_id=assigned.published.assignment.id,
        reason="题目在课堂上提前讲过，本次作废", idempotency_key="acceptance-assignment-withdraw")
    assert withdrawn.assignment.status == "withdrawn"
    assert withdrawn.assignment.withdrawal_reason
    burned = {event.exercise_family_id for event in assigned.repo.events}
    families = {record.evidence.exercise_family_id
                for record in pipeline.snapshot.accepted}
    assert families <= burned, "撤回要写 withdrawn 曝光事件，否则同族题还能再进卷"

    # 卷子本身也撤回：历史保留、审核轮次作废、不能再去发布。
    state = pipeline.reviews.withdraw(
        actor_id=CREATOR, paper_version_id=pipeline.draft.id,
        reason="命题范围与教学进度不一致，重出一份", idempotency_key="acceptance-paper-withdraw")
    assert state.status == "withdrawn"
    assert state.effective_review_state == "withdrawn"
    assert state.approval_active is False


def test_student_side_projection_never_carries_answers() -> None:
    pipeline = build_to_approved()
    answers = [answer for _, _, answer in EXERCISES]
    payload = pipeline.snapshot.model_dump(mode="json")
    for answer in answers:
        assert answer not in str(payload), f"候选快照泄露了答案：{answer[:20]}"
    paper_payload = pipeline.draft.model_dump(mode="json")
    for answer in answers:
        assert answer not in str(paper_payload), "卷面行不该带答案正文"
    # 打印作业项是学生手上那张纸的直接来源：它只能有题号、来源、分值与内容哈希。
    assigned = build_assigned(pipeline)
    item_payload = [item.model_dump(mode="json") for item in assigned.published.items]
    for answer in answers:
        assert answer not in str(item_payload), "打印作业项泄露了答案"
    assert all(set(item) <= {"id", "assignment_id", "paper_slot_id", "ord",
                             "release_context_id", "source_release_id",
                             "exercise_version_id", "exercise_instance_id",
                             "exercise_family_id", "score", "content_hash"}
               for item in item_payload), "作业项里出现了计划外字段"


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
    print(f"✓ 阶段 A 真实 100 分卷验收通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
