"""阶段 B 任务 2 的前置：求解前预检（不需要 CP-SAT 就能证明"无解"的那些情况）。

钉住的是"诊断必须可证、必须指出改什么、绝不放宽约束"：

  * 槽位没有候选、候选少于槽位、时长下界超限、覆盖上界不足、题型上界不足、
    两组必选题落在同一互斥组——这些都一定不可解，报告要能指到具体槽位；
  * 预检**通过**不代表可解（组合问题仍可能无解），所以 `diagnose_infeasible` 把
    `precheck_proved` 与 `solver_reported` 分开写，不许拿空预检冒充解释；
  * 诊断只读：跑完预检，蓝图与候选 manifest 必须与跑之前逐字节一致。

运行：`python server/tests/test_assembly_diagnostics.py`
"""

from __future__ import annotations

import sys
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.contracts import (  # noqa: E402
    BlueprintSnapshot,
    Candidate,
    CandidateManifest,
    ConceptWeight,
    CoverageRequirement,
    HardConstraints,
    KindRequirement,
    ManifestExclusion,
    SlotSpec,
    SoftObjectives,
    make_candidate_manifest,
    slot_identity,
)
from app.assembly.diagnostics import (  # noqa: E402
    candidate_supply,
    diagnose_infeasible,
    precheck,
    slot_candidates,
)

BP = UUID("4a0e0000-0000-5000-8000-000000000002")
COURSE = UUID(int=101)
OFFERING = UUID(int=102)
CONTEXT = UUID(int=103)
CONCEPT = UUID(int=77)


def slot(code: str, score: str = "50.00", section: str = "A", **changes) -> SlotSpec:
    values = dict(
        slot_id=slot_identity(BP, section, code), code=code, section_code=section,
        score=Decimal(score), estimated_seconds=600,
    )
    values.update(changes)
    return SlotSpec(**values)  # type: ignore[arg-type]


def blueprint(slots: tuple[SlotSpec, ...] | None = None, *, total: str = "100.00",
              hard: HardConstraints | None = None,
              soft: SoftObjectives | None = None) -> BlueprintSnapshot:
    if slots is None:
        slots = (slot("A1", "50.00"), slot("A2", "50.00"))
    return BlueprintSnapshot(
        blueprint_version_id=BP, course_id=COURSE, offering_id=OFFERING,
        release_context_id=CONTEXT, purpose="formal",
        total_score=Decimal(total), duration_seconds=5400, slots=slots,
        hard_constraints=hard or HardConstraints(), soft_objectives=soft or SoftObjectives(),
    )


def candidate(number: int, *, kind: str = "proof", seconds: int | None = 600,
              concepts: tuple[ConceptWeight, ...] = (), family: int | None = None,
              outcomes: tuple[UUID, ...] = ()) -> Candidate:
    return Candidate(
        exercise_version_id=UUID(int=number),
        stable_exercise_id=UUID(int=10_000 + number),
        exercise_family_id=UUID(int=(family if family is not None else 20_000 + number)),
        source_release_id=UUID(int=30_000 + number),
        decision_seq=1,
        exercise_kind=kind,
        dependency_hash=f"dep-{number}",
        content_hash=f"content-{number}",
        concept_weights=concepts,
        learning_outcome_ids=outcomes,
        difficulty=3,
        estimated_seconds=seconds,
    )


def manifest(blueprint_value: BlueprintSnapshot,
             candidates: list[Candidate] | tuple[Candidate, ...],
             exclusions: list[ManifestExclusion] | tuple[ManifestExclusion, ...] = (),
             ) -> CandidateManifest:
    return make_candidate_manifest(blueprint_value, candidates, exclusions)


# ---------------------------------------------------------------------------
# 资格判定与求解器同源
# ---------------------------------------------------------------------------

