"""受控负载：1,000 候选 / 50 槽位的组卷规模。

**这个脚本只衡量它能衡量的东西。** 计划书要求"1,000 候选/50 槽位受控压测"，
但 CP-SAT 在本机装不上（无外网），所以这里做的是：

  1. 真的构造 50 槽位蓝图与 1,000 道候选（含曝光/难度/时长/知识点）；
  2. 真的跑候选 manifest 固定（哈希、排序、排除）；
  3. 真的跑求解前预检（槽位候选集、覆盖上界、题型上界、时长下界）；
  4. 如果安装了 ortools，就真的跑一次求解；没装则**明确跳过并打印原因**。

因此下面的数字可以回答"固定候选与预检在这个规模下要多久"，
**不能**回答"求解要多久"。把它当成 SLO 就是在骗自己——脚本输出里也这么写。

用法：`.venv/Scripts/python.exe scripts/load_assembly.py [--candidates 1000] [--slots 50]`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.candidate import AssemblyCandidateService, CandidateSourceResult  # noqa: E402
from app.assembly.contracts import (  # noqa: E402
    BlueprintSnapshot,
    Candidate,
    ConceptWeight,
    HardConstraints,
    SlotSpec,
    SoftObjectives,
    SolverConfig,
    slot_identity,
)
from app.assembly.diagnostics import (  # noqa: E402
    candidate_supply,
    precheck,
    requires_seconds,
)
from app.assembly.solver_port import default_solver, ortools_version  # noqa: E402
from app.contracts.candidate import CandidateRecord  # noqa: E402
from app.contracts.hashing import canonical_hash  # noqa: E402
from app.contracts.paper import CandidateEvidence  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
BP = UUID("4a0e0000-0000-5000-8000-0000000000ff")


def build_blueprint(slot_count: int) -> BlueprintSnapshot:
    """50 个槽位、总分 100：每槽 2 分（用整数分表示，避免任何浮点）。"""
    slots = tuple(
        SlotSpec(
            slot_id=slot_identity(BP, "A", f"S{index:02d}"),
            code=f"S{index:02d}", section_code="A",
            score=Decimal("2.00"), estimated_seconds=120,
            allowed_kinds=("proof", "calculation") if index % 2 else ("proof",),
        )
        for index in range(1, slot_count + 1)
    )
    return BlueprintSnapshot(
        blueprint_version_id=BP,
        course_id=ref_uuid("course", "load-test"),
        offering_id=ref_uuid("offering", "load-test-2026"),
        release_context_id=CONTEXT,
        purpose="formal",
        total_score=Decimal("2.00") * slot_count,
        duration_seconds=slot_count * 120,
        slots=slots,
        hard_constraints=HardConstraints(
            max_duration_seconds=slot_count * 180,
            coverage_requirements=(),
            kind_requirements=(),
        ),
        soft_objectives=SoftObjectives(target_duration_seconds=slot_count * 120,
                                      duration_weight=1),
        assembly_mode="single",
        visible_from=NOW,
    )


def build_candidates(count: int, concepts: int = 40) -> list[Candidate]:
    candidates: list[Candidate] = []
    for index in range(1, count + 1):
        concept = UUID(int=500 + (index % concepts))
        candidates.append(Candidate(
            exercise_version_id=UUID(int=100_000 + index),
            stable_exercise_id=UUID(int=200_000 + index),
            exercise_family_id=UUID(int=300_000 + index),
            source_release_id=UUID(int=400_000 + index),
            decision_seq=1,
            exercise_kind="proof" if index % 2 else "calculation",
            dependency_hash=canonical_hash({"dep": index}),
            content_hash=canonical_hash({"content": index}),
            concept_weights=(ConceptWeight(concept, "primary", 1),),
            learning_outcome_ids=(),
            difficulty=1 + (index % 5),
            cognitive_level=1 + (index % 4),
            estimated_seconds=90 + (index % 60),
            recent_use_count=index % 7,
            exposure_count=index % 3,
            preferred=(index % 11 == 0),
        ))
    return candidates


def to_record(candidate: Candidate) -> CandidateRecord:
    """把合成候选包成阶段 A 的记录形状（服务要这份形状才认）。"""
    evidence = CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=candidate.source_release_id,
        exercise_id=candidate.stable_exercise_id,
        exercise_version_id=candidate.exercise_version_id,
        exercise_instance_id=None,
        exercise_family_id=candidate.exercise_family_id,
        exercise_kind=candidate.exercise_kind,
        concept_ids=tuple(item.concept_id for item in candidate.concept_weights),
        learning_outcome_ids=candidate.learning_outcome_ids,
        difficulty_label=f"difficulty-{candidate.difficulty}",
        estimated_seconds=candidate.estimated_seconds,
        decision_seqs={"exercise": 1},
        latest_decisions={"exercise": "approved"},
        dependency_manifest_hash=candidate.dependency_hash,
        content_hash=candidate.content_hash,
        in_context=True, license_valid=True, revoked=False, hidden_evaluation=False,
        private_draft=False, parameterized=False, instance_approved=True,
        dependencies_complete=True, render_ready=True, language_pair_valid=True,
        purpose_allowed=True, cloud_allowed=True,
    )
    return CandidateRecord(evidence=evidence, release_state="included",
                           review_status="teacher-reviewed", license="licensed",
                           answer_present=True)


class SyntheticSource:
    def __init__(self, records: list[CandidateRecord]) -> None:
        self.records = tuple(records)

    def load(self, *, actor_id, blueprint):
        return CandidateSourceResult(
            release_context_id=blueprint.release_context_id,
            accepted=self.records, rejected=(), exposure_events=(),
            as_of=NOW, candidate_snapshot_id=UUID(int=777),
        )


QUIET = False


def timed(label: str, fn):
    started = time.perf_counter()
    value = fn()
    elapsed = (time.perf_counter() - started) * 1000
    if not QUIET:
        print(f"  {label:<28} {elapsed:8.1f} ms")
    return value, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="组卷受控负载（不含求解器部分）")
    parser.add_argument("--candidates", type=int, default=1000)
    parser.add_argument("--slots", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    parser.add_argument("--seconds", type=int, default=30, help="求解时限（秒）")
    args = parser.parse_args()
    global QUIET
    # --json 时 stdout 必须只有 JSON：混进一行耗时打印，调用方就解析不了。
    QUIET = args.json

    timings: dict[str, float] = {}
    if not args.json:
        print(f"规模：{args.candidates} 道候选 / {args.slots} 个槽位\n")

    blueprint, timings["build_blueprint"] = timed(
        "构造蓝图", lambda: build_blueprint(args.slots))
    candidates, timings["build_candidates"] = timed(
        "构造候选", lambda: build_candidates(args.candidates))
    records = [to_record(item) for item in candidates]

    service = AssemblyCandidateService(source=SyntheticSource(records), clock=lambda: NOW)
    manifest, timings["candidate_manifest"] = timed(
        "固定候选 manifest", lambda: service.snapshot(
            actor_id=UUID(int=1), blueprint=blueprint))

    need_seconds = requires_seconds(blueprint)
    supply, timings["slot_supply"] = timed(
        "计算每槽候选供给",
        lambda: {slot.code: len(__import__(
            "app.assembly.diagnostics", fromlist=["slot_candidates"]
        ).slot_candidates(slot, manifest.candidates, require_seconds=need_seconds))
            for slot in blueprint.slots})
    issues, timings["precheck"] = timed(
        "求解前预检", lambda: precheck(blueprint, manifest))
    supplied, timings["supply_helper"] = timed(
        "供给（公开函数）", lambda: candidate_supply(blueprint, manifest))

    solver_ran = False
    solve_ms = None
    solver_note = "未运行（本机未安装 ortools）"
    installed = ortools_version()
    if installed:
        solver = default_solver()
        config = SolverConfig(time_limit_seconds=args.seconds, max_solutions=1)
        started = time.perf_counter()
        result = solver.solve(blueprint, manifest, config)
        solve_ms = (time.perf_counter() - started) * 1000
        solver_ran = True
        reason = result.diagnostics.get("reason", "-")
        variables = result.diagnostics.get("variables", 0)
        solver_note = (f"{result.status}（ortools {installed}，{solve_ms:.0f} ms；"
                       f"{reason}；变量 {variables}）")
        timings["solve"] = solve_ms

    summary = {
        "scale": {"candidates": args.candidates, "slots": args.slots},
        "candidate_manifest_hash": manifest.manifest_hash,
        "candidates_in_manifest": len(manifest.candidates),
        "exclusions": len(manifest.exclusions),
        "min_supply": min(supplied.values()) if supplied else 0,
        "max_supply": max(supplied.values()) if supplied else 0,
        "precheck_issues": [item.code for item in issues],
        "timings_ms": {key: round(value, 2) for key, value in timings.items()},
        "solver": {"ran": solver_ran, "note": solver_note,
                   "ortools": installed},
        "limits": "只测候选固定与预检；求解耗时未计入（缺 ortools 时不虚报）",
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"\n  候选清单 {manifest.manifest_hash[:16]}…（{len(manifest.candidates)} 道）")
    print(f"  每槽可选：最少 {summary['min_supply']} / 最多 {summary['max_supply']}")
    print(f"  预检问题：{summary['precheck_issues'] or '无'}")
    print(f"  求解：{solver_note}")
    if solver_ran:
        print("\n说明：上面的毫秒数覆盖候选固定、预检**与求解**。"
              "这是单机单次运行的耗时，不是生产 SLO——并发与真实候选分布都没测。")
    else:
        print("\n说明：上面的毫秒数只覆盖候选固定与预检。求解耗时没有计入——"
              "这台机器没装 ortools，任何「求解很快」的说法都会是假的。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
