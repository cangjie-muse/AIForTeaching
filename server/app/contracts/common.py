"""阶段 A 共享契约的公共类型：精确分值、状态枚举、基类。

为什么单独造一个 `Score` 类型：计划书 §0.3 强制"所有分值在 Python 里用 Decimal、
数据库用 NUMERIC(8,2)、JSON 用两位小数字符串"。如果只在序列化时格式化，内部
一旦有人用 float 累加，100 分卷就会出现 99.99999999999999，而这种差异不会抛错，
只会在打印卷和成绩对账时突然出现。所以把规则前移到类型本身：三位小数、NaN、
Infinity 在构造模型时就被拒绝。

`ContractModel` 统一 `extra="forbid"`：契约是"只有这些字段"的声明，不是"至少
这些字段"。上游多传一个 `solution` 时应该当场报错，而不是被静默收下再传到学生卷。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer

# 与 db/migrations/0027_assessment_baseline.sql 的 CHECK 约束逐字对应。
# 只允许四态：退回修改是追加审核事实后回到 draft，不是第五种状态。
PaperStatus = Literal["draft", "in_review", "approved", "withdrawn"]
Purpose = Literal["formal", "coursework", "practice"]
PaperKind = Purpose
RenderKind = Literal["student", "answer", "rubric"]
AssemblyMode = Literal["manual", "constraint_assisted"]
ReviewPolicy = Literal["formal_dual_review", "single_review"]
ReviewDecision = Literal["approved", "rejected", "revoked"]
# 整卷校验的结论分级：只有 error 让卷子无效；warning 是可解释的"未知/偏离"，
# 不能与硬约束混成同一个绿色分数（规格 §8.1）。
ValidationSeverity = Literal["error", "warning"]
ArtifactVisibility = Literal["private", "class_shared", "course_published"]
ExposureEventKind = Literal[
    "secure_assigned",
    "student_opened",
    "practice_answer_released",
    "formal_answer_released",
    "withdrawn",
]

SCORE_PLACES = Decimal("0.01")


def validate_score(value: Decimal) -> Decimal:
    """分值必须是有限、最多两位小数的 Decimal。"""
    if not value.is_finite():
        raise ValueError(f"score must be a finite decimal, got {value!r}")
    if value != value.quantize(SCORE_PLACES):
        raise ValueError(f"score must have at most two decimal places, got {value!r}")
    return value


# PlainSerializer 保证 JSON 永远是两位小数字符串（40 → "40.00"），
# 而不是 JSON 数字——JS 端拿到数字就会用二进制浮点参与求和。
Score = Annotated[
    Decimal,
    AfterValidator(validate_score),
    PlainSerializer(lambda value: f"{value:.2f}", return_type=str),
]


class ContractModel(BaseModel):
    """所有跨层公共模型的基类：拒绝未知字段，构造后不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
