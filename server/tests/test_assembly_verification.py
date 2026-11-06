"""方案复验：求解器说满足，不等于方案真的满足。

这是阶段 B 门禁"CP-SAT 硬约束与独立整卷校验结果一致"里**不依赖求解器**的那一半。
它只看方案与蓝图，所以现在就能跑；等求解器能用，它是第一道抓"求解器算错"的门。

每条断言都在回答同一个问题：这份方案里有没有一个字段是**编出来**的？
（分值对不上、所选题不在候选里、时长缺了当 0、覆盖率虚构、同一道题出现两次……）

运行：`python server/tests/test_assembly_verification.py`
"""

from __future__ import annotations

import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.contracts import (  # noqa: E402
    AssemblySelection,
    BlueprintSnapshot,
    Candidate,
    ConceptWeight,
    CoverageRequirement,
    HardConstraints,
    KindRequirement,
    ManifestExclusion,
    SlotSpec,
    SoftObjectives,
    SolutionManifest,
    make_candidate_manifest,
    score_to_hundredths,
    slot_identity,
)
from app.assembly.verification import (  # noqa: E402
    SolutionVerificationError,
    assert_solution_valid,
    verify_solution,
)

BP = UUID("4a0e0000-0000-5000-8000-0000000000dd")
CONCEPT = UUID(int=77)


def slot(code: str, score: str, section: str = "A", **changes) -> SlotSpec:
    values = dict(slot_id=slot_identity(BP, section, code), code=code,
                  section_code=section, score=Decimal(score), estimated_seconds=600)
    values.update(changes)
    return SlotSpec(**values)  # type: ignore[arg-type]


def blueprint(slots=(), *, total: str | None = None,
              hard: HardConstraints | None = None) -> BlueprintSnapshot:
    slots = tuple(slots) or (slot("A1", "50.00"), slot("A2", "50.00"))
    total_score = Decimal(total) if total else sum((item.score for item in slots),
                                                   Decimal("0"))
    return BlueprintSnapshot(
        blueprint_version_id=BP, course_id=UUID(int=101), offering_id=UUID(int=102),
        release_context_id=UUID(int=103), purpose="formal",
        total_score=total_score, duration_seconds=5400, slots=slots,
        hard_constraints=hard or HardConstraints(), soft_objectives=SoftObjectives())


def candidate(number: int, *, kind: str = "proof", seconds: int | None = 600,
              family: int | None = None, stable: int | None = None,
              concepts: tuple[ConceptWeight, ...] = (),
              outcomes: tuple[UUID, ...] = ()) -> Candidate:
    return Candidate(
        exercise_version_id=UUID(int=number),
        stable_exercise_id=UUID(int=(stable if stable is not None else 10_000 + number)),
        exercise_family_id=UUID(int=(family if family is not None else 20_000 + number)),
        source_release_id=UUID(int=30_000 + number),
        decision_seq=1, exercise_kind=kind,
        dependency_hash=f"dep-{number}", content_hash=f"content-{number}",
        concept_weights=concepts, learning_outcome_ids=outcomes,
        difficulty=3, estimated_seconds=seconds,
    )


def solution(bp: BlueprintSnapshot, mapping: dict[str, int], *,
             scores: dict[str, int] | None = None,
             seconds: dict[str, int | None] | None = None) -> SolutionManifest:
    """按 {槽位: 题目编号} 造方案；scores/seconds 用来故意写错某个字段。"""
    slots = {item.code: item for item in bp.slots}
    selections = []
    for code, number in mapping.items():
        slot_spec = slots[code]
        selections.append(AssemblySelection(
            slot_id=slot_spec.slot_id, slot_code=code,
            exercise_version_id=UUID(int=number), exercise_instance_id=None,
            stable_exercise_id=UUID(int=10_000 + number),
            exercise_family_id=UUID(int=20_000 + number),
            source_release_id=UUID(int=30_000 + number), decision_seq=1,
            score_hundredths=(scores or {}).get(code,
                                                score_to_hundredths(slot_spec.score)),
            estimated_seconds=(seconds or {}).get(code, 600),
            difficulty=3, dependency_hash=f"dep-{number}",
            content_hash=f"content-{number}",
        ))
    return SolutionManifest(
        variant_label="single", selections=tuple(selections), objective_terms=(),
        objective_value=0, best_bound=0, optimality_gap_milli=0,
        solver_status="optimal", optimal_proven=True, structure_report={},
        manifest_hash="a" * 64)


