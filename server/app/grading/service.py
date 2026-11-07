"""正式提交的确定性预评（阶段 D 任务 4）。

预评**只建议、不落成绩**。这是整个评分链条里最容易越界的一步：

  * 一旦预评写入 grade，教师在复核前就有了"已发布的分数"，学生能看到的
    就是机器分——而机器分在证明题上不可靠；
  * 一旦把"判不出来"记成 0，成绩单会凭空多出错误，且无法区分"学生错"与"系统错"。

所以本模块的三条语义与 `practice` 完全一致，只是出口不同：

| 结论 | 建议分 | 需要人工 |
| --- | --- | --- |
| `true` | 满分 | 否 |
| `false` | 0.00 | 否 |
| `fail` | **None** | **是** |

另有五类"非确定项"直接转人工、**不调用检查器**：空答、证明/开放题、输入不可解析、
OCR 置信度低、缺少 answer_test。它们不是"错"，所以建议分是 NULL 而不是 0。

重试语义：按 `(提交项, 修订, 检查器版本)` 复用已有运行；同一提交多次预评结果完全相同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Protocol, Sequence
from uuid import UUID, uuid4

from pydantic import Field

from ..contracts.common import ContractModel, Score, validate_score
from ..practice.contracts import CheckVerdict
from ..releases import utcnow

# 非确定项的固定原因码：每一种都要能单独统计（否则"为什么这么多人工"无法回答）
NON_DETERMINISTIC = (
    "empty_response", "proof_or_open", "invalid_response", "low_ocr_confidence",
    "missing_answer_test",
)
OCR_MIN_CONFIDENCE = Decimal("0.80")

SUGGESTION_SCOPE = "submission-evaluation"


class AnswerTestFailure(RuntimeError):
    """检查器失败的四类情况：全部转人工，不能当 0 分。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AnswerTestRunner(Protocol):
    """跑一次检查器（sympy/Z3/Lean 都从这个端口进来）。"""

    name: str
    version: str

    def run(self, response_text: str, answer_test: Any) -> CheckVerdict:
        ...


class UnavailableRunner:
    name = "unavailable"
    version = "0"

    def run(self, response_text: str, answer_test: Any) -> CheckVerdict:
        raise AnswerTestFailure("missing_answer_test")


@dataclass(frozen=True)
class SubmissionItemContext:
    """一个提交项的预评输入（只含判定需要的事实）。"""

    submission_item_id: UUID
    response_revision_id: UUID | None
    response_text: str | None
    parse_status: str              # parsed | invalid | empty | unparsed
    response_kind: str             # text | proof | open
    max_score: Decimal
    answer_test: Any | None = None
    ocr_confidence: Decimal | None = None


class EvaluationItemResult(ContractModel):
    """一个提交项的预评结论（建议分可为空，表示"要人看"）。"""

    submission_item_id: UUID
    outcome: CheckVerdict | None = None
    suggested_score: Score | None = None
    requires_human: bool = False
    reason: str | None = None
    answer_test_run_id: UUID | None = None

    @property
    def deterministic(self) -> bool:
        return self.outcome in ("true", "false") and not self.requires_human


class EvaluationSummary(ContractModel):
    submission_id: UUID
    items: tuple[EvaluationItemResult, ...]
    evaluated_at: datetime

    @property
    def suggested_total(self) -> Decimal:
        """建议总分：只累加确定项；不确定项**不加 0**，但也不阻断——它要人看。"""
        return sum((item.suggested_score or Decimal("0") for item in self.items),
                   Decimal("0"))

    @property
    def requires_human_count(self) -> int:
        return sum(1 for item in self.items if item.requires_human)


@dataclass(frozen=True)
class Detailed:
    """仓储里的一条既有运行（用于重试复用）。

    `id` 必须随行保存：重试时要返回**同一个** answer_test_run_id，
    否则"两次预评结果相同"就成了假话（幂等性会被 id 打破）。
    """

    id: UUID
    submission_item_id: UUID
    response_revision_id: UUID | None
    runner_version: str
    outcome: CheckVerdict
    failure_kind: str | None = None


class InMemoryEvaluationRepository:
    """语义与 0037 的 `uq_formal_answer_run` 一致。"""

    def __init__(self) -> None:
        self.runs: list[Detailed] = []
        self.saved_items: dict[UUID, EvaluationItemResult] = {}
        self.grade_revisions: list[UUID] = []  # 预评永远不往里写，测试用它证伪

    def find_run(self, *, submission_item_id: UUID,
                 response_revision_id: UUID | None, runner_version: str) -> Detailed | None:
        for run in self.runs:
            if (run.submission_item_id == submission_item_id
                    and run.response_revision_id == response_revision_id
                    and run.runner_version == runner_version):
                return run
        return None

    def save_run(self, run: Detailed) -> None:
        self.runs.append(run)


