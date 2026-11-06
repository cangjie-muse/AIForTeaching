"""求解运行的 worker 路径：幂等、不重解、不伪造。

这一层要证明的是"重复执行同一个 job 不会改变结论"，以及"候选清单被改过时宁可不解"。
求解器用桩（真 CP-SAT 装不上），运行仓储与蓝图翻译是真的。

钉住的四件事：

  1. 同一个运行被领两次：第二次直接返回终态，**不再调用求解器**；
  2. 运行行里的候选清单哈希对不上（有人改过库）：记 FAILED 且**不调用求解器**；
  3. 求解器不可用（本机真实状态）：记 FAILED 并写诊断，不抛异常拖住队列；
  4. 求解器给出方案：运行变 OPTIMAL，方案真的落进 solution_manifest。

运行：`python server/tests/test_assembly_worker.py`
"""

from __future__ import annotations

import sys
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.contracts import (  # noqa: E402
    AssemblySelection,
    BlueprintSnapshot,
    HardConstraints,
    RunStatus,
    SlotSpec,
    SoftObjectives,
    SolveResult,
    SolverConfig,
    make_candidate_manifest,
    make_solution_manifest,
    make_solution_set,
    score_to_hundredths,
    slot_identity,
    to_json_value,
)
from app.assembly.repository import (  # noqa: E402
    AssemblyRunRow,
    InMemoryAssemblyRunRepository,
)
from app.assembly.service import AssemblyService  # noqa: E402
from app.assembly.solver_port import UnavailableSolver  # noqa: E402
from app.contracts.paper import CandidateEvidence  # noqa: E402
from app.contracts.candidate import CandidateRecord  # noqa: E402
from app.contracts.hashing import canonical_hash  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402

COURSE = "analysis"
BP = UUID("4a0e0000-0000-5000-8000-0000000000cc")
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
ACTOR = ref_uuid("account", "teacher-creator")
NOW = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def blueprint() -> BlueprintSnapshot:
    return BlueprintSnapshot(
        blueprint_version_id=BP, course_id=ref_uuid("course", COURSE),
        offering_id=None, release_context_id=CONTEXT, purpose="formal",
        total_score=Decimal("10.00"), duration_seconds=600,
        slots=(SlotSpec(slot_id=slot_identity(BP, "A", "A1"), code="A1",
                        section_code="A", score=Decimal("10.00"),
                        estimated_seconds=600),),
        hard_constraints=HardConstraints(), soft_objectives=SoftObjectives(),
    )


def record() -> CandidateRecord:
    evidence = CandidateEvidence(
        release_context_id=CONTEXT, source_release_id=CONTEXT,
        exercise_id=UUID(int=1001), exercise_version_id=UUID(int=2001),
        exercise_instance_id=None, exercise_family_id=UUID(int=3001),
        exercise_kind="proof", estimated_seconds=600,
        dependency_manifest_hash=canonical_hash({"dep": 1}),
        content_hash=canonical_hash({"content": 1}),
        decision_seqs={"exercise": 1}, latest_decisions={"exercise": "approved"},
        in_context=True, license_valid=True, revoked=False, hidden_evaluation=False,
        private_draft=False, parameterized=False, instance_approved=True,
        dependencies_complete=True, render_ready=True, language_pair_valid=True,
        purpose_allowed=True, cloud_allowed=True,
    )
    return CandidateRecord(evidence=evidence, release_state="included",
                           review_status="teacher-reviewed", license="licensed",
                           answer_present=True)


class StubCandidateService:
    """候选 manifest 由测试直接给，不经过阶段 A 的取数。"""

    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def snapshot(self, *, actor_id, blueprint):  # pragma: no cover - worker 不建清单
        return self.manifest


