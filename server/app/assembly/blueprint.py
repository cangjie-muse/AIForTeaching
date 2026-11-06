"""把阶段 A 的蓝图版本翻译成求解器输入 `BlueprintSnapshot`。

为什么需要这一层：阶段 A 的蓝图是**人写给教师看的**结构（分节标题、每槽分值、
可选时长），阶段 B 的求解器要的是**整数、闭集、可哈希**的结构。两者字段名相近
但语义不同，直接让求解器读 `BlueprintContent` 会导致每次改蓝图格式都要改求解器。

翻译规则里有三处必须显式处理，否则会悄悄骗人：

1. **槽位身份。** 蓝图槽位没有数据库行，求解器又需要稳定 id 才能回放。这里用
   `slot_identity(蓝图版本, 分节码, 槽位码)` 派生 uuid5：同一槽位永远同一 id。
2. **不认识的约束键不假装评估。** 阶段 A 的 `hard_constraints` 是自由 dict，
   历史蓝图里已经有 `coverage_required: True` 这种"写了但没人评估"的键。
   翻译时把它们记进 `HardConstraints.unsupported_keys`，由校验环节报告，
   而不是收下就当没看见。
3. **A/B 尚未实现就拒绝。** 计划书里 `assembly_mode` 有 `single/ab`，而阶段 A 的
   蓝图枚举是 `manual/constraint_assisted`——这是两个不同的轴。本阶段只做单卷，
   蓝图若显式要求 A/B，这里直接拒绝，而不是把它当单卷跑出一个"看起来像 A/B"
   的结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

from .contracts import (
    BlueprintSnapshot,
    CoverageRequirement,
    HardConstraints,
    KindRequirement,
    SlotSpec,
    SoftObjectives,
    slot_identity,
)

# 阶段 A 校验器认识的硬约束键（server/app/validation.py）加上本阶段新增的两个。
KNOWN_HARD_KEYS = frozenset({
    "duration_max_seconds",
    "duration_required",
    "conflict_groups",
    "leak_groups",
    "coverage_requirements",
    "kind_requirements",
})

KNOWN_SOFT_KEYS = frozenset({
    "duration_target_seconds",
    "duration_weight",
    "difficulty_targets_bp",
    "difficulty_weight",
    "cognitive_targets_bp",
    "cognitive_weight",
    "recent_use_weight",
    "exposure_balance_weight",
    "teacher_preference_weight",
    "ab_structure_weight",
})

DIFFICULTY_PREFIX = "difficulty-"


class BlueprintTranslationError(ValueError):
    """蓝图内容无法翻译成求解器输入（形状错、键不认识、要 A/B）。"""


def _parse_difficulty(label: str | None) -> int | None:
    """阶段 A 用 `difficulty-3` 这样的标签；求解器用整数。其它标签视为未声明。"""
    if not label:
        return None
    text = str(label).strip()
    if not text.startswith(DIFFICULTY_PREFIX):
        return None
    try:
        value = int(text[len(DIFFICULTY_PREFIX):])
    except ValueError:
        return None
    return value


def _uuid_list(raw: Any, *, where: str) -> tuple[UUID, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BlueprintTranslationError(f"{where} 必须是 UUID 列表")
    parsed: list[UUID] = []
    for item in raw:
        try:
            parsed.append(UUID(str(item)))
        except (TypeError, ValueError) as exc:
            raise BlueprintTranslationError(f"{where} 里有非法 UUID：{item!r}") from exc
    return tuple(parsed)


def _groups(raw: Any, *, where: str) -> tuple[tuple[UUID, ...], ...]:
    """互斥组：`[[v1, v2], [v3, v4]]`；一组内最多进一道题。"""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BlueprintTranslationError(f"{where} 必须是分组列表")
    groups: list[tuple[UUID, ...]] = []
    for index, group in enumerate(raw):
        members = _uuid_list(group, where=f"{where}[{index}]")
        if len(members) < 2:
            # 单元素组等于"不许用它"，这不是互斥语义；宁可报错让人改蓝图。
            raise BlueprintTranslationError(f"{where}[{index}] 少于两道题，互斥组没有意义")
        groups.append(tuple(sorted(members, key=str)))
    return tuple(groups)


def _int_of(raw: Any, *, where: str) -> int:
    if isinstance(raw, bool):
        raise BlueprintTranslationError(f"{where} 必须是整数，不是布尔")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise BlueprintTranslationError(f"{where} 不是整数：{raw!r}") from exc
    return value


def _coverage_requirements(raw: Any) -> tuple[CoverageRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BlueprintTranslationError("coverage_requirements 必须是列表")
    items: list[CoverageRequirement] = []
    for index, entry in enumerate(raw):
        where = f"coverage_requirements[{index}]"
        if not isinstance(entry, dict):
            raise BlueprintTranslationError(f"{where} 必须是对象")
        unknown = set(entry) - {"target_id", "target_kind", "role", "min_weight"}
        if unknown:
            raise BlueprintTranslationError(f"{where} 有未知字段：{sorted(unknown)}")
        kind = str(entry.get("target_kind", "")).strip()
        if kind not in {"concept", "learning_outcome"}:
            raise BlueprintTranslationError(f"{where}.target_kind 只能是 concept 或 learning_outcome")
        try:
            target = UUID(str(entry.get("target_id")))
        except (TypeError, ValueError) as exc:
            raise BlueprintTranslationError(f"{where}.target_id 不是 UUID") from exc
        items.append(CoverageRequirement(
            target_id=target,
            target_kind=kind,
            role=str(entry.get("role", "primary")).strip() or "primary",
            min_weight=_int_of(entry.get("min_weight", 0), where=f"{where}.min_weight"),
        ))
    return tuple(items)


def _kind_requirements(raw: Any) -> tuple[KindRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BlueprintTranslationError("kind_requirements 必须是列表")
    items: list[KindRequirement] = []
    for index, entry in enumerate(raw):
        where = f"kind_requirements[{index}]"
        if not isinstance(entry, dict):
            raise BlueprintTranslationError(f"{where} 必须是对象")
        unknown = set(entry) - {"section_code", "exercise_kind", "min_count", "max_count"}
        if unknown:
            raise BlueprintTranslationError(f"{where} 有未知字段：{sorted(unknown)}")
        section = entry.get("section_code")
        exercise_kind = str(entry.get("exercise_kind", "")).strip()
        if not exercise_kind:
            raise BlueprintTranslationError(f"{where}.exercise_kind 不能为空")
        max_count = entry.get("max_count")
        items.append(KindRequirement(
            section_code=None if section is None else str(section).strip() or None,
            exercise_kind=exercise_kind,
            min_count=_int_of(entry.get("min_count", 0), where=f"{where}.min_count"),
            max_count=None if max_count is None else _int_of(max_count, where=f"{where}.max_count"),
        ))
    return tuple(items)


def _hard_constraints(raw: dict[str, Any]) -> HardConstraints:
    unsupported = tuple(sorted(key for key in raw if key not in KNOWN_HARD_KEYS))
    groups: list[tuple[UUID, ...]] = []
    groups.extend(_groups(raw.get("conflict_groups"), where="conflict_groups"))
    groups.extend(_groups(raw.get("leak_groups"), where="leak_groups"))
    max_duration = raw.get("duration_max_seconds")
    return HardConstraints(
        section_scores={},
        coverage_requirements=_coverage_requirements(raw.get("coverage_requirements")),
        kind_requirements=_kind_requirements(raw.get("kind_requirements")),
        max_duration_seconds=(
            None if max_duration is None else _int_of(max_duration, where="duration_max_seconds")
        ),
        exclusion_groups=tuple(groups),
        unsupported_keys=unsupported,
    )


def _targets_bp(raw: Any, *, where: str) -> dict[int, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BlueprintTranslationError(f"{where} 必须是 {{难度: 万分比}} 对象")
    parsed: dict[int, int] = {}
    for key, value in raw.items():
        parsed[_int_of(key, where=f"{where} 的键")] = _int_of(value, where=f"{where}[{key}]")
    return parsed


def _soft_objectives(raw: dict[str, Any]) -> SoftObjectives:
    unknown = sorted(key for key in raw if key not in KNOWN_SOFT_KEYS)
    if unknown:
        # 软目标是"教师填了数字就期待它影响结果"的东西，静默忽略比硬约束更糟：
        # 教师会以为自己调过权重，其实一次都没生效。
        raise BlueprintTranslationError(
            f"soft_objectives 有本实现不评估的键：{unknown}；要么去掉，要么先实现加权"
        )
    target_duration = raw.get("duration_target_seconds")
    return SoftObjectives(
        difficulty_targets_bp=_targets_bp(
            raw.get("difficulty_targets_bp"), where="difficulty_targets_bp"),
        difficulty_weight=_int_of(raw.get("difficulty_weight", 0), where="difficulty_weight"),
        target_duration_seconds=(
            None if target_duration is None
            else _int_of(target_duration, where="duration_target_seconds")
        ),
        duration_weight=_int_of(raw.get("duration_weight", 0), where="duration_weight"),
        cognitive_targets_bp=_targets_bp(
            raw.get("cognitive_targets_bp"), where="cognitive_targets_bp"),
        cognitive_weight=_int_of(raw.get("cognitive_weight", 0), where="cognitive_weight"),
        recent_use_weight=_int_of(raw.get("recent_use_weight", 0), where="recent_use_weight"),
        exposure_balance_weight=_int_of(
            raw.get("exposure_balance_weight", 0), where="exposure_balance_weight"),
        teacher_preference_weight=_int_of(
            raw.get("teacher_preference_weight", 0), where="teacher_preference_weight"),
        ab_structure_weight=_int_of(
            raw.get("ab_structure_weight", 0), where="ab_structure_weight"),
    )


def _slot_specs(version_id: UUID, sections: Iterable[Any]) -> tuple[SlotSpec, ...]:
    slots: list[SlotSpec] = []
    seen: set[str] = set()
    for section in sections:
        for slot in section.slots:
            code = str(slot.code)
            if code in seen:
                # 阶段 A 的 PaperService 也按 code 平铺匹配槽位；重码会让"选了哪个
                # 槽位"变成二义，这里提前拒绝。
                raise BlueprintTranslationError(f"槽位码在整份蓝图里重复：{code}")
            seen.add(code)
            slots.append(SlotSpec(
                slot_id=slot_identity(version_id, section.code, code),
                code=code,
                section_code=str(section.code),
                score=Decimal(slot.score),
                estimated_seconds=int(slot.estimated_seconds or 0),
                allowed_kinds=tuple(slot.exercise_kinds),
                required_concept_ids=tuple(slot.required_concept_ids),
                learning_outcome_ids=tuple(slot.learning_outcome_ids),
                difficulty=_parse_difficulty(slot.difficulty_label),
                mandatory_version_ids=tuple(slot.mandatory_exercise_version_ids),
                excluded_version_ids=tuple(slot.excluded_exercise_version_ids),
            ))
    return tuple(slots)


def blueprint_snapshot(version: Any) -> BlueprintSnapshot:
    """`BlueprintVersionRow` → `BlueprintSnapshot`（只读、不触库）。

    手工蓝图在这里被拒绝：`assembly_mode=manual` 的意思是"这份卷由教师逐题挑选"，
    把它交给求解器会得到一份**教师没打算让机器决定**的卷子，而且阶段 A 的建卷路径
    也会因为"手工蓝图不能声称来自求解运行"而拒绝落卷——两条路都堵着，早点报错
    比让教师跑完一次求解再看到 409 好。
    """
    content = version.content
    mode = str(getattr(version, "assembly_mode", "") or "").strip()
    if mode == "manual":
        raise BlueprintTranslationError(
            "这份蓝图是手工组卷（assembly_mode=manual）：它没有约束求解所需的"
            "槽位条件。要跑求解，请先另存为约束组卷蓝图（assembly_mode=constraint_assisted）。"
        )
    hard_raw = dict(getattr(content, "hard_constraints", None) or {})
    soft_raw = dict(getattr(content, "soft_objectives", None) or {})
    variant_mode = str(hard_raw.get("variant_mode", "single")).strip() or "single"
    if variant_mode not in {"single", "ab"}:
        raise BlueprintTranslationError(f"variant_mode 只能是 single 或 ab：{variant_mode!r}")
    if variant_mode == "ab":
        raise BlueprintTranslationError(
            "A/B 组卷尚未实现：本阶段只交付单卷求解，不能把 A/B 蓝图当单卷跑"
        )
    return BlueprintSnapshot(
        blueprint_version_id=version.id,
        course_id=content.course_id,
        offering_id=content.offering_id,
        release_context_id=content.release_context_id,
        purpose=content.purpose,
        total_score=Decimal(content.total_score),
        duration_seconds=int(content.duration_seconds),
        slots=_slot_specs(version.id, content.sections),
        hard_constraints=_hard_constraints(hard_raw),
        soft_objectives=_soft_objectives(soft_raw),
        assembly_mode=variant_mode,
        anchor_version_ids=(),
        visible_from=None,
    )


__all__ = [
    "BlueprintTranslationError",
    "KNOWN_HARD_KEYS",
    "KNOWN_SOFT_KEYS",
    "blueprint_snapshot",
]
