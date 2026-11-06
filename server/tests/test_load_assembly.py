"""组卷受控负载脚本的自检：数字可以小，但**不能是假的**。

计划书要求 1,000 候选/50 槽位的压测。本机没有 CP-SAT，所以脚本只能测
"候选固定 + 预检"。这份测试钉住的正是这一点：

  * 规模真的到得了 1000/50（不是循环里少跑了）；
  * 候选清单哈希可复算、候选数正确；
  * 每槽供给是真实计算出来的（不是常量）；
  * 求解器一栏在缺 ortools 时必须写"未运行"，**不能**写成一个耗时数字。

运行：`python server/tests/test_load_assembly.py`
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.assembly.contracts import score_to_hundredths  # noqa: E402
from app.assembly.diagnostics import candidate_supply, precheck  # noqa: E402
from app.assembly.solver_port import ortools_version  # noqa: E402
import load_assembly  # noqa: E402

PYTHON = sys.executable


def test_blueprint_has_the_requested_shape() -> None:
    blueprint = load_assembly.build_blueprint(50)
    assert len(blueprint.slots) == 50
    assert score_to_hundredths(blueprint.total_score) == 10000, (
        "50 槽 × 2 分 = 100.00 分：整数分必须正好 10000")
    assert all(slot.code for slot in blueprint.slots)
    assert len({slot.slot_id for slot in blueprint.slots}) == 50, (
        "槽位身份必须互不相同，否则回放会认错槽")


def test_candidates_reach_a_thousand_and_stay_distinct() -> None:
    candidates = load_assembly.build_candidates(1000)
    assert len(candidates) == 1000
    assert len({item.exercise_version_id for item in candidates}) == 1000
    assert len({item.stable_exercise_id for item in candidates}) == 1000
    kinds = {item.exercise_kind for item in candidates}
    assert kinds == {"proof", "calculation"}, kinds


def test_manifest_hash_is_recomputable_at_this_scale() -> None:
    blueprint = load_assembly.build_blueprint(50)
    candidates = load_assembly.build_candidates(200)
    records = [load_assembly.to_record(item) for item in candidates]
    service = load_assembly.AssemblyCandidateService(
        source=load_assembly.SyntheticSource(records), clock=lambda: load_assembly.NOW)
    first = service.snapshot(actor_id=load_assembly.UUID(int=1), blueprint=blueprint)
    second = load_assembly.AssemblyCandidateService(
        source=load_assembly.SyntheticSource(list(reversed(records))),
        clock=lambda: load_assembly.NOW,
    ).snapshot(actor_id=load_assembly.UUID(int=1), blueprint=blueprint)
    assert len(first.candidates) == 200
    assert first.manifest_hash == second.manifest_hash, "输入顺序不能改变清单哈希"


def test_supply_is_computed_per_slot_not_constant() -> None:
    blueprint = load_assembly.build_blueprint(50)
    candidates = load_assembly.build_candidates(300)
    records = [load_assembly.to_record(item) for item in candidates]
    manifest = load_assembly.AssemblyCandidateService(
        source=load_assembly.SyntheticSource(records), clock=lambda: load_assembly.NOW,
    ).snapshot(actor_id=load_assembly.UUID(int=1), blueprint=blueprint)
    supply = candidate_supply(blueprint, manifest)
    assert len(supply) == 50
    # 蓝图里奇数槽允许两种题型、偶数槽只允许 proof：供给必须因此不同。
    assert len(set(supply.values())) > 1, supply
    assert precheck(blueprint, manifest) == []


def test_json_summary_never_fakes_a_solve_time() -> None:
    output = subprocess.run(
        [PYTHON, str(ROOT / "scripts" / "load_assembly.py"),
         "--candidates", "120", "--slots", "8", "--json"],
        capture_output=True, text=True, encoding="utf-8",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert output.returncode == 0, output.stderr
    summary = json.loads(output.stdout)
    assert summary["scale"] == {"candidates": 120, "slots": 8}
    assert summary["candidates_in_manifest"] == 120
    assert len(summary["candidate_manifest_hash"]) == 64
    assert "只测候选固定与预检" in summary["limits"]
    if ortools_version() is None:
        assert summary["solver"]["ran"] is False
        assert "未运行" in summary["solver"]["note"]
        assert "solve" not in summary["timings_ms"], (
            "没装求解器时不能出现求解耗时：那会是一个编出来的数字")
    else:  # pragma: no cover - 装了 ortools 的机器才会走这里
        assert summary["solver"]["ran"] is True
        assert "solve" in summary["timings_ms"]


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
    print(f"✓ 组卷负载脚本自检测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