class StubBlueprints:
    def __init__(self, snapshot: BlueprintSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def get_version(self, *, actor_id, version_id):
        self.calls += 1
        return SimpleNamespace(id=version_id, content=None,
                               assembly_mode="constraint_assisted",
                               _snapshot=self.snapshot)


class CountingSolver:
    def __init__(self, *, status: RunStatus = RunStatus.OPTIMAL) -> None:
        self.status = status
        self.calls = 0

    def solve(self, blueprint, manifest, config, peer=None):
        self.calls += 1
        candidate = manifest.candidates[0]
        slot = blueprint.slots[0]
        selection = AssemblySelection(
            slot_id=slot.slot_id, slot_code=slot.code,
            exercise_version_id=candidate.exercise_version_id,
            exercise_instance_id=None,
            stable_exercise_id=candidate.stable_exercise_id,
            exercise_family_id=candidate.exercise_family_id,
            source_release_id=candidate.source_release_id,
            decision_seq=candidate.decision_seq,
            score_hundredths=score_to_hundredths(slot.score),
            estimated_seconds=candidate.estimated_seconds,
            difficulty=candidate.difficulty,
            dependency_hash=candidate.dependency_hash,
            content_hash=candidate.content_hash,
        )
        manifest_value = make_solution_manifest(
            variant_label="single", selections=(selection,), objective_terms=(),
            objective_value=0, best_bound=0, optimality_gap_milli=0,
            solver_status=self.status, optimal_proven=self.status == RunStatus.OPTIMAL,
            structure_report={},
        )
        return SolveResult(status=self.status,
                           solution_sets=(make_solution_set((manifest_value,)),),
                           diagnostics={}, best_bound=0, optimality_gap_milli=0)


def build(*, solver=None, tampered: bool = False):
    """装一个内存版 AssemblyService：蓝图翻译用真的，候选清单走真实候选服务。"""
    bp = blueprint()
    from app.assembly.candidate import AssemblyCandidateService, CandidateSourceResult

    source = SimpleNamespace(load=lambda **_: CandidateSourceResult(
        release_context_id=CONTEXT, accepted=(record(),), rejected=(),
        exposure_events=(), as_of=NOW, candidate_snapshot_id=UUID(int=4242)))
    manifest = AssemblyCandidateService(source=source, clock=lambda: NOW).snapshot(
        actor_id=ACTOR, blueprint=bp)
    stored = to_json_value(manifest)
    stored_hash = "0" * 64 if tampered else manifest.manifest_hash

    sentinel = solver or CountingSolver()
    service = AssemblyService(
        repository=InMemoryAssemblyRunRepository(), solver=sentinel,
        blueprints=_FakeBlueprints(bp), candidate_service=StubCandidateService(manifest),
        solver_name="cp-sat", solver_version="stub", clock=lambda: NOW,
    )
    row = AssemblyRunRow(
        id=UUID(int=9001), blueprint_version_id=BP, actor_id=ACTOR,
        idempotency_key="worker-key", candidate_manifest=stored,
        candidate_manifest_hash=stored_hash,
        candidate_snapshot_id=UUID(int=4242),
        solver_name="cp-sat", solver_version="stub",
        solver_config=to_json_value(SolverConfig(max_solutions=1)),
        status=RunStatus.QUEUED, created_at=NOW, started_at=NOW,
    )
    with service.repository.unit_of_work() as uow:
        uow.add(row)
    return service, sentinel, row.id


class _FakeBlueprints:
    """`BlueprintService.get_version` 的最小替身：worker 只需要它返回可翻译的版本。"""

    def __init__(self, snapshot: BlueprintSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def get_version(self, *, actor_id, version_id=None, actor_ref=None):
        self.calls += 1
        return SimpleNamespace(
            id=self.snapshot.blueprint_version_id,
            assembly_mode="constraint_assisted",
            content=_content(self.snapshot),
        )


def _content(snapshot: BlueprintSnapshot):
    """把快照折成 `BlueprintContent` 的形状（翻译层只读这些字段）。"""
    from app.contracts.paper import BlueprintContent, BlueprintSection, BlueprintSlot

    sections = []
    by_section: dict[str, list] = {}
    for slot in snapshot.slots:
        by_section.setdefault(slot.section_code, []).append(slot)
    for code, slots in by_section.items():
        sections.append(BlueprintSection(
            code=code, title=f"{code} 节",
            slots=tuple(BlueprintSlot(
                code=slot.code, score=slot.score,
                exercise_kinds=slot.allowed_kinds,
                required_concept_ids=slot.required_concept_ids,
                learning_outcome_ids=slot.learning_outcome_ids,
                estimated_seconds=slot.estimated_seconds,
                mandatory_exercise_version_ids=slot.mandatory_version_ids,
                excluded_exercise_version_ids=slot.excluded_version_ids,
            ) for slot in slots),
        ))
    return BlueprintContent(
        schema_version="paper-blueprint/v1", purpose=snapshot.purpose,
        course_id=snapshot.course_id, offering_id=snapshot.offering_id,
        release_context_id=snapshot.release_context_id,
        total_score=snapshot.total_score, duration_seconds=snapshot.duration_seconds,
        sections=tuple(sections), hard_constraints={}, soft_objectives={},
        review_policy="single_review", assembly_mode="constraint_assisted",
    )


# ---------------------------------------------------------------------------
# execute 的幂等与安全
# ---------------------------------------------------------------------------

def test_execute_solves_once_and_records_the_solution() -> None:
    service, solver, run_id = build()
    row = service.execute(run_id=run_id)
    assert row.status == RunStatus.OPTIMAL, row.status
    assert solver.calls == 1
    assert len(row.solution_manifest) == 1
    assert row.solution_manifest[0]["optimal_proven"] is True


def test_second_execute_does_not_solve_again() -> None:
    service, solver, run_id = build()
    service.execute(run_id=run_id)
    again = service.execute(run_id=run_id)
    assert solver.calls == 1, "终态运行被重复领取时不能再求解一次"
    assert again.status == RunStatus.OPTIMAL


def test_tampered_candidate_manifest_is_failed_without_solving() -> None:
    service, solver, run_id = build(tampered=True)
    row = service.execute(run_id=run_id)
    assert row.status == RunStatus.FAILED
    assert row.diagnostics["reason"] == "candidate_manifest_tampered"
    assert solver.calls == 0, "清单被改过时不能拿它去求解"
    assert row.solution_manifest == ()


def test_unavailable_solver_marks_failed_and_does_not_raise() -> None:
    service, _, run_id = build(solver=UnavailableSolver())
    row = service.execute(run_id=run_id)
    assert row.status == RunStatus.FAILED
    assert "ortools" in row.diagnostics["detail"]
    assert row.solution_manifest == ()


def test_unknown_run_raises_run_not_found() -> None:
    service, _, _ = build()
    from app.assembly.service import RunNotFound

    try:
        service.execute(run_id=UUID(int=12345))
    except RunNotFound:
        pass
    else:
        raise AssertionError("不存在的运行必须报 RunNotFound")


def test_worker_payload_without_run_id_is_rejected() -> None:
    from app.assembly_worker import AssemblyJobError

    try:
        from app.assembly_worker import run_job

        run_job({"payload": {}})
    except AssemblyJobError as error:
        assert "assembly_run_id" in str(error)
    else:
        raise AssertionError("缺 assembly_run_id 必须报载荷错误，而不是猜一个运行")


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
    print(f"✓ 组卷 worker 路径测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
