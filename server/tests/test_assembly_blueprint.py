"""阶段 B 任务 1：蓝图 → 求解器输入的翻译规则。

钉住的是"哪些东西会被悄悄丢掉"：

  * 槽位身份必须稳定（否则两次运行的方案 manifest 无法逐字节比较）；
  * 蓝图里写了、但本实现不评估的硬约束必须**显式带出来**——阶段 A 的校验器对
    不认识的键是静默忽略的，如果翻译层也忽略，教师会以为自己设了覆盖要求；
  * 软目标键不认识就报错，因为"教师调过权重但一次都没生效"是最难发现的那种错；
  * A/B 尚未实现就拒绝，不能当作单卷跑出一个"看起来像 A/B"的结果。

运行：`python server/tests/test_assembly_blueprint.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.blueprint import (  # noqa: E402
    BlueprintTranslationError,
    blueprint_snapshot,
)
from app.assembly.contracts import slot_identity  # noqa: E402
from app.contracts.paper import BlueprintContent, BlueprintSection, BlueprintSlot  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402

COURSE_ID = ref_uuid("course", "analysis")
OFFERING_ID = ref_uuid("offering", "analysis-2026a")
CONTEXT = UUID("3f8f0d1a-0000-5000-8000-000000000001")
VERSION_ID = UUID("4a0e0000-0000-5000-8000-000000000002")


def slot(code: str, score: str, **overrides: object) -> BlueprintSlot:
    payload: dict[str, object] = {"code": code, "score": Decimal(score)}
    payload.update(overrides)
    return BlueprintSlot(**payload)  # type: ignore[arg-type]


def content(
    sections: tuple[BlueprintSection, ...] | None = None,
    *,
    hard: dict[str, object] | None = None,
    soft: dict[str, object] | None = None,
) -> BlueprintContent:
    if sections is None:
        sections = (
            BlueprintSection(
                code="A", title="基础", slots=(slot("A1", "16.00"), slot("A2", "24.00"))),
            BlueprintSection(
                code="B", title="计算", slots=(slot("B1", "20.00"),)),
        )
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        total_score=Decimal("60.00"),
        duration_seconds=5400,
        sections=sections,
        hard_constraints=hard or {},
        soft_objectives=soft or {},
        review_policy="formal_dual_review",
        assembly_mode="constraint_assisted",
    )


def version(body: BlueprintContent | None = None):
    return SimpleNamespace(id=VERSION_ID, content=body or content())


# ---------------------------------------------------------------------------
# 槽位身份
# ---------------------------------------------------------------------------

def test_slot_identity_is_stable_and_distinct() -> None:
    first = slot_identity(VERSION_ID, "A", "A1")
    again = slot_identity(VERSION_ID, "A", "A1")
    other_slot = slot_identity(VERSION_ID, "A", "A2")
    other_version = slot_identity(UUID(int=999), "A", "A1")
    assert first == again, "同一槽位必须永远同一 id，否则回放对不上"
    assert len({first, other_slot, other_version}) == 3


def test_slots_carry_section_score_and_order() -> None:
    snapshot = blueprint_snapshot(version())
    assert [item.code for item in snapshot.slots] == ["A1", "A2", "B1"]
    assert [item.section_code for item in snapshot.slots] == ["A", "A", "B"]
    assert snapshot.slots[0].score == Decimal("16.00")
    # 分值保持 Decimal：一旦中途变 float，100 分卷会出现 99.999…
    assert isinstance(snapshot.slots[0].score, Decimal)
    assert snapshot.total_score == Decimal("60.00")
    assert snapshot.slots[0].slot_id == slot_identity(VERSION_ID, "A", "A1")


def test_duplicate_slot_code_across_sections_is_rejected() -> None:
    sections = (
        BlueprintSection(code="A", title="一", slots=(slot("1", "10.00"),)),
        BlueprintSection(code="B", title="二", slots=(slot("1", "10.00"),)),
    )
    try:
        blueprint_snapshot(version(content(sections)))
    except BlueprintTranslationError as error:
        assert "重复" in str(error), str(error)
    else:
        raise AssertionError("重码槽位必须被拒绝：阶段 A 按 code 平铺匹配，重码就是二义")


# ---------------------------------------------------------------------------
# 硬约束
# ---------------------------------------------------------------------------

def test_unknown_hard_constraint_is_reported_not_ignored() -> None:
    snapshot = blueprint_snapshot(version(content(hard={"coverage_required": True})))
    assert snapshot.hard_constraints.unsupported_keys == ("coverage_required",), (
        "蓝图里写了但本实现不评估的硬约束必须显式列出来"
    )


def test_duration_max_is_parsed_as_integer_seconds() -> None:
    snapshot = blueprint_snapshot(version(content(hard={"duration_max_seconds": 5400})))
    assert snapshot.hard_constraints.max_duration_seconds == 5400
    assert snapshot.hard_constraints.unsupported_keys == ()


def test_boolean_is_not_accepted_as_seconds() -> None:
    try:
        blueprint_snapshot(version(content(hard={"duration_max_seconds": True})))
    except BlueprintTranslationError as error:
        assert "整数" in str(error), str(error)
    else:
        raise AssertionError("True 不能被当成 1 秒：布尔与整数必须分开")


def test_conflict_and_leak_groups_become_exclusion_groups() -> None:
    first, second, third = (UUID(int=value) for value in (1, 2, 3))
    snapshot = blueprint_snapshot(version(content(hard={
        "conflict_groups": [[str(first), str(second)]],
        "leak_groups": [[str(second), str(third)]],
    })))
    groups = snapshot.hard_constraints.exclusion_groups
    assert len(groups) == 2
    assert groups[0] == tuple(sorted((first, second), key=str))
    # 组内排序让"同一组写成不同顺序"不会产生两份不同的 manifest
    assert groups[1] == tuple(sorted((second, third), key=str))


def test_single_member_group_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(hard={"conflict_groups": [[str(UUID(int=1))]]})))
    except BlueprintTranslationError as error:
        assert "互斥组" in str(error), str(error)
    else:
        raise AssertionError("单元素互斥组等于'禁用该题'，不该被当成互斥语义收下")


def test_malformed_group_member_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(hard={"conflict_groups": [["not-a-uuid", "x"]]})))
    except BlueprintTranslationError as error:
        assert "UUID" in str(error), str(error)
    else:
        raise AssertionError("非法 UUID 必须当场报错")


def test_coverage_and_kind_requirements_are_parsed() -> None:
    concept = UUID(int=77)
    snapshot = blueprint_snapshot(version(content(hard={
        "coverage_requirements": [{
            "target_id": str(concept), "target_kind": "concept",
            "role": "primary", "min_weight": 3,
        }],
        "kind_requirements": [{
            "section_code": "A", "exercise_kind": "proof", "min_count": 1, "max_count": 2,
        }],
    })))
    coverage = snapshot.hard_constraints.coverage_requirements
    kinds = snapshot.hard_constraints.kind_requirements
    assert coverage[0].target_id == concept
    assert coverage[0].min_weight == 3
    assert kinds[0].exercise_kind == "proof"
    assert kinds[0].section_code == "A"
    assert kinds[0].max_count == 2


def test_coverage_requirement_with_unknown_field_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(hard={"coverage_requirements": [{
            "target_id": str(UUID(int=1)), "target_kind": "concept", "min_weight": 1,
            "weight": 5,
        }]})))
    except BlueprintTranslationError as error:
        assert "未知字段" in str(error), str(error)
    else:
        raise AssertionError("多写的字段不能被静默忽略：教师以为设了权重")


def test_invalid_coverage_target_kind_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(hard={"coverage_requirements": [{
            "target_id": str(UUID(int=1)), "target_kind": "family", "min_weight": 1,
        }]})))
    except BlueprintTranslationError as error:
        assert "target_kind" in str(error), str(error)
    else:
        raise AssertionError("target_kind 是闭集，family 不在其中")


# ---------------------------------------------------------------------------
# 软目标
# ---------------------------------------------------------------------------

def test_soft_objectives_are_integers() -> None:
    snapshot = blueprint_snapshot(version(content(soft={
        "duration_target_seconds": 5400,
        "duration_weight": 5,
        "difficulty_targets_bp": {"3": 5000},
        "difficulty_weight": 2,
    })))
    soft = snapshot.soft_objectives
    assert soft.target_duration_seconds == 5400
    assert soft.duration_weight == 5
    assert soft.difficulty_targets_bp == {3: 5000}
    assert soft.difficulty_weight == 2


def test_unknown_soft_objective_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(soft={"guess_weight": 3})))
    except BlueprintTranslationError as error:
        assert "guess_weight" in str(error), str(error)
    else:
        raise AssertionError("不认识的软目标必须报错：静默忽略等于权重从未生效")


# ---------------------------------------------------------------------------
# 用途、难度、A/B
# ---------------------------------------------------------------------------

def test_anchor_versions_are_read_from_hard_constraints() -> None:
    """锚题写在蓝图 `hard_constraints.anchor_version_ids` 上，必须被翻译出来。

    A/B 的可比性支点就是这几道题；读不出来等于"没有锚题"，两卷会完全不重叠，
    教师也就没法解释两卷为什么可比较。
    """
    anchor_a = UUID(int=11)
    anchor_b = UUID(int=12)
    snapshot = blueprint_snapshot(version(content(hard={
        "anchor_version_ids": [str(anchor_a), str(anchor_b)],
    })))
    assert snapshot.anchor_version_ids == (anchor_a, anchor_b)
    assert "anchor_version_ids" not in snapshot.hard_constraints.unsupported_keys


def test_anchor_versions_must_be_uuids() -> None:
    try:
        blueprint_snapshot(version(content(hard={"anchor_version_ids": ["not-a-uuid"]})))
    except BlueprintTranslationError as error:
        assert "UUID" in str(error), str(error)
    else:
        raise AssertionError("非法锚题 id 必须当场报错，而不是静默当成没有锚题")


def test_ab_mode_is_refused_until_implemented() -> None:
    try:
        blueprint_snapshot(version(content(hard={"variant_mode": "ab"})))
    except BlueprintTranslationError as error:
        assert "A/B" in str(error), str(error)
    else:
        raise AssertionError("A/B 未实现时不能当单卷跑")


def test_manual_blueprint_is_refused_with_a_way_out() -> None:
    """手工蓝图不能跑求解：报错必须说清"另存为约束组卷蓝图"。"""
    body = content()
    manual = SimpleNamespace(id=VERSION_ID, content=body, assembly_mode="manual")
    try:
        blueprint_snapshot(manual)
    except BlueprintTranslationError as error:
        assert "手工组卷" in str(error), str(error)
        assert "constraint_assisted" in str(error), "必须给出可执行的下一步"
    else:
        raise AssertionError("手工蓝图交给求解器必须被拒绝")


def test_constraint_assisted_blueprint_is_accepted() -> None:
    body = content()
    assisted = SimpleNamespace(id=VERSION_ID, content=body,
                               assembly_mode="constraint_assisted")
    assert blueprint_snapshot(assisted).blueprint_version_id == VERSION_ID


def test_unknown_variant_mode_is_rejected() -> None:
    try:
        blueprint_snapshot(version(content(hard={"variant_mode": "parallel"})))
    except BlueprintTranslationError as error:
        assert "variant_mode" in str(error), str(error)
    else:
        raise AssertionError("variant_mode 是闭集")


def test_difficulty_label_becomes_integer() -> None:
    sections = (
        BlueprintSection(code="A", title="一", slots=(
            slot("A1", "10.00", difficulty_label="difficulty-3"),
            slot("A2", "10.00", difficulty_label="medium"),
        )),
    )
    snapshot = blueprint_snapshot(version(content(sections)))
    assert snapshot.slots[0].difficulty == 3
    # 非数字标签不是"难度 0"，而是"未声明"——虚构一个 0 会让软目标算错
    assert snapshot.slots[1].difficulty is None


def test_slot_fixed_choices_are_carried() -> None:
    mandatory = UUID(int=8)
    excluded = UUID(int=9)
    sections = (
        BlueprintSection(code="A", title="一", slots=(
            slot("A1", "10.00",
                 exercise_kinds=("proof",),
                 mandatory_exercise_version_ids=(mandatory,),
                 excluded_exercise_version_ids=(excluded,),
                 estimated_seconds=600),
        )),
    )
    snapshot = blueprint_snapshot(version(content(sections)))
    spec = snapshot.slots[0]
    assert spec.allowed_kinds == ("proof",)
    assert spec.mandatory_version_ids == (mandatory,)
    assert spec.excluded_version_ids == (excluded,)
    assert spec.estimated_seconds == 600


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
    print(f"✓ 蓝图翻译测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