class GradingService:
    def __init__(
        self,
        *,
        repository: InMemoryEvaluationRepository,
        runner: AnswerTestRunner | None = None,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self.runner = runner or UnavailableRunner()
        self._now = clock
        self._new_id = id_factory

    def evaluate_submission(self, *, submission_id: UUID,
                            items: Sequence[SubmissionItemContext]) -> EvaluationSummary:
        results = [self._evaluate_item(item) for item in items]
        return EvaluationSummary(submission_id=submission_id,
                                 items=tuple(results), evaluated_at=self._now())

    # -- 逐项 --------------------------------------------------------------
    def _evaluate_item(self, item: SubmissionItemContext) -> EvaluationItemResult:
        # 1) 非确定项：不调检查器，直接转人工，建议分 NULL（不是 0）。
        blocked = self._non_deterministic_reason(item)
        if blocked is not None:
            return EvaluationItemResult(
                submission_item_id=item.submission_item_id, outcome=None,
                suggested_score=None, requires_human=True, reason=blocked)

        # 2) 重试复用：同一 (提交项, 修订, 检查器版本) 只跑一次。
        existing = self.repository.find_run(
            submission_item_id=item.submission_item_id,
            response_revision_id=item.response_revision_id,
            runner_version=getattr(self.runner, "version", "0"))
        if existing is not None:
            return self._from_run(item, existing, existing.failure_kind)

        # 3) 跑检查器；失败一律转人工（不能当 0 分）。
        try:
            outcome = self.runner.run(item.response_text or "", item.answer_test)
        except AnswerTestFailure as failure:
            run = Detailed(id=self._new_id(),
                           submission_item_id=item.submission_item_id,
                           response_revision_id=item.response_revision_id,
                           runner_version=getattr(self.runner, "version", "0"),
                           outcome="fail", failure_kind=failure.code)
            self.repository.save_run(run)
            return EvaluationItemResult(
                submission_item_id=item.submission_item_id, outcome="fail",
                suggested_score=None, requires_human=True,
                reason=failure.code, answer_test_run_id=run.id)
        except Exception:  # noqa: BLE001 — 引擎炸了不等于学生答错
            run = Detailed(id=self._new_id(),
                           submission_item_id=item.submission_item_id,
                           response_revision_id=item.response_revision_id,
                           runner_version=getattr(self.runner, "version", "0"),
                           outcome="fail", failure_kind="engine_error")
            self.repository.save_run(run)
            return EvaluationItemResult(
                submission_item_id=item.submission_item_id, outcome="fail",
                suggested_score=None, requires_human=True,
                reason="engine_error", answer_test_run_id=run.id)

        run = Detailed(id=self._new_id(),
                       submission_item_id=item.submission_item_id,
                       response_revision_id=item.response_revision_id,
                       runner_version=getattr(self.runner, "version", "0"),
                       outcome=outcome)
        self.repository.save_run(run)
        return self._from_run(item, run, None)

    def _non_deterministic_reason(self, item: SubmissionItemContext) -> str | None:
        if item.response_text is None or not item.response_text.strip():
            return "empty_response"
        if item.response_kind in ("proof", "open"):
            return "proof_or_open"
        if item.parse_status != "parsed":
            return "invalid_response"
        if item.ocr_confidence is not None and item.ocr_confidence < OCR_MIN_CONFIDENCE:
            return "low_ocr_confidence"
        if item.answer_test is None:
            return "missing_answer_test"
        return None

    def _from_run(self, item: SubmissionItemContext, run: Detailed,
                  failure_kind: str | None) -> EvaluationItemResult:
        if failure_kind is not None or run.outcome == "fail":
            return EvaluationItemResult(
                submission_item_id=item.submission_item_id, outcome="fail",
                suggested_score=None, requires_human=True,
                reason=failure_kind or "cas_fail", answer_test_run_id=run.id)
        if run.outcome == "true":
            return EvaluationItemResult(
                submission_item_id=item.submission_item_id, outcome="true",
                suggested_score=item.max_score, requires_human=False,
                answer_test_run_id=run.id)
        return EvaluationItemResult(
            submission_item_id=item.submission_item_id, outcome="false",
            suggested_score=Decimal("0.00"), requires_human=False,
            answer_test_run_id=run.id)


__all__ = [
    "AnswerTestFailure",
    "AnswerTestRunner",
    "Detailed",
    "EvaluationItemResult",
    "EvaluationSummary",
    "GradingService",
    "InMemoryEvaluationRepository",
    "NON_DETERMINISTIC",
    "OCR_MIN_CONFIDENCE",
    "SUGGESTION_SCOPE",
    "SubmissionItemContext",
    "UnavailableRunner",
]
