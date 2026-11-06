"""阶段 A 共享契约（server/app/contracts）：模型、精确分值与学生投影白名单。

为什么单独测这一层：任务 1 之后所有固定卷代码都从这里 import 类型，而不是各自
定义近似结构。契约一旦允许额外字段，或者学生投影里混进 answer/solution/rubric，
后面每一层都会把泄露当成"正常字段"传下去；分值一旦落成二进制浮点，100 分的卷
会出现 99.99999999999999。这两类错误都不会当场报错，所以在最上游把它钉住。

运行方式沿用仓库习惯：`python server/tests/test_assessment_contracts.py`
（没有引入 pytest；文件顶层仍按 pytest 风格定义 test_ 函数，装了就同样能收集）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from inspect import isclass
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from pydantic import BaseModel, ValidationError  # noqa: E402

from app import contracts  # noqa: E402
from app.contracts import (  # noqa: E402
    STUDENT_FORBIDDEN_FIELDS,
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    PaperView,
    PublicAsset,
    StudentPaperProjection,
    StudentSlotProjection,
    StudentProjectionLeakError,
    assert_student_safe,
    canonical_hash,
)

# 学生卷槽位允许出现的字段，逐字段写死：多一个字段就是多一条泄露通道。
STUDENT_SLOT_FIELDS = {
    "slot_id",
    "question_no",
    "statement_ast",
    "public_assets",
    "response_kind",
    "answer_space_lines",
}
STUDENT_PAPER_FIELDS = {
    "paper_version_id",
    "version_short_code",
    "course_title",
    "instructions",
    "slots",
}


def blueprint() -> BlueprintContent:
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=uuid4(),
        offering_id=uuid4(),
        release_context_id=uuid4(),
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        sections=(
            BlueprintSection(
                code="A",
                title="基础",
                instruction="写出完整证明",
                slots=(
                    BlueprintSlot(
                        code="A1",
                        score=Decimal("40.00"),
                        exercise_kinds=("proof",),
                        required_concept_ids=(),
                        learning_outcome_ids=(),
                        difficulty_label=None,
                        estimated_seconds=1800,
                        mandatory_exercise_version_ids=(),
                        excluded_exercise_version_ids=(),
                    ),
                    BlueprintSlot(
                        code="A2",
                        score=Decimal("60.00"),
                        exercise_kinds=("expression",),
                        required_concept_ids=(),
                        learning_outcome_ids=(),
                        difficulty_label="中等",
                        estimated_seconds=2400,
                        mandatory_exercise_version_ids=(),
                        excluded_exercise_version_ids=(),
                    ),
                ),
            ),
        ),
        hard_constraints={"coverage_required": True},
        soft_objectives={},
        review_policy="formal_dual_review",
    )


def student_slot(**overrides: object) -> StudentSlotProjection:
    payload: dict[str, object] = {
        "slot_id": uuid4(),
        "question_no": "一、1",
        "statement_ast": {"latex": r"\int_0^1 x^2\,dx"},
        "public_assets": (),
        "response_kind": "proof",
        "answer_space_lines": 12,
    }
    payload.update(overrides)
    return StudentSlotProjection(**payload)  # type: ignore[arg-type]


def test_every_public_model_forbids_extra_fields() -> None:
    models = [
        value for value in vars(contracts).values()
        if isclass(value) and issubclass(value, BaseModel)
    ]
    assert models, "contracts 包没有导出任何 Pydantic 模型"
    for model in models:
        assert model.model_config.get("extra") == "forbid", (
            f"{model.__name__} 没有禁止额外字段（extra=forbid），"
            "上游多塞一个 answer 字段就会被静默接收"
        )


def test_scores_are_exact_decimals_and_serialize_as_two_place_strings() -> None:
    value = blueprint()
    assert value.total_score == Decimal("100.00")
    slot_scores = sum((s.score for s in value.sections[0].slots), Decimal("0"))
    assert slot_scores == Decimal("100.00"), f"槽位分求和不是精确 100：{slot_scores!r}"
    dumped = value.model_dump(mode="json")
    assert dumped["total_score"] == "100.00", dumped["total_score"]
    assert dumped["sections"][0]["slots"][0]["score"] == "40.00"


def test_scores_with_three_decimal_places_are_rejected() -> None:
    for bad in (Decimal("0.001"), Decimal("40.005")):
        try:
            BlueprintSlot(
                code="A1",
                score=bad,
                exercise_kinds=(),
                required_concept_ids=(),
                learning_outcome_ids=(),
                difficulty_label=None,
                estimated_seconds=None,
                mandatory_exercise_version_ids=(),
                excluded_exercise_version_ids=(),
            )
        except ValidationError:
            continue
        raise AssertionError(f"{bad!r} 有三位小数，仍然被接受")


def test_negative_and_non_finite_scores_are_rejected() -> None:
    for bad in (Decimal("-1.00"), Decimal("NaN"), Decimal("Infinity")):
        try:
            BlueprintSlot(
                code="A1",
                score=bad,
                exercise_kinds=(),
                required_concept_ids=(),
                learning_outcome_ids=(),
                difficulty_label=None,
                estimated_seconds=None,
                mandatory_exercise_version_ids=(),
                excluded_exercise_version_ids=(),
            )
        except ValidationError:
            continue
        raise AssertionError(f"{bad!r} 不是合法槽位分，仍然被接受")


def test_paper_status_accepts_only_the_four_contract_states() -> None:
    common = dict(
        blueprint_version_id=uuid4(),
        release_context_id=uuid4(),
        course_id=uuid4(),
        offering_id=None,
        purpose="formal",
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        edit_seq=1,
        manifest_hash="0" * 64,
        review_manifest_hash=None,
        sections=(),
    )
    for status in ("draft", "in_review", "approved", "withdrawn"):
        assert PaperView(id=uuid4(), status=status, **common).status == status  # type: ignore[arg-type]
    try:
        PaperView(id=uuid4(), status="returned", **common)  # type: ignore[arg-type]
    except ValidationError:
        return
    raise AssertionError("PaperStatus 接受了第五种状态 returned")


def test_student_projection_has_only_whitelisted_fields() -> None:
    assert set(StudentSlotProjection.model_fields) == STUDENT_SLOT_FIELDS
    assert set(StudentPaperProjection.model_fields) == STUDENT_PAPER_FIELDS
    leaked = STUDENT_SLOT_FIELDS & STUDENT_FORBIDDEN_FIELDS
    assert not leaked, f"学生投影白名单里出现答案类字段：{sorted(leaked)}"
    paper = StudentPaperProjection(
        paper_version_id=uuid4(),
        version_short_code="MATH-3F2A",
        course_title="数学分析",
        instructions="闭卷",
        slots=(student_slot(),),
    )
    dumped = paper.model_dump(mode="json")
    forbidden = STUDENT_FORBIDDEN_FIELDS
    flat_keys = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            flat_keys.update(str(key).lower() for key in value)
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(dumped)
    assert not (flat_keys & forbidden), f"学生卷序列化后出现答案类字段：{sorted(flat_keys & forbidden)}"


def test_student_projection_rejects_forbidden_extra_field() -> None:
    try:
        student_slot(solution="由中值定理可得")
    except ValidationError:
        return
    raise AssertionError("学生投影接受了 solution 字段")


def test_student_projection_rejects_answer_hidden_in_statement_ast() -> None:
    for key in ("answer", "solution", "rubric", "answer_test", "hidden_note"):
        try:
            student_slot(statement_ast={"latex": "x", key: "泄露内容"})
        except (ValidationError, StudentProjectionLeakError):
            continue
        raise AssertionError(f"statement_ast 里的 {key} 没有被拦下")


def test_assert_student_safe_walks_nested_structures() -> None:
    assert_student_safe({"statement_ast": {"latex": "x"}, "public_assets": [{"url": "u"}]})
    for payload in (
        {"rubric_json": "x"},
        {"statement_ast": {"answer_key": "x"}},
        {"public_assets": [{"solution": "x"}]},
        {"slots": ({"hidden_notes": ["x"]},)},
    ):
        try:
            assert_student_safe(payload)
        except StudentProjectionLeakError:
            continue
        raise AssertionError(f"嵌套结构中的泄露没有被发现：{payload!r}")


def test_public_asset_model_is_explicit_not_free_form() -> None:
    asset = PublicAsset(
        asset_id="fig-1", url="/assets/fig-1.png", mime_type="image/png", alt_text="函数图像"
    )
    slot = student_slot(public_assets=(asset,))
    assert slot.public_assets[0].alt_text == "函数图像"
    try:
        PublicAsset(
            asset_id="fig-1",
            url="/assets/fig-1.png",
            mime_type="image/png",
            alt_text="",
            solution="x",  # type: ignore[call-arg]
        )
    except ValidationError:
        return
    raise AssertionError("PublicAsset 接受了 solution 字段")


def test_canonical_hash_ignores_key_order_and_decimal_scale() -> None:
    first = canonical_hash({"b": 2, "a": Decimal("40.00")})
    second = canonical_hash({"a": Decimal("40.00"), "b": 2})
    third = canonical_hash({"a": Decimal("40"), "b": 2})
    assert first == second, "键顺序改变了内容哈希"
    assert first == third, "Decimal('40') 与 Decimal('40.00') 的哈希不一致"
    assert first != canonical_hash({"a": Decimal("41.00"), "b": 2}), "不同分值哈希相同"
    assert len(first) == 64


def test_canonical_hash_handles_time_uuid_and_nested_models() -> None:
    stamp = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    payload = {"at": stamp, "id": uuid4(), "tight": (1, 2)}
    digest = canonical_hash(payload)
    assert len(digest) == 64
    assert digest == canonical_hash(dict(payload)), "同一 payload 两次哈希不一致"
    assert canonical_hash({"value": {"x": 1}}) == canonical_hash({"value": {"x": 1}})
    value = blueprint()
    assert canonical_hash(value) == canonical_hash(value.model_dump()), (
        "模型与其字段字典的哈希不一致"
    )
    assert canonical_hash(value) == canonical_hash(value.model_dump(mode="json")), (
        "Python 模式与 JSON 模式的哈希不一致"
    )


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
        except AssertionError as exc:
            failures.append(name)
            print(f"  ✗ {name}：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 脚本模式下要把异常也记成失败
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 契约测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