def manifest(bp: BlueprintSnapshot, candidates: list[Candidate],
             exclusions: tuple[ManifestExclusion, ...] = ()):
    return make_candidate_manifest(bp, candidates, exclusions)


def codes(issues) -> set[str]:
    return {item.code for item in issues}


# ---------------------------------------------------------------------------
# 通过的方案
# ---------------------------------------------------------------------------

def test_clean_solution_passes() -> None:
    bp = blueprint()
    candidates = [candidate(1), candidate(2)]
    issues = verify_solution(bp, manifest(bp, candidates), solution(bp, {"A1": 1, "A2": 2}))
    assert issues == [], [item.code for item in issues]


def test_assert_returns_silently_when_valid() -> None:
    bp = blueprint()
    candidates = [candidate(1), candidate(2)]
    assert_solution_valid(bp, manifest(bp, candidates), solution(bp, {"A1": 1, "A2": 2}))


# ---------------------------------------------------------------------------
# 分值
# ---------------------------------------------------------------------------

def test_wrong_slot_score_is_caught() -> None:
    bp = blueprint()
    issues = verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]),
                             solution(bp, {"A1": 1, "A2": 2},
                                      scores={"A1": 4000}))
    assert "score_mismatch" in codes(issues), codes(issues)


def test_total_score_mismatch_is_caught_even_when_slots_are_right() -> None:
    """槽位分都对、但蓝图总分对不上（蓝图自己不自洽）也必须报。"""
    bp = blueprint(total="120.00")
    issues = verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]),
                             solution(bp, {"A1": 1, "A2": 2}))
    assert "total_score_mismatch" in codes(issues), codes(issues)


# ---------------------------------------------------------------------------
# 槽位完整性
# ---------------------------------------------------------------------------

def test_missing_slot_is_caught() -> None:
    bp = blueprint()
    issues = verify_solution(bp, manifest(bp, [candidate(1)]), solution(bp, {"A1": 1}))
    assert "missing_slot" in codes(issues)
    assert next(item for item in issues if item.code == "missing_slot").slot_code == "A2"


def test_duplicate_slot_selection_is_caught() -> None:
    bp = blueprint()
    built = solution(bp, {"A1": 1, "A2": 2})
    doubled = replace(built, selections=built.selections + (built.selections[0],))
    assert "duplicate_slot_selection" in codes(
        verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]), doubled))


def test_unknown_slot_is_caught() -> None:
    bp = blueprint()
    built = solution(bp, {"A1": 1, "A2": 2})
    stray = replace(built.selections[0], slot_code="Z9")
    built = replace(built, selections=(stray,) + built.selections[1:])
    assert "unknown_slot" in codes(
        verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]), built))


# ---------------------------------------------------------------------------
# 候选来源
# ---------------------------------------------------------------------------

def test_selected_candidate_outside_manifest_is_caught() -> None:
    """求解器不能"发明"一道没被审核的题。"""
    bp = blueprint()
    issues = verify_solution(bp, manifest(bp, [candidate(1)]), solution(bp, {"A1": 1, "A2": 9}))
    assert "selected_unknown_candidate" in codes(issues), codes(issues)


def test_selected_excluded_candidate_is_caught_separately() -> None:
    """被排除的题（曝光冲突）与"从未见过"的题要分开报：改法完全不同。"""
    bp = blueprint()
    excluded = ManifestExclusion(exercise_version_id=UUID(int=9), reason="exposure_conflict")
    issues = verify_solution(bp, manifest(bp, [candidate(1)], (excluded,)),
                             solution(bp, {"A1": 1, "A2": 9}))
    assert "selected_excluded_candidate" in codes(issues), codes(issues)


# ---------------------------------------------------------------------------
# 时长
# ---------------------------------------------------------------------------

def test_duration_exceeded_is_caught() -> None:
    bp = blueprint(hard=HardConstraints(max_duration_seconds=900))
    issues = verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]),
                             solution(bp, {"A1": 1, "A2": 2}))
    assert "duration_exceeded" in codes(issues)


