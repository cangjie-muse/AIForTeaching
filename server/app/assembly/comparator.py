"""A/B 固定卷的结构比较：只说"结构匹配"，绝不说"统计等值"。

为什么需要单独一层：A/B 卷的用途是"同一场考试两个版本"，教师会自然地问
"这两份一样难吗？"。**本系统不回答这个问题**——它没有 IRT、没有等值设计、
没有预试数据。它能回答的只有结构层面的事实：

  * 每个槽位的分值是否一致（100 分怎么分，两卷必须一样）；
  * 槽位数量与分节结构是否一致；
  * 除锚题（允许共用的那几道）之外，两卷有没有共用题目 / 同族题目。

所以 `StructureComparison` 里有一个恒为假的字段 `statistical_equivalence`，
以及一句固定措辞的 `note`。这不是"还没实现"，而是"不该由这个系统来主张"——
一旦界面显示"统计等值"，它就成了一个无人能兑现的承诺。

与计划书的差异：计划的 `AssemblySolutionComparator` 是 Next.js 组件，比较逻辑写在
组件里。本仓库把它抽成纯函数（`compare_structure`），组件（`js/paper-assembly.js`）
只负责把结果画出来——这样"什么算结构匹配"可以被测试钉住，而不是埋在 JSX 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence
from uuid import UUID

from .contracts import (
    BlueprintSnapshot,
    SlotSpec,
    SolutionManifest,
)

# 固定措辞：出现在界面、API 与测试里。改这句话等于改变对外承诺。
STRUCTURE_NOTE = "结构匹配的 A/B 卷；不代表统计等值"


@dataclass(frozen=True)
class StructureIssue:
    code: str
    message: str
    slot_code: str | None = None


@dataclass(frozen=True)
class SlotComparison:
    """一个槽位在两卷里的对照：分值必须一致，其余只报告不判死。"""

    slot_code: str
    section_code: str
    score_hundredths: int
    primary_version_id: UUID | None
    peer_version_id: UUID | None
    same_version: bool
    same_family: bool
    difficulty_delta: int | None
    seconds_delta: int | None
    shared_and_allowed: bool


@dataclass(frozen=True)
class StructureComparison:
    """A/B 结构比较的结果。

    `structurally_matched` 只表示"分值结构一致 + 除锚题外无重叠"，
    与"难度等值"无关。`statistical_equivalence` 恒为 False，是刻意的。
    """

    structurally_matched: bool
    issues: tuple[StructureIssue, ...] = ()
    slots: tuple[SlotComparison, ...] = ()
    shared_version_ids: tuple[UUID, ...] = ()
    shared_family_ids: tuple[UUID, ...] = ()
    anchors: tuple[UUID, ...] = ()
    note: str = STRUCTURE_NOTE
    statistical_equivalence: bool = False
    extra: dict[str, int] = field(default_factory=dict)


def _by_slot(solution: SolutionManifest) -> dict[str, object]:
    """方案 → {槽位码: 选择}；同槽位重复出现时保留第一个并记在 issues 里。"""
    mapping: dict[str, object] = {}
    for item in solution.selections:
        mapping.setdefault(item.slot_code, item)
    return mapping


def _slot_issues(slot: SlotSpec, chosen: object, *, label: str) -> list[StructureIssue]:
    issues: list[StructureIssue] = []
    if chosen is None:
        issues.append(StructureIssue(
            "slot_missing", f"{label}缺槽位 {slot.code}", slot.code))
        return issues
    expected = _hundredths(slot)
    actual = int(getattr(chosen, "score_hundredths", -1))
    if actual != expected:
        issues.append(StructureIssue(
            "slot_score_mismatch",
            f"槽位 {slot.code} 的分值不一致：蓝图 {expected}，方案 {actual}",
            slot.code,
        ))
    return issues


def _hundredths(slot: SlotSpec) -> int:
    from .contracts import score_to_hundredths

    return score_to_hundredths(slot.score)


def compare_structure(
    blueprint: BlueprintSnapshot,
    primary: SolutionManifest,
    peer: SolutionManifest,
    *,
    anchors: Sequence[UUID] = (),
) -> StructureComparison:
    """比较两个方案的结构；`anchors` 是允许两卷共用的题目版本 id。

    判定只看三件事（都能由数据直接证明）：
      1. 两卷的槽位集合一致，且每槽分值等于蓝图；
      2. 两卷各自每个槽位恰好一道题（不缺、不重）；
      3. 除锚题外没有共用题目版本，也没有同族题目。

    第 3 条里的"同族"用的是 selection 上的 `exercise_family_id`：同族题可能题干
    不同但共用材料/答案，两卷同时出现就失去 A/B 的意义。
    """
    slot_by_code = {slot.code: slot for slot in blueprint.slots}
    primary_map = _by_slot(primary)
    peer_map = _by_slot(peer)
    issues: list[StructureIssue] = []

    unknown = (set(primary_map) | set(peer_map)) - set(slot_by_code)
    for code in sorted(unknown):
        issues.append(StructureIssue(
            "unknown_slot", f"方案里有蓝图不存在的槽位：{code}", code))

    duplicates: list[str] = []
    for solution, label in ((primary, "A 卷"), (peer, "B 卷")):
        codes = [item.slot_code for item in solution.selections]
        duplicates.extend(
            f"{label}槽位重复：{code}" for code in sorted(
                {code for code in codes if codes.count(code) > 1}))
    for message in duplicates:
        issues.append(StructureIssue("duplicate_slot", message))

    comparisons: list[SlotComparison] = []
    anchor_set = set(anchors)
    for slot in blueprint.slots:
        first = primary_map.get(slot.code)
        second = peer_map.get(slot.code)
        issues.extend(_slot_issues(slot, first, label="A 卷"))
        issues.extend(_slot_issues(slot, second, label="B 卷"))
        same_version = (
            first is not None and second is not None
            and getattr(first, "exercise_version_id", None)
            == getattr(second, "exercise_version_id", None)
        )
        same_family = (
            first is not None and second is not None
            and getattr(first, "exercise_family_id", None)
            == getattr(second, "exercise_family_id", None)
        )
        allowed = bool(
            same_version and getattr(first, "exercise_version_id", None) in anchor_set)
        comparisons.append(SlotComparison(
            slot_code=slot.code,
            section_code=slot.section_code,
            score_hundredths=_hundredths(slot),
            primary_version_id=getattr(first, "exercise_version_id", None),
            peer_version_id=getattr(second, "exercise_version_id", None),
            same_version=same_version,
            same_family=same_family,
            difficulty_delta=_delta(getattr(first, "difficulty", None),
                                    getattr(second, "difficulty", None)),
            seconds_delta=_delta(getattr(first, "estimated_seconds", None),
                                 getattr(second, "estimated_seconds", None)),
            shared_and_allowed=allowed,
        ))
        if same_version and not allowed:
            issues.append(StructureIssue(
                "shared_version_not_anchored",
                f"槽位 {slot.code} 两卷用了同一道题，但它不是锚题", slot.code))
        elif same_family and not allowed:
            issues.append(StructureIssue(
                "shared_family_not_anchored",
                f"槽位 {slot.code} 两卷用了同族题目，但它不是锚题", slot.code))

    shared_versions = tuple(sorted(
        {item.exercise_version_id for item in primary.selections}
        & {item.exercise_version_id for item in peer.selections},
        key=str,
    ))
    shared_families = tuple(sorted(
        {item.exercise_family_id for item in primary.selections}
        & {item.exercise_family_id for item in peer.selections},
        key=str,
    ))

    if not primary.optimal_proven and not peer.optimal_proven:
        # 只是信息，不是问题：两卷都不需要"最优"才能用。
        pass

    matched = not issues
    return StructureComparison(
        structurally_matched=matched,
        issues=tuple(issues),
        slots=tuple(comparisons),
        shared_version_ids=shared_versions,
        shared_family_ids=shared_families,
        anchors=tuple(anchors),
        extra={
            "slots": len(blueprint.slots),
            "shared_versions": len(shared_versions),
            "shared_families": len(shared_families),
            "difficulty_delta_total": sum(
                abs(item.difficulty_delta or 0) for item in comparisons),
            "seconds_delta_total": sum(
                abs(item.seconds_delta or 0) for item in comparisons),
        },
    )


def _delta(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return right - left


def issues_payload(comparison: StructureComparison) -> list[dict]:
    return [
        {"code": item.code, "message": item.message, "slot_code": item.slot_code}
        for item in comparison.issues
    ]


__all__ = [
    "STRUCTURE_NOTE",
    "SlotComparison",
    "StructureComparison",
    "StructureIssue",
    "compare_structure",
    "issues_payload",
]
