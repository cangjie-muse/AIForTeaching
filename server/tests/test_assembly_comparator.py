"""A/B 结构比较：只主张结构匹配，不主张统计等值。

这份测试的每一条都在问同一个问题：**系统会不会替教师做出它做不到的承诺？**

  * 两卷分值结构一致 + 除锚题外无重叠 → 说"结构匹配"，并且仍然显示
    "不代表统计等值"；
  * 有共用题但不在锚题里 → 必须判不匹配，并指到具体槽位；
  * 同族题（题干不同、材料相同）也算重叠；
  * 锚题本身允许共用，但必须被显式声明，不能"用了同一道题就自动算锚题"。

运行：`python server/tests/test_assembly_comparator.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.comparator import (  # noqa: E402
    STRUCTURE_NOTE,
    compare_structure,
    issues_payload,
)
from app.assembly.contracts import (  # noqa: E402
    AssemblySelection,
    BlueprintSnapshot,
    HardConstraints,
    SlotSpec,
    SoftObjectives,
    SolutionManifest,
    score_to_hundredths,
    slot_identity,
)

BP = UUID("4a0e0000-0000-5000-8000-000000000002")


def blueprint(*scores: str) -> BlueprintSnapshot:
    slots = tuple(
        SlotSpec(
            slot_id=slot_identity(BP, "A", f"A{index}"),
            code=f"A{index}", section_code="A",
            score=Decimal(score), estimated_seconds=600,
        )
        for index, score in enumerate(scores, 1)
    )
    return BlueprintSnapshot(
        blueprint_version_id=BP, course_id=UUID(int=101), offering_id=UUID(int=102),
        release_context_id=UUID(int=103), purpose="formal",
        total_score=sum((Decimal(score) for score in scores), Decimal("0")),
        duration_seconds=5400, slots=slots,
        hard_constraints=HardConstraints(), soft_objectives=SoftObjectives(),
    )


def solution(bp: BlueprintSnapshot, versions: tuple[int, ...], *,
             families: tuple[int, ...] | None = None,
             label: str = "A", difficulty: int | None = 3,
             seconds: int | None = 600) -> SolutionManifest:
    families = families or tuple(9000 + number for number in versions)
    selections = tuple(
        AssemblySelection(
            slot_id=slot.slot_id, slot_code=slot.code,
            exercise_version_id=UUID(int=version),
            exercise_instance_id=None,
            stable_exercise_id=UUID(int=10_000 + version),
            exercise_family_id=UUID(int=families[index]),
            source_release_id=UUID(int=30_000 + version),
            decision_seq=1,
            score_hundredths=score_to_hundredths(slot.score),
            estimated_seconds=seconds, difficulty=difficulty,
            dependency_hash=f"dep-{version}", content_hash=f"content-{version}",
        )
        for index, (slot, version) in enumerate(zip(bp.slots, versions))
    )
    return SolutionManifest(
        variant_label=label, selections=selections, objective_terms=(),
        objective_value=0, best_bound=0, optimality_gap_milli=0,
        solver_status="optimal", optimal_proven=True, structure_report={},
        manifest_hash=f"{label}".ljust(64, "0"),
    )


# ---------------------------------------------------------------------------
# 匹配的情况
# ---------------------------------------------------------------------------

def test_disjoint_matching_structure_is_reported_as_matched() -> None:
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (1, 2), label="A")
    second = solution(bp, (3, 4), label="B")
    comparison = compare_structure(bp, first, second)
    assert comparison.structurally_matched is True, issues_payload(comparison)
    assert comparison.issues == ()
    assert comparison.shared_version_ids == ()
    assert comparison.shared_family_ids == ()


def test_note_always_says_not_statistically_equivalent() -> None:
    bp = blueprint("100.00")
    comparison = compare_structure(bp, solution(bp, (1,)), solution(bp, (2,), label="B"))
    assert comparison.note == STRUCTURE_NOTE
    assert "不代表统计等值" in comparison.note
    assert comparison.statistical_equivalence is False, (
        "本系统没有等值设计，不能主张统计等值；这个字段恒为 False 是刻意的"
    )


def test_anchor_shared_between_volumes_is_allowed() -> None:
    bp = blueprint("40.00", "60.00")
    shared = 7
    first = solution(bp, (shared, 1), label="A")
    second = solution(bp, (shared, 2), label="B")
    comparison = compare_structure(bp, first, second, anchors=(UUID(int=shared),))
    assert comparison.structurally_matched is True, issues_payload(comparison)
    assert comparison.slots[0].shared_and_allowed is True
    assert comparison.slots[1].shared_and_allowed is False
    assert comparison.shared_version_ids == (UUID(int=shared),)


# ---------------------------------------------------------------------------
# 不匹配的情况
# ---------------------------------------------------------------------------

def test_shared_question_without_anchor_is_a_violation() -> None:
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (5, 1), label="A")
    second = solution(bp, (5, 2), label="B")
    comparison = compare_structure(bp, first, second)
    assert comparison.structurally_matched is False
    codes = {(item.code, item.slot_code) for item in comparison.issues}
    assert ("shared_version_not_anchored", "A1") in codes, codes


def test_shared_family_without_anchor_is_a_violation() -> None:
    """同族不同版本也算重叠：材料/答案相同，A/B 就失去意义。"""
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (1, 2), families=(500, 501), label="A")
    second = solution(bp, (3, 4), families=(500, 502), label="B")
    comparison = compare_structure(bp, first, second)
    assert comparison.structurally_matched is False
    assert "shared_family_not_anchored" in {item.code for item in comparison.issues}
    assert comparison.shared_family_ids == (UUID(int=500),)


def test_anchor_does_not_cover_a_different_question() -> None:
    """锚题只对"被声明的那一道"生效，不能顺带放过同族的另一道题。"""
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (1, 2), families=(500, 501), label="A")
    second = solution(bp, (3, 4), families=(500, 502), label="B")
    comparison = compare_structure(bp, first, second, anchors=(UUID(int=1),))
    assert comparison.structurally_matched is False
    assert "shared_family_not_anchored" in {item.code for item in comparison.issues}


def test_missing_slot_is_reported_per_volume() -> None:
    bp = blueprint("50.00", "50.00")
    full = solution(bp, (1, 2), label="A")
    partial = SolutionManifest(
        variant_label="B",
        selections=full.selections[:1],
        objective_terms=(), objective_value=0, best_bound=0,
        optimality_gap_milli=0, solver_status="optimal", optimal_proven=True,
        structure_report={}, manifest_hash="b" * 64,
    )
    comparison = compare_structure(bp, full, partial)
    assert comparison.structurally_matched is False
    codes = {(item.code, item.message) for item in comparison.issues}
    assert any(code == "slot_missing" and "B 卷" in message for code, message in codes), (
        codes)


def test_slot_score_mismatch_is_reported() -> None:
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (1, 2), label="A")
    wrong = solution(bp, (3, 4), label="B")
    tampered = list(wrong.selections)
    tampered[0] = tampered[0].__class__(
        **{**tampered[0].__dict__, "score_hundredths": 4000})
    wrong = SolutionManifest(**{**wrong.__dict__, "selections": tuple(tampered)})
    comparison = compare_structure(bp, first, wrong)
    assert comparison.structurally_matched is False
    assert "slot_score_mismatch" in {item.code for item in comparison.issues}


def test_duplicate_slot_in_one_volume_is_reported() -> None:
    bp = blueprint("50.00", "50.00")
    full = solution(bp, (1, 2), label="A")
    duplicated = SolutionManifest(
        **{**full.__dict__, "selections": full.selections + (full.selections[0],)})
    comparison = compare_structure(bp, full, duplicated)
    assert "duplicate_slot" in {item.code for item in comparison.issues}


def test_unknown_slot_is_reported() -> None:
    bp = blueprint("100.00")
    full = solution(bp, (1,), label="A")
    stranger = full.selections[0].__class__(
        **{**full.selections[0].__dict__, "slot_code": "Z9"})
    other = SolutionManifest(**{**full.__dict__, "selections": (stranger,)})
    comparison = compare_structure(bp, full, other)
    codes = {item.code for item in comparison.issues}
    assert "unknown_slot" in codes, codes


# ---------------------------------------------------------------------------
# 对照数据（教师比较方案时看的数字）
# ---------------------------------------------------------------------------

def test_difficulty_and_seconds_deltas_are_reported_per_slot() -> None:
    bp = blueprint("50.00", "50.00")
    first = solution(bp, (1, 2), difficulty=2, seconds=600, label="A")
    second = solution(bp, (3, 4), difficulty=4, seconds=900, label="B")
    comparison = compare_structure(bp, first, second)
    assert comparison.slots[0].difficulty_delta == 2
    assert comparison.slots[0].seconds_delta == 300
    assert comparison.extra["difficulty_delta_total"] == 4
    assert comparison.extra["seconds_delta_total"] == 600


def test_unknown_difficulty_gives_no_delta() -> None:
    bp = blueprint("100.00")
    first = solution(bp, (1,), difficulty=None, label="A")
    second = solution(bp, (2,), difficulty=3, label="B")
    comparison = compare_structure(bp, first, second)
    assert comparison.slots[0].difficulty_delta is None, (
        "缺难度不等于难度 0：不能算出一个假的差值"
    )


def test_extra_counts_are_stable() -> None:
    bp = blueprint("50.00", "50.00")
    comparison = compare_structure(
        bp, solution(bp, (1, 2), label="A"), solution(bp, (3, 4), label="B"))
    assert comparison.extra["slots"] == 2
    assert comparison.extra["shared_versions"] == 0
    assert comparison.extra["shared_families"] == 0


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
    print(f"✓ A/B 结构比较测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
