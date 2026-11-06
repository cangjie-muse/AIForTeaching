"""阶段 A 共享契约（唯一 import 入口）。

用法：
    from app.contracts import PaperView, StudentPaperProjection, canonical_hash, Score

分域文件：
    common.py   —— 精确分值类型、四态状态枚举、ContractModel 基类
    hashing.py  —— canonical JSON + SHA-256
    paper.py    —— 蓝图、候选证据、卷面视图、曝光事件、复核证据
    student.py  —— 学生投影白名单与泄露扫描
    artifact.py —— 通用 Artifact 的元数据、版本与访问事件形状
    render.py   —— paper_render 行、渲染请求与卷种状态（任务 6）

约定（与计划书 §2 对齐，按本仓库技术栈改写）：
    * 计划书写的是 frozen dataclass，这里换成 Pydantic 2 模型，便于路由层直接复用
      做请求/响应校验，并统一 extra="forbid"；
    * 所有分值字段是 Decimal（common.Score），JSON 序列化为两位小数字符串；
    * 学生投影模型不得出现 answer / solution / rubric / answer_test / hidden_note。
"""

from __future__ import annotations

from .artifact import ArtifactAccessEvent, ArtifactMetadata, ArtifactVersion
from .common import (
    SCORE_PLACES,
    AssemblyMode,
    ArtifactVisibility,
    ContractModel,
    ExposureEventKind,
    PaperKind,
    PaperStatus,
    Purpose,
    RenderKind,
    ReviewDecision,
    ReviewPolicy,
    Score,
    ValidationSeverity,
    validate_score,
)
from .hashing import canonical_hash, canonical_json, normalize
from .paper import (
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    CandidateEvidence,
    ExposureEvent,
    PaperSectionView,
    PaperSlotView,
    PaperView,
    ReviewEvidence,
    SlotSelection,
    ValidationIssue,
    ValidationResult,
)
from .student import (
    STUDENT_FORBIDDEN_FIELDS,
    PublicAsset,
    StudentPaperProjection,
    StudentProjectionLeakError,
    StudentSlotProjection,
    assert_student_safe,
    normalize_field_name,
)
from .render import PaperRenderRecord, RenderKindStatus, RenderRequest
from .candidate import (
    REASON_MEANINGS,
    CandidateReason,
    CandidateRecord,
    CandidateScope,
    CandidateSnapshot,
    RejectedCandidate,
)

__all__ = [
    # common
    "SCORE_PLACES",
    "AssemblyMode",
    "ArtifactVisibility",
    "ContractModel",
    "ExposureEventKind",
    "PaperKind",
    "PaperStatus",
    "Purpose",
    "RenderKind",
    "ReviewDecision",
    "ReviewPolicy",
    "Score",
    "ValidationSeverity",
    "validate_score",
    # hashing
    "canonical_hash",
    "canonical_json",
    "normalize",
    # paper
    "BlueprintContent",
    "BlueprintSection",
    "BlueprintSlot",
    "CandidateEvidence",
    "ExposureEvent",
    "PaperSectionView",
    "PaperSlotView",
    "PaperView",
    "ReviewEvidence",
    "SlotSelection",
    "ValidationIssue",
    "ValidationResult",
    # student
    "STUDENT_FORBIDDEN_FIELDS",
    "PublicAsset",
    "StudentPaperProjection",
    "StudentProjectionLeakError",
    "StudentSlotProjection",
    "assert_student_safe",
    "normalize_field_name",
    # artifact
    "ArtifactAccessEvent",
    "ArtifactMetadata",
    "ArtifactVersion",
    # render（任务 6）
    "PaperRenderRecord",
    "RenderKindStatus",
    "RenderRequest",
    # candidate（任务 3）
    "REASON_MEANINGS",
    "CandidateReason",
    "CandidateRecord",
    "CandidateScope",
    "CandidateSnapshot",
    "RejectedCandidate",
]
