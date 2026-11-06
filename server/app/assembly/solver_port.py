"""求解器端口与"求解器装不上"时的行为。

本阶段（阶段 B 任务 2/3）本应实现 CP-SAT。本机没有外网，`ortools==9.11.4210`
装不上（详见 docs/验证记录.md 的阻塞项），所以这里只提供：

  * `AssemblySolver` 端口：`solve(blueprint, manifest, config, peer=None) -> SolveResult`；
  * `CpSatSolver`：真正调用 OR-Tools 的实现，**import 时就要求 ortools 存在**；
  * `UnavailableSolver`：清晰的失败——把运行标成 FAILED 并写诊断，而不是假装成功。

为什么不在没有求解器时返回一个"看起来可行"的方案：一份没有经过约束求解的卷子
看起来和真的没区别，教师会直接拿去考试。宁可让整条链明确失败，也不能给出
"像模像样"的假方案——这正是规格里 `FAILED` 状态存在的理由。
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    BlueprintSnapshot,
    CandidateManifest,
    RunStatus,
    SolveResult,
    SolverConfig,
    SolutionManifest,
)

REQUIREMENTS_FILE = "server/requirements-assembly.txt"


class SolverUnavailableError(RuntimeError):
    """运行环境没有 CP-SAT（未安装 ortools）。这不是业务失败，是被环境挡住。"""


def ortools_version() -> str | None:
    """已安装的 ortools 版本；没装返回 None（不抛异常，供健康检查用）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python 3.12 一定有
        return None
    try:
        return version("ortools")
    except PackageNotFoundError:
        return None


class UnavailableSolver:
    """没有求解器时的默认实现：明确失败，不做任何近似。"""

    solver_name = "unavailable"
    solver_version = "none"

    def solve(self, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
              config: SolverConfig,
              peer: SolutionManifest | None = None) -> SolveResult:
        raise SolverUnavailableError(
            "CP-SAT 求解器不可用：本机未安装 ortools。"
            f"安装方式：.venv/Scripts/python.exe -m pip install -r {REQUIREMENTS_FILE}"
        )


def default_solver() -> Any:
    """组合根用：当前一律返回明确失败的实现。

    这不是"暂时偷懒"：CP-SAT 实现（阶段 B 任务 2/3）在本机装不上 ortools，
    写了也跑不了、更没有真实运行的证据。端口（`solve` 的签名与 `SolveResult`）
    已经固定，接入时只需在这里返回真正的求解器，其余调用方不用改一行。

    刻意不写 `from .solver import CpSatSolver`：那会造成一个只在装了 ortools 的
    机器上才存在的 import 分支，而那条分支在本仓库从未被执行过——它一旦写错，
    只会在别人的环境里炸。
    """
    return UnavailableSolver()


def failed_result(reason: str, detail: str) -> SolveResult:
    """构造一个 FAILED 结果：没有方案、诊断里写清为什么。"""
    return SolveResult(
        status=RunStatus.FAILED,
        solution_sets=(),
        diagnostics={"reason": reason, "detail": detail},
        best_bound=None,
        optimality_gap_milli=None,
    )


__all__ = [
    "REQUIREMENTS_FILE",
    "SolverUnavailableError",
    "UnavailableSolver",
    "default_solver",
    "failed_result",
    "ortools_version",
]
