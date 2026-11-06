"""worker 日志：不能让"有方案"读成"验证过"。

`scripts/run_worker.py` 的 `describe()` 是运维唯一会读的一行字。求解任务的产物不是
"知识点/道题"，如果沿用旧措辞就会打印"成功：0 个知识点、0 道题 → None"——
看起来像失败，实际可能是"无解"这个正常结论。反过来，2 个方案但没证明最优时
不写"（未证明最优）"，读的人会以为两个方案都验证过了。

运行：`python server/tests/test_worker_describe.py`
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_worker.py"


def load():
    spec = importlib.util.spec_from_file_location("run_worker_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_worker_under_test"] = module
    try:
        spec.loader.exec_module(module)  # 只为了拿 describe()，不跑 main()
    except SystemExit:  # pragma: no cover - 模块没有顶层执行
        pass
    return module


MODULE = load()


def job(**kwargs) -> dict:
    base = {"id": 1, "status": "succeeded", "kind": "paper-assembly",
            "result": {"assembly_run_id": "run-1", "status": "optimal",
                       "solutions": 1, "optimal_proven": True, "solver": "cp-sat"}}
    base.update(kwargs)
    return base


def test_optimal_solution_is_labelled_proven() -> None:
    text = MODULE.describe(job())
    assert "已证明最优" in text, text
    assert "run-1" in text


def test_feasible_solutions_are_marked_unproven() -> None:
    text = MODULE.describe(job(result={
        "assembly_run_id": "run-2", "status": "feasible",
        "solutions": 3, "optimal_proven": False, "solver": "cp-sat"}))
    assert "3 个方案" in text and "未证明最优" in text, text
    assert "已证明最优" not in text


def test_infeasible_run_is_not_described_as_a_pointless_success() -> None:
    text = MODULE.describe(job(result={
        "assembly_run_id": "run-3", "status": "infeasible",
        "solutions": 0, "optimal_proven": False, "solver": "cp-sat"}))
    assert "infeasible" in text
    assert "0 个方案" in text
    # 旧的通用措辞会在这里打印"0 个知识点、0 道题 → None"，那是误导
    assert "知识点" not in text, text


def test_unavailable_solver_is_visible_in_the_log_line() -> None:
    text = MODULE.describe(job(result={
        "assembly_run_id": "run-4", "status": "failed", "solutions": 0,
        "optimal_proven": False, "solver": "unavailable"}))
    assert "unavailable" in text, "缺求解器这件事必须出现在日志里"


def test_render_branch_is_untouched() -> None:
    text = MODULE.describe({"id": 2, "status": "succeeded", "kind": "paper-render",
                            "result": {"render_kind": "student", "byte_size": 10,
                                       "output_hash": "abc123", "paper_render_id": "p1"}})
    assert "student" in text and "字节" in text


def test_failure_line_stays_plain() -> None:
    text = MODULE.describe({"id": 3, "status": "failed", "kind": "paper-assembly",
                            "error": "AssemblyJobError: 缺少 assembly_run_id"})
    assert text.startswith("失败："), text
    assert "assembly_run_id" in text


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
    print(f"✓ worker 日志措辞测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
