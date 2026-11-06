"""A/B 配对：跑两次 ≠ A/B 卷。

这份测试逐条钉住"什么情况下必须拒绝配对"：

  * 两卷选了同一道题且没有声明锚题 → 拒绝；
  * 两卷用了同族题（材料/答案相同）→ 拒绝；
  * 两卷来自不同蓝图版本 → 拒绝，而且报的是"蓝图不同"而不是一串分值不一致
    （后者会掩盖真正的原因）；
  * 分值结构一致 + 除锚题外不重叠 → 配对成功，并且结论里**仍然**写着
    「不代表统计等值」。

运行：`python server/tests/test_assembly_ab.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.ab import AbPairError, pair_solutions  # noqa: E402
from app.assembly.comparator import STRUCTURE_NOTE  # noqa: E402
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

BP = UUID("4a0e0000-0000-5000-8000-0000000000aa")
OTHER_BP = UUID("4a0e0000-0000-5000-8000-0000000000bb")


def blueprint(version: UUID = BP, *scores: str) -> BlueprintSnapshot:
    slots = tuple(
        SlotSpec(slot_id=slot_identity(version, "A", f"A{index}"), code=f"A{index}",
                 section_code="A", score=Decimal(score), estimated_seconds=600)
        for index, score in enumerate(scores, 1)
    )
    return BlueprintSnapshot(
        blueprint_version_id=version, course_id=UUID(int=101),
        offering_id=UUID(int=102), release_context_id=UUID(int=103),
        purpose="formal", total_score=sum((Decimal(s) for s in scores), Decimal("0")),
        duration_seconds=5400, slots=slots,
        hard_constraints=HardConstraints(), soft_objectives=SoftObjectives(),
    )


def solution(bp: BlueprintSnapshot, versions: tuple[int, ...], *,
             families: tuple[int, ...] | None = None, label: str = "A",
             optimal: bool = True) -> SolutionManifest:
    families = families or tuple(9000 + number for number in versions)
    selections = tuple(
        AssemblySelection(
            slot_id=slot.slot_id, slot_code=slot.code,
            exercise_version_id=UUID(int=version), exercise_instance_id=None,
            stable_exercise_id=UUID(int=10_000 + version),
            exercise_family_id=UUID(int=families[index]),
            source_release_id=UUID(int=30_000 + version), decision_seq=1,
            score_hundredths=score_to_hundredths(slot.score),
            estimated_seconds=600, difficulty=3,
            dependency_hash=f"dep-{version}", content_hash=f"content-{version}",
        )
        for index, (slot, version) in enumerate(zip(bp.slots, versions))
    )
    return SolutionManifest(
        variant_label=label, selections=selections, objective_terms=(),
        objective_value=0, best_bound=0, optimality_gap_milli=0,
        solver_status="optimal" if optimal else "feasible",
        optimal_proven=optimal, structure_report={},
        manifest_hash=label.ljust(64, "0"),
    )


# ---------------------------------------------------------------------------
# 可以配对
# ---------------------------------------------------------------------------

def test_disjoint_pair_is_accepted_and_keeps_the_honest_note() -> None:
    bp = blueprint(BP, "50.00", "50.00")
    first = solution(bp, (1, 2), label="A")
    second = solution(bp, (3, 4), label="B")
    pair = pair_solutions(bp, first, second,
                          primary_blueprint_version_id=BP,
                          peer_blueprint_version_id=BP)
    assert pair.labels == ("A", "B")
    payload = pair.payload()
    assert payload["structurally_matched"] is True
    assert payload["statistical_equivalence"] is False
    assert payload["note"] == STRUCTURE_NOTE
    assert "不代表统计等值" in payload["note"]
    assert payload["issues"] == []
    # 下一步必须说清"还要分别送审与渲染"，否则教师会以为配对即发布
    assert "分别提交审核" in payload["next_step"]


def test_anchor_shared_between_the_pair_is_accepted() -> None:
    bp = blueprint(BP, "40.00", "60.00")
    shared = 9
    first = solution(bp, (shared, 1), label="A")
    second = solution(bp, (shared, 2), label="B")
    pair = pair_solutions(bp, first, second, anchors=(UUID(int=shared),))
    assert pair.payload()["anchors"] == [str(UUID(int=shared))]
    assert pair.comparison.slots[0].shared_and_allowed is True


def test_pair_works_without_declaring_blueprint_versions() -> None:
    bp = blueprint(BP, "100.00")
    pair = pair_solutions(bp, solution(bp, (1,), label="A"),
                          solution(bp, (2,), label="B"))
    assert pair.blueprint_version_id == BP


# ---------------------------------------------------------------------------
# 必须拒绝
# ---------------------------------------------------------------------------

def test_same_question_without_anchor_is_rejected() -> None:
    bp = blueprint(BP, "50.00", "50.00")
    first = solution(bp, (5, 1), label="A")
    second = solution(bp, (5, 2), label="B")
    try:
        pair_solutions(bp, first, second)
    except AbPairError as error:
        assert error.code == "ab_pair_rejected"
        assert "shared_version_not_anchored" in str(error), str(error)
        assert error.comparison is not None
    else:
        raise AssertionError("两卷共用同一道题且没有锚题声明，必须拒绝配对")


def test_same_family_without_anchor_is_rejected() -> None:
    bp = blueprint(BP, "50.00", "50.00")
    first = solution(bp, (1, 2), families=(700, 701), label="A")
    second = solution(bp, (3, 4), families=(700, 702), label="B")
    try:
        pair_solutions(bp, first, second)
    except AbPairError as error:
        assert "shared_family_not_anchored" in str(error), str(error)
    else:
        raise AssertionError("同族题共用材料与答案，必须拒绝配对")


def test_different_blueprint_versions_are_rejected_before_structure() -> None:
    """版本不同时报"蓝图不同"，不是一串分值不一致——后者会掩盖真正原因。"""
    first_bp = blueprint(BP, "50.00", "50.00")
    second_bp = blueprint(OTHER_BP, "50.00", "50.00")
    first = solution(first_bp, (1, 2), label="A")
    second = solution(second_bp, (3, 4), label="B")
    try:
        pair_solutions(first_bp, first, second,
                       primary_blueprint_version_id=BP,
                       peer_blueprint_version_id=OTHER_BP)
    except AbPairError as error:
        assert error.code == "ab_blueprint_mismatch"
        assert "不同的蓝图版本" in str(error)
    else:
        raise AssertionError("不同蓝图版本不能配成 A/B")


def test_missing_slot_in_peer_is_rejected() -> None:
    bp = blueprint(BP, "50.00", "50.00")
    first = solution(bp, (1, 2), label="A")
    partial = SolutionManifest(
        **{**solution(bp, (3, 4), label="B").__dict__,
           "selections": solution(bp, (3, 4), label="B").selections[:1]})
    try:
        pair_solutions(bp, first, partial)
    except AbPairError as error:
        assert "slot_missing" in str(error), str(error)
    else:
        raise AssertionError("B 卷缺槽位不能配对")


def test_pair_rejection_carries_issues_payload() -> None:
    bp = blueprint(BP, "100.00")
    first = solution(bp, (1,), label="A")
    second = solution(bp, (1,), label="B")
    try:
        pair_solutions(bp, first, second)
    except AbPairError as error:
        assert error.issues, "拒绝必须带可展示的问题清单"
        assert error.issues[0]["code"] == "shared_version_not_anchored"
        assert error.issues[0]["slot_code"] == "A1"
    else:
        raise AssertionError("同一道题不能配成 A/B")


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
    print(f"✓ A/B 配对测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
