"""阶段 E 任务 1—3：QTI 导入留痕、拒绝也记录、导出不绕过治理。

要钉住的六条：

  1. 导入结果只能是 draft_ready / rejected，**永远不是 approved**；
  2. 同一份文件重复导入命中同一行（内容寻址，`replayed=True`）；
  3. 解析失败/被拒也要落一条记录与原因（不能只返回 422）；
  4. 有 error 的导入是 rejected，不能"部分导入"还标成功；
  5. 导出成功落产物哈希与条目数；
  6. 导出被拒落 reasons（blocked），**不生成产物哈希**。

运行：`python server/tests/test_qti_service.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.qti.contracts import ExportSlot, QtiImportError  # noqa: E402
from app.qti.service import InMemoryQtiRepository, QtiService  # noqa: E402

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
TEACHER = UUID("5e5e0000-0000-4000-8000-0000000000c1")
PAPER = UUID("ee000000-0000-4000-8000-000000000001")


def build() -> tuple[QtiService, InMemoryQtiRepository]:
    repo = InMemoryQtiRepository()
    return QtiService(repository=repo, clock=lambda: NOW,
                      id_factory=lambda: UUID(int=300 + len(repo.imports))), repo


def item(ref: str = "q-1", *, prompt: str = "求极限", score: str = "10.00",
         interaction: str = "qti-text-entry", metadata: dict | None = None) -> dict:
    body = {"identifier": ref, "itemBody": {"prompt": prompt},
            "interaction": {"type": interaction}, "maxScore": score}
    if metadata is not None:
        body["metadata"] = metadata
    return body


def payload(*items) -> dict:
    return {"items": list(items)}


def slot(number: int, code: str, **changes) -> ExportSlot:
    values = dict(slot_no=number, code=code, score=Decimal("10.00"), prompt="题干",
                  exercise_version_id=UUID(int=100 + number),
                  source_release_id=UUID(int=200 + number))
    values.update(changes)
    return ExportSlot(**values)


def expect(error_type, fn):
    try:
        fn()
    except error_type as error:
        return error
    raise AssertionError(f"预期 {error_type.__name__}")


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def test_import_becomes_draft_ready_not_approved() -> None:
    service, repo = build()
    run = service.import_document(course_ref="analysis", payload=payload(item()),
                                  imported_by=TEACHER)
    assert run.status == "draft_ready", "导入不能自称已审核"
    assert run.item_count == 1
    assert run.warning_count == 0
    assert run.replayed is False


def test_reimporting_the_same_file_hits_the_same_row() -> None:
    service, repo = build()
    first = service.import_document(course_ref="analysis", payload=payload(item()),
                                    imported_by=TEACHER)
    second = service.import_document(course_ref="analysis", payload=payload(item()),
                                     imported_by=TEACHER)
    assert second.replayed is True
    assert second.id == first.id
    assert len(repo.imports) == 1, "内容寻址：重复导入不产生垃圾记录"


def test_changed_content_is_a_new_import() -> None:
    service, repo = build()
    service.import_document(course_ref="analysis", payload=payload(item()), imported_by=TEACHER)
    service.import_document(course_ref="analysis",
                            payload=payload(item(prompt="另一道题")), imported_by=TEACHER)
    assert len(repo.imports) == 2, "文件内容变了就该是新导入"


def test_import_with_errors_is_rejected_and_keeps_reasons() -> None:
    service, repo = build()
    run = service.import_document(course_ref="analysis",
                                  payload=payload(item(prompt="  ")), imported_by=TEACHER)
    assert run.status == "rejected"
    assert run.error_count == 1
    differences = service.differences_of(run.id)
    assert [d.code for d in differences] == ["missing_prompt"], (
        "拒绝也要留下「为什么」（教师看得懂的那句）")


def test_unparseable_document_is_recorded_too() -> None:
    service, repo = build()
    run = service.import_document(course_ref="analysis", payload={"items": []},
                                  imported_by=TEACHER)
    assert run.status == "rejected"
    differences = service.differences_of(run.id)
    assert differences[0].code == "qti_parse_failed"
    assert "没有" in differences[0].message


def test_foreign_governance_fields_become_warnings_not_approvals() -> None:
    service, _ = build()
    run = service.import_document(
        course_ref="analysis",
        payload=payload(item(metadata={"reviewStatus": "approved", "license": "unknown"})),
        imported_by=TEACHER)
    assert run.status == "draft_ready"
    assert run.warning_count == 2
    differences = service.differences_of(run.id)
    assert all(d.severity == "warning" for d in differences)


def test_import_requires_a_course() -> None:
    service, _ = build()
    expect(QtiImportError, lambda: service.import_document(
        course_ref="  ", payload=payload(item()), imported_by=TEACHER))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def test_export_records_payload_hash_and_count() -> None:
    service, repo = build()
    run = service.export_paper(
        paper_version_id=PAPER, slots=[slot(1, "A1"), slot(2, "A2")],
        paper_status="approved", paper_title="数学分析 I", exported_by=TEACHER)
    assert run.status == "exported"
    assert run.item_count == 2
    assert run.payload_sha256 and len(run.payload_sha256) == 64
    assert run.payload["totalScore"] == "20.00"


def test_export_rejection_is_recorded_with_all_reasons() -> None:
    service, repo = build()
    run = service.export_paper(
        paper_version_id=PAPER,
        slots=[slot(1, "A1", prompt=None, exercise_version_id=None,
                    source_release_id=None)],
        paper_status="draft", paper_title="草稿", exported_by=TEACHER)
    assert run.status == "blocked"
    assert "paper_status_draft" in run.reasons
    assert "slot_A1_missing_exercise" in run.reasons
    assert run.payload_sha256 is None, "被拒绝就不能有产物哈希"
    assert repo.exports[-1].status == "blocked", "拒绝也是一条证据"


def test_export_history_is_queryable() -> None:
    service, _ = build()
    service.export_paper(paper_version_id=PAPER, slots=[slot(1, "A1")],
                         paper_status="approved", paper_title="卷", exported_by=TEACHER)
    service.export_paper(paper_version_id=PAPER, slots=[],
                         paper_status="draft", paper_title="卷", exported_by=TEACHER)
    history = service.exports_of(PAPER)
    assert [item.status for item in history] == ["exported", "blocked"]


def test_export_does_not_mutate_slots() -> None:
    service, _ = build()
    before = slot(1, "A1")
    service.export_paper(paper_version_id=PAPER, slots=[before],
                         paper_status="approved", paper_title="卷", exported_by=TEACHER)
    assert before.code == "A1" and before.prompt == "题干"


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
    print(f"✓ QTI 留痕服务测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
