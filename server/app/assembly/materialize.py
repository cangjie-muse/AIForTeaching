"""把求解方案落成阶段 A 的草稿卷，并立刻用阶段 A 的整卷校验独立复验一次。

为什么"落卷"和"复验"必须成对出现：求解器只保证"满足它被喂进去的约束"。而阶段 A
的 `PaperValidationService` 检查的是**另一套事实**——库里的候选资格、Release 状态、
渲染就绪、清单哈希能否重算一致。两份结论不一致时，唯一可接受的姿态是：卷子留在
draft、运行标成失败、把差异写进诊断。绝不"求解器说行就行"。

为什么先建草稿再复验（计划书写的是"复验后建卷"）：阶段 A 的校验器以数据库里的
paper_version 为输入（它会重新读库、重算 manifest），没有落库的卷子它无从校验。
所以顺序是"建草稿 → 复验 → 不通过就停在草稿"。这不放松任何门禁：草稿本身就不可
发布，`submit-review` 还会再校验一次，且那份报告会冻结成复核证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from ..contracts.paper import SlotSelection
from .contracts import (
    BlueprintSnapshot,
    PaperDraft,
    SolutionManifest,
)


class MaterializeError(RuntimeError):
    """方案无法落成卷子（方案与蓝图对不上、卷子已被别人改过等）。"""


class AssemblyValidationBlocked(MaterializeError):
    """落卷成功但阶段 A 独立复验不通过：卷子停在草稿，运行记失败。"""

    def __init__(self, message: str, *, paper_version_id: UUID, issues: Sequence[dict]) -> None:
        super().__init__(message)
        self.paper_version_id = paper_version_id
        self.issues = tuple(issues)


@dataclass(frozen=True)
class MaterializedPaper:
    paper_version_id: UUID
    manifest_hash: str
    status: str
    valid: bool
    issues: tuple[dict, ...]

    def as_draft(self) -> PaperDraft:
        return PaperDraft(
            paper_version_id=self.paper_version_id,
            manifest_hash=self.manifest_hash,
            status=self.status,
        )


def selections_for(solution: SolutionManifest,
                   blueprint: BlueprintSnapshot) -> tuple[SlotSelection, ...]:
    """方案 → 阶段 A 的 `SlotSelection`，并核对"每个槽位恰好被选一次"。

    核对放在这里而不是指望 `PaperService`：后者允许留空槽位（草稿可以缺题），
    但求解方案不该缺——缺了说明方案被改过，或者蓝图在求解之后变过。
    """
    known = {slot.code for slot in blueprint.slots}
    picked: dict[str, UUID] = {}
    for item in solution.selections:
        if item.slot_code not in known:
            raise MaterializeError(
                f"方案里有蓝图不存在的槽位：{item.slot_code}（蓝图可能已经改版）")
        if item.slot_code in picked:
            raise MaterializeError(f"方案里槽位 {item.slot_code} 被选了不止一次")
        picked[item.slot_code] = item.exercise_version_id
    missing = sorted(known - set(picked))
    if missing:
        raise MaterializeError(f"方案缺少这些槽位：{'、'.join(missing)}")
    return tuple(
        SlotSelection(slot_code=code, exercise_version_id=picked[code])
        for code in sorted(picked)
    )


class AssemblyMaterializer:
    """依赖两个阶段 A 端口：`papers`（建草稿）与 `validator`（独立整卷校验）。"""

    def __init__(self, *, papers: Any, validator: Any) -> None:
        self.papers = papers
        self.validator = validator

    def materialize(self, *, actor_id: UUID, blueprint: BlueprintSnapshot,
                    solution: SolutionManifest, candidate_snapshot_id: UUID,
                    assembly_run_id: UUID, selected_solution_index: int,
                    idempotency_key: str) -> MaterializedPaper:
        """建草稿卷时必须带组卷来源：`paper_version.assembly_run_id` +
        `selected_solution_index` 是"这份卷从哪来"的唯一证据。少了它们，卷子会被
        当成手工卷（0034 的 CHECK 允许两列都空），回放就此断链。
        """
        selections = selections_for(solution, blueprint)
        draft = self.papers.create_manual_draft(
            actor_id=actor_id,
            blueprint_version_id=blueprint.blueprint_version_id,
            candidate_snapshot_id=candidate_snapshot_id,
            selections=selections,
            idempotency_key=idempotency_key,
            assembly_run_id=assembly_run_id,
            selected_solution_index=selected_solution_index,
        )
        report = self.validator.validate(actor_id=actor_id, paper_version_id=draft.id)
        issues = tuple(
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
                "slot_no": issue.slot_no,
            }
            for issue in getattr(report, "issues", ())
        )
        valid = bool(getattr(report, "valid", False))
        materialized = MaterializedPaper(
            paper_version_id=draft.id,
            manifest_hash=draft.manifest_hash,
            status=draft.status,
            valid=valid,
            issues=issues,
        )
        if not valid:
            raise AssemblyValidationBlocked(
                "求解方案未通过阶段 A 独立整卷校验：卷子停在草稿，不能提交审核",
                paper_version_id=draft.id,
                issues=issues,
            )
        return materialized


__all__ = [
    "AssemblyMaterializer",
    "AssemblyValidationBlocked",
    "MaterializeError",
    "MaterializedPaper",
    "selections_for",
]