def test_unknown_duration_is_not_treated_as_zero() -> None:
    """方案与候选都缺时长 → 报"无法判断"，不能虚构 0 秒说"没超时"。"""
    bp = blueprint(hard=HardConstraints(max_duration_seconds=900))
    blind = [candidate(1, seconds=None), candidate(2, seconds=None)]
    issues = verify_solution(bp, manifest(bp, blind),
                             solution(bp, {"A1": 1, "A2": 2},
                                      seconds={"A1": None, "A2": None}))
    checklist = codes(issues)
    assert "duration_unknown" in checklist, checklist
    assert "duration_exceeded" not in checklist, "缺时长不等于超时"


def test_duration_falls_back_to_the_candidate_when_selection_omits_it() -> None:
    """方案里没写时长但候选有：用候选的值，而不是当成 0 或缺失。"""
    bp = blueprint(hard=HardConstraints(max_duration_seconds=900))
    issues = verify_solution(bp, manifest(bp, [candidate(1), candidate(2)]),
                             solution(bp, {"A1": 1, "A2": 2},
                                      seconds={"A1": None, "A2": None}))
    assert "duration_unknown" not in codes(issues)
    assert "duration_exceeded" in codes(issues), (
        "两道 600 秒的题放进上限 900 秒的卷子，必须判超时"
    )


# ---------------------------------------------------------------------------
# 覆盖与题型
# ---------------------------------------------------------------------------

def test_coverage_shortfall_is_caught() -> None:
    weights = (ConceptWeight(CONCEPT, "primary", 1),)
    bp = blueprint(hard=HardConstraints(coverage_requirements=(
        CoverageRequirement(CONCEPT, "concept", "primary", 2),)))
    issues = verify_solution(bp, manifest(bp, [candidate(1, concepts=weights),
                                               candidate(2)]),
                             solution(bp, {"A1": 1, "A2": 2}))
    assert "coverage_shortfall" in codes(issues)


def test_kind_requirement_violations_are_caught_both_ways() -> None:
    bp = blueprint(hard=HardConstraints(kind_requirements=(
        KindRequirement("A", "proof", 2, 2),)))
    too_few = verify_solution(bp, manifest(bp, [candidate(1, kind="proof"),
                                                candidate(2, kind="calculation")]),
                              solution(bp, {"A1": 1, "A2": 2}))
    assert "kind_below_minimum" in codes(too_few)

    bp2 = blueprint(hard=HardConstraints(kind_requirements=(
        KindRequirement("A", "proof", 0, 1),)))
    too_many = verify_solution(bp2, manifest(bp2, [candidate(1), candidate(2)]),
                               solution(bp2, {"A1": 1, "A2": 2}))
    assert "kind_above_maximum" in codes(too_many)


# ---------------------------------------------------------------------------
# 唯一性
# ---------------------------------------------------------------------------

def test_same_exercise_twice_is_caught() -> None:
    bp = blueprint()
    shared = candidate(1)
    # 两个槽位都指向同一道题：求解器（或手改的方案）不能把一道题印两遍。
    built = solution(bp, {"A1": 1, "A2": 1})
    issues = verify_solution(bp, manifest(bp, [shared, candidate(2)]), built)
    assert "duplicate_exercise" in codes(issues), codes(issues)


def test_exclusion_group_violation_is_caught() -> None:
    first, second = candidate(1), candidate(2)
    bp = blueprint(hard=HardConstraints(exclusion_groups=(
        (first.exercise_version_id, second.exercise_version_id),)))
    issues = verify_solution(bp, manifest(bp, [first, second]),
                             solution(bp, {"A1": 1, "A2": 2}))
    assert "exclusion_group_violated" in codes(issues)


# ---------------------------------------------------------------------------
# 门禁接口
# ---------------------------------------------------------------------------

def test_assert_raises_with_payload() -> None:
    bp = blueprint()
    try:
        assert_solution_valid(bp, manifest(bp, [candidate(1)]),
                              solution(bp, {"A1": 1}))
    except SolutionVerificationError as error:
        assert error.issues, "必须带问题清单"
        payload = error.payload()
        assert payload and payload[0]["code"] == "missing_slot"
        assert payload[0]["slot_code"] == "A2"
    else:
        raise AssertionError("缺槽位的方案不能通过门禁")


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
    print(f"✓ 方案复验测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
