"""方案复验：不看求解器，只看"这份方案到底满不满足蓝图"。

为什么需要这一层，明明阶段 A 已经有整卷校验：

  * 阶段 A 的 `PaperValidationService` 校验的是**已落库的卷子**，而且它读的是库里的
    候选资格、Release 状态、渲染就绪——它回答"这份卷现在还能不能用"。
  * 这里回答的是另一个问题：**求解器给出的方案本身对不对**（每槽一题、分值、
    总分、时长、覆盖、题型、互斥、同一道题不重复、所选题必须在候选清单里）。
    这个问题的答案与数据库无关，纯函数就能算。

分开的意义在阶段 B 的门禁上："CP-SAT 硬约束与独立整卷校验结果一致"。有了这一层，
即使求解器（或将来换掉的任何求解器）算错了，也会在建卷之前被抓住，而不是等
教师提交审核时才发现"这份卷的分加起来不是 100"。

与 `diagnostics.precheck` 的关系：预检回答"还没解就能确定无解吗"；这里回答
"已经解出来的这份方案是否真的可行"。两者判据同源（都用 `slot_candidates` 的资格
规则），但方向相反，所以都要有。
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Sequence

from .contracts import (
    BlueprintSnapshot,
    CandidateManifest,
    SolutionManifest,
    ValidationIssue,
    score_to_hundredths,
)
from .diagnostics import requires_seconds, slot_candidates


def _issue(code: str, message: str, *, slot_code: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, slot_code=slot_code)


def verify_solution(blueprint: BlueprintSnapshot, manifest: CandidateManifest,
                    solution: SolutionManifest) -> list[ValidationIssue]:
    """逐条核对一份方案是否满足蓝图；返回问题清单（空 = 通过）。

    这里**不做任何"大概可以"的判断**：每一条都能由方案与蓝图直接证明。
    """
    issues: list[ValidationIssue] = []
    slots = {slot.code: slot for slot in blueprint.slots}
    chosen: dict[str, object] = {}
    for item in solution.selections:
        if item.slot_code in chosen:
            issues.append(_issue(
                "duplicate_slot_selection",
                f"槽位 {item.slot_code} 被选了不止一次", slot_code=item.slot_code))
            continue
        chosen[item.slot_code] = item
    unknown = sorted(set(chosen) - set(slots))
    for code in unknown:
        issues.append(_issue("unknown_slot", f"方案里有蓝图不存在的槽位：{code}",
                             slot_code=code))
    missing = sorted(set(slots) - set(chosen))
    for code in missing:
        issues.append(_issue(
            "missing_slot", f"槽位 {code} 没有选题：一份可提交的卷子不能缺题",
            slot_code=code))

    candidates = {item.exercise_version_id: item for item in manifest.candidates}
    issues.extend(_check_scores(blueprint, slots, chosen))
    issues.extend(_check_membership(chosen, candidates, manifest))
    issues.extend(_check_duration(blueprint, chosen, candidates))
    issues.extend(_check_coverage(blueprint, chosen, candidates))
    issues.extend(_check_kinds(blueprint, chosen, candidates))
    issues.extend(_check_uniqueness(blueprint, chosen, candidates))
    return issues


def _check_scores(blueprint: BlueprintSnapshot, slots: dict, chosen: dict,
                  ) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    total = 0
    for code, item in chosen.items():
        slot = slots.get(code)
        if slot is None:
            continue
        expected = score_to_hundredths(slot.score)
        if int(item.score_hundredths) != expected:
            issues.append(_issue(
                "score_mismatch",
                f"槽位 {code} 的分值是 {item.score_hundredths}，蓝图要求 {expected}",
                slot_code=code))
        total += expected
    if total != score_to_hundredths(blueprint.total_score):
        issues.append(_issue(
            "total_score_mismatch",
            f"方案覆盖的分值合计 {total}，蓝图总分是"
            f" {score_to_hundredths(blueprint.total_score)}"))
    return issues


def _check_membership(chosen: dict, candidates: dict,
                      manifest: CandidateManifest) -> list[ValidationIssue]:
    """所选题必须在候选清单里：求解器不能凭空"发明"一道没被审核的题。"""
    issues: list[ValidationIssue] = []
    excluded = {item.exercise_version_id for item in manifest.exclusions}
    for code, item in chosen.items():
        if item.exercise_version_id in candidates:
            continue
        if item.exercise_version_id in excluded:
            issues.append(_issue(
                "selected_excluded_candidate",
                f"槽位 {code} 选了一道被排除的题（曝光冲突或资格不符）", slot_code=code))
        else:
            issues.append(_issue(
                "selected_unknown_candidate",
                f"槽位 {code} 选的题不在候选清单里", slot_code=code))
    return issues


def _check_duration(blueprint: BlueprintSnapshot, chosen: dict,
                    candidates: dict) -> list[ValidationIssue]:
    maximum = blueprint.hard_constraints.max_duration_seconds
    if maximum is None:
        return []
    total = 0
    unknown: list[str] = []
    for code, item in chosen.items():
        seconds = item.estimated_seconds
        if seconds is None:
            seconds = getattr(candidates.get(item.exercise_version_id), "estimated_seconds", None)
        if seconds is None:
            unknown.append(code)
            continue
        total += int(seconds)
    issues: list[ValidationIssue] = []
    if unknown:
        # 缺时长不是 0 秒：既不能说"没超时"，也不能说"超时"。
        issues.append(_issue(
            "duration_unknown",
            "这些槽位缺少预计时长，无法给出时长结论：" + "、".join(sorted(unknown))))
        return issues
    if total > maximum:
        issues.append(_issue(
            "duration_exceeded", f"方案预计总时长 {total} 秒，超过上限 {maximum} 秒"))
    return issues


def _check_coverage(blueprint: BlueprintSnapshot, chosen: dict,
                    candidates: dict) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for requirement in blueprint.hard_constraints.coverage_requirements:
        actual = 0
        for item in chosen.values():
            candidate = candidates.get(item.exercise_version_id)
            if candidate is None:
                continue
            if requirement.target_kind == "concept":
                actual += sum(
                    weight.weight for weight in candidate.concept_weights
                    if weight.concept_id == requirement.target_id
                    and weight.role == requirement.role)
            elif requirement.target_kind == "learning_outcome":
                actual += int(requirement.target_id in candidate.learning_outcome_ids)
        if actual < requirement.min_weight:
            issues.append(_issue(
                "coverage_shortfall",
                f"覆盖要求 {requirement.target_kind}:{requirement.target_id}"
                f"（角色 {requirement.role}）要求 {requirement.min_weight}，实际 {actual}"))
    return issues


def _check_kinds(blueprint: BlueprintSnapshot, chosen: dict,
                 candidates: dict) -> list[ValidationIssue]:
    slot_section = {slot.code: slot.section_code for slot in blueprint.slots}
    issues: list[ValidationIssue] = []
    for requirement in blueprint.hard_constraints.kind_requirements:
        count = 0
        for code, item in chosen.items():
            if requirement.section_code is not None \
                    and slot_section.get(code) != requirement.section_code:
                continue
            candidate = candidates.get(item.exercise_version_id)
            if candidate is not None and candidate.exercise_kind == requirement.exercise_kind:
                count += 1
        scope = f"分节 {requirement.section_code}" if requirement.section_code else "全卷"
        if count < requirement.min_count:
            issues.append(_issue(
                "kind_below_minimum",
                f"{scope}的 {requirement.exercise_kind} 只有 {count} 道，"
                f"要求至少 {requirement.min_count}"))
        if requirement.max_count is not None and count > requirement.max_count:
            issues.append(_issue(
                "kind_above_maximum",
                f"{scope}的 {requirement.exercise_kind} 有 {count} 道，"
                f"最多 {requirement.max_count}"))
    return issues


def _check_uniqueness(blueprint: BlueprintSnapshot, chosen: dict,
                      candidates: dict) -> list[ValidationIssue]:
    """同一道题不能进两个槽位；互斥组内最多一道。"""
    issues: list[ValidationIssue] = []
    stable = Counter()
    families = Counter()
    for code, item in chosen.items():
        stable[item.stable_exercise_id] += 1
        families[item.exercise_family_id] += 1
    for key, count in stable.items():
        if count > 1:
            issues.append(_issue(
                "duplicate_exercise",
                f"同一道题（stable {key}）进了 {count} 个槽位"))
    for group in blueprint.hard_constraints.exclusion_groups:
        members = {version_id for version_id in group}
        hits = [code for code, item in chosen.items()
                if item.exercise_version_id in members]
        if len(hits) > 1:
            issues.append(_issue(
                "exclusion_group_violated",
                f"互斥组内有 {len(hits)} 道题同时进卷：" + "、".join(sorted(hits))))
    conflicts = Counter()
    for item in chosen.values():
        candidate = candidates.get(item.exercise_version_id)
        for group_id in getattr(candidate, "conflict_group_ids", ()):
            conflicts[group_id] += 1
    for group_id, count in conflicts.items():
        if count > 1:
            issues.append(_issue(
                "conflict_group_violated",
                f"冲突组 {group_id} 有 {count} 道题同时进卷"))
    return issues


def assert_solution_valid(blueprint: BlueprintSnapshot, manifest: CandidateManifest,
                          solution: SolutionManifest) -> None:
    """不通过就抛错（携带问题清单），给建卷前的门禁用。"""
    issues = verify_solution(blueprint, manifest, solution)
    if issues:
        raise SolutionVerificationError(
            "方案未满足蓝图约束：" + "、".join(item.code for item in issues), issues)


class SolutionVerificationError(ValueError):
    def __init__(self, message: str, issues: Sequence[ValidationIssue]) -> None:
        super().__init__(message)
        self.issues = tuple(issues)

    def payload(self) -> list[dict]:
        return [
            {"code": item.code, "message": item.message, "slot_code": item.slot_code}
            for item in self.issues
        ]


def _unused(value: Decimal) -> Decimal:  # pragma: no cover - 保留类型引用
    return value


__all__ = [
    "SolutionVerificationError",
    "assert_solution_valid",
    "verify_solution",
]
