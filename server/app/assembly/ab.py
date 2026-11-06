"""A/B 配对：把两次独立求解的方案配成一对，或在配对前就拒绝。

为什么要单独一层，而不是"跑两次就是 A/B"：A/B 卷是**一次考试的两个版本**，它们共享
蓝图（同分值结构）、必须除锚题外不重叠、并且要分别送审与渲染。跑两次求解决不自动
等于 A/B：

  * 两次运行可能选了同一道题（没有锚题声明就是事故）；
  * 两次运行可能用了同族题（题干不同、材料相同，等于没换题）；
  * 两次运行的蓝图版本可能不同（A 卷是蓝图 v2、B 卷是 v3，分值结构就不一样了）。

所以"配对"是一个**判定**动作，不是命名动作。判定不通过就抛错，绝不产出一份
名叫 A/B、实质是同一套题的卷子。

与计划书的差异：计划的 A/B 由 `AssemblySolver.solve(peer=...)` 一次解出两卷
（锚题固定 + 其余互斥）。那需要 CP-SAT，本机装不上。这里的降级方案是"分别求解 →
显式配对判定"，它不能保证一次就配得上（可能要重跑），但**判定规则是同一套**，
将来接上 `peer` 约束时可以直接复用 `compare_structure` 的结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from .comparator import StructureComparison, compare_structure, issues_payload
from .contracts import BlueprintSnapshot, SolutionManifest


class AbPairError(ValueError):
    """两个方案不能配成 A/B（重叠、结构不一致、蓝图版本不同等）。"""

    def __init__(self, message: str, *, comparison: StructureComparison | None = None,
                 code: str = "ab_pair_rejected") -> None:
        super().__init__(message)
        self.code = code
        self.comparison = comparison
        self.issues = () if comparison is None else issues_payload(comparison)


@dataclass(frozen=True)
class AbPair:
    """配好对的两卷：标签、蓝图版本与结构结论。"""

    blueprint_version_id: UUID
    primary: SolutionManifest
    peer: SolutionManifest
    comparison: StructureComparison
    anchors: tuple[UUID, ...] = ()

    @property
    def labels(self) -> tuple[str, str]:
        return (self.primary.variant_label or "A", self.peer.variant_label or "B")

    def payload(self) -> dict:
        return {
            "blueprint_version_id": str(self.blueprint_version_id),
            "labels": list(self.labels),
            "anchors": [str(item) for item in self.anchors],
            "structurally_matched": self.comparison.structurally_matched,
            "statistical_equivalence": False,
            "note": self.comparison.note,
            "issues": issues_payload(self.comparison),
            "shared_version_ids": [str(item) for item in self.comparison.shared_version_ids],
            "shared_family_ids": [str(item) for item in self.comparison.shared_family_ids],
            "summary": self.comparison.extra,
            "next_step": "A/B 两卷仍需分别提交审核、分别导出与发布；本接口只做配对判定",
        }


def pair_solutions(
    blueprint: BlueprintSnapshot,
    primary: SolutionManifest,
    peer: SolutionManifest,
    *,
    anchors: Sequence[UUID] = (),
    primary_blueprint_version_id: UUID | None = None,
    peer_blueprint_version_id: UUID | None = None,
) -> AbPair:
    """把两个方案配成 A/B；不合格就抛 `AbPairError`（带结构对照与问题清单）。

    判定顺序是有讲究的：
      1. **蓝图版本**先比。版本不同说明两卷的分值结构来自不同蓝图，后面逐槽比较
         只会给出一串"分值不一致"，掩盖真正的原因。
      2. 再比结构（分值、槽位完整性、除锚题外的重叠）。
      3. 最后才谈"能不能当 A/B 用"——前两步任一失败都不行。
    """
    if primary_blueprint_version_id is not None and peer_blueprint_version_id is not None:
        if primary_blueprint_version_id != peer_blueprint_version_id:
            raise AbPairError(
                "两卷来自不同的蓝图版本：A/B 必须共享同一份蓝图（分值结构相同的"
                f"前提），收到 {primary_blueprint_version_id} 与 {peer_blueprint_version_id}",
                code="ab_blueprint_mismatch",
            )
    comparison = compare_structure(blueprint, primary, peer, anchors=anchors)
    if not comparison.structurally_matched:
        codes = sorted({item.code for item in comparison.issues})
        raise AbPairError(
            "两个方案不能配成 A/B：" + "、".join(codes),
            comparison=comparison,
        )
    return AbPair(
        blueprint_version_id=blueprint.blueprint_version_id,
        primary=primary,
        peer=peer,
        comparison=comparison,
        anchors=tuple(anchors),
    )


__all__ = ["AbPair", "AbPairError", "pair_solutions"]
