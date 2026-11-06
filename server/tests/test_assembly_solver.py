"""CP-SAT 求解器测试（**当前环境无法运行：未安装 ortools**）。

这份文件是 TDD 里"先写失败的测试"那一步的产物，但它现在处于一个特殊状态：
本机没有外网，`ortools==9.11.4210` 装不上，所以下面的断言**一次都没有跑过**。

因此它：

* 没有登记进 `npm run check`（没装依赖时它只能"跳过"，而跳过看起来像通过）；
* 在依赖缺失时打印明确的"未验证"，并以退出码 0 结束——这是为了让
  `npm run check` 保持诚实（不把一个跑不了的东西算进通过数），**不是**在声称通过；
* 装好依赖后应当用 `python server/tests/test_assembly_solver.py`
  单独跑，并看它是否真的全绿。

要钉住的语义（都是规格里写死的）：

1. 分数用整数分进模型：100.00 分 = 10000，不会出现浮点误差；
2. 同一道题（stable id）不会进两个槽位；蓝图互斥组里最多一道题；
3. 总分等式、分节分、时长上限、覆盖要求、题型区间都由 CP-SAT 执行；
4. `map_status` 不把 FEASIBLE 说成 OPTIMAL，也不把超时说成最优；
5. 最多 3 个方案且互不相同；非首个方案不继承"已证明最优"；
6. 无解时没有方案（调用方据此拒绝建卷）。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

MISSING_DEPENDENCY = None
try:  # pragma: no cover - 本机走的是 except 分支
    from app.assembly.solver import CpSatSolver, map_status
    from ortools.sat.python import cp_model
except ImportError as error:  # pragma: no cover
    MISSING_DEPENDENCY = str(error)

from app.assembly.contracts import (  # noqa: E402
    BlueprintSnapshot,
    Candidate,
    ConceptWeight,
    CoverageRequirement,
    HardConstraints,
    KindRequirement,
    RunStatus,
    SlotSpec,
    SoftObjectives,
    SolverConfig,
    make_candidate_manifest,
    slot_identity,
)

BP = UUID("4a0e0000-0000-5000-8000-000000000002")
CONCEPT = UUID(int=77)


def slot(code: str, score: str, *, section: str = "A", **changes) -> SlotSpec:
    values = dict(slot_id=slot_identity(BP, section, code), code=code,
                  section_code=section, score=Decimal(score), estimated_seconds=600)
    values.update(changes)
    return SlotSpec(**values)  # type: ignore[arg-type]


def blueprint(slots, *, total: str = "100.00", hard: HardConstraints | None = None,
              soft: SoftObjectives | None = None) -> BlueprintSnapshot:
    return BlueprintSnapshot(
        blueprint_version_id=BP, course_id=UUID(int=101), offering_id=UUID(int=102),
        release_context_id=UUID(int=103), purpose="formal",
        total_score=Decimal(total), duration_seconds=5400, slots=tuple(slots),
        hard_constraints=hard or HardConstraints(), soft_objectives=soft or SoftObjectives())


def candidate(number: int, *, kind: str = "proof", seconds: int | None = 600,
              family: int | None = None, stable: int | None = None,
              concepts: tuple[ConceptWeight, ...] = (), preferred: bool = False,
              recent: int = 0, exposure: int = 0) -> Candidate:
    return Candidate(
        exercise_version_id=UUID(int=number),
        stable_exercise_id=UUID(int=(stable if stable is not None else 10_000 + number)),
        exercise_family_id=UUID(int=(family if family is not None else 20_000 + number)),
        source_release_id=UUID(int=30_000 + number),
        decision_seq=1, exercise_kind=kind,
        dependency_hash=f"dep-{number}", content_hash=f"content-{number}",
        concept_weights=concepts, difficulty=3, estimated_seconds=seconds,
        preferred=preferred, recent_use_count=recent, exposure_count=exposure,
    )


def config(**changes) -> SolverConfig:
    values = dict(random_seed=7, time_limit_seconds=5, max_solutions=1)
    values.update(changes)
    return SolverConfig(**values)


def manifest(bp, candidates):
    return make_candidate_manifest(bp, candidates, [])


# ---------------------------------------------------------------------------
# 状态映射（不需要 CP-SAT 也能测，但依赖 cp_model 常量）
# ---------------------------------------------------------------------------

def test_status_never_promotes_feasible_or_timeout() -> None:
    assert map_status(cp_model.OPTIMAL, 1, False) == RunStatus.OPTIMAL
    assert map_status(cp_model.FEASIBLE, 1, False) == RunStatus.FEASIBLE
    assert map_status(cp_model.FEASIBLE, 1, True) == RunStatus.TIMEOUT
    assert map_status(cp_model.INFEASIBLE, 0, False) == RunStatus.INFEASIBLE
    assert map_status(cp_model.MODEL_INVALID, 0, False) == RunStatus.FAILED
    assert map_status(cp_model.UNKNOWN, 0, True) == RunStatus.TIMEOUT


# ---------------------------------------------------------------------------
# 硬约束
# ---------------------------------------------------------------------------

def test_decimal_score_conversion_has_no_float_error() -> None:
    bp = blueprint([slot("A1", "33.33"), slot("A2", "33.33"), slot("A3", "33.34")])
    candidates = [candidate(n) for n in (1, 2, 3, 4)]
    result = CpSatSolver().solve(bp, manifest(bp, candidates), config())
    assert result.status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE}, result.status
    selected = result.solution_sets[0].variants[0].selections
    assert sum(item.score_hundredths for item in selected) == 10000


def test_same_stable_exercise_is_never_placed_twice() -> None:
    """两个槽位、两道候选：只能各用一道，同一 stable id 不能进两次。

    这里只有两道题可选，所以结果必然是"两道都用上、各占一个槽位"——
    要断言的是**互不相同**，不是"有一道没用上"（上一版断言写反了，
    第一次真实运行就暴露了）。
    """
    bp = blueprint([slot("A1", "50.00"), slot("A2", "50.00")])
    shared = candidate(1, stable=999)
    other = candidate(2, stable=998)
    result = CpSatSolver().solve(bp, manifest(bp, [shared, other]), config())
    selections = result.solution_sets[0].variants[0].selections
    versions = [item.exercise_version_id for item in selections]
    stables = [item.stable_exercise_id for item in selections]
    assert len(versions) == 2, versions
    assert len(set(versions)) == 2, f"同一道题进了两个槽位：{versions}"
    assert len(set(stables)) == 2, f"同一 stable id 进了两个槽位：{stables}"
    assert set(versions) == {shared.exercise_version_id, other.exercise_version_id}


def test_blueprint_exclusion_group_allows_at_most_one() -> None:
    first, second = candidate(1), candidate(2)
    bp = blueprint(
        [slot("A1", "50.00"), slot("A2", "50.00")],
        hard=HardConstraints(exclusion_groups=((first.exercise_version_id,
                                                second.exercise_version_id),)),
    )
    result = CpSatSolver().solve(bp, manifest(bp, [first, second, candidate(3)]), config())
    selections = result.solution_sets[0].variants[0].selections
    hits = {item.exercise_version_id for item in selections} & {
        first.exercise_version_id, second.exercise_version_id}
    assert len(hits) <= 1, hits


def test_duration_upper_bound_is_enforced() -> None:
    bp = blueprint([slot("A1", "50.00"), slot("A2", "50.00")],
                   hard=HardConstraints(max_duration_seconds=700))
    # 每道题 600 秒：两槽必然 1200 秒 > 700，必须无解
    result = CpSatSolver().solve(bp, manifest(bp, [candidate(1), candidate(2)]), config())
    assert result.status == RunStatus.INFEASIBLE
    assert result.solution_sets == ()


def test_coverage_and_kind_requirements_are_enforced() -> None:
    weights = (ConceptWeight(CONCEPT, "primary", 1),)
    covered = candidate(1, concepts=weights, kind="proof")
    other = candidate(2, kind="calculation")
    bp = blueprint([slot("A1", "50.00"), slot("A2", "50.00")],
                   hard=HardConstraints(
                       coverage_requirements=(
                           CoverageRequirement(CONCEPT, "concept", "primary", 1),),
                       kind_requirements=(KindRequirement("A", "proof", 1, 1),)))
    result = CpSatSolver().solve(bp, manifest(bp, [covered, other]),
                                 config(max_solutions=3))
    for solution_set in result.solution_sets:
        for variant in solution_set.variants:
            kinds = [item.exercise_version_id for item in variant.selections]
            assert covered.exercise_version_id in kinds, "覆盖要求必须选中那一道题"


def test_blueprint_score_mismatch_is_precheck_not_solver() -> None:
    """总分与槽位分对不上：由**预检**拦下，求解器不再用一条恒真等式去"发现"它。

    这条断言是首跑后的修正：原来总分等式写在模型里，于是"蓝图自相矛盾"表现为
    INFEASIBLE；去掉那条冗余等式后，求解器会正常给出方案——因为槽位分之和本来
    就不在选择里。蓝图自洽归 precheck 管（`blueprint_score_mismatch`），
    求解器不该替它背锅。
    """
    from app.assembly.diagnostics import precheck

    bp = blueprint([slot("A1", "50.00"), slot("A2", "60.00")], total="100.00")
    built_manifest = manifest(bp, [candidate(1), candidate(2)])
    assert "blueprint_score_mismatch" in {item.code for item in precheck(bp, built_manifest)}


def test_infeasible_when_a_slot_has_no_eligible_candidate() -> None:
    """某槽位一个合格候选都没有 → 无解，且一条方案都不给。"""
    bp = blueprint([slot("A1", "50.00"),
                    slot("A2", "50.00", allowed_kinds=("calculation",))])
    proof_only = [candidate(1, kind="proof"), candidate(2, kind="proof")]
    result = CpSatSolver().solve(bp, manifest(bp, proof_only), config())
    assert result.status == RunStatus.INFEASIBLE
    assert result.solution_sets == ()
    assert result.has_solutions is False


def test_timeout_without_a_solution_is_not_reported_as_infeasible() -> None:
    """超时与无解必须分开报：前者调大时限或缩小规模，后者要改蓝图。"""
    bp = blueprint([slot("A1", "100.00")])
    many = [candidate(n) for n in range(1, 401)]
    result = CpSatSolver().solve(bp, manifest(bp, many),
                                 config(time_limit_seconds=1, max_solutions=3))
    if result.status == RunStatus.TIMEOUT:
        assert result.diagnostics["reason"] == "time_limit_no_solution"
        assert result.diagnostics["verdict"] == "timeout"
    else:
        # 机器快、1 秒内解出来也算正常：此时必须是"有解"，且 reason 不是无解
        assert result.solution_sets
        assert result.diagnostics.get("reason") != "hard_constraints"


# ---------------------------------------------------------------------------
# 软目标与多方案
# ---------------------------------------------------------------------------

def test_preference_and_recent_use_influence_the_choice() -> None:
    good = candidate(1, preferred=True, recent=0, exposure=0)
    bad = candidate(2, preferred=False, recent=9, exposure=9)
    bp = blueprint([slot("A1", "100.00")],
                   soft=SoftObjectives(teacher_preference_weight=11,
                                       recent_use_weight=3,
                                       exposure_balance_weight=2))
    result = CpSatSolver().solve(bp, manifest(bp, [bad, good]), config())
    chosen = result.solution_sets[0].variants[0].selections[0]
    assert chosen.exercise_version_id == good.exercise_version_id


def test_three_solutions_are_distinct_and_extras_are_not_optimal() -> None:
    bp = blueprint([slot("A1", "100.00")])
    candidates = [candidate(n) for n in range(1, 7)]
    result = CpSatSolver().solve(bp, manifest(bp, candidates), config(max_solutions=3))
    variants = result.solution_sets[0].variants
    assert len(variants) == 3
    hashes = [item.manifest_hash for item in variants]
    assert len(set(hashes)) == 3, "三个方案必须互不相同"
    assert variants[0].optimal_proven is True
    assert all(item.optimal_proven is False for item in variants[1:]), (
        "排除前解后得到的备选不继承'已证明最优'"
    )


def test_max_solutions_is_capped_at_three() -> None:
    try:
        SolverConfig(max_solutions=4)
    except ValueError as error:
        assert "3" in str(error), str(error)
    else:
        raise AssertionError("方案数上限 3 必须由类型本身拒绝")


# ---------------------------------------------------------------------------
# 组合根：装了 ortools 就必须真的用它
# ---------------------------------------------------------------------------

def test_default_solver_uses_cp_sat_when_available() -> None:
    """`default_solver()` 不能永远返回"不可用"——服务器上 ortools 已装。

    这条断言是给一次真实事故加的：9.11 装不上的时期，`default_solver()` 被写成
    "一律返回 UnavailableSolver"，等服务器装好 9.15 后，脚本仍在报
    "本机未安装 ortools"，白查一轮。
    """
    from app.assembly.solver_port import UnavailableSolver, default_solver, ortools_version

    solver = default_solver()
    if ortools_version() is None:  # pragma: no cover - 只在没装依赖的机器上走
        assert isinstance(solver, UnavailableSolver)
    else:
        assert isinstance(solver, CpSatSolver)
        assert solver.solver_version not in ("", "unknown")


def run_tests() -> int:
    if MISSING_DEPENDENCY is not None:
        print("⊘ 未验证：本机未安装 ortools，CP-SAT 求解器的断言一次都没有运行过。")
        print(f"  原因：{MISSING_DEPENDENCY}")
        print("  安装后重新运行：python -m pip install"
              " -r server/requirements-assembly.txt")
        print("  然后：python server/tests/test_assembly_solver.py")
        # 退出码 0 是给"批量跑全部套件"用的：这不是通过，是"没跑"。本文件刻意
        # 不在 npm run check 的清单里，避免"跳过"被当成"通过"。
        return 0
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
    print(f"✓ CP-SAT 求解器测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
