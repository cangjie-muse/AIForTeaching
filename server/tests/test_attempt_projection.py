"""阶段 C 任务 2：学生重连投影（白名单 + 最新修订 + 越权 404）。

这份测试的核心不是"字段对不对"，而是**学生端能不能看到不该看的东西**：

  * 题干里混进答案 → 抓；
  * `public_assets` 是自由 dict，嵌套的 `answer` → 抓；
  * 重连必须给服务端最新修订（不信浏览器草稿）；
  * 别人的 attempt → 404（不是 403：不告诉试探者"存在但你没权限"）；
  * 已撤回的作业：进行中 → `Withdrawn`；已交卷 → 回执 + 空 slots，不回传题干。

运行：`python server/tests/test_attempt_projection.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.online.contracts import (  # noqa: E402
    AssignmentWindow,
    PolicyTiming,
    ResourceNotFound,
    SlotRow,
    StudentAttemptDTO,
    SubmissionReceiptDTO,
    Withdrawn,
    build_student_attempt,
    compute_deadline,
)

NOW = datetime(2026, 11, 7, 1, 30, tzinfo=timezone.utc)
START = NOW - timedelta(minutes=30)
ATTEMPT = UUID("aa000000-0000-4000-8000-000000000001")
ASSIGNMENT = UUID("dd000000-0000-4000-8000-000000000001")
PAPER = UUID("ee000000-0000-4000-8000-000000000001")
SLOT_A = UUID("cc000000-0000-4000-8000-0000000000a1")
SLOT_B = UUID("cc000000-0000-4000-8000-0000000000b1")


def policy() -> PolicyTiming:
    return PolicyTiming(time_limit_seconds=None, grace_seconds=600,
                        late_policy="read_only_grace", max_attempts=1)


def window() -> AssignmentWindow:
    return AssignmentWindow(visible_at=START - timedelta(minutes=10),
                            due_at=START + timedelta(minutes=120),
                            hard_close_at=START + timedelta(minutes=150))


def rows() -> list[SlotRow]:
    return [
        SlotRow(slot_id=SLOT_A, slot_no=1, stem="计算极限", input_kind="text",
                raw_response={"text": "第一版"}, parse_status="parsed", revision_no=1),
        SlotRow(slot_id=SLOT_B, slot_no=2, stem="证明连续", input_kind="text",
                raw_response={"text": "第二版"}, parse_status="parsed", revision_no=2),
    ]


def build(**overrides) -> StudentAttemptDTO:
    values = dict(
        attempt_id=ATTEMPT, assignment_id=ASSIGNMENT, paper_version_id=PAPER,
        status="in_progress", assignment_status="published", attempt_no=1,
        started_at=START, server_now=NOW,
        window=compute_deadline(START, window(), policy()),
        policy=policy(), slots=rows(),
    )
    values.update(overrides)
    return build_student_attempt(**values)


# ---------------------------------------------------------------------------
# 重连带的是服务端最新修订
# ---------------------------------------------------------------------------

def test_reconnect_returns_server_latest_revision_per_slot() -> None:
    dto = build()
    assert [slot.revision_no for slot in dto.slots] == [1, 2]
    assert dto.slots[1].raw_response == {"text": "第二版"}


def test_slot_order_is_stable() -> None:
    dto = build()
    assert [slot.slot_no for slot in dto.slots] == [1, 2]


def test_deadline_and_save_intervals_are_exposed_to_the_client() -> None:
    dto = build()
    assert dto.server_deadline_at == compute_deadline(START, window(), policy()).effective_deadline
    assert dto.save_debounce_ms == policy().autosave_debounce_ms
    assert dto.max_save_interval_ms == policy().autosave_max_interval_ms
    assert dto.late_policy == "read_only_grace"


# ---------------------------------------------------------------------------
# 白名单：学生卷里不可能有答案
# ---------------------------------------------------------------------------

def test_dumped_dto_contains_no_forbidden_keys() -> None:
    import json

    encoded = json.dumps(build().model_dump(mode="json"), ensure_ascii=False)
    for forbidden in ('"answer":', '"solution":', '"rubric":', '"answer_test":'):
        assert forbidden not in encoded, f"学生投影里出现了 {forbidden}"


def test_slot_fields_are_the_whitelist_only() -> None:
    dto = build()
    assert set(type(dto.slots[0]).model_fields) == {
        "slot_id", "slot_no", "stem", "public_assets", "input_kind",
        "raw_response", "parse_status", "revision_no",
    }, "槽位字段变了就要重新审一遍：这是学生能看到的一切"


def test_nested_answer_in_public_assets_is_still_caught() -> None:
    """`public_assets` 是自由 dict：上游塞一个 answer 进去也能被封住。"""
    try:
        build(slots=[SlotRow(slot_id=SLOT_A, slot_no=1, stem="题",
                             public_assets=({"answer": "隐藏答案"},),
                             input_kind="text")])
    except AssertionError as error:
        assert "answer" in str(error)
    else:
        raise AssertionError("嵌套 answer 必须被 build_student_attempt 拦下")


# ---------------------------------------------------------------------------
# 撤回与交卷
# ---------------------------------------------------------------------------

def test_submitted_attempt_returns_receipt_without_stems() -> None:
    receipt = SubmissionReceiptDTO(
        submission_id=UUID(int=9), attempt_id=ATTEMPT, finalized_at=NOW,
        item_count=2, unanswered_count=0, invalid_count=0,
        response_manifest_hash="a" * 64, receipt_hash="b" * 64,
        submission_reason="manual")
    dto = build(status="submitted", assignment_status="withdrawn",
                receipt=receipt, include_slots=False)
    assert dto.slots == (), "已交卷（且作业撤回）时不能再回传题干"
    assert dto.submission_receipt is not None
    assert dto.submission_receipt.response_manifest_hash == "a" * 64


def test_withdrawn_in_progress_attempt_is_signalled_separately() -> None:
    """撤回对进行中的作答是"停止作答"，对已交卷是"事实保留"——两种语义不能混。"""
    # 这里只验证 DTO 能承载 withdrawn 状态；抛出 Withdrawn 由服务层负责（任务 3）。
    dto = build(status="in_progress", assignment_status="withdrawn")
    assert dto.assignment_status == "withdrawn"
    assert Withdrawn("assignment withdrawn").code == "assignment_withdrawn"
    assert ResourceNotFound("not yours").code == "attempt_not_found"


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
    print(f"✓ 学生重连投影测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
