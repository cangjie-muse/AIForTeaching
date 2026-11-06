"""候选 manifest：把阶段 A 已审核的候选固化成"带哈希、可回放"的求解器输入。

这一层要解决的是**口径**问题，不是再筛一遍题：

* 阶段 A 的 `CandidateService` 已经在固定的 Release Context 上判过审核、许可、
  撤回、内容漂移、曝光冲突；它的结论是权威的，本模块不重复判定，只做翻译。
* 但求解器还需要几个阶段 A 记录里没有的量：这题最近被用了几次、曝光过几次、
  答案是否已经公开。这些量**必须从真实的 `item_exposure_event` 统计**，不能让
  调用方自报——否则教师只要在请求里写 `exposure_count=0`，曝光软目标就形同虚设。

于是分工是：

    StageACandidateSource   取权威记录 + 真实曝光事件（授权、Release、审核都在阶段 A）
    AssemblyCandidateService 逐题再跑一次曝光判定，翻译成 Candidate，求哈希

逐题再判一次不是多余：阶段 A 的曝光判定发生在它的快照时刻，而本 manifest 是
**另一次**固定。中间几分钟里可能有班级发布了答案卷；这一遍复核让"这一份 manifest"
本身站得住，而不是靠"上游应该判过了"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID

from ..contracts.candidate import CandidateRecord, CandidateScope, RejectedCandidate
from ..contracts.paper import CandidateEvidence, ExposureEvent
from ..exposure import ExposureConflict, ExposureService
from ..releases import utcnow
from .contracts import (
    BlueprintSnapshot,
    Candidate,
    CandidateManifest,
    ConceptWeight,
    ManifestExclusion,
    make_candidate_manifest,
)

# 与 server/app/exposure.py 的事件分类保持一致：这里是"统计用途"，那边是"判定用途"。
SECURE_EVENT_KINDS = frozenset({"secure_assigned", "student_opened"})
ANSWER_RELEASED_EVENT_KINDS = frozenset({"practice_answer_released", "formal_answer_released"})

DIFFICULTY_PREFIX = "difficulty-"


class CandidateSnapshotError(ValueError):
    """候选无法固化成 manifest（缺哈希、缺时长、范围不符等）。"""


@dataclass(frozen=True)
class CandidateSourceResult:
    """一次取数的结果：权威记录 + 真实曝光事件 + 上游快照身份。

    `candidate_snapshot_id` 是阶段 A 的 `assessment.candidate_snapshot` 行 id。
    建卷时 `paper_version.candidate_snapshot_id` 是 NOT NULL，所以这个 id 必须从
    一开始就带着——等到落卷那一步再去补，就只能伪造一个。
    """

    release_context_id: UUID
    accepted: tuple[CandidateRecord, ...]
    rejected: tuple[RejectedCandidate, ...]
    exposure_events: tuple[Any, ...]
    as_of: datetime
    candidate_snapshot_id: UUID | None = None


def _visible(events: Iterable[Any], as_of: datetime) -> list[Any]:
    """只有 `visible_from <= as_of` 的事件算"已经发生"（与阶段 A 同口径）。"""
    return [event for event in events if event.visible_from <= as_of]


def _difficulty(label: str | None) -> int | None:
    if not label:
        return None
    text = str(label).strip()
    if not text.startswith(DIFFICULTY_PREFIX):
        return None
    try:
        return int(text[len(DIFFICULTY_PREFIX):])
    except ValueError:
        return None


def _decision_seq(evidence: CandidateEvidence) -> int:
    """取该题最大的决策序号：同一道题可能被多次审核，最新一次代表当前结论。"""
    return max((int(value) for value in (evidence.decision_seqs or {}).values()), default=0)


def _concept_weights(evidence: CandidateEvidence) -> tuple[ConceptWeight, ...]:
    """阶段 A 只有"这道题覆盖哪些知识点"，没有权重真源。

    所以一律记 `primary/1`：覆盖硬约束因此等价于"这些知识点至少各出现一次"。
    不虚构权重，也不给 secondary 这种无法解释的角色。
    """
    return tuple(
        ConceptWeight(concept_id=concept_id, role="primary", weight=1)
        for concept_id in evidence.concept_ids
    )


class AssemblyCandidateService:
    """把一次取数结果固定成 `CandidateManifest`。"""

    def __init__(self, *, source: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self.source = source
        self._now = clock

    def snapshot(self, *, actor_id: UUID, blueprint: BlueprintSnapshot) -> CandidateManifest:
        """固化候选清单；同一输入必然得到同一 `manifest_hash`。

        授权不在这里做：`source` 走的是阶段 A 的服务，它用真实的 `course_ref`
        判定课程范围。本方法不接受调用方自报的 purpose/offering——它们只能来自
        蓝图，否则换个 id 就能绕开"这门课的候选范围"。
        """
        result = self.source.load(actor_id=actor_id, blueprint=blueprint)
        if result.release_context_id != blueprint.release_context_id:
            raise CandidateSnapshotError(
                "候选来源的 Release Context 与蓝图不一致："
                f"{result.release_context_id} != {blueprint.release_context_id}"
            )
        requires_seconds = self._requires_seconds(blueprint)
        events = _visible(result.exposure_events, result.as_of)
        accepted: list[Candidate] = []
        exclusions: list[ManifestExclusion] = [
            ManifestExclusion(
                exercise_version_id=item.exercise_version_id,
                reason=reason,
            )
            for item in result.rejected
            for reason in item.reasons
            if reason == "exposure_conflict"
        ]
        for record in result.accepted:
            candidate = self._candidate(
                record, events, result.as_of, blueprint, requires_seconds, exclusions)
            if candidate is not None:
                accepted.append(candidate)
        return make_candidate_manifest(
            blueprint, accepted, exclusions,
            candidate_snapshot_id=result.candidate_snapshot_id,
        )

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _requires_seconds(blueprint: BlueprintSnapshot) -> bool:
        return (
            blueprint.hard_constraints.max_duration_seconds is not None
            or blueprint.soft_objectives.target_duration_seconds is not None
            or blueprint.assembly_mode == "ab"
        )

    def _candidate(self, record: CandidateRecord, events: Sequence[Any], as_of: datetime,
                   blueprint: BlueprintSnapshot, requires_seconds: bool,
                   exclusions: list[ManifestExclusion]) -> Candidate | None:
        """翻译一条候选；这一遍复核发现曝光冲突时返回 None 并记下原因。"""
        evidence = record.evidence
        if not evidence.content_hash or not evidence.dependency_manifest_hash:
            raise CandidateSnapshotError(
                f"候选缺少内容哈希或依赖哈希：{evidence.exercise_version_id}"
            )
        if requires_seconds and evidence.estimated_seconds is None:
            raise CandidateSnapshotError(
                "启用时长约束（或 A/B）时每个候选都必须有 estimated_seconds："
                f"{evidence.exercise_version_id}"
            )
        try:
            ExposureService.assert_no_conflict(
                blueprint.purpose, blueprint.offering_id, (evidence,), events, as_of)
        except ExposureConflict:
            # 阶段 A 判过没问题，这一遍却冲突了：说明取数之后情况变了。
            # 排除它并留下原因，而不是让整次求解带着一个已经泄了的题。
            exclusions.append(ManifestExclusion(evidence.exercise_version_id, "exposure_conflict"))
            return None
        family_events = [
            event for event in events
            if event.exercise_family_id == evidence.exercise_family_id
        ]
        return Candidate(
            exercise_version_id=evidence.exercise_version_id,
            stable_exercise_id=evidence.exercise_id,
            exercise_family_id=evidence.exercise_family_id,
            source_release_id=evidence.source_release_id,
            decision_seq=_decision_seq(evidence),
            exercise_kind=evidence.exercise_kind,
            dependency_hash=evidence.dependency_manifest_hash,
            content_hash=evidence.content_hash,
            exercise_instance_id=evidence.exercise_instance_id,
            concept_weights=_concept_weights(evidence),
            learning_outcome_ids=tuple(evidence.learning_outcome_ids),
            difficulty=_difficulty(evidence.difficulty_label),
            cognitive_level=None,
            estimated_seconds=evidence.estimated_seconds,
            recent_use_count=sum(
                1 for event in family_events if event.event_kind in SECURE_EVENT_KINDS
            ),
            exposure_count=sum(
                1 for event in family_events
                if event.event_kind in ANSWER_RELEASED_EVENT_KINDS
            ),
            preferred=False,
            conflict_group_ids=(),
            answer_released=any(
                event.event_kind in ANSWER_RELEASED_EVENT_KINDS for event in family_events
            ),
            permitted_offering_ids=(),
            security_window_key=None,
        )


class StageACandidateSource:
    """用阶段 A 的真实服务取候选：授权、Release、审核、曝光都由它负责。

    `candidates` 是 `server.app.candidates.CandidateService`，`versions` 是
    `server.app.blueprints.BlueprintService`（只用它的 `get_version`）。
    这里**不**复制任何资格规则——复制出来的第二份规则迟早与第一份不一致。
    """

    def __init__(self, *, candidates: Any, versions: Any) -> None:
        self.candidates = candidates
        self.versions = versions

    def load(self, *, actor_id: UUID, blueprint: BlueprintSnapshot) -> CandidateSourceResult:
        version = self.versions.get_version(
            actor_id=actor_id, version_id=blueprint.blueprint_version_id)
        scope = CandidateScope.from_version(version)
        if scope.release_context_id != blueprint.release_context_id:
            raise CandidateSnapshotError(
                "蓝图版本的 Release Context 与快照不一致："
                f"{scope.release_context_id} != {blueprint.release_context_id}"
            )
        snapshot = self.candidates.snapshot(actor_id=actor_id, scope=scope)
        return CandidateSourceResult(
            release_context_id=scope.release_context_id,
            accepted=snapshot.accepted,
            rejected=snapshot.rejected,
            # 落库的 manifest 里事件是 JSON；这里还原成契约对象，让下游只有一种形状。
            exposure_events=tuple(
                ExposureEvent.model_validate(item)
                for item in snapshot.manifest.get("exposure_events", ())
            ),
            as_of=snapshot.created_at,
            candidate_snapshot_id=snapshot.id,
        )


__all__ = [
    "ANSWER_RELEASED_EVENT_KINDS",
    "AssemblyCandidateService",
    "CandidateSnapshotError",
    "CandidateSourceResult",
    "SECURE_EVENT_KINDS",
    "StageACandidateSource",
]
