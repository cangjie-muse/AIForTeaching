"""求解前的确定性预检：在浪费一次求解之前，先算出"根本不可能"的部分。

为什么要有这一层：CP-SAT 返回 INFEASIBLE 只是一句话——"这组约束无解"。教师拿到的
是"组不出来"，却不知道是缺题、缺覆盖、还是时长不够。诊断的成本很低（全是对候选
集合做计数与求和），而一次误判的代价是教师回去改蓝图、再等一次求解。

三条纪律：

1. **只报告可证的结论。** 每个问题都带"为什么"与"改什么"，且都能用候选集合直接
   证明。这里不做"大概不行"的猜测。
2. **不放松任何约束。** 诊断只读，绝不改蓝图、不改候选；它不产生方案，也不建卷。
3. **与求解器不冲突。** 预检通过不代表可解（组合问题仍然可能无解），预检失败则
   一定不可解——所以预检失败时可以不调求解器，直接记 INFEASIBLE 并附诊断。

与计划书的差异：计划的 `diagnostics.py` 还要"在明确标记的副本中逐组移除约束定位
原因"。本模块先把**可证**的部分做掉（缺题、缺覆盖、时长下界、互斥冲突），
"逐组移除"要等求解器能跑（本机无 ortools）才有意义——没有求解器去证明"移除这组
就可行"，那就只是猜。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from itertools import combinations
from typing import Iterable, Sequence
from uuid import UUID

from .contracts import (
    BlueprintSnapshot,
    Candidate,
    CandidateManifest,
    SlotSpec,
    ValidationIssue,
    score_to_hundredths,
)


def _issue(code: str, message: str, *, slot_code: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, slot_code=slot_code)


def slot_candidates(slot: SlotSpec, candidates: Sequence[Candidate],
                    *, require_seconds: bool) -> list[Candidate]:
    """这道题在这个槽位里的候选集合（与求解器同一套资格规则）。

    与求解器共用同一份判定，是为了让"预检说有 3 道可选"与"求解器只用这 3 道"
    永远一致；各写一套的后果是预检索出了一个不可能的组合。
    """
    eligible: list[Candidate] = []
    for candidate in candidates:
        if slot.allowed_kinds and candidate.exercise_kind not in slot.allowed_kinds:
            continue
        concept_ids = {weight.concept_id for weight in candidate.concept_weights}
        if not set(slot.required_concept_ids) <= concept_ids:
            continue
        if not set(slot.learning_outcome_ids) <= set(candidate.learning_outcome_ids):
            continue
        if slot.difficulty is not None and slot.difficulty != candidate.difficulty:
            continue
        if candidate.exercise_version_id in slot.excluded_version_ids:
            continue
        if candidate.exercise_family_id in slot.excluded_family_ids:
            continue
        if slot.mandatory_version_ids and (
            candidate.exercise_version_id not in slot.mandatory_version_ids
        ):
            continue
        if require_seconds and candidate.estimated_seconds is None:
            continue
        eligible.append(candidate)
    return eligible


def requires_seconds(blueprint: BlueprintSnapshot) -> bool:
    return (
        blueprint.hard_constraints.max_duration_seconds is not None
        or blueprint.soft_objectives.target_duration_seconds is not None
        or blueprint.assembly_mode == "ab"
    )


def precheck(blueprint: BlueprintSnapshot,
             manifest: CandidateManifest) -> list[ValidationIssue]:
    """对所有"可证不可行"与"可证偏离"给出问题清单（空列表 = 没发现问题）。"""
    issues: list[ValidationIssue] = []
    issues.extend(_check_blueprint_score(blueprint))
    need_seconds = requires_seconds(blueprint)
    by_slot = {slot.code: slot_candidates(slot, manifest.candidates,
                                          require_seconds=need_seconds)
               for slot in blueprint.slots}
    issues.extend(_check_slots_have_candidates(blueprint, by_slot))
    issues.extend(_check_enough_candidates(blueprint, manifest))
    issues.extend(_check_duration_lower_bound(blueprint, by_slot, need_seconds))
    issues.extend(_check_coverage_reachable(blueprint, by_slot))
    issues.extend(_check_kind_requirements(blueprint, by_slot))
    issues.extend(_check_mutual_exclusion(blueprint))
    return issues


def _check_blueprint_score(blueprint: BlueprintSnapshot) -> list[ValidationIssue]:
    """蓝图的分数自洽：槽位之和 = 卷总分，分节和与分节内槽位和一致。

    这两个数不自洽时，求解器的"总分 == 蓝图总分"约束会直接判 INFEASIBLE，
    而报告只会说"总分对不上"；预检能指出是哪一节的哪个槽位。
    """
    issues: list[ValidationIssue] = []
    slot_sum = sum((slot.score for slot in blueprint.slots), Decimal("0"))
    if slot_sum != blueprint.total_score:
        issues.append(_issue(
            "blueprint_score_mismatch",
            f"槽位分之和 {slot_sum} 与卷总分 {blueprint.total_score} 不一致："
            "求解器的总分等式必然无解",
        ))
    per_section: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for slot in blueprint.slots:
        per_section[slot.section_code] += slot.score
    for section_code, declared in blueprint.hard_constraints.section_scores.items():
        actual = per_section.get(section_code, Decimal("0"))
        if actual != declared:
            issues.append(_issue(
                "section_score_mismatch",
                f"分节 {section_code} 的槽位分之和 {actual} 与声明的 {declared} 不一致",
            ))
    return issues


def _check_slots_have_candidates(blueprint: BlueprintSnapshot,
                                 by_slot: dict[str, list[Candidate]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for slot in blueprint.slots:
        if by_slot[slot.code]:
            continue
        reason = "允许题型/知识点/难度/必选/时长等条件过滤后没有剩余候选"
        if slot.mandatory_version_ids:
            reason = "必选题不在候选清单里，或被其它条件排除"
        issues.append(_issue(
            "slot_has_no_candidate",
            f"槽位 {slot.code}（{slot.section_code} 节，{slot.score} 分）没有任何可选候选：{reason}",
            slot_code=slot.code,
        ))
    return issues


def _check_enough_candidates(blueprint: BlueprintSnapshot,
                             manifest: CandidateManifest) -> list[ValidationIssue]:
    """候选总数少于槽位数 → 一定不可解（每槽必须恰好一道题）。"""
    if len(manifest.candidates) >= len(blueprint.slots):
        return []
    return [_issue(
        "not_enough_candidates",
        f"候选只有 {len(manifest.candidates)} 道，槽位有 {len(blueprint.slots)} 个："
        "一题只能进一个槽位，必然无解",
    )]


def _check_duration_lower_bound(blueprint: BlueprintSnapshot,
                                by_slot: dict[str, list[Candidate]],
                                need_seconds: bool) -> list[ValidationIssue]:
    """时长下界：每个槽位取最短候选，加起来仍然超上限就一定无解。

    缺时长的候选在 `slot_candidates` 阶段就被排除了（不虚构 0 秒），所以这里
    不会遇到"时长未知"的槽位；如果整卷因此凑不满槽位，会先由
    `slot_has_no_candidate` 报出来，而不是拿一个假的 0 秒去判"没超时"。
    """
    maximum = blueprint.hard_constraints.max_duration_seconds
    if maximum is None or not need_seconds:
        return []
    total = 0
    for slot in blueprint.slots:
        seconds = [item.estimated_seconds for item in by_slot[slot.code]
                   if item.estimated_seconds is not None]
        if not seconds:
            # 这个槽位已经在 slot_has_no_candidate 里报过了，不重复报。
            return []
        total += min(seconds)
    if total <= maximum:
        return []
    return [_issue(
        "duration_impossible",
        f"每个槽位取最短候选，总时长下界已是 {total} 秒，超过上限 {maximum} 秒",
    )]


def _check_coverage_reachable(blueprint: BlueprintSnapshot,
                              by_slot: dict[str, list[Candidate]]) -> list[ValidationIssue]:
    """覆盖要求的上界：每个槽位只能取一道题，所以可获得的权重是有上限的。

    做法是逐个槽位取"该槽位可选候选里的最大权重"，加起来与要求比较。
    这个上界可能高估（不同要求会抢同一道题），所以只报"连上界都不到"的情况——
    那种情况一定不可解，没有误报。
    """
    issues: list[ValidationIssue] = []
    for requirement in blueprint.hard_constraints.coverage_requirements:
        best = 0
        for slot in blueprint.slots:
            slot_best = 0
            for candidate in by_slot[slot.code]:
                if requirement.target_kind == "concept":
                    weight = sum(
                        item.weight for item in candidate.concept_weights
                        if item.concept_id == requirement.target_id
                        and item.role == requirement.role
                    )
                elif requirement.target_kind == "learning_outcome":
                    weight = int(requirement.target_id in candidate.learning_outcome_ids)
                else:  # pragma: no cover - 翻译层已挡
                    weight = 0
                slot_best = max(slot_best, weight)
            best += slot_best
        if best < requirement.min_weight:
            issues.append(_issue(
                "coverage_unreachable",
                f"覆盖要求 {requirement.target_kind}:{requirement.target_id}"
                f"（角色 {requirement.role}）要求至少 {requirement.min_weight}，"
                f"但所有槽位加起来最多只能提供 {best}",
            ))
    return issues


def _check_kind_requirements(blueprint: BlueprintSnapshot,
                             by_slot: dict[str, list[Candidate]]) -> list[ValidationIssue]:
    """题型数量要求：能提供该题型的槽位数量就是上界。"""
    issues: list[ValidationIssue] = []
    for requirement in blueprint.hard_constraints.kind_requirements:
        slots = [
            slot for slot in blueprint.slots
            if requirement.section_code is None or slot.section_code == requirement.section_code
        ]
        capable = [
            slot for slot in slots
            if any(item.exercise_kind == requirement.exercise_kind
                   for item in by_slot[slot.code])
        ]
        scope = f"分节 {requirement.section_code}" if requirement.section_code else "全卷"
        if requirement.min_count > len(capable):
            issues.append(_issue(
                "kind_requirement_unreachable",
                f"{scope}要求至少 {requirement.min_count} 道 "
                f"{requirement.exercise_kind}，但最多只有 {len(capable)} 个槽位能放这种题",
            ))
        if requirement.max_count is not None and requirement.min_count > requirement.max_count:
            issues.append(_issue(
                "kind_requirement_invalid",
                f"{scope}的 {requirement.exercise_kind} 数量区间倒挂："
                f"最少 {requirement.min_count} > 最多 {requirement.max_count}",
            ))
    return issues


def _check_mutual_exclusion(blueprint: BlueprintSnapshot) -> list[ValidationIssue]:
    """两组必选题落在同一个互斥组里 → 无论怎么选都不行。"""
    mandatory = {
        version_id
        for slot in blueprint.slots
        for version_id in slot.mandatory_version_ids
    }
    issues: list[ValidationIssue] = []
    for index, group in enumerate(blueprint.hard_constraints.exclusion_groups):
        hits = sorted(mandatory.intersection(group), key=str)
        for left, right in combinations(hits, 2):
            issues.append(_issue(
                "mutually_exclusive_mandatory",
                f"互斥组 {index} 里有两道题都被槽位指定为必选：{left} 与 {right}",
            ))
    return issues


def diagnose_infeasible(blueprint: BlueprintSnapshot,
                        manifest: CandidateManifest,
                        solver_diagnostics: dict | None = None) -> dict:
    """INFEASIBLE 时的诊断载荷：可证的问题 + 求解器自己的说法 + 计数摘要。

    `reason` 只有两种取值，必须分开写：`precheck_proved`（预检已证不可行）、
    `solver_reported`（求解器说不可行，但预检没找到可证的独立原因）。
    后者不是"没有原因"，而是"原因在组合里"，不能拿预检的空清单冒充解释。
    """
    issues = precheck(blueprint, manifest)
    return {
        "reason": "precheck_proved" if issues else "solver_reported",
        "issues": [
            {"code": item.code, "message": item.message, "slot_code": item.slot_code}
            for item in issues
        ],
        "solver": dict(solver_diagnostics or {}),
        "summary": {
            "slots": len(blueprint.slots),
            "candidates": len(manifest.candidates),
            "exclusions": len(manifest.exclusions),
        },
    }


def candidate_supply(blueprint: BlueprintSnapshot,
                     manifest: CandidateManifest) -> dict[str, int]:
    """每个槽位有多少可选候选（教师工作台上"哪一题最难凑"的可解释数字）。"""
    need_seconds = requires_seconds(blueprint)
    return {
        slot.code: len(slot_candidates(slot, manifest.candidates, require_seconds=need_seconds))
        for slot in blueprint.slots
    }


def issues_to_payload(issues: Iterable[ValidationIssue]) -> list[dict]:
    return [
        {"code": item.code, "message": item.message, "slot_code": item.slot_code}
        for item in issues
    ]


__all__ = [
    "candidate_supply",
    "diagnose_infeasible",
    "issues_to_payload",
    "precheck",
    "requires_seconds",
    "slot_candidates",
]