def test_slot_filter_matches_the_documented_rules() -> None:
    restricted = slot("A1", allowed_kinds=("proof",),
                      required_concept_ids=(CONCEPT,), difficulty=3)
    good = candidate(1, concepts=(ConceptWeight(CONCEPT, "primary", 1),))
    wrong_kind = candidate(2, kind="calculation",
                           concepts=(ConceptWeight(CONCEPT, "primary", 1),))
    wrong_concept = candidate(3)
    other_difficulty = replace(candidate(4), difficulty=5)
    picked = slot_candidates(restricted,
                             [good, wrong_kind, wrong_concept, other_difficulty],
                             require_seconds=False)
    assert [item.exercise_version_id for item in picked] == [good.exercise_version_id]


def test_slot_filter_requires_seconds_only_when_needed() -> None:
    plain = slot("A1")
    timed = candidate(1, seconds=None)
    assert slot_candidates(plain, [timed], require_seconds=False) == [timed]
    assert slot_candidates(plain, [timed], require_seconds=True) == []


def test_mandatory_version_restricts_the_slot() -> None:
    fixed = candidate(7)
    forced = slot("A1", mandatory_version_ids=(fixed.exercise_version_id,))
    others = candidate(8)
    picked = slot_candidates(forced, [others, fixed], require_seconds=False)
    assert [item.exercise_version_id for item in picked] == [fixed.exercise_version_id]


# ---------------------------------------------------------------------------
# 可证不可行
# ---------------------------------------------------------------------------

def test_slot_without_candidates_is_reported_with_its_code() -> None:
    """槽位的过滤条件把候选全排除时，要指到具体槽位——教师才知道改哪个槽。"""
    bp = blueprint((slot("A1", "50.00"), slot("A2", "50.00", allowed_kinds=("calculation",))))
    issues = precheck(bp, manifest(bp, [candidate(1), candidate(2)]))
    codes = {(item.code, item.slot_code) for item in issues}
    assert ("slot_has_no_candidate", "A2") in codes, codes
    assert ("slot_has_no_candidate", "A1") not in codes, "A1 有候选就不该报"


def test_not_enough_candidates_is_proved() -> None:
    bp = blueprint((slot("A1", "30.00"), slot("A2", "30.00"), slot("A3", "40.00")))
    issues = precheck(bp, manifest(bp, [candidate(1), candidate(2)]))
    assert "not_enough_candidates" in {item.code for item in issues}, [i.code for i in issues]


def test_duration_lower_bound_is_reported() -> None:
    bp = blueprint(hard=HardConstraints(max_duration_seconds=600))
    issues = precheck(bp, manifest(bp, [candidate(1, seconds=900), candidate(2, seconds=1200)]))
    codes = {item.code for item in issues}
    assert "duration_impossible" in codes, codes


def test_missing_seconds_makes_the_slot_unusable_when_timed() -> None:
    """启用时长上限时，没有预计时长的候选不能进卷——也不会被当成 0 秒。"""
    bp = blueprint(hard=HardConstraints(max_duration_seconds=600))
    issues = precheck(bp, manifest(bp, [candidate(1, seconds=None)]))
    codes = {item.code for item in issues}
    assert "slot_has_no_candidate" in codes, codes
    assert "duration_impossible" not in codes, (
        "缺时长不等于超时：不能虚构一个下界去判死"
    )


def test_coverage_upper_bound_is_reported() -> None:
    bp = blueprint(hard=HardConstraints(coverage_requirements=(
        CoverageRequirement(CONCEPT, "concept", "primary", 5),
    )))
    weights = (ConceptWeight(CONCEPT, "primary", 1),)
    issues = precheck(bp, manifest(bp, [
        candidate(1, concepts=weights), candidate(2, concepts=weights)]))
    # 两个槽位 × 每槽最多 1 分 = 上界 2 < 要求 5
    assert "coverage_unreachable" in {item.code for item in issues}, [i.code for i in issues]


def test_coverage_reachable_is_not_reported() -> None:
    bp = blueprint(hard=HardConstraints(coverage_requirements=(
        CoverageRequirement(CONCEPT, "concept", "primary", 2),
    )))
    weights = (ConceptWeight(CONCEPT, "primary", 1),)
    issues = precheck(bp, manifest(bp, [
        candidate(1, concepts=weights), candidate(2, concepts=weights)]))
    assert "coverage_unreachable" not in {item.code for item in issues}


