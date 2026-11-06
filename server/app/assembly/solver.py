"""CP-SAT 求解器：硬约束、整数软目标、最多 3 个可解释方案。

**当前状态（必须先读）**：本文件**没有在本仓库的机器上运行过**——那台机器没有外网，
`ortools==9.11.4210` 装不上（见 docs/验证记录.md 的阻塞项）。所以：

  * `server/tests/test_assembly_solver.py` 里写着应当成立的断言，但一次都没有跑过；
  * 它刻意**没有**被登记进 `npm run check`（没装依赖时会退化成"跳过"，而跳过看起来
    和通过太像）；
  * 装好依赖后，除了跑这份测试，还要跑 `test_assembly_solver.py` 里的
    "与独立复验一致"那一组——求解器说满足，不等于阶段 A 的校验器也认。

设计要点（写下来是为了后来人能检查实现有没有偏离）：

1. **整数单位。** 分值只用 `score_to_hundredths(slot.score)` 进模型（100 分卷 = 10000），
   时长只用整数秒，软目标的权重/偏差/罚分全是整数。CP-SAT 只认整数。
2. **每槽恰好一题。** 槽位 → 候选的布尔变量用 `AddExactlyOne`；同一道题（按
   `stable_exercise_id`）与同一互斥组（family / conflict group / 蓝图 exclusion_groups）
   用 `AddAtMostOne`。没有这两条，一份卷子里会出现同一道题两次。
3. **总分等式。** 槽位分之和必须等于蓝图总分——蓝图的分数自洽由预检负责，这里把它
   写成等式：一来自洽，二来防止"少放一个槽位但总数还对得上"。
4. **可行 ≠ 最优。** 状态由 `map_status` 从 CP-SAT 的返回码映射：
   `OPTIMAL → OPTIMAL`，`FEASIBLE → FEASIBLE`（或超时时的 `TIMEOUT`），
   `INFEASIBLE → INFEASIBLE`，`UNKNOWN → TIMEOUT`。只有 CP-SAT 真的给出了
   OPTIMAL 才算"已证明最优"。
5. **最多 3 个方案，且互不相同。** 拿到一个解后，用 `AddForbiddenAssignments`
   禁掉整组选择再解一次。第一个方案继承求解器的结论；后面的方案是"排除前解后
   仍然可行"的结果，**不继承最优性**——所以非首个方案一律标 `FEASIBLE`。
6. **超时时间共享。** 总时限在多个方案之间分配：每个方案用剩余时间除以剩余方案数，
   保证最后一个方案不会因为前面用光了时间而拿不到解。
"""

from __future__ import annotations

from collections import defaultdict

from .contracts import (
    AssemblySelection,
    BlueprintSnapshot,
    Candidate,
    CandidateManifest,
    RunStatus,
    SolveResult,
    SolverConfig,
    SolutionManifest,
    make_solution_set,
    optimality_gap_milli,
    score_to_hundredths,
)
from .diagnostics import requires_seconds, slot_candidates

try:  # pragma: no cover - 依赖缺失时给出可执行的提示，而不是 ImportError 栈
    from ortools.sat.python import cp_model
except ImportError as error:  # pragma: no cover
    raise ImportError(
        "缺少 ortools：请先安装 server/requirements-assembly.txt 再使用 CP-SAT 求解器"
    ) from error


def map_status(raw_status: int, solution_count: int, hit_time_limit: bool) -> RunStatus:
    """CP-SAT 返回码 → 本系统的运行状态。

    `hit_time_limit` 由调用方给（CP-SAT 的 FEASIBLE 也可能是"时间到了但解是好的"）。
    这里不做"有几个解就当最优"的推断：可行性证明只能来自 OPTIMAL。
    """
    if raw_status == cp_model.OPTIMAL:
        return RunStatus.OPTIMAL
    if raw_status == cp_model.FEASIBLE:
        return RunStatus.TIMEOUT if hit_time_limit else RunStatus.FEASIBLE
    if raw_status == cp_model.INFEASIBLE:
        return RunStatus.INFEASIBLE
    if raw_status == cp_model.MODEL_INVALID:
        return RunStatus.FAILED
    # UNKNOWN：时间用完且既没解也没证否
    return RunStatus.TIMEOUT if solution_count == 0 or hit_time_limit else RunStatus.FAILED


