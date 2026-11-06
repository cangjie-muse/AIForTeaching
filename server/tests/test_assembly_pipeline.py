"""阶段 B 的"求解之后"整条链：启动运行 → 选择方案 → 落草稿卷 → 阶段 A 独立复验。

被模拟的只有求解器（本机装不上 ortools，见 docs/验证记录.md 的阻塞项）和授权端口
（真库判定要 teaching_class 名单）。其余全是真实服务：

    真实 BlueprintService       建蓝图版本、按 course_ref 判课程范围
    真实 CandidateService       固定候选快照（授权、Release、审核、曝光）
    真实 AssemblyCandidateService 翻译成带哈希的候选 manifest
    真实 PaperService           按方案建草稿卷
    真实 PaperValidationService 独立整卷校验（重新读库、重算 manifest）

要回答的问题：**求解器给出的方案，能不能变成一份阶段 A 认可、可以继续送审的卷子；
以及"没有方案 / 复验不通过"时，系统会不会照样给出卷子。** 所以后半段的断言全是
"拒绝"的断言。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.contracts import (  # noqa: E402
    AssemblySelection,
    RunStatus,
    SolveResult,
    make_solution_manifest,
    solution_from_json,
    solution_hash_matches,
    solution_to_json,
    make_solution_set,
    score_to_hundredths,
    solution_from_json,
)
from app.assembly.materialize import (  # noqa: E402
    AssemblyMaterializer,
    AssemblyValidationBlocked,
)
from app.assembly.repository import (  # noqa: E402
    InMemoryAssemblyRunRepository,
)
from app.assembly.service import (  # noqa: E402
    AssemblyError,
    AssemblyService,
    IdempotencyConflict,
    RunNotFound,
    RunNotSolved,
    SolutionIndexInvalid,
    SolutionMismatch,
)
from app.assembly.solver_port import UnavailableSolver  # noqa: E402
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
    canonical_hash,
)
from app.papers import (  # noqa: E402
    InMemoryPaperRepository,
    PaperService,
)
from app.releases import content_hash, exercise_uuid, family_id_for, version_id_for  # noqa: E402
from app.validation import (  # noqa: E402
    PaperValidationService,
    ValidationSubject,
)

COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "analysis-2026a"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
BLUEPRINT_ID = UUID("22222222-2222-5222-8222-222222222222")
CREATOR = ref_uuid("account", "teacher-creator")
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)

EXERCISES = tuple(
    (f"analysis-assembly-{index}", f"第 {index} 题：计算并说明依据。", f"答案 {index}")
    for index in range(1, 8)
)
SECTION_SPEC = (
    ("A", "计算题", (("A1", "16.00"), ("A2", "24.00"))),
    ("B", "证明题", (("B1", "20.00"), ("B2", "20.00"))),
    ("C", "综合题", (("C1", "20.00"),)),
)


# ---------------------------------------------------------------------------
# 边界替身
# ---------------------------------------------------------------------------

class ScopeAuthorizer:
    """按课程范围放行的授权端口替身（真库要查 teaching_class）。"""

    def __init__(self) -> None:
        self.denied: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if actor_id in self.denied:
            raise PermissionError(f"course scope denied: {scope.get('course_ref')}")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return True


class StubSolver:
    """按蓝图槽位顺序挑候选，返回一个真实的 `SolutionManifest`。

    这不是求解器：它不懂约束。用它的唯一目的是把"求解器给出的方案"这个输入
    喂给真实的落卷与复验链路。它挑出来的方案如果违反约束，阶段 A 的独立复验
    应该拒绝——这正是我们要测的。
    """

    def __init__(self, *, status: RunStatus = RunStatus.OPTIMAL,
                 optimal_proven: bool = True, error: Exception | None = None,
                 drop_solutions: bool = False, pick_offset: int = 0) -> None:
        self.status = status
        self.optimal_proven = optimal_proven
        self.error = error
        self.drop_solutions = drop_solutions
        self.pick_offset = pick_offset
        self.calls = 0

    def solve(self, blueprint, manifest, config, peer=None) -> SolveResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.drop_solutions:
            return SolveResult(self.status, (), {}, None, None)
        selections = []
        for index, slot in enumerate(blueprint.slots):
            candidate = manifest.candidates[(index + self.pick_offset) % len(manifest.candidates)]
            selections.append(AssemblySelection(
                slot_id=slot.slot_id,
                slot_code=slot.code,
                exercise_version_id=candidate.exercise_version_id,
                exercise_instance_id=candidate.exercise_instance_id,
                stable_exercise_id=candidate.stable_exercise_id,
                exercise_family_id=candidate.exercise_family_id,
                source_release_id=candidate.source_release_id,
                decision_seq=candidate.decision_seq,
                score_hundredths=score_to_hundredths(slot.score),
                estimated_seconds=candidate.estimated_seconds,
                difficulty=candidate.difficulty,
                dependency_hash=candidate.dependency_hash,
                content_hash=candidate.content_hash,
            ))
        manifest_value = make_solution_manifest(
            variant_label="single",
            selections=tuple(selections),
            objective_terms=(),
            objective_value=0,
            best_bound=0,
            optimality_gap_milli=0,
            solver_status=self.status,
            optimal_proven=self.optimal_proven,
            structure_report={},
        )
        return SolveResult(
            status=self.status,
            solution_sets=(make_solution_set((manifest_value,)),),
            diagnostics={"stub": True},
            best_bound=0,
            optimality_gap_milli=0,
        )


class LazyValidationRepository:
    """校验仓储的懒加载版：卷子是在 materialize 里才建出来的。"""

    def __init__(self) -> None:
        self.subject: ValidationSubject | None = None

    @contextmanager
    def unit_of_work(self):
        yield self

    def load(self, paper_version_id: UUID):
        if self.subject is not None and self.subject.paper.id == paper_version_id:
            return self.subject
        return None


class PaperProxy:
    """在真实 `PaperService` 外面套一层：建完草稿就把校验主体准备好。

    生产环境里校验仓储直接读库，不需要这一步；内存版没有库，所以由调用方把
    "刚建出来的卷 + 蓝图 + 候选快照"组装成 `ValidationSubject`。
    """

    def __init__(self, papers: PaperService, repo: LazyValidationRepository,
                 candidate_repo: InMemoryCandidateRepository, content: BlueprintContent) -> None:
        self.papers = papers
        self.repo = repo
        self.candidate_repo = candidate_repo
        self.content = content
        self.created: list[UUID] = []

    def create_manual_draft(self, **kwargs):
        draft = self.papers.create_manual_draft(**kwargs)
        snapshot = self.candidate_repo.get(draft.candidate_snapshot_id)
        if snapshot is None:  # pragma: no cover - 真实链路一定有
            raise AssertionError("草稿引用的候选快照不存在：这本身就是一个缺陷")
        # 组卷来源必须真的落到了卷子上（0034 的两列），否则这份卷会自称手工卷。
        assert draft.assembly_run_id == kwargs["assembly_run_id"], (
            "草稿卷必须记录 assembly_run_id：否则回放断链")
        assert draft.selected_solution_index == kwargs["selected_solution_index"]
        self.repo.subject = ValidationSubject(
            paper=draft,
            blueprint=self.content,
            blueprint_content_hash=canonical_hash(self.content),
            snapshot=snapshot,
            course_ref=COURSE,
            offering_ref=OFFERING,
        )
        self.created.append(draft.id)
        return draft


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

def blueprint_content(**changes) -> BlueprintContent:
    sections = tuple(
        BlueprintSection(
            code=code, title=title, instruction="写出完整过程",
            slots=tuple(BlueprintSlot(code=slot_code, score=Decimal(score))
                        for slot_code, score in slots),
        )
        for code, title, slots in SECTION_SPEC
    )
    values: dict = dict(
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
        assembly_mode="constraint_assisted",
    )
    values.update(changes)
    return BlueprintContent(**values)


def make_record(ref: str, prompt: str, answer: str) -> CandidateRecord:
    digest = content_hash(prompt, answer)
    evidence = CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=CONTEXT,
        exercise_id=exercise_uuid(COURSE, ref),
        exercise_version_id=version_id_for(COURSE, ref, digest),
        exercise_family_id=family_id_for(COURSE, ref),
        exercise_kind="proof",
        estimated_seconds=600,
        difficulty_label="difficulty-3",
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
        answer_present=True,
    )


def build_environment(*, solver=None, content: BlueprintContent | None = None) -> SimpleNamespace:
    authorizer = ScopeAuthorizer()
    blueprints = BlueprintService(
        repository=InMemoryBlueprintRepository(), authorizer=authorizer, clock=lambda: NOW)
    body = content or blueprint_content()
    version = blueprints.create_version(
        actor_id=CREATOR, content=body, blueprint_id=BLUEPRINT_ID,
        base_version_id=None, idempotency_key="assembly-blueprint",
        course_ref=COURSE, offering_ref=OFFERING, actor_ref="teacher-creator",
    )
    candidate_repo = InMemoryCandidateRepository(
        release=ReleaseScopeInfo(
            release_context_id=CONTEXT, course_id=COURSE_ID, course_ref=COURSE,
            offering_id=OFFERING_ID, offering_ref=OFFERING, status="frozen",
            item_count=len(EXERCISES),
        ),
        records=tuple(make_record(*item) for item in EXERCISES),
    )
    candidates = CandidateService(
        repository=candidate_repo, authorizer=authorizer, clock=lambda: NOW)
    papers = PaperService(
        repository=InMemoryPaperRepository(), authorizer=authorizer,
        blueprints=blueprints, candidates=candidates, clock=lambda: NOW,
    )
    validation_repo = LazyValidationRepository()
    validator = PaperValidationService(
        repository=validation_repo, candidates=candidates, authorizer=authorizer,
        clock=lambda: NOW,
    )
    proxy = PaperProxy(papers, validation_repo, candidate_repo, body)
    runs = InMemoryAssemblyRunRepository()
    assembly = AssemblyService(
        repository=runs, solver=solver or StubSolver(), blueprints=blueprints,
        candidates=candidates,
        materializer=AssemblyMaterializer(papers=proxy, validator=validator),
        solver_version="stub-1.0", clock=lambda: NOW,
    )
    return SimpleNamespace(
        authorizer=authorizer, blueprints=blueprints, version=version, body=body,
        candidate_repo=candidate_repo, candidates=candidates, papers=papers,
        proxy=proxy, validation_repo=validation_repo, validator=validator,
        runs=runs, assembly=assembly, solver=assembly.solver,
    )


# ---------------------------------------------------------------------------
# 顺利路径
# ---------------------------------------------------------------------------

def test_solved_run_materializes_a_valid_draft() -> None:
    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-1")
    assert started.status == RunStatus.OPTIMAL, started.status
    assert started.reused is False

    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert row.solved is True
    assert len(row.solution_manifest) == 1
    assert row.candidate_snapshot_id is not None, "落卷需要阶段 A 的候选快照 id"
    assert row.diagnostics["precheck_codes"] == [], row.diagnostics

    draft = env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                                index=0, idempotency_key="pick-1")
    assert draft.status == "draft", "求解方案只能落成草稿，不能跳过审核"
    assert env.proxy.created == [draft.paper_version_id], "必须真的建了卷"

    after = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert after.diagnostics["validation"]["valid"] is True
    assert after.diagnostics["validation"]["paper_version_id"] == str(draft.paper_version_id)


def test_materialized_paper_can_enter_review_pipeline() -> None:
    """落卷后阶段 A 的复验报告必须真的通过（不是我们口头保证）。"""
    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-review")
    draft = env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                                index=0, idempotency_key="pick-review")
    report = env.validator.validate(actor_id=CREATOR, paper_version_id=draft.paper_version_id)
    assert report.valid is True, [issue.code for issue in report.issues]
    assert report.error_count == 0


def test_feasible_solution_keeps_its_honest_label() -> None:
    solver = StubSolver(status=RunStatus.FEASIBLE, optimal_proven=False)
    env = build_environment(solver=solver)
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-feasible")
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert row.status == RunStatus.FEASIBLE
    restored = solution_from_json(row.solution_manifest[0])
    assert restored.optimal_proven is False, "可行不能被写成'已证明最优'"
    assert restored.solver_status == RunStatus.FEASIBLE
    # 可行方案可以用来建卷——但不能宣称最优
    draft = env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                                index=0, idempotency_key="pick-feasible")
    assert draft.status == "draft"


# ---------------------------------------------------------------------------
# 幂等与隔离
# ---------------------------------------------------------------------------

def test_same_key_returns_the_same_run_and_does_not_resolve_again() -> None:
    env = build_environment()
    first = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                              idempotency_key="run-idem")
    second = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                               idempotency_key="run-idem")
    assert second.reused is True
    assert second.assembly_run_id == first.assembly_run_id
    assert env.solver.calls == 1, "重试不该再求解一次"


def test_key_reuse_for_another_blueprint_is_a_conflict() -> None:
    env = build_environment()
    env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                      idempotency_key="run-conflict")
    other = env.blueprints.create_version(
        actor_id=CREATOR, content=blueprint_content(total_score=Decimal("100.00")),
        blueprint_id=UUID("44444444-4444-5444-8444-444444444444"),
        base_version_id=None, idempotency_key="other-blueprint",
        course_ref=COURSE, offering_ref=OFFERING, actor_ref="teacher-creator",
    )
    try:
        env.assembly.start(actor_id=CREATOR, blueprint_version_id=other.id,
                          idempotency_key="run-conflict")
    except IdempotencyConflict as error:
        assert "不同的蓝图" in str(error), str(error)
    else:
        raise AssertionError("同一把键换了蓝图必须报冲突，不能返回上一次的运行")


def test_unknown_run_is_not_found() -> None:
    env = build_environment()
    try:
        env.assembly.run(actor_id=CREATOR, run_id=UUID(int=999))
    except RunNotFound:
        pass
    else:
        raise AssertionError("不存在的运行必须报 RunNotFound")


def test_latest_lists_runs_newest_first() -> None:
    env = build_environment()
    env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                      idempotency_key="run-a")
    env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                      idempotency_key="run-b")
    rows = env.assembly.latest(actor_id=CREATOR, blueprint_version_id=env.version.id)
    assert len(rows) == 2
    assert rows[0].idempotency_key == "run-b", "最新的运行必须排在前面"


# ---------------------------------------------------------------------------
# 没有方案就没有卷子
# ---------------------------------------------------------------------------

def test_precheck_infeasible_skips_the_solver_and_stores_the_proof() -> None:
    content = blueprint_content(
        hard_constraints={"total_score": "100.00", "duration_max_seconds": 60})
    # 每道题 600 秒 > 60 秒上限：预检就能证明无解，不该浪费一次求解
    env = build_environment(content=content)
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-infeasible")
    assert started.status == RunStatus.INFEASIBLE
    assert env.solver.calls == 0, "预检已证无解时不应再调求解器"
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert row.solution_manifest == ()
    assert row.diagnostics["reason"] == "precheck_proved"
    assert "duration_impossible" in {item["code"] for item in row.diagnostics["issues"]}


def test_unavailable_solver_marks_the_run_failed_without_a_paper() -> None:
    """本机没有 ortools 时的真实行为：明确失败，不产出任何卷子。"""
    env = build_environment(solver=UnavailableSolver())
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-unavailable")
    assert started.status == RunStatus.FAILED
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert row.solution_manifest == ()
    assert row.diagnostics["reason"] == "solver_error"
    assert "ortools" in row.diagnostics["detail"]
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-unavailable")
    except RunNotSolved as error:
        assert "没有可选方案" in str(error), str(error)
    else:
        raise AssertionError("失败的运行必须拒绝建卷")
    assert env.proxy.created == [], "失败运行不能留下任何卷子"


def test_solver_claiming_solutions_but_returning_none_is_rejected() -> None:
    env = build_environment(solver=StubSolver(drop_solutions=True))
    try:
        env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                          idempotency_key="run-empty")
    except AssemblyError as error:
        assert "没有方案" in str(error), str(error)
    else:
        raise AssertionError("OPTIMAL 却没有方案是适配层错误，不能记成一次成功")


def test_index_out_of_range_is_rejected() -> None:
    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-index")
    for index in (-1, 1):
        try:
            env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                                index=index, idempotency_key=f"pick-{index}")
        except SolutionIndexInvalid:
            continue
        raise AssertionError(f"序号 {index} 应该越界")


def test_tampered_solution_payload_is_rejected() -> None:
    """改掉方案里的一道题：重算哈希必然变，必须拒绝建卷（不是只比字段）。"""
    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-tamper")
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    tampered = dict(row.solution_manifest[0])
    selections = [dict(item) for item in tampered["selections"]]
    selections[0]["exercise_version_id"] = str(UUID(int=123))
    tampered["selections"] = selections
    env.runs.rows[row.id] = row.model_copy(update={"solution_manifest": (tampered,)})
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-tamper")
    except SolutionMismatch:
        pass
    else:
        raise AssertionError("方案载荷被改过时必须拒绝（重算哈希不一致）")
    assert env.proxy.created == []


# ---------------------------------------------------------------------------
# 复验不通过
# ---------------------------------------------------------------------------

def test_validation_failure_marks_run_failed_and_keeps_the_draft() -> None:
    """阶段 A 的独立复验不通过：运行标 FAILED，卷子留在草稿，绝不放行。"""

    class InvalidValidator:
        def __init__(self) -> None:
            self.calls = 0

        def validate(self, *, actor_id, paper_version_id):
            self.calls += 1
            return SimpleNamespace(
                valid=False,
                issues=(SimpleNamespace(code="estimated_seconds_missing",
                                        message="有槽位缺少预计时长",
                                        severity="error", slot_no=2),),
            )

    env = build_environment()
    invalid = InvalidValidator()
    env.assembly.materializer = AssemblyMaterializer(papers=env.proxy, validator=invalid)
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-invalid")
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-invalid")
    except AssemblyValidationBlocked as blocked:
        assert blocked.paper_version_id is not None
        assert blocked.issues[0]["code"] == "estimated_seconds_missing"
    else:
        raise AssertionError("复验不通过必须阻断，不能返回一个'看起来成功'的草稿")

    assert invalid.calls == 1
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert row.status == RunStatus.FAILED, "复验不通过后这次运行不能再被选择"
    assert row.diagnostics["validation"]["valid"] is False
    assert row.solution_manifest, "方案仍在（证据保留），但状态不允许再用"


def test_solution_violating_the_blueprint_never_creates_a_paper() -> None:
    """求解器给出的方案不满足蓝图（这里：分值被改）→ 拒绝建卷，运行标 FAILED。

    这是"CP-SAT 硬约束与独立复验一致"那条门禁在不接求解器时也能验的部分：
    方案复验只看方案与蓝图，所以它今天就能抓出"求解器算错"。
    """
    from app.assembly.verification import SolutionVerificationError

    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-bad-solution")
    row = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    original = solution_from_json(row.solution_manifest[0])
    # 造一份"自身哈希自洽、但分值不符合蓝图"的方案：这样"载荷被改过"那道检查不会
    # 触发，测到的就纯粹是方案复验这一层。
    broken_selection = replace(original.selections[0], score_hundredths=1)
    broken = make_solution_manifest(
        variant_label=original.variant_label,
        selections=(broken_selection,) + original.selections[1:],
        objective_terms=original.objective_terms,
        objective_value=original.objective_value,
        best_bound=original.best_bound,
        optimality_gap_milli=original.optimality_gap_milli,
        solver_status=original.solver_status,
        optimal_proven=original.optimal_proven,
        structure_report=original.structure_report,
    )
    assert solution_hash_matches(broken)
    env.runs.rows[row.id] = row.model_copy(
        update={"solution_manifest": (solution_to_json(broken),)})

    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-bad")
    except SolutionVerificationError as error:
        assert "score_mismatch" in {item.code for item in error.issues}, error.payload()
    else:
        raise AssertionError("分值不符合蓝图的方案不能建卷")
    assert env.proxy.created == [], "复验不通过时一份卷子都不能建"
    after = env.assembly.run(actor_id=CREATOR, run_id=started.assembly_run_id)
    assert after.status == RunStatus.FAILED, "方案不满足蓝图的运行不能再被选"
    assert after.diagnostics["verification"]["valid"] is False
    assert after.diagnostics["verification"]["issues"][0]["code"] == "score_mismatch"


def test_ab_pair_can_be_materialized_as_two_labelled_papers() -> None:
    """A/B 两卷：同一分组、各自字母，分别建卷（分别送审的前提）。"""
    env = build_environment()
    first = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                               idempotency_key="run-ab-a")
    second = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-ab-b")
    group = UUID("5e5e0000-0000-4000-8000-000000000010")
    paper_a = env.assembly.select(
        actor_id=CREATOR, run_id=first.assembly_run_id, index=0,
        idempotency_key="ab-a", variant_group_id=group, variant_label="A")
    paper_b = env.assembly.select(
        actor_id=CREATOR, run_id=second.assembly_run_id, index=0,
        idempotency_key="ab-b", variant_group_id=group, variant_label="B")
    assert paper_a.paper_version_id != paper_b.paper_version_id, "A/B 必须是两份卷"
    stored = [env.papers.repository.versions[paper_a.paper_version_id],
              env.papers.repository.versions[paper_b.paper_version_id]]
    assert [item.variant_label for item in stored] == ["A", "B"]
    assert {item.variant_group_id for item in stored} == {group}
    # 两份卷都各自通过了独立复验（不是共享一次结论）：看各自运行的诊断。
    for run_id in (first.assembly_run_id, second.assembly_run_id):
        row = env.assembly.run(actor_id=CREATOR, run_id=run_id)
        assert row.diagnostics["validation"]["valid"] is True, row.diagnostics
    assert (env.assembly.run(actor_id=CREATOR, run_id=first.assembly_run_id)
            .diagnostics["validation"]["paper_version_id"]) == str(paper_a.paper_version_id)


def test_label_without_group_is_refused_before_creating_a_paper() -> None:
    env = build_environment()
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                 idempotency_key="run-bad-variant")
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-bad-variant",
                            variant_label="A")
    except Exception as error:  # PaperRuleError 会一路抛上来
        assert "invalid_variant_identity" in str(error) or "variant_group_id" in str(error)
    else:
        raise AssertionError("只有字母没有分组的建卷请求必须被拒绝")


def test_second_select_after_failure_is_refused() -> None:
    class InvalidValidator:
        def validate(self, *, actor_id, paper_version_id):
            return SimpleNamespace(
                valid=False, issues=(SimpleNamespace(code="x", message="x",
                                                     severity="error", slot_no=None),))

    env = build_environment()
    env.assembly.materializer = AssemblyMaterializer(papers=env.proxy, validator=InvalidValidator())
    started = env.assembly.start(actor_id=CREATOR, blueprint_version_id=env.version.id,
                                idempotency_key="run-twice")
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-twice")
    except AssemblyValidationBlocked:
        pass
    try:
        env.assembly.select(actor_id=CREATOR, run_id=started.assembly_run_id,
                            index=0, idempotency_key="pick-twice-again")
    except RunNotSolved:
        pass
    else:
        raise AssertionError("失败后的运行不能再次建卷")


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
    print(f"✓ 组卷落卷与复验链路测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
