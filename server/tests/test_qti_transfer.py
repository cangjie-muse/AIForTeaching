"""阶段 E：QTI 导入只产生草稿、差异不静默、导出不绕过治理。

三条门禁的测试对应设计 §13.2：

  * 导入进来的卷子**不是 approved**（不继承别人的审核）；
  * 不支持的交互类型、缺失题干、非法分值都要出现在差异报告里；
  * 导出遇到未批准 / 缺题 / 缺来源 Release / 缺题干 → 拒绝并列出全部原因。

运行：`python server/tests/test_qti_transfer.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.qti.contracts import (  # noqa: E402
    ExportSlot,
    QtiExportBlocked,
    QtiImportError,
    diff_roundtrip,
    export_qti,
    import_qti,
)


def payload(*items) -> dict:
    return {"items": list(items)}


def item(ref: str, prompt: str = "求极限", *, score: str = "10.00",
         interaction: str = "qti-text-entry", answer: str | None = None,
         metadata: dict | None = None) -> dict:
    body = {
        "identifier": ref,
        "itemBody": {"prompt": prompt},
        "interaction": {"type": interaction},
        "maxScore": score,
    }
    if answer is not None:
        body["correctResponse"] = answer
    if metadata is not None:
        body["metadata"] = metadata
    return body


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def test_import_produces_draft_only() -> None:
    report = import_qti(payload(item("q-1")))
    assert report.status == "draft", "导入不继承外部审核结论"
    assert report.importable is True
    assert report.items[0].max_score == Decimal("10.00")


def test_empty_file_is_rejected() -> None:
    try:
        import_qti({"items": []})
    except QtiImportError as error:
        assert "没有任何题目" in str(error)
    else:
        raise AssertionError("空文件必须拒绝，而不是导入一份空卷")


def test_missing_prompt_is_an_error_not_a_silent_skip() -> None:
    report = import_qti(payload(item("q-1", prompt="  "), item("q-2")))
    assert [d.code for d in report.errors] == ["missing_prompt"]
    assert [it.ref for it in report.items] == ["q-2"]
    assert report.importable is False, "有 error 的导入不能直接可用"


def test_unsupported_interaction_is_a_warning_with_downgrade() -> None:
    report = import_qti(payload(item("q-1", interaction="qti-hotspot")))
    warning = [d for d in report.differences if d.code == "unsupported_interaction"]
    assert warning and warning[0].severity == "warning"
    assert report.items[0].interaction == "qti-text-entry", "降级要说出来，不能悄悄丢"
    assert report.importable is True, "降级是 warning：仍然可导入"


def test_invalid_score_is_rejected() -> None:
    report = import_qti(payload(item("q-1", score="abc"), item("q-2", score="0")))
    codes = [d.code for d in report.differences if d.item_ref in {"q-1", "q-2"}]
    assert codes == ["invalid_score", "invalid_score"], codes
    assert report.items == ()


def test_foreign_governance_fields_are_dropped_and_reported() -> None:
    report = import_qti(payload(item(
        "q-1", metadata={"reviewStatus": "approved", "license": "all-rights-reserved"})))
    dropped = [d for d in report.differences if d.code == "foreign_governance_dropped"]
    assert len(dropped) == 2, "每个外部治理字段都要报一次"
    assert report.status == "draft"
    assert report.importable is True


def test_answer_is_carried_but_optional() -> None:
    with_answer = import_qti(payload(item("q-1", answer="1")))
    without = import_qti(payload(item("q-2")))
    assert with_answer.items[0].answer == "1"
    assert without.items[0].answer is None


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def slot(number: int, code: str, *, score: str = "10.00", prompt: str | None = "题干",
         exercise: UUID | None = None, release: UUID | None = None) -> ExportSlot:
    return ExportSlot(
        slot_no=number, code=code, score=Decimal(score), prompt=prompt,
        exercise_version_id=exercise or UUID(int=100 + number),
        source_release_id=release or UUID(int=200 + number),
    )


def test_export_preserves_order_and_scores() -> None:
    exported = export_qti(
        slots=[slot(2, "A2", score="20.00"), slot(1, "A1", score="30.00")],
        paper_status="approved", paper_title="数学分析 I 期末")
    assert [entry["identifier"] for entry in exported["items"]] == ["A1", "A2"]
    assert [entry["maxScore"] for entry in exported["items"]] == ["30.00", "20.00"]
    assert exported["totalScore"] == "50.00"


def test_export_refuses_unapproved_paper() -> None:
    try:
        export_qti(slots=[slot(1, "A1")], paper_status="draft",
                   paper_title="草稿卷")
    except QtiExportBlocked as error:
        assert "paper_status_draft" in error.reasons
    else:
        raise AssertionError("未批准卷不能导出")


def test_export_refuses_missing_release_or_exercise() -> None:
    missing_release = ExportSlot(
        slot_no=1, code="A1", score=Decimal("10.00"), prompt="题干",
        exercise_version_id=UUID(int=1), source_release_id=None)
    try:
        export_qti(slots=[missing_release], paper_status="approved",
                   paper_title="卷")
    except QtiExportBlocked as error:
        assert "slot_A1_missing_release" in error.reasons
    else:
        raise AssertionError("缺来源 Release 不能导出（会绕过治理）")


def test_export_refuses_empty_prompt() -> None:
    try:
        export_qti(slots=[slot(1, "A1", prompt="  ")], paper_status="approved",
                   paper_title="卷")
    except QtiExportBlocked as error:
        assert "slot_A1_missing_prompt" in error.reasons
    else:
        raise AssertionError("缺题干不能导出")


def test_export_lists_all_reasons_at_once() -> None:
    bad = [
        ExportSlot(slot_no=1, code="A1", score=Decimal("10.00"), prompt=None,
                   exercise_version_id=None, source_release_id=None),
    ]
    try:
        export_qti(slots=bad, paper_status="draft", paper_title="卷")
    except QtiExportBlocked as error:
        assert len(error.reasons) >= 3, error.reasons
    else:
        raise AssertionError("应一次列出全部原因，而不是只报第一个")


def test_export_refuses_empty_paper() -> None:
    try:
        export_qti(slots=[], paper_status="approved", paper_title="空卷")
    except QtiExportBlocked as error:
        assert "paper_has_no_slots" in error.reasons
    else:
        raise AssertionError("空卷不能导出成功")


def test_roundtrip_is_lossless_for_supported_items() -> None:
    exported = export_qti(slots=[slot(1, "A1", score="30.00"),
                                 slot(2, "A2", score="20.00")],
                          paper_status="approved", paper_title="数学分析 I")
    report = diff_roundtrip(exported)
    assert [it.ref for it in report.items] == ["A1", "A2"]
    assert [it.max_score for it in report.items] == [Decimal("30.00"), Decimal("20.00")]
    assert not report.errors, report.differences


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
    print(f"✓ QTI 导入导出测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
