"""学生卷投影：唯一允许跨到学生侧的数据形状。

规格 §14.1 的白名单只有：题号、题干、公开资产、允许输入类型、作答区域、说明与
版本短码。答案、解析、量规、answer_test、隐藏批注必须在数据组装之前就被排除，
不能"先生成再用 CSS 隐藏"——PDF 隐藏层、SSE、预取和错误堆栈都会把隐藏字段带出去。

这里用两道锁：
  1. 模型字段逐个写死 + `extra="forbid"`：多传一个 `solution=` 当场报错；
  2. `assert_student_safe` 递归扫描：题干 AST 和公开资产是自由字典，字段锁管不到
     它们内部，所以在模型构造后（以及任何手工组装 payload 的地方）再扫一遍键名。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from .common import ContractModel


class StudentProjectionLeakError(ValueError):
    """学生投影里出现答案/解析/量规类字段。"""


# 归一化后的禁用键名。只做"整键相等"判断，不做子串匹配：
# answer_space_lines 是合法的作答区域字段，不能被 "answer" 误伤。
STUDENT_FORBIDDEN_FIELDS = frozenset({
    "answer",
    "answers",
    "answer_key",
    "answer_md",
    "answer_test",
    "answer_tests",
    "answer_analysis",
    "official_answer",
    "reference_answer",
    "solution",
    "solutions",
    "solution_md",
    "reference_solution",
    "model_answer",
    "rubric",
    "rubrics",
    "rubric_json",
    "hidden_note",
    "hidden_notes",
    "explanation",
    "explanations",
})

_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")


def normalize_field_name(name: object) -> str:
    return _KEY_SEPARATOR.sub("_", str(name).lower()).strip("_")


def assert_student_safe(payload: Any, *, path: str = "") -> None:
    """递归确认 payload 中没有任何学生投影禁用字段。

    递归是必须的：`statement_ast` 是题库 AST 的透传字典，`public_assets` 来自资产
    表，两者都可能在某个版本里多出 `answer_key` 之类的键。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = normalize_field_name(key)
            here = f"{path}.{key}" if path else str(key)
            if normalized in STUDENT_FORBIDDEN_FIELDS:
                raise StudentProjectionLeakError(f"学生投影不得包含字段：{here}")
            assert_student_safe(value, path=here)
    elif isinstance(payload, (list, tuple, set, frozenset)):
        for index, item in enumerate(payload):
            assert_student_safe(item, path=f"{path}[{index}]")


class PublicAsset(ContractModel):
    """学生卷里允许出现的公开资产（图片、附加材料）。

    字段写死而不是用 `Mapping[str, str]`：自由字典等于给答案留了一个洞。
    """

    asset_id: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    mime_type: str = Field(min_length=1, max_length=120)
    alt_text: str = ""


class StudentSlotProjection(ContractModel):
    """学生卷的一道题：只有题号、题干、公开资产、作答方式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: UUID
    question_no: str = Field(min_length=1, max_length=32)
    statement_ast: dict[str, object]
    public_assets: tuple[PublicAsset, ...] = ()
    response_kind: str = Field(min_length=1, max_length=64)
    answer_space_lines: int = Field(ge=0, le=200)

    @model_validator(mode="after")
    def _reject_answer_material(self) -> "StudentSlotProjection":
        assert_student_safe(self.model_dump())
        return self


class StudentPaperProjection(ContractModel):
    """整份学生卷：版本短码 + 说明 + 槽位，不含任何答案侧数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_version_id: UUID
    version_short_code: str = Field(min_length=1, max_length=12)
    course_title: str = Field(min_length=1, max_length=200)
    instructions: str = ""
    slots: tuple[StudentSlotProjection, ...]

    @model_validator(mode="after")
    def _reject_answer_material(self) -> "StudentPaperProjection":
        assert_student_safe(self.model_dump())
        return self
