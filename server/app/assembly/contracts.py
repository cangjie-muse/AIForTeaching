"""阶段 B 共享契约：求解状态、整数单位模型、候选与方案 manifest。

与计划书的差异（都在同一分层内，不是改名换壳）：
  * 计划的 `modules/assessment/assembly/contracts.py` → 本仓库的
    `server/app/assembly/contracts.py`（阶段 A 的契约层在 `server/app/contracts/`）；
  * 计划用自己的 `canonical_hash`，这里复用 `server/app/contracts/hashing.py`——
    哈希算法只能有一份，否则"候选清单没变"这句话在两个模块里会给出两个答案；
  * 计划的 `SlotSelection` 在本仓库与阶段 A 的 `contracts.paper.SlotSelection`
    重名（那个是手工选卷的 `{slot_code, exercise_version_id}`），这里叫
    `AssemblySelection`，字段更全（含来源与哈希）。

三条不可谈判的语义，写进类型里而不是写进注释里：

1. **整数单位。** 分值进模型前用 `Decimal` 精确转换成"分/100"（`1.50` → `150`），
   时长用整数秒，软目标权重/偏差/罚分全是整数。任何地方出现 `float` 累加，
   100 分卷就会变成 99.99999999999999，而且不会抛错。
2. **可行 ≠ 最优。** `RunStatus.FEASIBLE` 与 `OPTIMAL` 是两种结论，
   `SolutionManifest.optimal_proven` 单独记一笔：备选方案用"排除前解"得到，
   它们继承的是"可行"，不继承"最优"。
3. **没有方案就没有卷子。** `INFEASIBLE / TIMEOUT(无解) / FAILED` 时
   `SolveResult.solution_sets` 为空，调用方据此拒绝创建 paper——不靠"记得判一下"。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid5

from ..contracts.hashing import canonical_hash, normalize

# 蓝图槽位目前没有自己的数据库行（0027 的 paper_slot 是"已定卷面"上的槽位），
# 求解要用一个稳定的槽位身份。用 uuid5 而不是 uuid4：同一 (蓝图版本, 分节, 槽位码)
# 永远得到同一个 id，这样两次运行的方案 manifest 才能逐字节比较、回放才成立。
SLOT_NAMESPACE = UUID("8f14e45f-ceea-567f-a1b2-3c4d5e6f7a80")


class RunStatus(StrEnum):
    """一次求解运行的状态。

    `QUEUED/RUNNING` 是运行中；`OPTIMAL/FEASIBLE/INFEASIBLE/TIMEOUT/FAILED/CANCELLED`
    是终态。终态一旦写入不再改（重跑是新的一行），回放读的是历史行。
    """

    QUEUED = "queued"
    RUNNING = "running"
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 有方案可用的终态：只有这两个能让教师挑选并生成卷子。
SOLVED_STATUSES = frozenset({RunStatus.OPTIMAL, RunStatus.FEASIBLE})


def score_to_hundredths(value: Decimal) -> int:
    """分值 → 整数"分/100"。`Decimal("1.50")` 是 150，不是 1.5。"""
    scaled = Decimal(value) * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"score must have at most two decimal places, got {value!r}")
    return int(scaled)


def slot_identity(blueprint_version_id: UUID, section_code: str, code: str) -> UUID:
    return uuid5(SLOT_NAMESPACE, f"paper-slot:{blueprint_version_id}/{section_code}/{code}")


# ---------------------------------------------------------------------------
# 蓝图侧的输入
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConceptWeight:
    """一个候选对某个知识点的覆盖权重（角色区分 primary/secondary）。"""

    concept_id: UUID
    role: str
    weight: int


@dataclass(frozen=True)
class CoverageRequirement:
    """覆盖硬约束：某知识点/学习产出在整卷里的最小权重。"""

    target_id: UUID
    target_kind: str  # concept 或 learning_outcome
    role: str
    min_weight: int


@dataclass(frozen=True)
class KindRequirement:
    """题型硬约束：某分节（或全卷）内某种题型的数量区间。"""

    section_code: str | None
    exercise_kind: str
    min_count: int
    max_count: int | None = None


@dataclass(frozen=True)
class SlotSpec:
    """蓝图里的一个槽位：分值、预计时长、允许的题型与硬性固定。"""

    slot_id: UUID
    code: str
    section_code: str
    score: Decimal
    estimated_seconds: int
    allowed_kinds: tuple[str, ...] = ()
    required_concept_ids: tuple[UUID, ...] = ()
    learning_outcome_ids: tuple[UUID, ...] = ()
    difficulty: int | None = None
    mandatory_version_ids: tuple[UUID, ...] = ()
    excluded_version_ids: tuple[UUID, ...] = ()
    excluded_family_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class HardConstraints:
    section_scores: dict[str, Decimal] = field(default_factory=dict)
    coverage_requirements: tuple[CoverageRequirement, ...] = ()
    kind_requirements: tuple[KindRequirement, ...] = ()
    max_duration_seconds: int | None = None
    # 阶段 A 的蓝图把"互斥组"写在 hard_constraints.conflict_groups / leak_groups：
    # 同一组内最多进一道题（共用材料、互相泄答案）。这里保留成元组，让求解器不必
    # 去猜 blueprint dict 的形状。
    exclusion_groups: tuple[tuple[UUID, ...], ...] = ()
    # 蓝图里写了、但本实现不评估的硬约束键。必须显式带出来：阶段 A 的校验器对
    # 不认识的键是静默忽略的，如果这里也忽略，教师会得到一份"看起来满足了
    # coverage_required"的卷子。
    unsupported_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class SoftObjectives:
    difficulty_targets_bp: dict[int, int] = field(default_factory=dict)
    difficulty_weight: int = 0
    target_duration_seconds: int | None = None
    duration_weight: int = 0
    cognitive_targets_bp: dict[int, int] = field(default_factory=dict)
    cognitive_weight: int = 0
    recent_use_weight: int = 0
    exposure_balance_weight: int = 0
    teacher_preference_weight: int = 0
    ab_structure_weight: int = 0


@dataclass(frozen=True)
class SolverConfig:
    """求解配置：全整数、单 worker，保证同一输入得到同一结论。"""

    random_seed: int = 20261105
    time_limit_seconds: int = 30
    num_workers: int = 1
    max_solutions: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_solutions <= 3:
            raise ValueError("max_solutions must be between 1 and 3")
        if self.time_limit_seconds < 1:
            raise ValueError("time_limit_seconds must be positive")
        if self.num_workers != 1:
            # 多 worker 会让同一 seed 解出不同方案，"回放"就成了空话。
            raise ValueError("deterministic solving requires num_workers == 1")


@dataclass(frozen=True)
class BlueprintSnapshot:
    blueprint_version_id: UUID
    course_id: UUID
    offering_id: UUID | None
    release_context_id: UUID
    purpose: str
    total_score: Decimal
    duration_seconds: int
    slots: tuple[SlotSpec, ...]
    hard_constraints: HardConstraints = field(default_factory=HardConstraints)
    soft_objectives: SoftObjectives = field(default_factory=SoftObjectives)
    assembly_mode: str = "single"
    anchor_version_ids: tuple[UUID, ...] = ()
    visible_from: datetime | None = None


# ---------------------------------------------------------------------------
# 候选侧
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """固定 Release Context 里的一条候选，进入求解器的全部输入都在这里。

    `recent_use_count / exposure_count / answer_released` 不是调用方自报：
    它们由 `AssemblyCandidateService` 从真实的 `assessment.item_exposure_event`
    统计出来（见 candidate.py），教师无法通过请求体把"用过很多次"改成"没用过"。
    """

    exercise_version_id: UUID
    stable_exercise_id: UUID
    exercise_family_id: UUID
    source_release_id: UUID
    decision_seq: int
    exercise_kind: str
    dependency_hash: str
    content_hash: str
    exercise_instance_id: UUID | None = None
    concept_weights: tuple[ConceptWeight, ...] = ()
    learning_outcome_ids: tuple[UUID, ...] = ()
    difficulty: int | None = None
    cognitive_level: int | None = None
    estimated_seconds: int | None = None
    recent_use_count: int = 0
    exposure_count: int = 0
    preferred: bool = False
    conflict_group_ids: tuple[UUID, ...] = ()
    answer_released: bool = False
    permitted_offering_ids: tuple[UUID, ...] = ()
    security_window_key: str | None = None


@dataclass(frozen=True)
class ManifestExclusion:
    """被排除的候选：保持"题目版本 → 原因码"，让教师知道换哪道题。"""

    exercise_version_id: UUID
    reason: str


@dataclass(frozen=True)
class CandidateManifest:
    """一次运行的固定候选清单；`manifest_hash` 覆盖候选与排除，不含操作者。

    `candidate_snapshot_id` 是**来源**，不是清单内容：它指向阶段 A 的
    `assessment.candidate_snapshot` 行（建卷时 `paper_version.candidate_snapshot_id`
    是 NOT NULL，没有它就落不了卷）。它刻意**不参与** `manifest_hash`——同一批题
    无论是哪一次阶段 A 快照固化的，候选清单都是同一份。
    """

    blueprint_version_id: UUID
    release_context_id: UUID
    candidates: tuple[Candidate, ...]
    exclusions: tuple[ManifestExclusion, ...]
    manifest_hash: str
    candidate_snapshot_id: UUID | None = None


# ---------------------------------------------------------------------------
# 方案侧
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssemblySelection:
    """方案里"槽位 → 题目版本"的一个固定决定，带来源与哈希。"""

    slot_id: UUID
    slot_code: str
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    stable_exercise_id: UUID
    exercise_family_id: UUID
    source_release_id: UUID
    decision_seq: int
    score_hundredths: int
    estimated_seconds: int | None
    difficulty: int | None
    dependency_hash: str
    content_hash: str


@dataclass(frozen=True)
class ObjectiveTerm:
    """软目标的一项分解：原始量、偏差、罚分都是整数。"""

    name: str
    raw_value: dict[str, int]
    deviation: int
    penalty: int


@dataclass(frozen=True)
class SolutionManifest:
    variant_label: str
    selections: tuple[AssemblySelection, ...]
    objective_terms: tuple[ObjectiveTerm, ...]
    objective_value: int
    best_bound: int | None
    optimality_gap_milli: int | None
    solver_status: RunStatus
    optimal_proven: bool
    structure_report: dict[str, int]
    manifest_hash: str


@dataclass(frozen=True)
class SolutionSet:
    """一组方案（单卷 1 个，A/B 2 个）；组内共享同一份候选快照。"""

    variants: tuple[SolutionManifest, ...]
    manifest_hash: str


@dataclass(frozen=True)
class SolveResult:
    status: RunStatus
    solution_sets: tuple[SolutionSet, ...]
    diagnostics: dict[str, Any]
    best_bound: int | None
    optimality_gap_milli: int | None

    @property
    def has_solutions(self) -> bool:
        return bool(self.solution_sets)


# ---------------------------------------------------------------------------
# 复验与落卷
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    slot_code: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    issues: tuple[ValidationIssue, ...]
    manifest_hash: str


@dataclass(frozen=True)
class CreatePaperDraftRequest:
    blueprint_version_id: UUID
    assembly_run_id: UUID
    selected_solution_index: int
    actor_id: UUID
    idempotency_key: str
    solution: SolutionManifest
    variant_group_id: UUID | None = None
    variant_label: str = "A"


@dataclass(frozen=True)
class PaperDraft:
    paper_version_id: UUID
    manifest_hash: str
    status: str


@dataclass(frozen=True)
class AssemblyStartResult:
    assembly_run_id: UUID
    status: RunStatus
    reused: bool


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------

class CandidateEligibilityService(Protocol):
    def list_eligible(self, blueprint_version_id: UUID) -> list[Candidate]: ...


class PaperValidationService(Protocol):
    def validate(self, *, blueprint: BlueprintSnapshot,
                 candidate_manifest: CandidateManifest,
                 solution: SolutionManifest,
                 peer: SolutionManifest | None = None) -> ValidationReport: ...


class PaperService(Protocol):
    def blueprint_snapshot(self, blueprint_version_id: UUID) -> BlueprintSnapshot: ...

    def create_draft(self, request: CreatePaperDraftRequest) -> PaperDraft: ...


# ---------------------------------------------------------------------------
# manifest 构造：哈希规则只有这一份
# ---------------------------------------------------------------------------

def make_candidate_manifest(
    blueprint: BlueprintSnapshot,
    candidates: list[Candidate] | tuple[Candidate, ...],
    exclusions: list[ManifestExclusion] | tuple[ManifestExclusion, ...],
    *,
    candidate_snapshot_id: UUID | None = None,
) -> CandidateManifest:
    """按题目版本 id 排序后求哈希：输入顺序不同不该改变"同一份清单"的判断。"""
    ordered = tuple(sorted(candidates, key=lambda item: str(item.exercise_version_id)))
    ordered_exclusions = tuple(
        sorted(exclusions, key=lambda item: str(item.exercise_version_id))
    )
    body = {
        "blueprint_version_id": str(blueprint.blueprint_version_id),
        "release_context_id": str(blueprint.release_context_id),
        "candidates": [normalize(item) for item in ordered],
        "exclusions": [normalize(item) for item in ordered_exclusions],
    }
    return CandidateManifest(
        blueprint_version_id=blueprint.blueprint_version_id,
        release_context_id=blueprint.release_context_id,
        candidates=ordered,
        exclusions=ordered_exclusions,
        manifest_hash=canonical_hash(body),
        candidate_snapshot_id=candidate_snapshot_id,
    )


def make_solution_manifest(**fields: Any) -> SolutionManifest:
    """先算哈希再构造：`manifest_hash` 不参与自身哈希（否则永远对不上）。"""
    body = {key: value for key, value in fields.items() if key != "manifest_hash"}
    digest = canonical_hash(body)
    return SolutionManifest(**{**body, "manifest_hash": digest})


def make_solution_set(variants: tuple[SolutionManifest, ...]) -> SolutionSet:
    ordered = tuple(variants)
    return SolutionSet(
        variants=ordered,
        manifest_hash=canonical_hash([asdict(item) for item in ordered]),
    )


def optimality_gap_milli(objective: int, best_bound: int | None) -> int | None:
    """最优性间隙（千分比，整数）。没有界或目标为 0 时返回 None，不返回 0。"""
    if best_bound is None or objective == 0:
        return None
    return int(math.floor(abs(objective - best_bound) * 1000 / abs(objective)))


def to_json_value(value: Any) -> Any:
    """落库前的 JSON 化（UUID/Decimal/Enum → 字符串），与哈希用的归一化同源。"""
    return normalize(value)


def solution_hash_matches(manifest: SolutionManifest) -> bool:
    """重算方案哈希并与记录值比较：这是"方案有没有被改过"的唯一判据。

    只比"载荷里的 manifest_hash 字段"没有意义——篡改者当然会一起改它。真正要
    证明的是：**从内容重新算出来的哈希**等于记录的哈希。改任何一道题、任何一个
    分值，重算结果都会变。
    """
    body = {key: value for key, value in asdict(manifest).items() if key != "manifest_hash"}
    return canonical_hash(body) == manifest.manifest_hash


def solution_to_json(manifest: SolutionManifest) -> dict[str, Any]:
    """方案 manifest → jsonb 载荷（字段显式列出，不做 `asdict` 的隐式扩散）。

    显式列字段的代价是多写十几行，收益是：将来给 `SolutionManifest` 加字段时，
    这里会"少一个键"而报错，而不是悄悄把新字段塞进历史运行的回放载荷里。
    """
    return {
        "variant_label": manifest.variant_label,
        "selections": [
            {
                "slot_id": str(item.slot_id),
                "slot_code": item.slot_code,
                "exercise_version_id": str(item.exercise_version_id),
                "exercise_instance_id": (
                    None if item.exercise_instance_id is None else str(item.exercise_instance_id)
                ),
                "stable_exercise_id": str(item.stable_exercise_id),
                "exercise_family_id": str(item.exercise_family_id),
                "source_release_id": str(item.source_release_id),
                "decision_seq": item.decision_seq,
                "score_hundredths": item.score_hundredths,
                "estimated_seconds": item.estimated_seconds,
                "difficulty": item.difficulty,
                "dependency_hash": item.dependency_hash,
                "content_hash": item.content_hash,
            }
            for item in manifest.selections
        ],
        "objective_terms": [
            {"name": item.name, "raw_value": dict(item.raw_value),
             "deviation": item.deviation, "penalty": item.penalty}
            for item in manifest.objective_terms
        ],
        "objective_value": manifest.objective_value,
        "best_bound": manifest.best_bound,
        "optimality_gap_milli": manifest.optimality_gap_milli,
        "solver_status": str(manifest.solver_status),
        "optimal_proven": manifest.optimal_proven,
        "structure_report": dict(manifest.structure_report),
        "manifest_hash": manifest.manifest_hash,
    }


def solution_from_json(payload: dict[str, Any]) -> SolutionManifest:
    """jsonb 载荷 → 方案 manifest；缺键直接 KeyError，不做"尽量还原"。

    回放的可信度取决于"读回来的就是写进去的"。任何字段缺失都说明载荷不是本版本
    写的（或被人改过），此时抛错比补默认值安全。
    """
    selections = tuple(
        AssemblySelection(
            slot_id=UUID(item["slot_id"]),
            slot_code=item["slot_code"],
            exercise_version_id=UUID(item["exercise_version_id"]),
            exercise_instance_id=(
                None if item["exercise_instance_id"] is None
                else UUID(item["exercise_instance_id"])
            ),
            stable_exercise_id=UUID(item["stable_exercise_id"]),
            exercise_family_id=UUID(item["exercise_family_id"]),
            source_release_id=UUID(item["source_release_id"]),
            decision_seq=int(item["decision_seq"]),
            score_hundredths=int(item["score_hundredths"]),
            estimated_seconds=item["estimated_seconds"],
            difficulty=item["difficulty"],
            dependency_hash=item["dependency_hash"],
            content_hash=item["content_hash"],
        )
        for item in payload["selections"]
    )
    terms = tuple(
        ObjectiveTerm(
            name=item["name"],
            raw_value=dict(item["raw_value"]),
            deviation=int(item["deviation"]),
            penalty=int(item["penalty"]),
        )
        for item in payload["objective_terms"]
    )
    return SolutionManifest(
        variant_label=payload["variant_label"],
        selections=selections,
        objective_terms=terms,
        objective_value=int(payload["objective_value"]),
        best_bound=payload["best_bound"],
        optimality_gap_milli=payload["optimality_gap_milli"],
        solver_status=RunStatus(payload["solver_status"]),
        optimal_proven=bool(payload["optimal_proven"]),
        structure_report=dict(payload["structure_report"]),
        manifest_hash=payload["manifest_hash"],
    )


__all__ = [
    "SOLVED_STATUSES",
    "AssemblySelection",
    "AssemblyStartResult",
    "BlueprintSnapshot",
    "Candidate",
    "CandidateEligibilityService",
    "CandidateManifest",
    "ConceptWeight",
    "CoverageRequirement",
    "CreatePaperDraftRequest",
    "HardConstraints",
    "KindRequirement",
    "ManifestExclusion",
    "ObjectiveTerm",
    "PaperDraft",
    "PaperService",
    "PaperValidationService",
    "RunStatus",
    "SLOT_NAMESPACE",
    "SoftObjectives",
    "SlotSpec",
    "SolverConfig",
    "SolutionManifest",
    "SolutionSet",
    "SolveResult",
    "ValidationIssue",
    "ValidationReport",
    "make_candidate_manifest",
    "make_solution_manifest",
    "make_solution_set",
    "optimality_gap_milli",
    "score_to_hundredths",
    "slot_identity",
    "solution_from_json",
    "solution_hash_matches",
    "solution_to_json",
    "to_json_value",
]
