"""阶段 D 任务 4：正式预评的三态语义、非确定项转人工、重试复用。

要钉住的都是"机器建议会不会污染成绩"：

  * `true` → 满分、`false` → 0、`fail` → **NULL + 人工**（不是 0！）；
  * 空答/证明题/不可解析/低 OCR/无检查器 → 转人工且**不调用检查器**；
  * 重试复用同一运行，两次预评结果完全一致；
  * 预评**一条 grade revision 都不写**（没有复核就没有分数）。

运行：`python server/tests/test_evaluation_service.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.grading.service import (  # noqa: E402
    AnswerTestFailure,
    GradingService,
    InMemoryEvaluationRepository,
    SubmissionItemContext,
    UnavailableRunner,
)

NOW = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
ITEM_A = UUID("dd000000-0000-4000-8000-0000000000a1")
ITEM_B = UUID("dd000000-0000-4000-8000-0000000000b1")
ITEM_C = UUID("dd000000-0000-4000-8000-0000000000c1")
REV_A = UUID("cc000000-0000-4000-8000-0000000000a1")


class ScriptedRunner:
    """按顺序返回预设结论的检查器；记录调用次数以证明"没有多跑"。"""

    name = "scripted"
    version = "7"

    def __init__(self, results, *, fail_with: str | None = None) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.fail_with = fail_with

    def run(self, response_text: str, answer_test):
        self.calls.append(response_text)
        if self.fail_with is not None:
            raise AnswerTestFailure(self.fail_with)
        return self.results.pop(0) if self.results else "fail"


def item(number: int, **changes) -> SubmissionItemContext:
    values = dict(
        submission_item_id=number, response_revision_id=REV_A,
        response_text="x^3+C", parse_status="parsed", response_kind="text",
        max_score=Decimal("5.00"), answer_test={"kind": "sympy"},
    )
    values.update(changes)
    return SubmissionItemContext(**values)


def service(runner=None, **kwargs):
    repo = InMemoryEvaluationRepository()
    return GradingService(repository=repo, runner=runner, clock=lambda: NOW, **kwargs), repo


# ---------------------------------------------------------------------------
# 三态语义
# ---------------------------------------------------------------------------

def test_true_false_fail_map_to_distinct_suggestions() -> None:
    grading, repo = service(ScriptedRunner(["true", "false", "fail"]))
    summary = grading.evaluate_submission(submission_id=SUBMISSION, items=[
        item(ITEM_A), item(ITEM_B), item(ITEM_C)])
    assert [entry.outcome for entry in summary.items] == ["true", "false", "fail"]
    assert summary.items[0].suggested_score == Decimal("5.00")
    assert summary.items[1].suggested_score == Decimal("0.00")
    assert summary.items[2].suggested_score is None, "fail 的建议分必须是 NULL，不是 0"
    assert summary.items[2].requires_human is True
    assert repo.grade_revisions == [], "预评不能写任何 grade revision"


def test_suggested_total_only_counts_deterministic_items() -> None:
    grading, _ = service(ScriptedRunner(["true", "fail"]))
    summary = grading.evaluate_submission(submission_id=SUBMISSION,
                                          items=[item(ITEM_A), item(ITEM_B)])
    assert summary.suggested_total == Decimal("5.00")
    assert summary.requires_human_count == 1


def test_false_is_not_flagged_for_human() -> None:
    grading, _ = service(ScriptedRunner(["false"]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                        items=[item(ITEM_A)]).items[0]
    assert entry.requires_human is False, "确定的错不需要人工复核"
    assert entry.deterministic is True


# ---------------------------------------------------------------------------
# 非确定项：不调用检查器、建议分 NULL
# ---------------------------------------------------------------------------

def test_empty_response_goes_to_human_without_calling_the_checker() -> None:
    grading, _ = service(ScriptedRunner([]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                        items=[item(ITEM_A, response_text="  ")]).items[0]
    assert entry.suggested_score is None
    assert entry.requires_human is True
    assert entry.reason == "empty_response"
    assert grading.runner.calls == [], "空答不该惊动检查器"


def test_proof_question_goes_to_human() -> None:
    grading, _ = service(ScriptedRunner([]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                        items=[item(ITEM_A, response_kind="proof")]).items[0]
    assert entry.reason == "proof_or_open"
    assert grading.runner.calls == []


def test_invalid_response_goes_to_human() -> None:
    grading, _ = service(ScriptedRunner([]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                        items=[item(ITEM_A, parse_status="invalid")]).items[0]
    assert entry.reason == "invalid_response"


def test_low_ocr_confidence_goes_to_human() -> None:
    grading, _ = service(ScriptedRunner([]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION, items=[
        item(ITEM_A, ocr_confidence=Decimal("0.62"))]).items[0]
    assert entry.reason == "low_ocr_confidence"


def test_missing_answer_test_goes_to_human() -> None:
    grading, _ = service(ScriptedRunner([]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                        items=[item(ITEM_A, answer_test=None)]).items[0]
    assert entry.reason == "missing_answer_test"
    assert entry.suggested_score is None


def test_ocr_confidence_at_threshold_is_deterministic() -> None:
    grading, _ = service(ScriptedRunner(["true"]))
    entry = grading.evaluate_submission(submission_id=SUBMISSION, items=[
        item(ITEM_A, ocr_confidence=Decimal("0.80"))]).items[0]
    assert entry.requires_human is False, "0.80 含等于，不该被判成低置信"


# ---------------------------------------------------------------------------
# 检查器失败与重试
# ---------------------------------------------------------------------------

def test_engine_failure_becomes_fail_and_human() -> None:
    for code in ("domain_uncertain", "cas_timeout", "engine_error"):
        grading, _ = service(ScriptedRunner([], fail_with=code))
        entry = grading.evaluate_submission(submission_id=SUBMISSION,
                                            items=[item(ITEM_A)]).items[0]
        assert entry.outcome == "fail"
        assert entry.suggested_score is None
        assert entry.requires_human is True
        assert entry.reason == code


def test_unavailable_runner_reports_missing_answer_test() -> None:
    grading, _ = service(UnavailableRunner())
    entry = grading.evaluate_submission(submission_id=SUBMISSION, items=[
        item(ITEM_A, answer_test={"kind": "sympy"})]).items[0]
    assert entry.reason == "missing_answer_test"
    assert entry.requires_human is True


def test_retry_reuses_runs_and_returns_the_same_summary() -> None:
    grading, repo = service(ScriptedRunner(["true", "false"]))
    items = [item(ITEM_A), item(ITEM_B)]
    first = grading.evaluate_submission(submission_id=SUBMISSION, items=items)
    second = grading.evaluate_submission(submission_id=SUBMISSION, items=items)
    assert first.items == second.items, "重试必须给出同一份结论"
    assert len(repo.runs) == 2, "重试不该再跑一遍检查器"
    assert len(grading.runner.calls) == 2


def test_different_runner_version_is_a_new_run() -> None:
    grading, repo = service(ScriptedRunner(["true"]))
    grading.evaluate_submission(submission_id=SUBMISSION, items=[item(ITEM_A)])
    grading.runner.version = "8"  # 升级检查器后必须重新评
    grading.evaluate_submission(submission_id=SUBMISSION, items=[item(ITEM_A)])
    assert len(repo.runs) == 2, "检查器版本变了就不能复用旧结论"


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
    print(f"✓ 正式预评测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
