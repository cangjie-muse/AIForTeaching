"""候选资格与候选快照契约：拒绝原因码、候选记录、快照视图、候选范围。

为什么"拒绝原因"必须是枚举而不是布尔：教师看到的界面是「这道题不能进卷」，
但他需要知道的是**改哪一项**——是重新审核、补许可、补答案，还是重新冻结 Release。
一个 `eligible=False` 只能让人去翻日志；一个自由文本 `message` 又会在前端分不了组、
在测试里没法断言。所以这里把原因码钉成 `Literal`，运行时由 Pydantic 拒绝拼错的码。

五个"保留位"原因码（`hidden_evaluation / private_draft / missing_approved_instance /
dependencies_incomplete / purpose_not_allowed`）说明：规格 §6.2 要求这些维度也要拦，
但本仓库没有对应的数据真源（没有隐藏评测集标记、没有独立的学生草稿可见性、
没有 exercise_instance 表、没有 answer_test/资产依赖表、没有按用途区分可用性的表）。
契约层照样留门——一旦上游真的给出这些标志，候选不会因为"本仓库没这个概念"就漏过去；
生产 source 目前把它们算成"不存在"（见 server/app/candidates.py）。

与计划书的差异：计划书把这些类型放在 `modules/assessment/models/exposure.py`，
本仓库的契约层是 `server/app/contracts/`（任务 1 已定），这里按同一分层落地。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from .common import ContractModel, Purpose
from .paper import CandidateEvidence

# 拒绝原因码：每一个都必须能回答"教师该改什么"。
CandidateReason = Literal[
    # —— 固定 Release Context（硬门槛）——
    "outside_release_context",
    "exercise_missing",
    "content_drift",
    "release_withdrawn",
    "release_not_frozen",
    "scope_mismatch",
    # —— 审核与许可（硬门槛）——
    "review_not_approved",
    "revoked",
    "answer_missing",
    "license_missing",
    "license_invalid",
    # —— 渲染与语言（硬门槛）——
    "language_pair_invalid",
    "render_not_ready",
    # —— 曝光（硬门槛）——
    "exposure_conflict",
    # —— 保留位：本仓库暂无数据真源，见模块 docstring ——
    "hidden_evaluation",
    "private_draft",
    "missing_approved_instance",
    "dependencies_incomplete",
    "purpose_not_allowed",
]

# 原因码 → （含义，触发条件）。同一份文字供 API 文档、前端提示与测试断言使用，
# 避免"码在服务里改了、界面上的解释没跟上"。
REASON_MEANINGS: dict[str, tuple[str, str]] = {
    "outside_release_context": (
        "不在固定 Release Context 内",
        "沿 base 链找不到该题的有效 release_item，或条目被显式排除（state=excluded）",
    ),
    "exercise_missing": (
        "题目已不存在",
        "release_item 指向的 public.exercise 行已被删除（本题只剩 Release 残留）",
    ),
    "content_drift": (
        "Release 固定的版本已被原地编辑",
        "release_item 固定的内容哈希 ≠ 当前题干+答案的内容哈希；需重新冻结 Release",
    ),
    "release_withdrawn": (
        "Release 条目已撤回",
        "release_item.state = 'withdrawn'（严重撤回，任何用途都不得再进卷）",
    ),
    "release_not_frozen": (
        "正式卷引用了未冻结的 Release Context",
        "purpose = formal 且 release_context.status ≠ 'frozen'；未冻结意味着内容还可能变",
    ),
    "scope_mismatch": (
        "Release Context 与蓝图的课程/班级不一致",
        "release_context 的 course/offering 与蓝图版本声明的范围不同（跨课程取题）",
    ),
    "review_not_approved": (
        "审核未到有效终态",
        "review_status ∈ {draft, machine-checked}，或没有任何人工终态决策记录",
    ),
    "revoked": (
        "审核结论为撤回",
        "review_status = 'retired'，或最新决策值为 revoked",
    ),
    "answer_missing": (
        "缺答案/解析",
        "exercise.answer_md 为空或只有空白；没有答案的题不能进任何卷",
    ),
    "license_missing": (
        "没有许可声明",
        "exercise.license 为空",
    ),
    "license_invalid": (
        "许可不允许进卷",
        "license 命中拒绝集合（unknown / unlicensed / none / denied / all-rights-reserved / proprietary）",
    ),
    "language_pair_invalid": (
        "语言配对无效",
        "exercise.language 为空或不在受支持语言码内，或与蓝图要求的语言不一致",
    ),
    "render_not_ready": (
        "题干无法渲染",
        "题干为空或只有空白；打印卷要求题干可渲染",
    ),
    "exposure_conflict": (
        "曝光冲突",
        "同族题已公开练习/正式答案而本卷要求保密，或其他班级的保密窗口与本卷重叠，或该族已撤回",
    ),
    "hidden_evaluation": (
        "属于隐藏评测集（保留位）",
        "上游标记 hidden_evaluation=True；本仓库暂无该标记的写入来源",
    ),
    "private_draft": (
        "学生/教师私人草稿（保留位）",
        "上游标记 private_draft=True；本仓库暂无独立草稿可见性概念",
    ),
    "missing_approved_instance": (
        "参数化题缺已审核实例（保留位）",
        "parameterized=True 且没有已审核的 exercise_instance_id；本仓库无实例表",
    ),
    "dependencies_incomplete": (
        "依赖不完整（保留位）",
        "answer_test / 渲染资产 / 数学约定 / 依赖版本的完整性标记为 False；本仓库无依赖表",
    ),
    "purpose_not_allowed": (
        "用途不允许（保留位）",
        "上游标记 purpose_allowed=False；本仓库暂无按用途区分题目可用性的数据",
    ),
}


class CandidateScope(ContractModel):
    """一次候选快照的作用域。

    `purpose / course / offering / release_context` 全部来自**蓝图版本**，不是请求体：
    调用方能自己选 Release Context 的话，绕过一个门禁只需要换个 id。
    """

    blueprint_version_id: UUID
    purpose: Purpose
    course_id: UUID
    course_ref: str = Field(min_length=1, max_length=200)
    offering_id: UUID | None = None
    offering_ref: str | None = Field(default=None, max_length=200)
    release_context_id: UUID

    @classmethod
    def from_version(cls, version: Any) -> "CandidateScope":
        """从 `BlueprintVersionRow` 派生作用域；缺 course_ref 直接拒绝。

        为什么不回退到"用 uuid 授权"：课程范围授权的真源是文本课程号
        （`teaching_class.course_id`），没有它就无法判断这个教师是否教这门课。
        """
        course_ref = getattr(version, "course_ref", None)
        if not course_ref:
            raise ValueError(
                "蓝图版本缺少 course_ref（文本课程号），无法做课程范围授权"
            )
        return cls(
            blueprint_version_id=version.id,
            purpose=version.purpose,
            course_id=version.course_id,
            course_ref=course_ref,
            offering_id=version.offering_id,
            offering_ref=getattr(version, "offering_ref", None),
            release_context_id=version.release_context_id,
        )


class CandidateRecord(ContractModel):
    """一条候选：契约证据 + Release 成员关系 + 快照时刻的审核/许可原文。

    为什么额外带 `review_status / license / answer_present`：`CandidateEvidence`
    里只有布尔结论，而计划书 §6.2 要求候选清单保存"快照时刻的审核与许可状态"。
    只存布尔的话，半年后没人能解释"当时为什么判它合格"。
    """

    evidence: CandidateEvidence
    # included（有效）/ excluded（Release 里被排除）/ withdrawn（撤回）/
    # missing（题目行已删除）/ drifted（内容被原地编辑）
    release_state: Literal["included", "excluded", "withdrawn", "missing", "drifted"]
    review_status: str = Field(min_length=1, max_length=40)
    license: str | None = Field(default=None, max_length=200)
    answer_present: bool


class RejectedCandidate(ContractModel):
    """一条被拒绝的候选：身份 + 全部适用原因码（不是布尔、不是自由文本）。"""

    exercise_version_id: UUID
    exercise_id: UUID
    exercise_family_id: UUID
    review_status: str = Field(min_length=1, max_length=40)
    reasons: tuple[CandidateReason, ...] = Field(min_length=1)


class CandidateSnapshot(ContractModel):
    """`assessment.candidate_snapshot` 一行：不可变候选清单。

    `manifest` 是参与哈希的规范结构，`exclude=True` 表示**不进 API 响应**——
    响应只需要 accepted / rejected / 哈希，manifest 是审计与幂等用的内部形态。
    """

    id: UUID
    blueprint_version_id: UUID
    release_context_id: UUID
    purpose: Purpose
    course_id: UUID
    offering_id: UUID | None = None
    accepted: tuple[CandidateRecord, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()
    candidate_manifest_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
    created: bool = False
    manifest: dict[str, Any] = Field(default_factory=dict, exclude=True)


__all__ = [
    "CandidateReason",
    "REASON_MEANINGS",
    "CandidateScope",
    "CandidateRecord",
    "RejectedCandidate",
    "CandidateSnapshot",
]