def test_kind_requirement_unreachable_is_reported() -> None:
    bp = blueprint(hard=HardConstraints(kind_requirements=(
        KindRequirement("A", "calculation", 2, 2),
    )))
    issues = precheck(bp, manifest(bp, [
        candidate(1, kind="proof"), candidate(2, kind="proof")]))
    assert "kind_requirement_unreachable" in {item.code for item in issues}


def test_kind_requirement_interval_inverted_is_reported() -> None:
    bp = blueprint(hard=HardConstraints(kind_requirements=(
        KindRequirement("A", "proof", 3, 1),
    )))
    codes = {item.code for item in precheck(bp, manifest(bp, [candidate(1), candidate(2)]))}
    assert "kind_requirement_invalid" in codes, codes


def test_mutually_exclusive_mandatory_is_reported() -> None:
    first = candidate(1)
    second = candidate(2)
    bp = blueprint(
        slots=(slot("A1", "50.00", mandatory_version_ids=(first.exercise_version_id,)),
               slot("A2", "50.00", mandatory_version_ids=(second.exercise_version_id,))),
        hard=HardConstraints(exclusion_groups=((first.exercise_version_id,
                                                second.exercise_version_id),)),
    )
    issues = precheck(bp, manifest(bp, [first, second]))
    assert "mutually_exclusive_mandatory" in {item.code for item in issues}, (
        [item.code for item in issues])


def test_blueprint_score_mismatch_is_reported() -> None:
    bp = blueprint(total="90.00")
    codes = {item.code for item in precheck(bp, manifest(bp, [candidate(1), candidate(2)]))}
    assert "blueprint_score_mismatch" in codes, codes


def test_section_score_mismatch_is_reported() -> None:
    bp = blueprint(hard=HardConstraints(section_scores={"A": Decimal("40.00")}))
    codes = {item.code for item in precheck(bp, manifest(bp, [candidate(1), candidate(2)]))}
    assert "section_score_mismatch" in codes, codes


# ---------------------------------------------------------------------------
# 通过 / 诊断载荷 / 只读
# ---------------------------------------------------------------------------

def test_clean_plan_has_no_issues() -> None:
    bp = blueprint()
    assert precheck(bp, manifest(bp, [candidate(1), candidate(2)])) == []


def test_diagnose_separates_proved_from_solver_reported() -> None:
    bp = blueprint()
    proved = diagnose_infeasible(bp, manifest(bp, [candidate(1)]))
    assert proved["reason"] == "precheck_proved"
    assert proved["issues"], "预检有结论时必须列出来"
    assert proved["summary"] == {"slots": 2, "candidates": 1, "exclusions": 0}

    ok_bp = blueprint()
    unclear = diagnose_infeasible(ok_bp, manifest(ok_bp, [candidate(1), candidate(2)]),
                                  solver_diagnostics={"reason": "hard_constraints"})
    assert unclear["reason"] == "solver_reported", (
        "预检没找到可证原因时不能声称'已定位'，原因在组合里"
    )
    assert unclear["issues"] == []
    assert unclear["solver"] == {"reason": "hard_constraints"}


def test_diagnosis_is_read_only() -> None:
    bp = blueprint()
    built = manifest(bp, [candidate(1)])
    before = (asdict(bp), asdict(built))
    precheck(bp, built)
    diagnose_infeasible(bp, built)
    after = (asdict(bp), asdict(built))
    assert before == after, "诊断不能修改蓝图或候选 manifest（否则回放哈希会变）"


def test_candidate_supply_counts_per_slot() -> None:
    bp = blueprint()
    supply = candidate_supply(bp, manifest(bp, [candidate(1), candidate(2), candidate(3)]))
    assert supply == {"A1": 3, "A2": 3}
    empty = candidate_supply(bp, manifest(bp, []))
    assert empty == {"A1": 0, "A2": 0}, "空候选也要给出每槽 0，而不是省略键"


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
    print(f"✓ 组卷预检与诊断测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
