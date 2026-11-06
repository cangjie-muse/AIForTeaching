"""练习逐题检查（阶段 D 任务 2）：真正区分 `true / false / fail`。

设计 §12.2 与 §4.3 门禁的核心就一句话：**"判不出来"不等于"判错了"**。

  * `true`  —— 检查器证明答案正确（CAS 返回真）；
  * `false` —— 检查器证明答案错误（CAS 返回假，且**有反例**）；
  * `fail`  —— 判不出来（超时、语法错误、CAS 自身异常、题面不支持自动判）。

把 `fail` 打成 0 分是最常见的错误：学生会看到"答错了"，教师会在成绩单上看到
一个假的错误统计。这里把三种状态做成独立类型，并强制 `fail` 不带分数。

三条附加规则：

1. **检查绑定 revision。** 结果属于某一条 `response_revision`；自动保存不会触发检查，
   只有显式的"检查"动作会，并且必须回传它检查的是哪一版（§12.1）。
2. **反馈是白名单。** 练习反馈可以带提示与解析，但**不能带完整答案文本**——
   `answer_md` 只在"答案已发布"时才允许出现，且由调用方显式传入。
3. **不确定不打分。** `fail` 的 `score` 必须是 None，而不是 0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from ..contracts.common import ContractModel
from pydantic import Field

CheckVerdict = Literal["true", "false", "fail"]
CheckReason = Literal[
    "cas_true", "cas_false", "timeout", "syntax_error", "unsupported_exercise",
    "checker_error", "empty_response",
]


class CheckStatus(StrEnum):
    TRUE = "true"
    FALSE = "false"
    FAIL = "fail"


class CheckResult(ContractModel):
    """一次逐题检查的结论。

    `counterexample` 只在 `false` 时可能有值：没有反例的"错误"对学生没有学习价值，
    而且很容易是检查器自己错了。
    """

    revision_id: UUID
    exercise_version_id: UUID
    status: CheckVerdict
    reason: CheckReason
    score: float | None = None
    counterexample: dict[str, Any] | None = None
    feedback: str | None = Field(default=None, max_length=2000)
    # 检查器标识与耗时：出问题时能说清"是谁判的、花了多久"
    checker: str = Field(default="sympy", min_length=1, max_length=40)
    elapsed_ms: int = Field(default=0, ge=0)

    @property
    def correct(self) -> bool | None:
        """三态投影：True/False/None（None = 判不出来）。不要用布尔糊过去。"""
        if self.status == "true":
            return True
        if self.status == "false":
            return False
        return None


def make_result(*, revision_id: UUID, exercise_version_id: UUID,
                status: CheckVerdict, reason: CheckReason,
                score: float | None = None, counterexample: dict[str, Any] | None = None,
                feedback: str | None = None, checker: str = "sympy",
                elapsed_ms: int = 0) -> CheckResult:
    if status == "fail":
        if score is not None:
            raise ValueError("fail 不能带分数：判不出来 ≠ 判错了")
    if status == "false" and counterexample is None:
        # 允许"没有反例的错误"，但必须显式选择：调用方要自己承担这个置信度。
        pass
    return CheckResult(
        revision_id=revision_id, exercise_version_id=exercise_version_id,
        status=status, reason=reason, score=score, counterexample=counterexample,
        feedback=feedback, checker=checker, elapsed_ms=elapsed_ms)


class PracticeFeedback(ContractModel):
    """发给学生的反馈：只有这些字段，答案正文不在此列。"""

    revision_id: UUID
    status: CheckVerdict
    reason: CheckReason
    message: str = Field(min_length=1, max_length=2000)
    hint: str | None = Field(default=None, max_length=1000)
    solution: str | None = Field(default=None, max_length=2000)
    counterexample: dict[str, Any] | None = None
    attempts_remaining: int | None = Field(default=None, ge=0)


POLICY_MESSAGE = {
    "true": "回答正确。",
    "false": "这一次不对，看看提示再试一次。",
    "fail": "系统暂时无法判定这道题，已记录，不影响你的成绩。",
}


def build_feedback(result: CheckResult, *, hint: str | None = None,
                   solution: str | None = None,
                   attempts_remaining: int | None = None,
                   answer_released: bool = False) -> PracticeFeedback:
    """组装练习反馈。

    `solution` 只在答案已发布时才透传（`answer_released=True`）；否则即使调用方
    传进来也会被丢掉——这是"解析不能顺手泄露"的那道门。
    """
    if result.status == "fail":
        message = POLICY_MESSAGE["fail"]
    elif result.status == "true":
        message = POLICY_MESSAGE["true"]
    else:
        message = POLICY_MESSAGE["false"]
    return PracticeFeedback(
        revision_id=result.revision_id, status=result.status, reason=result.reason,
        message=message, hint=hint, solution=solution if answer_released else None,
        counterexample=result.counterexample if result.status == "false" else None,
        attempts_remaining=attempts_remaining,
    )


# ---------------------------------------------------------------------------
# 策略矩阵（§12.1）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PracticePolicy:
    """练习与正式卷的区别，集中在一处，避免页面各自判断。"""

    purpose: Literal["practice", "coursework", "formal"]
    show_syntax_errors: bool = True
    show_math_interpretation: bool = True
    show_input_preview: bool = True
    correctness_release: Literal["immediate", "deferred", "manual"] = "immediate"
    score_release: Literal["immediate", "deferred", "manual"] = "immediate"
    hint_release: Literal["immediate", "after_attempt", "manual", "none"] = "immediate"

    def allows_immediate_feedback(self) -> bool:
        """只有"练习且立即释放"才能边做边看结果；正式卷永远不行。"""
        return self.purpose == "practice" and self.correctness_release == "immediate"

    def is_formal(self) -> bool:
        return self.purpose == "formal"


def decide_release(policy: PracticePolicy, *, attempt_index: int) -> dict[str, bool]:
    """现在能不能给学生看正确性、分数、提示。返回布尔字典，供 API/DTO 直接读。"""
    return {
        "correctness": policy.purpose == "practice"
        and policy.correctness_release == "immediate",
        "score": policy.purpose == "practice" and policy.score_release == "immediate",
        "hint": policy.hint_release == "immediate"
        or (policy.hint_release == "after_attempt" and attempt_index > 0),
        "solution": False,  # 解析永远要单独判定（答案是否已发布）
    }


__all__ = [
    "CheckReason",
    "CheckResult",
    "CheckStatus",
    "CheckVerdict",
    "POLICY_MESSAGE",
    "PracticeFeedback",
    "PracticePolicy",
    "build_feedback",
    "decide_release",
    "make_result",
]
