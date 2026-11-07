"""练习反馈的提示/解析策略与 Artifact 重检（阶段 D 任务 3）。

这一层回答两个问题，两者都不能靠"链接签名过了就放行"：

1. **学生这次能看到几级提示、能不能看解析？**
   —— 由策略（`max_hint_level` / `solution_enabled` / `solution_requires_valid_response`）
   与**当前 revision 的解析状态**共同决定。越级请求（hint=3 而上限 2）必须拒绝，
   而不是"给到 2 就算了"——前端会被这种宽容喂出错误的期待。

2. **这个已发出的旧链接还作数吗？**
   —— 每次打开都重新决策：权限、时间窗、反馈策略（可能已收窄）、
   以及**保密正式卷与练习解析的曝光冲突**。签名 URL 只是一张"通行证照片"，
   它证明不了"现在仍然允许"。

设计 §12.1/§14.2 的三条硬规则：

* `invalid`/`empty` 的回答不能解锁解析（`solution_requires_valid_response`）；
* 解析打开成功才写"练习解析已释放"的曝光事件；被拒绝的路径**不写**成功事件；
* 解析与保密正式卷冲突时（ExposureConflict），**不返回下载 URL**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID

from .contracts import PracticePolicy


class FeedbackError(ValueError):
    code = "feedback_error"


class PolicyDenied(FeedbackError):
    code = "feedback_denied"

    def __init__(self, message: str, *, deny_code: str = "feedback_denied") -> None:
        super().__init__(message)
        self.deny_code = deny_code


class ExposureConflict(FeedbackError):
    """练习解析与仍然保密/未公开的正式卷冲突：不能给学生看解析。"""

    code = "exposure_conflict"


@dataclass(frozen=True)
class FeedbackArtifact:
    """一份可展示的反馈产物（提示或解析）。

    `artifact_version_id` 指向已审核的产物版本；本服务**不生成**内容，
    只从固定题目的既有版本里挑——重新生成会让"已审核"这件事失效。
    """

    artifact_version_id: UUID
    level: int
    kind: str = "hint"           # hint | solution
    download_url: str | None = None
    contains_solution: bool = False


@dataclass(frozen=True)
class FeedbackSelection:
    hints: tuple[FeedbackArtifact, ...] = ()
    solution: FeedbackArtifact | None = None

    def levels(self) -> list[int]:
        return [item.level for item in self.hints]


@dataclass(frozen=True)
class CatalogEntry:
    """反馈目录里的一条：某一级提示或解析。`approved=False` 的不可见。"""

    level: int
    kind: str
    artifact_version_id: UUID
    approved: bool = True
    download_url: str | None = None


class FeedbackCatalog:
    """只读目录：按 (槽位, 级别, 是否看解析) 选出**已审核**的产物。"""

    def __init__(self, entries: Sequence[CatalogEntry] = ()) -> None:
        self.entries = tuple(entries)

    def select(self, *, max_level: int, reveal_solution: bool) -> FeedbackSelection:
        hints = tuple(
            FeedbackArtifact(artifact_version_id=entry.artifact_version_id,
                             level=entry.level, kind="hint",
                             download_url=entry.download_url)
            for entry in sorted(self.entries, key=lambda item: item.level)
            if entry.approved and entry.kind == "hint" and entry.level <= max_level
        )
        solution: FeedbackArtifact | None = None
        if reveal_solution:
            approved = [entry for entry in self.entries
                        if entry.approved and entry.kind == "solution"]
            if approved:
                entry = sorted(approved, key=lambda item: item.level)[-1]
                solution = FeedbackArtifact(
                    artifact_version_id=entry.artifact_version_id, level=entry.level,
                    kind="solution", download_url=entry.download_url,
                    contains_solution=True)
        return FeedbackSelection(hints=hints, solution=solution)


@dataclass(frozen=True)
class RevisionState:
    revision_id: UUID
    parse_status: str          # parsed | invalid | empty | unparsed
    revision_no: int


@dataclass(frozen=True)
class PracticePolicyState:
    """策略（数据库里的快照）在打开时的当前值——可能比发布时更窄。"""

    max_hint_level: int = 0
    solution_enabled: bool = False
    solution_requires_valid_response: bool = True
    version_id: UUID | None = None


@dataclass(frozen=True)
class ExposureRecord:
    """一条曝光事件：某个题目版本是否已对某份卷公开过。"""

    exercise_version_id: UUID
    released_practice: bool = False
    secure_formal_pending: bool = False


class ExposureService:
    """曝光裁决：练习解析不能与仍保密的正式卷冲突。"""

    def __init__(self, records: Sequence[ExposureRecord] = ()) -> None:
        self.records = tuple(records)

    def assert_solution_release(self, *, exercise_version_id: UUID) -> None:
        for record in self.records:
            if record.exercise_version_id != exercise_version_id:
                continue
            if record.secure_formal_pending and not record.released_practice:
                raise ExposureConflict(
                    f"exposure_conflict: 该题仍用于未公开的正式卷（{exercise_version_id}），"
                    "暂不能展示解析")
        return None

    def record_practice_answer_released(self, *, exercise_version_id: UUID) -> None:
        """记录"练习解析已释放"。只允许在真正打开解析时调用。"""
        if not any(record.exercise_version_id == exercise_version_id for record in self.records):
            self.records = (*self.records, ExposureRecord(
                exercise_version_id=exercise_version_id, released_practice=True))


class PolicyDecisionPoint:
    """打开产物的权限/时间/策略裁决（PDP）。

    这里只做**域内**判断：策略是否允许、产物是否属于该题目版本、
    以及"这条产物是否包含解析"。HTTP 鉴权在 API 层，不在这里重复。
    """

    def check_artifact(self, *, artifact_version_id: UUID,
                       allowed_versions: Sequence[UUID],
                       policy: PracticePolicyState,
                       revision: RevisionState) -> FeedbackArtifact | None:
        if artifact_version_id not in allowed_versions:
            raise PolicyDenied("artifact_not_for_this_exercise", deny_code="artifact_mismatch")
        if not policy.solution_enabled:
            raise PolicyDenied("这份练习没有开放解析", deny_code="solution_disabled")
        if policy.solution_requires_valid_response and revision.parse_status != "parsed":
            raise PolicyDenied(
                "回答无法解析时不能解锁解析（先修正答案）",
                deny_code="solution_requires_valid_response")
        return None


@dataclass(frozen=True)
class ArtifactOpenResult:
    artifact_version_id: UUID
    download_url: str
    response_revision_id: UUID
    revision_no: int
    latest_revision_no: int
    superseded: bool


@dataclass(frozen=True)
class CheckFeedbackRequest:
    revision: RevisionState
    requested_hint_level: int = 0
    reveal_solution: bool = False


class FeedbackService:
    def __init__(
        self,
        *,
        catalog: FeedbackCatalog,
        exposure: ExposureService | None = None,
        policy_decision: PolicyDecisionPoint | None = None,
    ) -> None:
        self.catalog = catalog
        self.exposure = exposure or ExposureService()
        self.policy_decision = policy_decision or PolicyDecisionPoint()

    # -- 检查结果附带的反馈 ---------------------------------------------
    def for_check(self, request: CheckFeedbackRequest,
                  policy: PracticePolicyState) -> FeedbackSelection:
        if not 0 <= request.requested_hint_level <= policy.max_hint_level:
            raise PolicyDenied(
                f"提示级别 {request.requested_hint_level} 超出上限 "
                f"{policy.max_hint_level}", deny_code="hint_level_exceeded")
        if request.reveal_solution:
            if not policy.solution_enabled:
                raise PolicyDenied("这份练习没有开放解析", deny_code="solution_disabled")
            if (policy.solution_requires_valid_response
                    and request.revision.parse_status != "parsed"):
                raise PolicyDenied(
                    "回答无法解析时不能解锁解析",
                    deny_code="solution_requires_valid_response")
        return self.catalog.select(
            max_level=request.requested_hint_level,
            reveal_solution=request.reveal_solution)

    # -- 打开旧产物（链接续期/下载都走这里） ------------------------------
    def open_artifact(self, *, artifact_version_id: UUID,
                      allowed_versions: Sequence[UUID],
                      policy: PracticePolicyState,
                      revision: RevisionState,
                      latest_revision_no: int,
                      contains_solution: bool,
                      artifact: FeedbackArtifact | None = None) -> ArtifactOpenResult:
        self.policy_decision.check_artifact(
            artifact_version_id=artifact_version_id, allowed_versions=allowed_versions,
            policy=policy, revision=revision)
        if contains_solution:
            url = artifact.download_url if artifact else None
            if not url:
                raise PolicyDenied("这份产物没有可用的下载地址",
                                   deny_code="artifact_unavailable")
            # 曝光重检必须在返回 URL 之前：冲突时不给出任何地址。
            self.exposure.assert_solution_release(
                exercise_version_id=artifact_version_id)
            self.exposure.record_practice_answer_released(
                exercise_version_id=artifact_version_id)
        else:
            url = artifact.download_url if artifact else None
            if not url:
                raise PolicyDenied("这份产物没有可用的下载地址",
                                   deny_code="artifact_unavailable")
        return ArtifactOpenResult(
            artifact_version_id=artifact_version_id, download_url=url,
            response_revision_id=revision.revision_id,
            revision_no=revision.revision_no,
            latest_revision_no=latest_revision_no,
            superseded=latest_revision_no > revision.revision_no)


__all__ = [
    "ArtifactOpenResult",
    "CatalogEntry",
    "CheckFeedbackRequest",
    "ExposureConflict",
    "ExposureRecord",
    "ExposureService",
    "FeedbackArtifact",
    "FeedbackCatalog",
    "FeedbackError",
    "FeedbackSelection",
    "FeedbackService",
    "PolicyDecisionPoint",
    "PolicyDenied",
    "PracticePolicyState",
    "RevisionState",
]