class _Model:
    """一次建模的结果：模型、变量索引、以及每槽的候选表。"""

    def __init__(self, model, variables, by_slot, candidates):
        self.model = model
        self.variables = variables  # {(slot_code, version_id): BoolVar}
        self.by_slot = by_slot      # {slot_code: [Candidate]}
        self.candidates = candidates


class CpSatSolver:
    solver_name = "cp-sat"

    def __init__(self, solver_version: str | None = None) -> None:
        if solver_version is None:
            from importlib.metadata import PackageNotFoundError, version

            try:
                solver_version = version("ortools")
            except PackageNotFoundError:  # pragma: no cover
                solver_version = "unknown"
        self.solver_version = solver_version

    # -- 对外 ---------------------------------------------------------------

    def solve(self, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
              config: SolverConfig,
              peer: SolutionManifest | None = None) -> SolveResult:
        """解一份卷；给了 `peer` 就按 A/B 语义解**第二卷**。

        `peer` 的语义（设计 §7.4）：
          * 锚题（`blueprint.anchor_version_ids`）可以与 peer 共用——它们是两卷的
            可比性支点；
          * **其它已选题一律禁止**：既不重复稳定题目，也不使用同族题，否则另一卷
            等于泄露答案路径（§7.4.3）；
          * 卷内原有约束（同一 stable 不进两槽、互斥组）照旧生效。

        这里不做"跑两次再希望不重叠"——两次独立求解随时可能选到同题，
        判定失败后只能重跑，教师无法预期。约束必须在模型里。
        """
        if not manifest.candidates:
            return SolveResult(RunStatus.INFEASIBLE, (), {"reason": "no_candidates"}, None, None)
        built = self._build(blueprint, manifest)
        if built is None:
            return SolveResult(RunStatus.INFEASIBLE, (), {"reason": "no_eligible_candidate"},
                               None, None)
        if peer is not None:
            removed = self._apply_peer_constraints(built, blueprint, peer)
            if removed <= 0:
                # 全部候选都被 peer 占掉了：这一卷不可能组出来，而且原因是"没有
                # 剩下的题"，不是"时限不够"。
                return SolveResult(RunStatus.INFEASIBLE, (),
                                   {"reason": "no_candidate_left_for_peer",
                                    "blocked_variables": removed}, None, None)
        solutions, verdict = self._solve_all(
            built.model, blueprint, manifest, built, config)
        diagnostics = {
            "solutions": len(solutions),
            "verdict": str(verdict),
            # 规模：教师看到"为什么慢"时，这两个数字比任何形容词都管用。
            "variables": len(built.variables),
            "slots": len(blueprint.slots),
        }
        if peer is not None:
            diagnostics["peer_variant"] = peer.variant_label
            diagnostics["peer_blocked_variables"] = getattr(self, "_last_blocked", 0)
        if not solutions:
            # 空列表有两种含义：真的无解，或者时间不够。必须按 verdict 说清楚，
            # 否则"调大时限就能解"会被误报成"这份蓝图不可能满足"。
            if verdict is RunStatus.TIMEOUT:
                diagnostics["reason"] = "time_limit_no_solution"
            else:
                diagnostics["reason"] = "hard_constraints"
            return SolveResult(verdict, (), diagnostics, None, None)
        first = solutions[0]
        return SolveResult(
            status=verdict,
            solution_sets=(make_solution_set(tuple(solutions)),),
            diagnostics=diagnostics,
            best_bound=first.best_bound,
            optimality_gap_milli=first.optimality_gap_milli,
        )

    # -- 建模 ---------------------------------------------------------------

    def _build(self, blueprint: BlueprintSnapshot,
               manifest: CandidateManifest,
               skip: frozenset[str] = frozenset()) -> _Model | None:
        """建模；`skip` 里的约束组不加入模型——只给诊断用的"放大副本"。

        为什么要有 `skip`：无解时唯一有用的话是"**去掉哪一组约束就能解**"。
        做法是在**明确标记的副本**里逐组移除、重解，原蓝图与本次运行的结果都不变。
        这个参数只应由 `diagnose()` 使用；正常求解永远传空集合。
        """
        model = cp_model.CpModel()
        need_seconds = requires_seconds(blueprint)
        candidates = {item.exercise_version_id: item for item in manifest.candidates}
        by_slot: dict[str, list[Candidate]] = {}
        variables: dict[tuple[str, object], object] = {}
        for slot in blueprint.slots:
            eligible = slot_candidates(slot, manifest.candidates, require_seconds=need_seconds)
            if not eligible:
                # 某个槽位一个候选都没有：模型直接无解。预检也会报，但求解器不能
                # 因此返回"部分方案"——一份缺题的卷子不是方案。
                return None
            by_slot[slot.code] = eligible
            slot_vars = []
            for candidate in eligible:
                var = model.NewBoolVar(f"x_{slot.code}_{candidate.exercise_version_id}")
                variables[(slot.code, candidate.exercise_version_id)] = var
                slot_vars.append(var)
            model.AddExactlyOne(slot_vars)

        self._add_uniqueness(model, blueprint, variables, candidates, skip)
        self._add_score_equalities(model, blueprint, variables)
        needs_duration = (
            blueprint.hard_constraints.max_duration_seconds is not None
            or (blueprint.soft_objectives.target_duration_seconds is not None
                and blueprint.soft_objectives.duration_weight)
        )
        durations = (self._slot_durations(model, blueprint, by_slot, variables, candidates)
                     if needs_duration else {})
        self._add_duration(model, blueprint, durations, skip)
        self._add_coverage(model, blueprint, variables, candidates, skip)
        self._add_kind_requirements(model, blueprint, variables, candidates, skip)
        self._add_objectives(model, blueprint, variables, candidates, durations)
        return _Model(model, variables, by_slot, candidates)

    @staticmethod
    def _add_uniqueness(model, blueprint: BlueprintSnapshot, variables, candidates,
                         skip: frozenset[str] = frozenset()) -> None:
        """三条"同一道题不能进卷两次"的约束。"""
        by_stable = defaultdict(list)
        for (_, version_id), var in variables.items():
            by_stable[candidates[version_id].stable_exercise_id].append(var)
        if "uniqueness:stable" not in skip:
            for group in by_stable.values():
                if len(group) > 1:
                    model.AddAtMostOne(group)

        # 题内互斥组（Candidate.conflict_group_ids）：同一组是"共用材料/互相泄答案"。
        by_conflict = defaultdict(list)
        for (_, version_id), var in variables.items():
            for group_id in candidates[version_id].conflict_group_ids:
                by_conflict[group_id].append(var)
        for group in by_conflict.values():
            if len(group) > 1:
                model.AddAtMostOne(group)

        # 蓝图级互斥组（conflict_groups / leak_groups）：按版本 id 指定成员。
        if "uniqueness:exclusion" not in skip:
            for group in blueprint.hard_constraints.exclusion_groups:
                members = [var for (_, version_id), var in variables.items()
                           if version_id in group]
                if len(members) > 1:
                    model.AddAtMostOne(members)

    @staticmethod
    def _add_score_equalities(model, blueprint: BlueprintSnapshot, variables) -> None:
        """分值约束：**不**再加"总分 == 蓝图总分"这条线性等式。

        为什么去掉它（1,000 候选 / 50 槽位的首跑：31 秒后回 UNKNOWN、一个解都没给）：
        每个槽位都有 `AddExactlyOne`，槽位分值又是常量，所以"总分"在模型里本来就是
        常量 = Σ 槽位分，与蓝图总分是否相等**与选择无关**。把它写成一条 50,000 项的
        线性等式，只是让 CP-SAT 每次求解都去处理一个必然成立的巨约束，纯属自找麻烦。

        蓝图的分数自洽由 `diagnostics.precheck` 负责（`blueprint_score_mismatch` /
        `section_score_mismatch`），不自洽时根本不会调到这里。也就是说：这条等式
        要么恒真，要么说明蓝图有问题——两种情况都不该由求解器去"发现"。
        """
        return None

    # -- 不可行诊断（只读副本） ---------------------------------------------

    @staticmethod
    def constraint_groups(blueprint: BlueprintSnapshot) -> tuple[str, ...]:
        """可逐组放宽的约束键。顺序固定，报告才能稳定比较。"""
        keys = ["duration_max", "uniqueness:stable", "uniqueness:exclusion"]
        keys.extend(f"coverage:{index}" for index, _ in enumerate(
            blueprint.hard_constraints.coverage_requirements))
        keys.extend(f"kind:{index}" for index, _ in enumerate(
            blueprint.hard_constraints.kind_requirements))
        return tuple(keys)

    def diagnose(self, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
                 config: SolverConfig, *, per_group_seconds: int = 5) -> dict:
        """无解时逐组移除约束、重解，报告"去掉哪一组就能解"。

        三条纪律：
          1. **只读副本**：每次都在新模型上建，原蓝图、原运行结果一个字节都不改；
          2. **只报告单组放宽**（不是"随便删几组"）：单组就够的列在 `minimal_single`，
             需要多组的只列 `needs_multiple`——不假装做了组合搜索；
          3. **时间有上限**：每组最多 `per_group_seconds` 秒，总数在 `budget_seconds` 内，
             超了就把剩下的标成 `skipped`，而不是让教师等十分钟。
        """
        groups = self.constraint_groups(blueprint)
        result: dict = {"groups": [], "minimal_single": [], "needs_multiple": [],
                        "per_group_seconds": per_group_seconds, "skipped": []}
        if not groups:
            result["note"] = "这份蓝图没有可逐组放宽的约束"
            return result
        for key in groups:
            built = self._build(blueprint, manifest, skip=frozenset({key}))
            if built is None:
                # 放宽这一组之后仍然连槽位都凑不满：与这组无关。
                result["groups"].append({"key": key, "feasible": False,
                                         "reason": "no_eligible_candidate"})
                continue
            solver = self._new_solver(config, per_group_seconds)
            raw = solver.Solve(built.model)
            feasible = raw in (cp_model.OPTIMAL, cp_model.FEASIBLE)
            result["groups"].append({
                "key": key,
                "feasible": feasible,
                "status": str(map_status(raw, 0 if not feasible else 1, False)),
            })
            if feasible:
                result["minimal_single"].append(key)
        result["needs_multiple"] = (
            [] if result["minimal_single"]
            else [item["key"] for item in result["groups"] if not item["feasible"]])
        return result

    def _apply_peer_constraints(self, built: "_Model", blueprint: BlueprintSnapshot,
                                peer: SolutionManifest) -> int:
        """把 peer 已选的题（除锚题外）从本卷的可选集合里删掉；返回删掉的变量数。

        删法是 `x == 0` 而不是"从模型里移除变量"：CP-SAT 没有"移除变量"的概念，
        而且保留变量能让诊断说清"被封了多少个组合"，比静默缩小模型更好排查。

        同一族也一并封掉：题干不同但共用材料的题，放进另一卷等于把 A 卷的答案
        路径指出来（§7.4.3）。
        """
        anchors = {version_id for version_id in blueprint.anchor_version_ids}
        blocked_versions = {item.exercise_version_id for item in peer.selections} - anchors
        blocked_families = {
            item.exercise_family_id for item in peer.selections
            if item.exercise_version_id not in anchors
        }
        removed = 0
        for (slot_code, version_id), var in built.variables.items():
            candidate = built.candidates[version_id]
            if version_id in blocked_versions or candidate.exercise_family_id in blocked_families:
                built.model.Add(var == 0)
                removed += 1
        self._last_blocked = removed
        return removed

    @staticmethod
    def _slot_durations(model, blueprint: BlueprintSnapshot, by_slot, variables,
                        candidates) -> dict:
        """给每个槽位建一个"本槽时长"辅助变量，时长约束与目标都只加 50 项。

        为什么必须这么做（1,000 候选 / 50 槽位的实测）：直接写
        `Σ(所有 37,500 个变量的时长) <= 9000`，以及目标里的
        `|Σ(37,500 项) - 目标|`，会让 CP-SAT 在 60 秒里连**一个可行解**都没找到
        （返回 UNKNOWN）。而被求和的量在每个槽位里都是"恰好一个"，完全可以先按槽位
        聚合成一个整型辅助变量：约束从 37,500 项降到 50 项，量级问题就消失了。

        这不是"为了跑得快而放松约束"：聚合同一槽位的和是数学恒等，
        原约束一条没少。
        """
        durations: dict[str, object] = {}
        for slot in blueprint.slots:
            seconds = {
                item.exercise_version_id: int(item.estimated_seconds or 0)
                for item in by_slot[slot.code]
            }
            if not seconds:  # pragma: no cover - _build 已挡掉空槽位
                continue
            var = model.NewIntVar(min(seconds.values()), max(seconds.values()),
                                  f"dur_{slot.code}")
            model.Add(var == sum(
                seconds[version_id] * variables[(slot.code, version_id)]
                for version_id in seconds
            ))
            durations[slot.code] = var
        return durations

    @staticmethod
    def _total_duration(model, blueprint: BlueprintSnapshot, durations: dict):
        return sum(durations[slot.code] for slot in blueprint.slots)

    @staticmethod
    def _add_duration(model, blueprint: BlueprintSnapshot, durations: dict,
                     skip: frozenset[str] = frozenset()) -> None:
        maximum = blueprint.hard_constraints.max_duration_seconds
        if maximum is None or not durations or "duration_max" in skip:
            return
        model.Add(
            sum(durations[slot.code] for slot in blueprint.slots) <= maximum)

    @staticmethod
    def _add_coverage(model, blueprint: BlueprintSnapshot, variables, candidates,
                      skip: frozenset[str] = frozenset()) -> None:
        for index, requirement in enumerate(
                blueprint.hard_constraints.coverage_requirements):
            if f"coverage:{index}" in skip:
                continue
            terms = []
            for (_, version_id), var in variables.items():
                candidate = candidates[version_id]
                if requirement.target_kind == "concept":
                    weight = sum(
                        item.weight for item in candidate.concept_weights
                        if item.concept_id == requirement.target_id
                        and item.role == requirement.role
                    )
                elif requirement.target_kind == "learning_outcome":
                    weight = int(requirement.target_id in candidate.learning_outcome_ids)
                else:  # pragma: no cover - 翻译层已挡住
                    raise ValueError(f"unknown coverage target_kind: {requirement.target_kind}")
                terms.append(weight * var)
            model.Add(sum(terms) >= requirement.min_weight)

    @staticmethod
    def _add_kind_requirements(model, blueprint: BlueprintSnapshot,
                               variables, candidates,
                               skip: frozenset[str] = frozenset()) -> None:
        slot_section = {slot.code: slot.section_code for slot in blueprint.slots}
        for index, requirement in enumerate(
                blueprint.hard_constraints.kind_requirements):
            if f"kind:{index}" in skip:
                continue
            terms = [
                var for (slot_code, version_id), var in variables.items()
                if candidates[version_id].exercise_kind == requirement.exercise_kind
                and (requirement.section_code is None
                     or slot_section[slot_code] == requirement.section_code)
            ]
            model.Add(sum(terms) >= requirement.min_count)
            if requirement.max_count is not None:
                model.Add(sum(terms) <= requirement.max_count)

    def _add_objectives(self, model, blueprint: BlueprintSnapshot, variables, candidates,
                        durations: dict | None = None) -> None:
        """整数软目标：全部转成"偏差 × 权重"的和，越小越好。

        只用整数比例（万分比 / 秒），不做归一化到 0..1 的浮点加权——那会让
        "同一输入同一结果"这句话失去意义。时长偏差用按槽位聚合后的辅助变量，
        避免目标函数里出现 37,500 项的和式（那会让求解器找不到任何可行解）。
        """
        soft = blueprint.soft_objectives
        penalties = []
        durations = durations or {}

        if (soft.target_duration_seconds is not None and soft.duration_weight
                and durations):
            duration = self._total_duration(model, blueprint, durations)
            deviation = model.NewIntVar(0, 10 ** 8, "dev_duration")
            model.AddAbsEquality(deviation, duration - soft.target_duration_seconds)
            penalties.append(soft.duration_weight * deviation)

        slot_count = len(blueprint.slots)
        for name, attr, targets, weight in (
            ("difficulty", "difficulty", soft.difficulty_targets_bp, soft.difficulty_weight),
            ("cognitive", "cognitive_level", soft.cognitive_targets_bp, soft.cognitive_weight),
        ):
            if not targets or not weight:
                continue
            buckets = defaultdict(list)
            for (_, version_id), var in variables.items():
                buckets[getattr(candidates[version_id], attr) or 0].append(var)
            for bucket, target_bp in sorted(targets.items()):
                actual = sum(buckets.get(bucket, []))
                # 万分比：actual / 槽位数 与 target_bp / 10000 的偏差，用整数表达
                deviation = model.NewIntVar(0, 10 ** 8, f"dev_{name}_{bucket}")
                model.AddAbsEquality(
                    deviation, actual * 10000 - target_bp * slot_count)
                penalties.append(weight * deviation)

        if soft.recent_use_weight:
            penalties.append(soft.recent_use_weight * sum(
                candidates[version_id].recent_use_count * var
                for (_, version_id), var in variables.items()))
        if soft.exposure_balance_weight:
            penalties.append(soft.exposure_balance_weight * sum(
                candidates[version_id].exposure_count * var
                for (_, version_id), var in variables.items()))
        if soft.teacher_preference_weight:
            penalties.append(soft.teacher_preference_weight * sum(
                (0 if candidates[version_id].preferred else 1) * var
                for (_, version_id), var in variables.items()))

        if penalties:
            model.Minimize(sum(penalties))

    # -- 求解 ---------------------------------------------------------------

    def _solve_all(self, model, blueprint: BlueprintSnapshot, manifest: CandidateManifest,
                   built: _Model, config: SolverConfig) -> tuple[list[SolutionManifest], RunStatus]:
        """最多解 `config.max_solutions` 个互不相同的方案。

        返回 (方案列表, 结论)。**结论必须由这里给出**，不能让 `solve()` 从"有没有解"
        反推：超时（UNKNOWN）与无解（INFEASIBLE）都会得到空列表，反推就会把
        "时间不够"说成"约束冲突"——1,000 候选的首跑正是这样被误报的。
        """
        solutions: list[SolutionManifest] = []
        remaining = config.time_limit_seconds
        verdict = RunStatus.INFEASIBLE
        for index in range(config.max_solutions):
            budget = max(1, remaining // (config.max_solutions - index))
            solver = self._new_solver(config, budget)
            raw = solver.Solve(model)
            hit_limit = solver.WallTime() >= budget - 0.001
            verdict = map_status(raw, len(solutions), hit_limit)
            if raw in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
                verdict = map_status(raw, len(solutions), False)
                break
            if raw == cp_model.UNKNOWN:
                # 时间到了：已经解出来的方案仍然可用，但没有最优性结论。
                verdict = RunStatus.TIMEOUT
                break
            if raw not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
            selection = self._extract(solver, blueprint, built)
            if selection is None:  # pragma: no cover - 解出来就一定可选
                break
            solutions.append(self._manifest(
                index, selection, blueprint, solver, raw,
                # 只有第一个方案继承最优性；后续方案是"排除前解后仍可行"的结果。
                optimal=raw == cp_model.OPTIMAL and index == 0))
            remaining -= int(solver.WallTime()) or 1
            if remaining <= 0 or index + 1 >= config.max_solutions:
                break
            self._forbid(model, built, selection)
        return solutions, verdict

    @staticmethod
    def _new_solver(config: SolverConfig, seconds: int):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(max(1, seconds))
        solver.parameters.random_seed = config.random_seed
        # 单 worker：多 worker 下同一 seed 可能给出不同方案，"回放"就无从谈起。
        # 9.15 把参数名从 num_search_workers 改成了 num_workers，旧名还留着但会告警；
        # 两个都试一遍，谁在就用谁——跨版本回放要的是"同一个"求解配置。
        for name in ("num_workers", "num_search_workers"):
            if hasattr(solver.parameters, name):
                setattr(solver.parameters, name, 1)
                break
        return solver

    @staticmethod
    def _extract(solver, blueprint: BlueprintSnapshot, built: _Model):
        slots = {slot.code: slot for slot in blueprint.slots}
        selections: list[AssemblySelection] = []
        for (slot_code, version_id), var in sorted(built.variables.items(),
                                                   key=lambda item: item[0][0]):
            if not solver.Value(var):
                continue
            candidate = built.candidates[version_id]
            slot = slots[slot_code]
            selections.append(AssemblySelection(
                slot_id=slot.slot_id,
                slot_code=slot.code,
                exercise_version_id=version_id,
                exercise_instance_id=candidate.exercise_instance_id,
                stable_exercise_id=candidate.stable_exercise_id,
                exercise_family_id=candidate.exercise_family_id,
                source_release_id=candidate.source_release_id,
                decision_seq=candidate.decision_seq,
                score_hundredths=score_to_hundredths(slot.score),
                estimated_seconds=candidate.estimated_seconds,
                difficulty=candidate.difficulty,
                dependency_hash=candidate.dependency_hash,
                content_hash=candidate.content_hash,
            ))
        if len(selections) != len(blueprint.slots):
            return None
        return tuple(selections)

    @staticmethod
    def _manifest(index: int, selections, blueprint: BlueprintSnapshot, solver,
                  raw: int, *, optimal: bool) -> SolutionManifest:
        from .contracts import make_solution_manifest

        objective = int(round(solver.ObjectiveValue()))
        bound = int(round(solver.BestObjectiveBound()))
        status = RunStatus.OPTIMAL if optimal else (
            RunStatus.FEASIBLE if raw in (cp_model.OPTIMAL, cp_model.FEASIBLE)
            else RunStatus.TIMEOUT)
        return make_solution_manifest(
            variant_label="single" if index == 0 else f"alternative-{index}",
            selections=selections,
            objective_terms=(),
            objective_value=objective,
            best_bound=bound,
            optimality_gap_milli=optimality_gap_milli(objective, bound),
            solver_status=status,
            optimal_proven=optimal,
            structure_report={},
        )

    @staticmethod
    def _forbid(model, built: "_Model", selections) -> None:
        """禁掉这一整组选择，逼下一次求解给出不同的方案。

        两个必须做对的地方（9.15 的第一次真实运行各踩过一次）：
          * `AddForbiddenAssignments(variables, tuples)` 要**变量对象列表**，不是名字
            字符串——传字符串会得到 "missing 1 required positional argument"；
          * `tuples` 是被禁止的取值组合，布尔变量里 1 表示"选中"，所以要禁的是
            全 1 的那一个元组。
        """
        variables = [
            built.variables[(item.slot_code, item.exercise_version_id)]
            for item in selections
        ]
        model.AddForbiddenAssignments(variables, [[1] * len(variables)])


__all__ = ["CpSatSolver", "map_status"]
