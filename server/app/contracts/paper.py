"""固定卷域契约：蓝图、候选证据、卷面视图、曝光事件与整卷复核证据。

这些模型对应 docs/superpowers/specs/2026-11-04-paper-assembly-and-online-attempts-design.md
§10.1—§10.11，以及计划书 §2「共享契约」。任务 2 之后的服务层、路由层、渲染层都从
这里 import 同一份定义，禁止在各自文件里重新定义近似结构——否则同名字段会在某处
多一个、某处少一个，最后表现为"审核通过的卷子和打印出来的卷子不是同一份"。

字段语义要点（与数据库 0027 迁移一一对应）：
  * 所有 UUID 在 JSON 中是字符串，由 Pydantic 自动完成；
  * 所有分值走 common.Score，JSON 是两位小数字符串；
  * `decision_seqs` 的键是"依赖类型 + 版本 ID"，值是审核序号；
  * `latest_decisions` 是该键对应的最新终态，发布时重新核对。

本文件的模型全部继承 `common.ContractModel`，其 `model_config` 固定为
`ConfigDict(extra="forbid", frozen=True)`——这里不再逐个重复声明。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import (
    AssemblyMode,
    ContractModel,
    ExposureEventKind,
    PaperKind,
    PaperStatus,
    Purpose,
    ReviewDecision,
    ReviewPolicy,
    Score,
)


class BlueprintSlot(ContractModel):
    """蓝图里的一个槽位（一道题的位置），不含具体题目。"""

    code: str = Field(min_length=1, max_length=64)
    score: Score = Field(ge=0)
    exercise_kinds: tuple[str, ...] = ()
    required_concept_ids: tuple[UUID, ...] = ()
    learning_outcome_ids: tuple[UUID, ...] = ()
    difficulty_label: str | None = None
    estimated_seconds: int | None = Field(default=None, ge=0)
    mandatory_exercise_version_ids: tuple[UUID, ...] = ()
    excluded_exercise_version_ids: tuple[UUID, ...] = ()


class BlueprintSection(ContractModel):
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    instruction: str = ""
    slots: tuple[BlueprintSlot, ...]

    @property
    def section_score(self) -> Decimal:
        """分节分 = 槽位分之和；用 Decimal 求和，不走 float。"""
        return sum((slot.score for slot in self.slots), Decimal("0"))


class BlueprintContent(ContractModel):
    """一个不可变蓝图版本的全部内容，哈希后即 manifest 的输入。"""

    schema_version: Literal["paper-blueprint/v1"]
    purpose: Purpose
    course_id: UUID
    offering_id: UUID | None = None
    release_context_id: UUID
    total_score: Score = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    sections: tuple[BlueprintSection, ...]
    hard_constraints: dict[str, object] = {}
    soft_objectives: dict[str, object] = {}
    review_policy: ReviewPolicy
    assembly_mode: AssemblyMode = "manual"


class CandidateEvidence(ContractModel):
    """候选快照里的一条题目证据：审查、许可、撤回、渲染就绪等判定结果。"""

    release_context_id: UUID
    source_release_id: UUID
    exercise_id: UUID
    exercise_version_id: UUID
    exercise_instance_id: UUID | None = None
    exercise_family_id: UUID
    exercise_kind: str
    concept_ids: tuple[UUID, ...] = ()
    learning_outcome_ids: tuple[UUID, ...] = ()
    difficulty_label: str | None = None
    estimated_seconds: int | None = Field(default=None, ge=0)
    decision_seqs: dict[str, int] = {}
    latest_decisions: dict[str, str] = {}
    dependency_manifest_hash: str
    content_hash: str
    in_context: bool
    license_valid: bool
    revoked: bool
    hidden_evaluation: bool
    private_draft: bool
    parameterized: bool
    instance_approved: bool
    dependencies_complete: bool
    render_ready: bool
    language_pair_valid: bool
    purpose_allowed: bool
    cloud_allowed: bool


class SlotSelection(ContractModel):
    """一次手工选卷里"槽位 → 题目版本"的固定决定。"""

    slot_code: str = Field(min_length=1, max_length=64)
    exercise_version_id: UUID


class PaperSlotView(ContractModel):
    """已固定卷面上的一个槽位；只读写入数据库的权威字段。

    `release_context_id` 与 `content_hash` 是任务 4 追加的可选字段（只做加法）：
      * 前者让"槽位固定来源"落到槽位本身，而不是只能从整卷推断——打印作业项
        （assignment_item）投影的来源字段就是这四个 + 实例；
      * 后者是槽位与依赖的完整内容哈希（0027 的 `paper_slot.content_hash` 是
        NOT NULL，契约里不给出去，前端与回放就看不到槽位级完整性判据）。
    空槽位（draft 允许的缺口）这四个来源字段全为 None，绝不出现"只有题号"的槽位。
    """

    id: UUID
    section_code: str
    code: str
    slot_no: int = Field(ge=1)
    exercise_id: UUID | None = None
    exercise_version_id: UUID | None = None
    exercise_instance_id: UUID | None = None
    exercise_family_id: UUID | None = None
    source_release_id: UUID | None = None
    release_context_id: UUID | None = None
    score: Score = Field(ge=0)
    estimated_seconds: int | None = Field(default=None, ge=0)
    display_config: dict[str, object] = {}
    dependency_manifest_hash: str | None = None
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class PaperSectionView(ContractModel):
    code: str
    title: str
    instruction: str = ""
    sort_order: int = Field(ge=0)
    section_score: Score = Field(ge=0)
    slots: tuple[PaperSlotView, ...]
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class PaperView(ContractModel):
    """整卷视图。status 只有四态；approved 后内容与顺序不可 UPDATE。"""

    id: UUID
    blueprint_version_id: UUID
    release_context_id: UUID
    course_id: UUID
    offering_id: UUID | None = None
    purpose: Purpose
    paper_kind: PaperKind | None = None
    status: PaperStatus
    total_score: Score = Field(ge=0)
    duration_seconds: int = Field(gt=0)
    edit_seq: int = Field(ge=1)
    manifest_hash: str
    review_manifest_hash: str | None = None
    sections: tuple[PaperSectionView, ...] = ()


class ValidationIssue(ContractModel):
    """整卷独立校验的一条结论；slot_id 为空表示卷级问题。"""

    code: str
    message: str
    slot_id: UUID | None = None


class ExposureEvent(ContractModel):
    """题目曝光事件：练习放答案、正式卷保密使用之间的冲突判据。"""

    exercise_version_id: UUID
    exercise_instance_id: UUID | None = None
    exercise_family_id: UUID
    scope_offering_id: UUID | None = None
    event_kind: ExposureEventKind
    visible_from: datetime
    occurred_at: datetime


class ReviewEvidence(ContractModel):
    """一名复核者的整卷决定，聚合进 review_manifest_hash。

    `human` / `authorized` / `contributor` 三个布尔必须分别成立，不能用一个
    `approved=True` 代替：正式卷要求两名不同、有课程范围权限、且无内容贡献的人。
    """

    reviewer_user_id: UUID
    decision_seq: int = Field(ge=1)
    manifest_hash: str
    decision: ReviewDecision
    human: bool
    authorized: bool
    contributor: bool
