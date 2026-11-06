# 约束辅助组卷与 A/B 固定卷实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在阶段 A 的手工固定卷、整卷校验和作业发布链路上，交付固定候选快照、OR-Tools CP-SAT 约束求解、不可行诊断、最多 3 个可解释方案、独立整卷复验、A/B 固定卷、曝光冲突阻断、教师方案比较和幂等 worker。

**架构：** `CandidateService` 将阶段 A 已审核候选固化为带哈希的 manifest；`AssemblySolver` 只读取该快照，以整数分/100、整数秒和整数权重建立 CP-SAT 模型。worker 保存求解状态与结果 manifest，所有候选方案先经阶段 A `PaperValidationService` 独立复验，再由阶段 A `PaperService` 创建草稿固定卷；A/B 卷共享蓝图和候选快照，但分别复验、审核和分配。曝光检查嵌入阶段 A `AssignmentService.publish` 的既有事务，不新建第二套试卷或作业真源。

**技术栈：** Python 3.12、OR-Tools 9.11.4210、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL 16、Celery、Next.js/React/TypeScript、pytest、Vitest、Testing Library。

---

## 0. 实施边界与状态语义

### 0.1 前置依赖

开始任务 1 前，阶段 A 已提供可运行的 `PaperService`、`PaperValidationService`、`AssignmentService`、蓝图/固定卷模型、题目审核与 Release Context 门禁。阶段 A 已创建 `assessment.item_exposure_event` 和 `attempt_policy_version`，本计划只读前者结构并复用，不重复建表或迁移。本计划不重复实现手工挑题、手工调分、整卷人工审核或打印渲染。

### 0.2 本阶段范围

- 固定 Release Context 的候选 manifest、证据哈希、确定性预检与回放。
- CP-SAT 硬约束、软目标、不可行诊断、求解配置与结果解释。
- `OPTIMAL / FEASIBLE / INFEASIBLE / TIMEOUT / FAILED` 的持久化、API 与 UI 语义。
- 每次运行最多 3 个不同方案；每个方案独立整卷复验。
- 单卷与 A/B 固定卷；A/B 只声明结构匹配，不声明统计等值。
- 复用 `item_exposure_event`，实现候选与发布终检的曝光查询、冲突规则和事件写入组合。
- `AssemblySolutionComparator`、worker 幂等和 1,000 候选/50 槽位受控压测。

### 0.3 明确排除

- 不实现在线 attempt、自动保存、交卷、在线练习评分或 QTI。
- 不实现运行时随机抽题、IRT、自适应组卷或统计等值结论。
- 不重新实现阶段 A 手工卷编辑器、整卷审核和 assignment 生命周期。
- 不用 LLM 选题，不赋予 worker 人工审核或发布权限。

### 0.4 不可变语义

1. `OPTIMAL`：至少第一个方案由 CP-SAT 证明最优；排除前解后得到的备选只标 `FEASIBLE`，不继承最优结论。
2. `FEASIBLE`：有可行方案，但未证明最优；API 和 UI 必须显示「可行，未证明最优」。
3. `INFEASIBLE`：原硬约束不可行；诊断可以在明确标记的副本中逐组移除约束定位原因，但原蓝图不变、原运行不产生方案、不创建 paper。
4. `TIMEOUT`：达到时间上限。已有可行解时可保存并显示「可行，未证明最优」；无可行解时 `solution_manifest=[]`，选择按钮不可用，不创建 paper。
5. `FAILED`：模型无效、worker 异常或独立复验失败；保存错误诊断，不创建 paper。
6. 回放读取数据库保存的 candidate manifest、求解配置和 solution manifest。跨 OR-Tools 版本或硬件重新求解只产生新运行，不覆盖历史结果。
7. 分值进入模型前由 `Decimal` 精确转换为整数分/100；时间使用整数秒；软目标权重、偏差和罚分全部使用整数。
8. 手工修改求解方案后不再保留「求解器已验证」标签，必须重新调用阶段 A 整卷校验。

## 1. 文件结构

```text
AIForTeaching/
├─ requirements/assembly.lock                    # 锁定 ortools==9.11.4210
├─ db/migrations/versions/
│  └─ 20261105_090000_assessment_assembly.py     # 仅新增 assembly_run；down_revision 固定
├─ modules/assessment/
│  ├─ models/
│  │  ├─ paper.py                                # 复用阶段 A 蓝图、paper、variant 字段
│  │  ├─ assembly.py                             # AssemblyRun ORM
│  │  └─ exposure.py                             # 复用阶段 A ItemExposureEvent，不新增表
│  ├─ assembly/
│  │  ├─ contracts.py                            # 不可变 DTO、枚举、哈希和共享 Protocol
│  │  ├─ candidate.py                            # 候选曝光检查、固定快照与 manifest 哈希
│  │  ├─ solver.py                               # CP-SAT 硬约束、状态映射、排除前解与回放输入
│  │  ├─ objectives.py                           # 整数软目标、目标分解和 A/B 结构偏差
│  │  ├─ diagnostics.py                          # 预检和只读诊断副本
│  │  ├─ validation.py                           # 调阶段 A 独立整卷校验的门禁
│  │  ├─ materialize.py                          # 复验后调阶段 A PaperService 创建草稿
│  │  └─ service.py                              # AssemblyService、幂等启动、查询与方案选择
│  ├─ exposure.py                                # 曝光冲突查询与既有事件表写入组合
│  └─ assignment.py                              # 仅在既有 publish 事务注入曝光终检
├─ apps/
│  ├─ api/routers/assembly_runs.py               # 启动、查询回放、选择方案
│  ├─ worker/assembly.py                         # 幂等执行、独立复验、状态持久化
│  └─ web/components/assessment/
│     ├─ AssemblySolutionComparator.tsx
│     └─ AssemblySolutionComparator.test.tsx
├─ scripts/load_assembly.py                      # 1,000 候选/50 槽位受控负载
└─ tests/
   ├─ fixtures/assembly.py                       # 共享 DTO fixture 工厂
   ├─ unit/assessment/test_candidate.py
   ├─ unit/assessment/test_solver_hard.py
   ├─ unit/assessment/test_solver_objectives.py
   ├─ unit/assessment/test_diagnostics.py
   ├─ integration/assessment/test_assembly_migration.py
   ├─ integration/assessment/test_materialize.py
   ├─ integration/assessment/test_assembly_api_worker.py
   ├─ integration/assessment/test_ab_assembly.py
   ├─ integration/assessment/test_exposure.py
   └─ e2e/test_constraint_assembly.py
```

## 2. 下方共享契约

以下签名是任务间唯一接口。DTO 的完整字段在任务 1 的 `contracts.py` 代码中定义；阶段 A 服务只扩展适配，不另建平行实现。

```python
class CandidateEligibilityService(Protocol):
    def list_eligible(self, blueprint_version_id: UUID) -> list[Candidate]: ...

class CandidateService:
    def snapshot(self, actor_id: UUID, blueprint_version_id: UUID,
                 purpose: str, offering_id: UUID | None) -> CandidateManifest: ...

class AssemblySolver:
    def solve(
        self,
        blueprint: BlueprintSnapshot,
        manifest: CandidateManifest,
        config: SolverConfig,
        peer: SolutionManifest | None = None,
    ) -> SolveResult: ...

class AssemblyService:
    def start(
        self,
        blueprint_version_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
    ) -> AssemblyStartResult: ...

class PaperValidationService(Protocol):
    def validate(
        self,
        *,
        blueprint: BlueprintSnapshot,
        candidate_manifest: CandidateManifest,
        solution: SolutionManifest,
        peer: SolutionManifest | None,
    ) -> ValidationReport: ...

class PaperService(Protocol):
    def blueprint_snapshot(self, blueprint_version_id: UUID) -> BlueprintSnapshot: ...
    def create_draft(self, request: CreatePaperDraftRequest) -> PaperDraft: ...

class ExposureService:
    def assert_no_conflict(
        self,
        *,
        purpose: str,
        scope_offering_id: UUID,
        visible_from: datetime,
        candidate: Candidate | None = None,
        paper_version_id: UUID | None = None,
        assignment_id: UUID | None = None,
    ) -> None: ...

class AssignmentService(Protocol):
    def publish(self, session: Session, *, assignment_id: UUID, teacher_id: UUID,
                base_version: int, idempotency_key: str, now: datetime) -> dict: ...
```

`AssemblyService.start` 从蓝图读取 `assembly_mode`，单卷与 A/B 共用此入口。`ExposureService.assert_no_conflict` 要求 `candidate` 与 `paper_version_id` 恰好提供一个：候选快照逐题调用，发布终检按整卷调用。两种调用都必须实际查询阶段 A 的 `item_exposure_event`。

### 任务 1：锁定求解器、定义契约并生成候选 manifest

**文件：**
- 创建：`requirements/assembly.lock`
- 创建：`modules/assessment/assembly/contracts.py`
- 创建：`modules/assessment/assembly/candidate.py`
- 创建：`modules/assessment/models/assembly.py`
- 创建：`db/migrations/versions/20261105_090000_assessment_assembly.py`
- 创建：`tests/fixtures/assembly.py`
- 测试：`tests/unit/assessment/test_candidate.py`
- 测试：`tests/integration/assessment/test_assembly_migration.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/test_candidate.py
from dataclasses import asdict
from importlib.metadata import version
from uuid import UUID

import pytest

from modules.assessment.assembly.candidate import CandidateService, CandidateSnapshotError
from tests.fixtures.assembly import make_blueprint, make_candidate


def test_locked_ortools_version():
    assert version("ortools") == "9.11.4210"


def test_manifest_hash_is_stable_and_formal_exposure_is_excluded():
    kept = make_candidate(1)
    released = make_candidate(2, answer_released=True)
    eligibility = FakeEligibility([released, kept])
    exposure = FakeExposure(conflict_version_ids={released.exercise_version_id})
    service = CandidateService(eligibility, exposure, make_blueprint())
    first = service.snapshot(UUID(int=88), UUID(int=100), "formal", UUID(int=102))
    second = CandidateService(
        FakeEligibility([kept, released]), exposure, make_blueprint()
    ).snapshot(UUID(int=88), UUID(int=100), "formal", UUID(int=102))
    assert first.manifest_hash == second.manifest_hash
    assert [c.exercise_version_id for c in first.candidates] == [kept.exercise_version_id]
    assert first.exclusions[0].reason == "exposure_conflict"


def test_hard_duration_rejects_missing_candidate_seconds():
    candidate = make_candidate(1, estimated_seconds=None)
    service = CandidateService(
        FakeEligibility([candidate]), FakeExposure(set()),
        make_blueprint(max_duration_seconds=3600),
    )
    with pytest.raises(CandidateSnapshotError, match="estimated_seconds"):
        service.snapshot(UUID(int=88), UUID(int=100), "formal", UUID(int=102))


class FakeEligibility:
    def __init__(self, rows):
        self.rows = rows

    def list_eligible(self, blueprint_version_id):
        return self.rows


class FakeExposure:
    def __init__(self, conflict_version_ids):
        self.conflict_version_ids = conflict_version_ids
        self.calls = 0

    def assert_no_conflict(self, **kwargs):
        self.calls += 1
        candidate = kwargs["candidate"]
        if candidate.exercise_version_id in self.conflict_version_ids:
            raise ExposureConflictError("practice_answer_released")
```

```python
# tests/integration/assessment/test_assembly_migration.py
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


def test_assembly_upgrade_and_downgrade(db_engine, monkeypatch):
    config = Config("alembic.ini")
    config.attributes["connection"] = db_engine.connect()
    command.upgrade(config, "20261105_090000_assessment_assembly")
    columns = {c["name"] for c in inspect(db_engine).get_columns("assembly_run", schema="assessment")}
    assert {"candidate_manifest_hash", "solution_manifest", "solver_config", "status"} <= columns
    command.downgrade(config, "20261105_080000_assessment_paper")
    assert not inspect(db_engine).has_table("assembly_run", schema="assessment")
    command.upgrade(config, "20261105_090000_assessment_assembly")
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
python -m pip install "ortools==9.11.4210"
pytest tests/unit/assessment/test_candidate.py tests/integration/assessment/test_assembly_migration.py -q
```

预期：FAIL；`modules.assessment.assembly.candidate` 不存在，迁移 revision 不存在。依赖安装成功后才允许继续，不能把依赖缺失当作业务测试失败。

- [ ] **步骤 3：编写最少实现代码**

`requirements/assembly.lock` 只写入：

```text
ortools==9.11.4210
```

`contracts.py` 定义完整共享 DTO；分值、秒、权重和目标值均为整数或 `Decimal`，不使用 `float`。

```python
# modules/assessment/assembly/contracts.py
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


def new_uuid7() -> UUID:
    milliseconds = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (milliseconds << 80) | (0x7 << 76)
    value |= secrets.randbits(12) << 64
    value |= (0b10 << 62) | secrets.randbits(62)
    return UUID(int=value)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    TIMEOUT = "timeout"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ConceptWeight:
    concept_id: UUID
    role: str
    weight: int


@dataclass(frozen=True)
class CoverageRequirement:
    target_id: UUID
    target_kind: str  # concept 或 learning_outcome
    role: str
    min_weight: int


@dataclass(frozen=True)
class KindRequirement:
    section_code: str | None
    exercise_kind: str
    min_count: int
    max_count: int | None


@dataclass(frozen=True)
class SlotSpec:
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
class Candidate:
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
class HardConstraints:
    section_scores: dict[str, Decimal] = field(default_factory=dict)
    coverage_requirements: tuple[CoverageRequirement, ...] = ()
    kind_requirements: tuple[KindRequirement, ...] = ()
    max_duration_seconds: int | None = None


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
    random_seed: int = 20261105
    time_limit_seconds: int = 30
    num_workers: int = 1
    max_solutions: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_solutions <= 3:
            raise ValueError("max_solutions must be between 1 and 3")
        if self.time_limit_seconds < 1 or self.num_workers != 1:
            raise ValueError("deterministic solving requires positive seconds and one worker")


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


@dataclass(frozen=True)
class ManifestExclusion:
    exercise_version_id: UUID
    reason: str


@dataclass(frozen=True)
class CandidateManifest:
    blueprint_version_id: UUID
    release_context_id: UUID
    candidates: tuple[Candidate, ...]
    exclusions: tuple[ManifestExclusion, ...]
    manifest_hash: str


@dataclass(frozen=True)
class SlotSelection:
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
    name: str
    raw_value: dict[str, int]
    deviation: int
    penalty: int


@dataclass(frozen=True)
class SolutionManifest:
    variant_label: str
    selections: tuple[SlotSelection, ...]
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
    variants: tuple[SolutionManifest, ...]
    manifest_hash: str


@dataclass(frozen=True)
class SolveResult:
    status: RunStatus
    solution_sets: tuple[SolutionSet, ...]
    diagnostics: dict[str, Any]
    best_bound: int | None
    optimality_gap_milli: int | None


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    slot_code: str | None


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
    variant_group_id: UUID | None
    variant_label: str


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


class ExposureConflictError(RuntimeError):
    pass


class CandidateEligibilityService(Protocol):
    def list_eligible(self, blueprint_version_id: UUID) -> list[Candidate]: ...


def score_to_hundredths(value: Decimal) -> int:
    scaled = value * Decimal("100")
    if scaled != scaled.to_integral_value():
        raise ValueError("score must have at most two decimal places")
    return int(scaled)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def to_json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def make_candidate_manifest(
    blueprint: BlueprintSnapshot,
    candidates: list[Candidate],
    exclusions: list[ManifestExclusion],
) -> CandidateManifest:
    ordered = tuple(sorted(candidates, key=lambda c: str(c.exercise_version_id)))
    body = {
        "blueprint_version_id": str(blueprint.blueprint_version_id),
        "release_context_id": str(blueprint.release_context_id),
        "candidates": [asdict(c) for c in ordered],
        "exclusions": [asdict(e) for e in sorted(exclusions, key=lambda e: str(e.exercise_version_id))],
    }
    ordered_exclusions = tuple(sorted(exclusions, key=lambda e: str(e.exercise_version_id)))
    return CandidateManifest(
        blueprint_version_id=blueprint.blueprint_version_id,
        release_context_id=blueprint.release_context_id,
        candidates=ordered,
        exclusions=ordered_exclusions,
        manifest_hash=canonical_hash(body),
    )


def make_solution_set(variants: tuple[SolutionManifest, ...]) -> SolutionSet:
    return SolutionSet(variants=variants, manifest_hash=canonical_hash([asdict(v) for v in variants]))
```

`candidate.py` 对每题执行曝光检查；阶段 A 的审核、Release、许可、实例与私有草稿过滤仍由 `CandidateEligibilityService` 完成。

```python
# modules/assessment/assembly/candidate.py
from datetime import UTC, datetime
from uuid import UUID

from modules.assessment.assembly.contracts import (
    BlueprintSnapshot, CandidateEligibilityService, CandidateManifest,
    ExposureConflictError, ManifestExclusion,
)
from modules.assessment.assembly.contracts import make_candidate_manifest


class CandidateSnapshotError(ValueError):
    pass


class CandidateService:
    def __init__(self, eligibility, exposure, blueprint: BlueprintSnapshot):
        self.eligibility = eligibility
        self.exposure = exposure
        self.blueprint = blueprint

    def snapshot(self, actor_id: UUID, blueprint_version_id: UUID,
                 purpose: str, offering_id: UUID | None) -> CandidateManifest:
        if blueprint_version_id != self.blueprint.blueprint_version_id:
            raise CandidateSnapshotError("blueprint_version_id mismatch")
        if purpose != self.blueprint.purpose or offering_id != self.blueprint.offering_id:
            raise CandidateSnapshotError("candidate purpose/offering mismatch")
        accepted = []
        exclusions = []
        for candidate in self.eligibility.list_eligible(blueprint_version_id):
            if not candidate.content_hash or not candidate.dependency_hash:
                raise CandidateSnapshotError("candidate content/dependency hash is required")
            requires_seconds = (
                self.blueprint.hard_constraints.max_duration_seconds is not None
                or self.blueprint.soft_objectives.target_duration_seconds is not None
                or self.blueprint.assembly_mode == "ab"
            )
            if requires_seconds and candidate.estimated_seconds is None:
                raise CandidateSnapshotError("estimated_seconds is required by duration rule")
            try:
                self.exposure.assert_no_conflict(
                    purpose=purpose,
                    scope_offering_id=offering_id,
                    visible_from=self.blueprint.visible_from or datetime.now(UTC),
                    candidate=candidate,
                )
            except ExposureConflictError:
                exclusions.append(ManifestExclusion(candidate.exercise_version_id, "exposure_conflict"))
                continue
            accepted.append(candidate)
        return make_candidate_manifest(self.blueprint, accepted, exclusions)
```

`models/assembly.py` 使用数据库枚举和 JSONB；方案数组在数据库层限制最多 3 项。

```python
# modules/assessment/models/assembly.py
import uuid
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from modules.common.db import Base
from modules.assessment.assembly.contracts import new_uuid7


class AssemblyRun(Base):
    __tablename__ = "assembly_run"
    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key", name="uq_assembly_actor_key"),
        CheckConstraint("jsonb_array_length(solution_manifest) <= 3", name="ck_assembly_max_3"),
        {"schema": "assessment"},
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid7)
    blueprint_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment.paper_blueprint_version.id"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    candidate_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    solver_name: Mapped[str] = mapped_column(String(32), nullable=False, default="cp-sat")
    solver_version: Mapped[str] = mapped_column(String(32), nullable=False)
    solver_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    solution_manifest: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    objective_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    diagnostics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    best_bound: Mapped[int | None] = mapped_column(Integer)
    optimality_gap_milli: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
```

迁移严格使用统一 revision；`downgrade` 只删除本阶段表，不触碰阶段 A 的 paper 或 exposure 表。

```python
# db/migrations/versions/20261105_090000_assessment_assembly.py
revision = "20261105_090000_assessment_assembly"
down_revision = "20261105_080000_assessment_paper"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade():
    op.create_table(
        "assembly_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("blueprint_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("assessment.paper_blueprint_version.id"), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("candidate_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_manifest_hash", sa.String(64), nullable=False),
        sa.Column("solver_name", sa.String(32), nullable=False),
        sa.Column("solver_version", sa.String(32), nullable=False),
        sa.Column("solver_config", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("solution_manifest", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("objective_breakdown", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("best_bound", sa.BigInteger()),
        sa.Column("optimality_gap_milli", sa.Integer()),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_assembly_actor_key"),
        sa.CheckConstraint("jsonb_array_length(solution_manifest) <= 3", name="ck_assembly_max_3"),
        schema="assessment",
    )
    op.drop_constraint("ck_paper_manual_only", "paper_version", schema="assessment", type_="check")
    op.create_foreign_key(
        "fk_paper_assembly_run", "paper_version", "assembly_run",
        ["assembly_run_id"], ["id"], source_schema="assessment", referent_schema="assessment",
    )
    op.create_check_constraint(
        "ck_paper_assembly_source", "paper_version",
        "(assembly_run_id IS NULL AND selected_solution_index IS NULL) OR "
        "(assembly_run_id IS NOT NULL AND selected_solution_index IS NOT NULL)",
        schema="assessment",
    )


def downgrade():
    linked = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM assessment.paper_version WHERE assembly_run_id IS NOT NULL"
    )).scalar_one()
    if linked:
        raise RuntimeError("cannot downgrade assembly while paper versions retain assembly provenance")
    op.drop_constraint("fk_paper_assembly_run", "paper_version", schema="assessment", type_="foreignkey")
    op.drop_constraint("ck_paper_assembly_source", "paper_version", schema="assessment", type_="check")
    op.create_check_constraint(
        "ck_paper_manual_only", "paper_version",
        "assembly_run_id IS NULL AND selected_solution_index IS NULL", schema="assessment",
    )
    op.drop_table("assembly_run", schema="assessment")
```

`tests/fixtures/assembly.py` 提供后续测试共用的真实 DTO 工厂。

```python
# tests/fixtures/assembly.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from modules.assessment.assembly.contracts import (
    BlueprintSnapshot, Candidate, CandidateManifest, HardConstraints, SlotSpec,
    SolverConfig, make_candidate_manifest,
)


def make_candidate(number: int, **changes):
    values = dict(
        exercise_version_id=UUID(int=number),
        stable_exercise_id=UUID(int=10000 + number),
        exercise_family_id=UUID(int=20000 + number),
        source_release_id=UUID(int=30000 + number),
        decision_seq=number,
        exercise_kind="expression",
        dependency_hash=f"dependency-{number}",
        content_hash=f"content-{number}",
        difficulty=1 + number % 5,
        cognitive_level=1 + number % 4,
        estimated_seconds=300,
    )
    values.update(changes)
    return Candidate(**values)


def make_slot(number: int = 1, **changes):
    values = dict(
        slot_id=UUID(int=1000 + number), code=f"A{number}", section_code="A",
        score=Decimal("10.00"), estimated_seconds=300,
        allowed_kinds=("expression",),
    )
    values.update(changes)
    return SlotSpec(**values)


def make_blueprint(**changes):
    slots = changes.pop("slots", (make_slot(),))
    max_duration = changes.pop("max_duration_seconds", None)
    values = dict(
        blueprint_version_id=UUID(int=100), course_id=UUID(int=101),
        offering_id=UUID(int=102), release_context_id=UUID(int=103),
        purpose="formal", total_score=sum((s.score for s in slots), Decimal("0")),
        duration_seconds=3600, slots=slots,
        hard_constraints=HardConstraints(
            section_scores={"A": sum((s.score for s in slots), Decimal("0"))},
            max_duration_seconds=max_duration,
        ),
        visible_from=datetime(2026, 11, 5, tzinfo=UTC),
    )
    values.update(changes)
    return BlueprintSnapshot(**values)


def make_manifest(blueprint=None, candidates=None):
    blueprint = blueprint or make_blueprint()
    return make_candidate_manifest(blueprint, candidates or [make_candidate(1)], [])


def make_config(**changes):
    values = dict(time_limit_seconds=2, max_solutions=1, random_seed=7, num_workers=1)
    values.update(changes)
    return SolverConfig(**values)
```

在测试文件顶部加入 `from modules.assessment.assembly.contracts import ExposureConflictError`，保证 fake 与生产异常类型一致。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/test_candidate.py tests/integration/assessment/test_assembly_migration.py -q`
预期：PASS；同一输入哈希稳定，曝光冲突题被排除，硬时长缺失被阻断，迁移升级、回滚、再升级均成功。

- [ ] **步骤 5：Commit**

```bash
git add requirements/assembly.lock modules/assessment/assembly/contracts.py modules/assessment/assembly/candidate.py modules/assessment/models/assembly.py db/migrations/versions/20261105_090000_assessment_assembly.py tests/fixtures/assembly.py tests/unit/assessment/test_candidate.py tests/integration/assessment/test_assembly_migration.py
git commit -m "feat: 固定约束组卷候选快照与运行模型"
```

### 任务 2：建立 CP-SAT 硬约束与整数单位模型

**文件：**
- 创建：`modules/assessment/assembly/solver.py`
- 测试：`tests/unit/assessment/test_solver_hard.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/test_solver_hard.py
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from modules.assessment.assembly.contracts import (
    CoverageRequirement, KindRequirement, RunStatus, score_to_hundredths,
)
from modules.assessment.assembly.solver import AssemblySolver
from tests.fixtures.assembly import make_blueprint, make_candidate, make_manifest, make_slot


def test_decimal_score_conversion_is_exact():
    assert score_to_hundredths(Decimal("0.05")) == 5
    assert score_to_hundredths(Decimal("100.00")) == 10000
    with pytest.raises(ValueError):
        score_to_hundredths(Decimal("0.001"))


def test_duplicate_stable_item_and_family_conflict_are_infeasible():
    slots = (make_slot(1), make_slot(2))
    first = make_candidate(1, stable_exercise_id=UUID(int=9))
    second = make_candidate(2, stable_exercise_id=UUID(int=9))
    blueprint = make_blueprint(slots=slots)
    result = AssemblySolver().solve(blueprint, make_manifest(blueprint, [first, second]), config())
    assert result.status == RunStatus.INFEASIBLE
    assert result.solution_sets == ()


def test_mandatory_coverage_kind_and_duration_constraints():
    required = make_candidate(1, concept_weights=(concept_weight(77, "primary", 3),))
    other = make_candidate(2, estimated_seconds=900)
    slot = make_slot(mandatory_version_ids=(required.exercise_version_id,))
    hard = replace(
        make_blueprint(slots=(slot,), max_duration_seconds=600).hard_constraints,
        coverage_requirements=(CoverageRequirement(UUID(int=77), "concept", "primary", 3),),
        kind_requirements=(KindRequirement("A", "expression", 1, 1),),
    )
    blueprint = make_blueprint(slots=(slot,), hard_constraints=hard)
    result = AssemblySolver().solve(blueprint, make_manifest(blueprint, [required, other]), config())
    assert result.status == RunStatus.OPTIMAL
    selected = result.solution_sets[0].variants[0].selections[0]
    assert selected.exercise_version_id == required.exercise_version_id
    assert selected.score_hundredths == 1000


def config():
    from tests.fixtures.assembly import make_config
    return make_config()


def concept_weight(number: int, role: str, weight: int):
    from modules.assessment.assembly.contracts import ConceptWeight
    return ConceptWeight(UUID(int=number), role, weight)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_solver_hard.py -q`
预期：FAIL，报错 `ModuleNotFoundError: modules.assessment.assembly.solver`。

- [ ] **步骤 3：编写最少实现代码**

```python
# modules/assessment/assembly/solver.py
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict
from uuid import UUID

from ortools.sat.python import cp_model

from modules.assessment.assembly.contracts import (
    BlueprintSnapshot, CandidateManifest, RunStatus, SlotSelection, SolutionManifest,
    SolveResult, SolverConfig, canonical_hash, make_solution_set, score_to_hundredths,
)


class AssemblySolver:
    def solve(self, blueprint, manifest, config, peer=None):
        if peer is not None:
            raise ValueError("peer constraints are enabled by the A/B task")
        model, variables, candidates = self._build_model(blueprint, manifest, peer)
        solver = self._new_solver(config, config.time_limit_seconds)
        started = time.monotonic()
        raw = solver.Solve(model)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if raw == cp_model.INFEASIBLE:
            return SolveResult(RunStatus.INFEASIBLE, (), {"reason": "hard_constraints"}, None, None)
        if raw == cp_model.MODEL_INVALID:
            return SolveResult(RunStatus.FAILED, (), {"reason": "model_invalid"}, None, None)
        if raw == cp_model.UNKNOWN:
            return SolveResult(RunStatus.TIMEOUT, (), {"reason": "time_limit_no_solution"}, None, None)
        solution = self._extract_solution("single", raw, blueprint, manifest, variables, candidates, solver)
        return SolveResult(RunStatus.OPTIMAL, (make_solution_set((solution,)),), {}, solution.best_bound, solution.optimality_gap_milli)

    def _new_solver(self, config, remaining_seconds):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(remaining_seconds)
        solver.parameters.random_seed = config.random_seed
        solver.parameters.num_workers = config.num_workers
        return solver

    def _eligible(self, slot, candidate, require_duration):
        concept_ids = {c.concept_id for c in candidate.concept_weights}
        return (
            (not slot.allowed_kinds or candidate.exercise_kind in slot.allowed_kinds)
            and set(slot.required_concept_ids) <= concept_ids
            and set(slot.learning_outcome_ids) <= set(candidate.learning_outcome_ids)
            and (slot.difficulty is None or slot.difficulty == candidate.difficulty)
            and candidate.exercise_version_id not in slot.excluded_version_ids
            and candidate.exercise_family_id not in slot.excluded_family_ids
            and (not require_duration or candidate.estimated_seconds is not None)
        )

    def _build_model(self, blueprint, manifest, peer):
        model = cp_model.CpModel()
        candidates = {c.exercise_version_id: c for c in manifest.candidates}
        variables = {}
        require_duration = (
            blueprint.hard_constraints.max_duration_seconds is not None
            or blueprint.soft_objectives.target_duration_seconds is not None
            or blueprint.assembly_mode == "ab"
        )
        for slot in blueprint.slots:
            eligible = [c for c in manifest.candidates if self._eligible(slot, c, require_duration)]
            if not eligible:
                model.Add(0 == 1)
                continue
            slot_vars = []
            for candidate in eligible:
                var = model.NewBoolVar(f"x_{slot.code}_{candidate.exercise_version_id}")
                variables[(slot.code, candidate.exercise_version_id)] = var
                slot_vars.append(var)
            model.AddExactlyOne(slot_vars)

        by_stable = defaultdict(list)
        by_conflict_group = defaultdict(list)
        for (slot_code, version_id), var in variables.items():
            candidate = candidates[version_id]
            by_stable[candidate.stable_exercise_id].append(var)
            for group_id in candidate.conflict_group_ids:
                by_conflict_group[group_id].append(var)
        for group in list(by_stable.values()) + list(by_conflict_group.values()):
            if len(group) > 1:
                model.AddAtMostOne(group)

        for slot in blueprint.slots:
            for mandatory_id in slot.mandatory_version_ids:
                mandatory_vars = [v for (code, version_id), v in variables.items() if version_id == mandatory_id]
                if not mandatory_vars:
                    model.Add(0 == 1)
                else:
                    model.Add(sum(mandatory_vars) == 1)

        total_expr = sum(
            score_to_hundredths(slot.score) * sum(
                var for (code, _), var in variables.items() if code == slot.code
            ) for slot in blueprint.slots
        )
        model.Add(total_expr == score_to_hundredths(blueprint.total_score))
        for section_code, target in blueprint.hard_constraints.section_scores.items():
            section_expr = sum(
                score_to_hundredths(slot.score) * sum(
                    var for (code, _), var in variables.items() if code == slot.code
                ) for slot in blueprint.slots if slot.section_code == section_code
            )
            model.Add(section_expr == score_to_hundredths(target))

        if blueprint.hard_constraints.max_duration_seconds is not None:
            duration_expr = sum(
                candidates[version_id].estimated_seconds * var
                for (_, version_id), var in variables.items()
            )
            model.Add(duration_expr <= blueprint.hard_constraints.max_duration_seconds)

        for requirement in blueprint.hard_constraints.coverage_requirements:
            terms = []
            for (_, version_id), var in variables.items():
                candidate = candidates[version_id]
                if requirement.target_kind == "concept":
                    weight = sum(c.weight for c in candidate.concept_weights if c.concept_id == requirement.target_id and c.role == requirement.role)
                elif requirement.target_kind == "learning_outcome":
                    weight = int(requirement.target_id in candidate.learning_outcome_ids)
                else:
                    raise ValueError("unknown coverage target_kind")
                terms.append(weight * var)
            model.Add(sum(terms) >= requirement.min_weight)

        for requirement in blueprint.hard_constraints.kind_requirements:
            terms = [
                var for (slot_code, version_id), var in variables.items()
                if candidates[version_id].exercise_kind == requirement.exercise_kind
                and (requirement.section_code is None or slot_code in {
                    s.code for s in blueprint.slots if s.section_code == requirement.section_code
                })
            ]
            model.Add(sum(terms) >= requirement.min_count)
            if requirement.max_count is not None:
                model.Add(sum(terms) <= requirement.max_count)
        return model, variables, candidates

    def _extract_solution(self, variant, raw, blueprint, manifest, variables, candidates, solver):
        slots = {slot.code: slot for slot in blueprint.slots}
        selections = []
        for (slot_code, version_id), var in sorted(variables.items(), key=lambda item: item[0][0]):
            if solver.Value(var):
                candidate = candidates[version_id]
                slot = slots[slot_code]
                selections.append(SlotSelection(
                    slot_id=slot.slot_id, slot_code=slot.code,
                    exercise_version_id=version_id,
                    exercise_instance_id=candidate.exercise_instance_id,
                    stable_exercise_id=candidate.stable_exercise_id,
                    exercise_family_id=candidate.exercise_family_id,
                    source_release_id=candidate.source_release_id,
                    decision_seq=candidate.decision_seq,
                    score_hundredths=score_to_hundredths(slot.score),
                    estimated_seconds=candidate.estimated_seconds,
                    difficulty=candidate.difficulty,
                    dependency_hash=candidate.dependency_hash,
                    content_hash=candidate.content_hash,
                ))
        objective = int(round(solver.ObjectiveValue())) if solver.ResponseStats() else 0
        best_bound = int(math.ceil(solver.BestObjectiveBound())) if solver.ResponseStats() else 0
        gap = 0 if objective == 0 else max(0, (objective - best_bound) * 1000 // abs(objective))
        status = RunStatus.OPTIMAL if raw == cp_model.OPTIMAL else RunStatus.FEASIBLE
        body = dict(
            variant_label=variant, selections=tuple(selections), objective_terms=(),
            objective_value=objective, best_bound=best_bound, optimality_gap_milli=gap,
            solver_status=status, optimal_proven=status == RunStatus.OPTIMAL,
            structure_report={}, manifest_hash="",
        )
        body["manifest_hash"] = canonical_hash({**asdict(body), "manifest_hash": None})
        return SolutionManifest(**body)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/test_solver_hard.py -q`
预期：PASS；Decimal 精确缩放，重复题、必选、覆盖、题型、分节分和整数秒约束均由 CP-SAT 执行。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/solver.py tests/unit/assessment/test_solver_hard.py
git commit -m "feat: 用 CP-SAT 固化组卷硬约束"
```

### 任务 3：实现整数软目标、求解状态与最多 3 个不同方案

**文件：**
- 修改：`modules/assessment/assembly/contracts.py`
- 创建：`modules/assessment/assembly/objectives.py`
- 修改：`modules/assessment/assembly/solver.py`
- 修改：`tests/fixtures/assembly.py`
- 测试：`tests/unit/assessment/test_solver_objectives.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/test_solver_objectives.py
from dataclasses import replace
from ortools.sat.python import cp_model

from modules.assessment.assembly.contracts import RunStatus, SoftObjectives
from modules.assessment.assembly.solver import AssemblySolver, map_status
from tests.fixtures.assembly import make_blueprint, make_candidate, make_config, make_manifest


def test_integer_objectives_explain_choice():
    soft = SoftObjectives(
        target_duration_seconds=300, duration_weight=5,
        recent_use_weight=3, exposure_balance_weight=2,
        teacher_preference_weight=11,
    )
    blueprint = make_blueprint(soft_objectives=soft)
    good = make_candidate(1, preferred=True, recent_use_count=0, exposure_count=0)
    bad = make_candidate(2, preferred=False, recent_use_count=4, exposure_count=7)
    result = AssemblySolver().solve(blueprint, make_manifest(blueprint, [bad, good]), make_config())
    solution = result.solution_sets[0].variants[0]
    assert solution.selections[0].exercise_version_id == good.exercise_version_id
    assert {term.name for term in solution.objective_terms} == {
        "duration", "recent_use", "exposure_balance", "teacher_preference"
    }
    assert all(isinstance(term.penalty, int) for term in solution.objective_terms)


def test_three_solutions_are_distinct_and_fourth_is_never_returned():
    candidates = [make_candidate(n) for n in range(1, 6)]
    blueprint = make_blueprint()
    result = AssemblySolver().solve(
        blueprint, make_manifest(blueprint, candidates), make_config(max_solutions=3)
    )
    hashes = [s.manifest_hash for s in result.solution_sets]
    assert len(hashes) == len(set(hashes)) == 3


def test_status_never_promotes_feasible_or_timeout():
    blueprint = make_blueprint()
    feasible = AssemblySolver().solve(
        blueprint, make_manifest(blueprint), make_config(prove_optimality=False)
    )
    assert feasible.status == RunStatus.FEASIBLE
    assert feasible.solution_sets[0].variants[0].optimal_proven is False
    assert map_status(cp_model.OPTIMAL, 1, False) == RunStatus.OPTIMAL
    assert map_status(cp_model.FEASIBLE, 1, False) == RunStatus.FEASIBLE
    assert map_status(cp_model.FEASIBLE, 1, True) == RunStatus.TIMEOUT
    assert map_status(cp_model.INFEASIBLE, 0, False) == RunStatus.INFEASIBLE
    assert map_status(cp_model.UNKNOWN, 0, True) == RunStatus.TIMEOUT
    assert map_status(cp_model.UNKNOWN, 1, True) == RunStatus.TIMEOUT
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_solver_objectives.py -q`
预期：FAIL；`map_status` 与 `objectives` 不存在，当前求解器只返回一个空目标分解方案。

- [ ] **步骤 3：编写最少实现代码**

在 `SolverConfig` 增加 `prove_optimality: bool = True`，在 fixture 的默认配置中保留该默认值。`False` 时设置 `stop_after_first_solution=True`，用于明确产生未证明最优的 `FEASIBLE`。

```python
# modules/assessment/assembly/objectives.py
from collections import defaultdict
from dataclasses import asdict
from ortools.sat.python import cp_model

from modules.assessment.assembly.contracts import ObjectiveTerm


def _absolute(model, name, expression, upper):
    deviation = model.NewIntVar(0, upper, f"deviation_{name}")
    model.AddAbsEquality(deviation, expression)
    return deviation


def _histogram(model, terms, name, expressions, targets, weight, slot_count):
    for bucket, target_bp in sorted(targets.items()):
        actual = sum(expressions.get(bucket, []))
        deviation = _absolute(
            model, f"{name}_{bucket}", actual * 10000 - target_bp * slot_count,
            max(10000 * slot_count, target_bp * slot_count),
        )
        terms.append(dict(name=f"{name}_{bucket}", deviation=deviation,
                          penalty=weight * deviation,
                          raw={"selected_count": actual, "target_bp": target_bp}))


def add_soft_objectives(model, variables, candidates, blueprint, peer=None):
    soft = blueprint.soft_objectives
    terms = []
    if soft.target_duration_seconds is not None and soft.duration_weight:
        duration = sum(candidates[v].estimated_seconds * x for (_, v), x in variables.items())
        deviation = _absolute(model, "duration", duration - soft.target_duration_seconds, 10**8)
        terms.append(dict(name="duration", deviation=deviation,
                          penalty=soft.duration_weight * deviation,
                          raw={"seconds": duration, "target_seconds": soft.target_duration_seconds}))

    for name, values, targets, weight in (
        ("difficulty", {v: candidates[v].difficulty or 0 for (_, v) in variables},
         soft.difficulty_targets_bp, soft.difficulty_weight),
        ("cognitive", {v: candidates[v].cognitive_level or 0 for (_, v) in variables},
         soft.cognitive_targets_bp, soft.cognitive_weight),
    ):
        if targets and weight:
            expressions = defaultdict(list)
            for (slot_code, version_id), var in variables.items():
                expressions[values[version_id]].append(var)
            _histogram(model, terms, name, expressions, targets, weight, len(blueprint.slots))

    for name, weight, coefficient in (
        ("recent_use", soft.recent_use_weight, lambda c: c.recent_use_count),
        ("exposure_balance", soft.exposure_balance_weight, lambda c: c.exposure_count),
        ("teacher_preference", soft.teacher_preference_weight, lambda c: int(not c.preferred)),
    ):
        if weight:
            raw = sum(coefficient(candidates[v]) * x for (_, v), x in variables.items())
            terms.append(dict(name=name, deviation=raw, penalty=weight * raw, raw={"count": raw}))

    if peer is not None and soft.ab_structure_weight:
        peer_by_version = {s.exercise_version_id: s for s in peer.selections}
        peer_seconds = sum(s.estimated_seconds or 0 for s in peer.selections)
        current_seconds = sum(candidates[v].estimated_seconds * x for (_, v), x in variables.items())
        delta = _absolute(model, "ab_duration", current_seconds - peer_seconds, 10**8)
        terms.append(dict(name="ab_duration", deviation=delta,
                          penalty=soft.ab_structure_weight * delta,
                          raw={"peer_seconds": peer_seconds, "current_seconds": current_seconds}))
        for bucket in range(0, 6):
            peer_count = sum(
                1 for version_id in peer_by_version
                if (candidates[version_id].difficulty or 0) == bucket
            )
            current_count = sum(
                x for (_, v), x in variables.items() if (candidates[v].difficulty or 0) == bucket
            )
            delta = _absolute(model, f"ab_difficulty_{bucket}", current_count - peer_count,
                              len(blueprint.slots))
            terms.append(dict(name=f"ab_difficulty_{bucket}", deviation=delta,
                              penalty=soft.ab_structure_weight * delta,
                              raw={"peer_count": peer_count, "current_count": current_count}))
    model.Minimize(sum(term["penalty"] for term in terms))
    return terms


def read_objective_terms(terms, solver):
    result = []
    for term in terms:
        raw = {key: solver.Value(value) for key, value in term["raw"].items()}
        deviation = solver.Value(term["deviation"])
        result.append(ObjectiveTerm(
            name=term["name"], raw_value=raw, deviation=deviation,
            penalty=solver.Value(term["penalty"]),
        ))
    return tuple(result)
```

在 `solver.py` 增加状态映射和多方案循环。每次排除前一个完整选择向量，而不是逐题排除，因此仍允许其他方案复用该题；最多循环 3 次。

```python
# modules/assessment/assembly/solver.py 新增导入与函数
from dataclasses import asdict, replace
from modules.assessment.assembly.objectives import add_soft_objectives, read_objective_terms


def map_status(raw, solution_count, reached_limit):
    if raw == cp_model.OPTIMAL:
        return RunStatus.OPTIMAL
    if raw == cp_model.INFEASIBLE and solution_count == 0:
        return RunStatus.INFEASIBLE
    if raw == cp_model.MODEL_INVALID:
        return RunStatus.FAILED
    if reached_limit or raw == cp_model.UNKNOWN:
        return RunStatus.TIMEOUT
    if raw == cp_model.FEASIBLE:
        return RunStatus.FEASIBLE
    return RunStatus.FAILED
```

```python
# AssemblySolver.solve 的完整替换
from dataclasses import asdict, replace


def solve(self, blueprint, manifest, config, peer=None):
    solutions = []
    exclusions = []
    deadline = time.monotonic() + config.time_limit_seconds
    overall = RunStatus.FEASIBLE
    first_bound = None
    first_gap = None
    for index in range(config.max_solutions):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            overall = RunStatus.TIMEOUT
            break
        model, variables, candidates = self._build_model(blueprint, manifest, peer)
        terms = add_soft_objectives(model, variables, candidates, blueprint, peer)
        for previous in exclusions:
            model.Add(sum(variables[key] for key in previous) < len(previous))
        solver = self._new_solver(config, remaining)
        if not config.prove_optimality:
            solver.parameters.stop_after_first_solution = True
        raw = solver.Solve(model)
        reached_limit = time.monotonic() >= deadline
        if raw == cp_model.INFEASIBLE and solutions:
            break
        if raw not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            overall = map_status(raw, len(solutions), reached_limit)
            break
        variant = peer.variant_label if peer else "single"
        base = self._extract_solution(variant, raw, blueprint, manifest, variables, candidates, solver)
        if index > 0:
            base = replace(base, solver_status=RunStatus.FEASIBLE, optimal_proven=False)
        objective_terms = read_objective_terms(terms, solver)
        base = replace(base, objective_terms=objective_terms,
                       objective_value=sum(t.penalty for t in objective_terms))
        base = replace(base, manifest_hash=canonical_hash({
            **asdict(base), "manifest_hash": None
        }))
        solutions.append(base)
        exclusions.append([
            key for key, var in variables.items() if solver.Value(var)
        ])
        if index == 0:
            overall = map_status(raw, 1, reached_limit)
            first_bound, first_gap = base.best_bound, base.optimality_gap_milli
        if reached_limit:
            overall = RunStatus.TIMEOUT
            break
    sets = tuple(make_solution_set((solution,)) for solution in solutions)
    return SolveResult(overall, sets, {"returned_solutions": len(sets)}, first_bound, first_gap)
```

`_new_solver` 将剩余时限换算为 CP-SAT API 的秒值；业务配置、耗时上限和持久化仍只使用整数秒。`tests/fixtures/assembly.py` 的 `make_config` 通过 `values.update(changes)` 支持 `prove_optimality=False`。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/test_solver_hard.py tests/unit/assessment/test_solver_objectives.py -q`
预期：PASS；软目标罚分全为整数，3 个 manifest 互不重复，`FEASIBLE` 不显示最优，达到时限统一映射为 `TIMEOUT`。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/contracts.py modules/assessment/assembly/objectives.py modules/assessment/assembly/solver.py tests/fixtures/assembly.py tests/unit/assessment/test_solver_objectives.py
git commit -m "feat: 解释组卷软目标并返回最多三个方案"
```

### 任务 4：提供候选预检与不修改原蓝图的不可行诊断

**文件：**
- 创建：`modules/assessment/assembly/diagnostics.py`
- 测试：`tests/unit/assessment/test_diagnostics.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/test_diagnostics.py
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import UUID

from modules.assessment.assembly.contracts import CoverageRequirement, RunStatus, to_json_value
from modules.assessment.assembly.diagnostics import diagnose_infeasible, preflight
from tests.fixtures.assembly import make_blueprint, make_candidate, make_config, make_manifest


def test_empty_slot_is_reported_before_solver():
    blueprint = make_blueprint()
    report = preflight(blueprint, make_manifest(blueprint, [make_candidate(1, exercise_kind="proof")]))
    assert report["can_solve"] is False
    assert report["empty_slots"] == ["A1"]


def test_impossible_score_is_reported_before_solver():
    blueprint = make_blueprint(total_score=Decimal("99.99"))
    report = preflight(blueprint, make_manifest(blueprint))
    assert report["can_solve"] is False
    assert report["score_mismatches"] == ["total", "section:A"]


def test_diagnostic_probe_never_relaxes_original_run():
    requirement = CoverageRequirement(UUID(int=999), "concept", "primary", 1)
    hard = replace(make_blueprint().hard_constraints, coverage_requirements=(requirement,))
    original = make_blueprint(hard_constraints=hard)
    result = diagnose_infeasible(original, make_manifest(original), make_config())
    assert result["diagnostic_only"] is True
    assert result["original_hard_constraints"] == to_json_value(asdict(hard))
    assert result["probes"][0]["constraint_group"] == "coverage"
    assert result["probes"][0]["feasible_without_group"] is True
    assert "solution_sets" not in result
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_diagnostics.py -q`
预期：FAIL，报错 `ModuleNotFoundError: modules.assessment.assembly.diagnostics`。

- [ ] **步骤 3：编写最少实现代码**

```python
# modules/assessment/assembly/diagnostics.py
from dataclasses import asdict, replace
from modules.assessment.assembly.contracts import RunStatus, canonical_hash, score_to_hundredths, to_json_value
from modules.assessment.assembly.solver import AssemblySolver


def preflight(blueprint, manifest):
    solver = AssemblySolver()
    require_duration = (
        blueprint.hard_constraints.max_duration_seconds is not None
        or blueprint.soft_objectives.target_duration_seconds is not None
        or blueprint.assembly_mode == "ab"
    )
    slot_counts = {
        slot.code: sum(solver._eligible(slot, c, require_duration) for c in manifest.candidates)
        for slot in blueprint.slots
    }
    empty = [code for code, count in slot_counts.items() if count == 0]
    mandatory_ids = {v for s in blueprint.slots for v in s.mandatory_version_ids}
    candidate_by_id = {c.exercise_version_id: c for c in manifest.candidates}
    missing_mandatory = sorted(str(v) for v in mandatory_ids - set(candidate_by_id))
    mandatory_stable = [candidate_by_id[v].stable_exercise_id for v in mandatory_ids if v in candidate_by_id]
    mandatory_conflicts = len(mandatory_stable) != len(set(mandatory_stable))
    score_mismatches = []
    actual_total = sum(score_to_hundredths(s.score) for s in blueprint.slots)
    if actual_total != score_to_hundredths(blueprint.total_score):
        score_mismatches.append("total")
    for section, target in blueprint.hard_constraints.section_scores.items():
        actual = sum(score_to_hundredths(s.score) for s in blueprint.slots if s.section_code == section)
        if actual != score_to_hundredths(target):
            score_mismatches.append(f"section:{section}")
    coverage_shortages = []
    for requirement in blueprint.hard_constraints.coverage_requirements:
        if requirement.target_kind == "concept":
            available = sum(
                max(0, sum(c.weight for c in candidate.concept_weights
                           if c.concept_id == requirement.target_id and c.role == requirement.role))
                for candidate in manifest.candidates
            )
        else:
            available = sum(requirement.target_id in c.learning_outcome_ids for c in manifest.candidates)
        if available < requirement.min_weight:
            coverage_shortages.append(str(requirement.target_id))
    blocked = empty or missing_mandatory or mandatory_conflicts or score_mismatches or coverage_shortages
    return {
        "can_solve": not blocked,
        "slot_candidate_counts": slot_counts,
        "empty_slots": empty,
        "missing_mandatory_version_ids": missing_mandatory,
        "mandatory_stable_item_conflict": mandatory_conflicts,
        "score_mismatches": score_mismatches,
        "coverage_shortages": coverage_shortages,
        "candidate_manifest_hash": manifest.manifest_hash,
        "suggestions": (["增加空槽位的合格候选"] if empty else [])
                       + (["补充必选题或修改必选清单"] if missing_mandatory or mandatory_conflicts else [])
                       + (["调整槽位或分节分值"] if score_mismatches else [])
                       + (["增加覆盖目标所需的候选"] if coverage_shortages else []),
    }


def diagnose_infeasible(blueprint, manifest, config):
    hard = blueprint.hard_constraints
    probes = (
        ("coverage", replace(hard, coverage_requirements=())),
        ("kind", replace(hard, kind_requirements=())),
        ("duration", replace(hard, max_duration_seconds=None)),
    )
    reports = []
    for name, copied_hard in probes:
        diagnostic_blueprint = replace(blueprint, hard_constraints=copied_hard)
        probe = AssemblySolver().solve(
            diagnostic_blueprint, manifest, replace(config, max_solutions=1)
        )
        reports.append({
            "constraint_group": name,
            "feasible_without_group": bool(probe.solution_sets),
            "status": probe.status.value,
            "message": f"仅在诊断副本移除 {name}；原蓝图未修改",
        })
    extra_probes = (
        ("mandatory", replace(blueprint, slots=tuple(
            replace(s, mandatory_version_ids=()) for s in blueprint.slots
        )), manifest),
        ("mutual_exclusion", blueprint, replace(manifest, candidates=tuple(
            replace(c, conflict_group_ids=()) for c in manifest.candidates
        ))),
    )
    for name, copied_blueprint, copied_manifest in extra_probes:
        probe = AssemblySolver().solve(copied_blueprint, copied_manifest, replace(config, max_solutions=1))
        reports.append({
            "constraint_group": name,
            "feasible_without_group": bool(probe.solution_sets),
            "status": probe.status.value,
            "message": f"仅在诊断副本移除 {name}；原蓝图未修改",
        })
    return {
        "diagnostic_only": True,
        "original_blueprint_hash": canonical_hash(asdict(blueprint)),
        "original_hard_constraints": to_json_value(asdict(hard)),
        "probes": reports,
        "suggestions": ["增加候选", "调整槽位概念范围", "修改题数或分值", "由教师显式修改互斥要求"],
    }
```

`AssemblyService` 只在原求解结果为 `INFEASIBLE` 时调用 `diagnose_infeasible`，并且只保存其诊断字典；probe 的 `solution_sets` 从不写入 `assembly_run.solution_manifest`，也不传给 `PaperService`。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/test_diagnostics.py -q`
预期：PASS；空槽在求解前报告，诊断副本可定位覆盖冲突，原硬约束保持原值且诊断不携带可落卷方案。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/diagnostics.py tests/unit/assessment/test_diagnostics.py
git commit -m "feat: 输出组卷预检与不可行诊断"
```

### 任务 5：独立整卷复验后才创建固定卷草稿

**文件：**
- 创建：`modules/assessment/assembly/validation.py`
- 创建：`modules/assessment/assembly/materialize.py`
- 测试：`tests/integration/assessment/test_materialize.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/integration/assessment/test_materialize.py
from dataclasses import replace
from uuid import UUID

import pytest
from modules.assessment.assembly.contracts import (
    CreatePaperDraftRequest, PaperDraft, RunStatus, ValidationIssue, ValidationReport,
)
from modules.assessment.assembly.materialize import SolutionValidationError, create_papers
from modules.assessment.assembly.solver import AssemblySolver
from tests.fixtures.assembly import make_blueprint, make_candidate, make_config, make_manifest


def test_tampered_solution_is_rejected_without_creating_paper():
    blueprint = make_blueprint()
    manifest = make_manifest(blueprint, [make_candidate(1)])
    result = AssemblySolver().solve(blueprint, manifest, make_config())
    solution = result.solution_sets[0].variants[0]
    tampered = replace(solution, manifest_hash="0" * 64)
    paper_service = FakePaperService()
    with pytest.raises(SolutionValidationError):
        create_papers(
            run=FakeRun(result.status), solution_set=replace(result.solution_sets[0], variants=(tampered,)),
            selected_solution_index=0, blueprint=blueprint, candidate_manifest=manifest,
            idempotency_key="select-test",
            paper_service=paper_service, validator=RejectingValidator(), actor_id=UUID(int=88),
        )
    assert paper_service.requests == []


def test_timeout_with_valid_solution_may_create_draft_without_optimality_claim():
    blueprint = make_blueprint()
    manifest = make_manifest(blueprint, [make_candidate(1)])
    result = AssemblySolver().solve(blueprint, manifest, make_config(prove_optimality=False))
    solution = replace(result.solution_sets[0].variants[0], solver_status=RunStatus.TIMEOUT)
    paper_service = FakePaperService()
    drafts = create_papers(
        run=FakeRun(RunStatus.TIMEOUT), solution_set=replace(result.solution_sets[0], variants=(solution,)),
        selected_solution_index=0, blueprint=blueprint, candidate_manifest=manifest,
        idempotency_key="select-test",
        paper_service=paper_service, validator=PassingValidator(solution.manifest_hash), actor_id=UUID(int=88),
    )
    assert len(drafts) == 1
    assert drafts[0].status == "draft"


class PassingValidator:
    def __init__(self, manifest_hash):
        self.manifest_hash = manifest_hash

    def validate(self, **kwargs):
        return ValidationReport(True, (), self.manifest_hash)


class RejectingValidator:
    def validate(self, **kwargs):
        return ValidationReport(False, (ValidationIssue("manifest_mismatch", "哈希不匹配", None),), "0" * 64)


class FakePaperService:
    def __init__(self):
        self.requests = []

    def create_draft(self, request: CreatePaperDraftRequest):
        self.requests.append(request)
        return PaperDraft(UUID(int=len(self.requests)), request.solution.manifest_hash, "draft")


class FakeRun:
    id = UUID(int=500)

    def __init__(self, status):
        self.status = status
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_materialize.py -q`
预期：FAIL，报错 `ModuleNotFoundError: modules.assessment.assembly.materialize`。

- [ ] **步骤 3：编写最少实现代码**

```python
# modules/assessment/assembly/validation.py
from modules.assessment.assembly.contracts import ValidationReport


class SolutionValidationError(ValueError):
    pass


def validate_solution_set(validator, blueprint, candidate_manifest, solution_set):
    reports = []
    peer = solution_set.variants[0] if len(solution_set.variants) == 2 else None
    for index, solution in enumerate(solution_set.variants):
        report = validator.validate(
            blueprint=blueprint, candidate_manifest=candidate_manifest,
            solution=solution, peer=peer if index == 1 else None,
        )
        if not report.valid or report.manifest_hash != solution.manifest_hash:
            raise SolutionValidationError(
                f"{solution.variant_label}: " + ",".join(i.code for i in report.issues)
            )
        reports.append(report)
    return tuple(reports)
```

```python
# modules/assessment/assembly/materialize.py
from modules.assessment.assembly.contracts import CreatePaperDraftRequest, RunStatus, new_uuid7
from modules.assessment.assembly.validation import SolutionValidationError, validate_solution_set


def create_papers(*, run, solution_set, selected_solution_index, blueprint, candidate_manifest,
                  idempotency_key, paper_service, validator, actor_id):
    if run.status not in {RunStatus.OPTIMAL, RunStatus.FEASIBLE, RunStatus.TIMEOUT}:
        raise SolutionValidationError(f"{run.status} cannot create paper")
    if run.status == RunStatus.TIMEOUT and not solution_set.variants:
        raise SolutionValidationError("timeout without feasible solution cannot create paper")
    validate_solution_set(validator, blueprint, candidate_manifest, solution_set)
    variant_group_id = new_uuid7() if len(solution_set.variants) == 2 else None
    drafts = []
    for solution in solution_set.variants:
        drafts.append(paper_service.create_draft(CreatePaperDraftRequest(
            blueprint_version_id=blueprint.blueprint_version_id,
            assembly_run_id=run.id, selected_solution_index=selected_solution_index,
            actor_id=actor_id, idempotency_key=f"{idempotency_key}:{solution.variant_label}",
            solution=solution,
            variant_group_id=variant_group_id, variant_label=solution.variant_label,
        )))
    return tuple(drafts)
```

`selected_solution_index` 由 `AssemblyService.select` 显式传入，并写入新 paper_version 的既有字段。`PaperService.create_draft` 以 `actor_id + idempotency_key` 返回既有草稿，A/B 的 key 分别带 `:A` 与 `:B` 后缀；它直接消费保存的 `SolutionManifest`，逐槽写入既有 paper/section/slot，不重新抽题。两个 A/B 草稿使用同一 `variant_group_id`，但拥有不同 paper_version、manifest 和审核链。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/integration/assessment/test_materialize.py -q`
预期：PASS；独立校验拒绝哈希篡改，`PaperService.create_draft` 未被调用，未产生草稿。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/validation.py modules/assessment/assembly/materialize.py tests/integration/assessment/test_materialize.py
git commit -m "feat: 组卷方案通过独立整卷复验后落草稿"
```

### 任务 6：贯通 AssemblyService、API 与幂等 worker

**文件：**
- 创建：`modules/assessment/assembly/service.py`
- 创建：`apps/api/routers/assembly_runs.py`
- 修改：`apps/api/main.py`
- 创建：`apps/worker/assembly.py`
- 测试：`tests/integration/assessment/test_assembly_api_worker.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/integration/assessment/test_assembly_api_worker.py
from uuid import UUID

from modules.assessment.assembly.contracts import RunStatus, SolveResult
from modules.assessment.assembly.service import AssemblyService
from apps.worker.assembly import execute_assembly


def test_start_and_worker_are_idempotent(assembly_context):
    context = assembly_context
    first = context.service.start(UUID(int=100), UUID(int=88), "same-key")
    second = context.service.start(UUID(int=100), UUID(int=88), "same-key")
    assert first.assembly_run_id == second.assembly_run_id
    assert second.reused is True
    execute_assembly(context.session, first.assembly_run_id, context.solver, context.validator, context.paper_service)
    saved_hash = context.run(first.assembly_run_id).solution_manifest[0]["manifest_hash"]
    execute_assembly(context.session, first.assembly_run_id, context.solver, context.validator, context.paper_service)
    assert context.solver.calls == 1
    assert context.run(first.assembly_run_id).solution_manifest[0]["manifest_hash"] == saved_hash


def test_timeout_without_solution_cannot_create_paper(assembly_context, client):
    context = assembly_context
    context.solver.result = SolveResult(RunStatus.TIMEOUT, (), {"reason": "time_limit"}, None, None)
    run_id = context.service.start(UUID(int=100), UUID(int=88), "timeout-key").assembly_run_id
    execute_assembly(context.session, run_id, context.solver, context.validator, context.paper_service)
    response = client.post(
        f"/assembly-runs/{run_id}/select", json={"solution_index": 0},
        headers={"Idempotency-Key": "select-timeout"},
    )
    assert response.status_code == 409
    assert context.paper_service.requests == []
    assert client.get(f"/assembly-runs/{run_id}").json()["solution_manifest"] == []
    assert context.solver.calls == 1
```

`assembly_context` 是集成测试夹具：使用真实 PostgreSQL session、阶段 A 内存 `PaperService` 适配器、通过校验的 `PaperValidationService` fake，以及记录 `calls` 的求解器 fake。它在每个用例前登记 `app.state.assembly_service`，确保 GET 只读取运行行。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_assembly_api_worker.py -q`
预期：FAIL；`AssemblyService`、路由和 worker 模块不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
# modules/assessment/assembly/service.py
import json
from dataclasses import asdict
from importlib.metadata import version

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from modules.assessment.assembly.candidate import CandidateService
from modules.assessment.assembly.contracts import (
    AssemblyStartResult, CandidateManifest, RunStatus, SolutionSet, SolverConfig, new_uuid7, to_json_value,
)
from modules.assessment.assembly.diagnostics import preflight
from modules.assessment.assembly.materialize import create_papers
from modules.assessment.models.assembly import AssemblyRun


class AssemblyService:
    def __init__(self, session, paper_service, eligibility, exposure):
        self.session = session
        self.paper_service = paper_service
        self.eligibility = eligibility
        self.exposure = exposure

    def start(self, blueprint_version_id, actor_id, idempotency_key):
        existing = self.session.scalar(select(AssemblyRun).where(
            AssemblyRun.actor_id == actor_id,
            AssemblyRun.idempotency_key == idempotency_key,
        ))
        if existing:
            return AssemblyStartResult(existing.id, RunStatus(existing.status), True)
        blueprint = self.paper_service.blueprint_snapshot(blueprint_version_id)
        manifest = CandidateService(self.eligibility, self.exposure, blueprint).snapshot(
            actor_id, blueprint_version_id, blueprint.purpose, blueprint.offering_id)
        config = SolverConfig()
        report = preflight(blueprint, manifest)
        run = AssemblyRun(
            id=new_uuid7(), blueprint_version_id=blueprint_version_id, actor_id=actor_id,
            idempotency_key=idempotency_key, candidate_manifest=to_json_value(asdict(manifest)),
            candidate_manifest_hash=manifest.manifest_hash, solver_version=version("ortools"),
            solver_config=asdict(config),
            status=RunStatus.QUEUED if report["can_solve"] else RunStatus.INFEASIBLE,
            solution_manifest=[], diagnostics=report, job_id=None,
        )
        if report["can_solve"]:
            run.job_id = new_uuid7()
        self.session.add(run)
        try:
            if run.job_id:
                self.session.execute(text("""
                    INSERT INTO ops.job (id, job_type, payload, status)
                    VALUES (:id, 'assembly.run', CAST(:payload AS jsonb), 'queued')
                """), {"id": run.job_id, "payload": json.dumps({"assembly_run_id": str(run.id)})})
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            reused = self.session.scalar(select(AssemblyRun).where(
                AssemblyRun.actor_id == actor_id,
                AssemblyRun.idempotency_key == idempotency_key,
            ))
            return AssemblyStartResult(reused.id, RunStatus(reused.status), True)
        return AssemblyStartResult(run.id, RunStatus(run.status), False)

    def get(self, run_id, actor_id):
        run = self.session.get(AssemblyRun, run_id)
        if run is None or run.actor_id != actor_id:
            raise LookupError("assembly run not found")
        return run

    def select(self, run_id, solution_index, actor_id, idempotency_key, paper_service, validator):
        run = self.get(run_id, actor_id)
        sets = TypeAdapter(list[SolutionSet]).validate_python(run.solution_manifest)
        if not sets or not 0 <= solution_index < len(sets):
            raise ValueError("no selectable feasible solution")
        blueprint = self.paper_service.blueprint_snapshot(run.blueprint_version_id)
        manifest = TypeAdapter(CandidateManifest).validate_python(run.candidate_manifest)
        return create_papers(
            run=run, solution_set=sets[solution_index], selected_solution_index=solution_index,
            blueprint=blueprint, idempotency_key=idempotency_key,
            candidate_manifest=manifest, paper_service=paper_service,
            validator=validator, actor_id=actor_id,
        )
```

```python
# apps/worker/assembly.py
from dataclasses import asdict
from datetime import UTC, datetime

from pydantic import TypeAdapter
from sqlalchemy import select

from modules.assessment.assembly.contracts import CandidateManifest, RunStatus, SolverConfig, to_json_value
from modules.assessment.assembly.diagnostics import diagnose_infeasible
from modules.assessment.assembly.validation import validate_solution_set
from modules.assessment.models.assembly import AssemblyRun


def execute_assembly(session, run_id, solver, validator, paper_service):
    run = session.scalar(select(AssemblyRun).where(AssemblyRun.id == run_id).with_for_update())
    if run.status != RunStatus.QUEUED:
        return run
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(UTC)
    session.commit()
    blueprint = paper_service.blueprint_snapshot(run.blueprint_version_id)
    manifest = TypeAdapter(CandidateManifest).validate_python(run.candidate_manifest)
    try:
        result = solver.solve(blueprint, manifest, TypeAdapter(SolverConfig).validate_python(run.solver_config))
        for solution_set in result.solution_sets:
            validate_solution_set(validator, blueprint, manifest, solution_set)
        if result.status == RunStatus.INFEASIBLE:
            result.diagnostics.update(diagnose_infeasible(blueprint, manifest, SolverConfig()))
        run.status = result.status
        run.solution_manifest = to_json_value([asdict(s) for s in result.solution_sets])
        run.objective_breakdown = to_json_value({
            str(i): [asdict(t) for v in s.variants for t in v.objective_terms]
            for i, s in enumerate(result.solution_sets)
        })
        run.diagnostics = to_json_value(result.diagnostics)
        run.best_bound = result.best_bound
        run.optimality_gap_milli = result.optimality_gap_milli
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.solution_manifest = []
        run.diagnostics = {"reason": exc.__class__.__name__, "message": str(exc)}
    run.completed_at = datetime.now(UTC)
    session.commit()
    return run
```

Celery 入口只在 `ops.job` 投递后调用 `execute_assembly`；入口使用同一 `assembly_run_id`。`RUNNING` 或终态重入直接返回，首次提交后才执行求解，因此重复 delivery 不重复产生方案或 paper。选择方案始终是独立 API 动作，worker 不创建 paper。

```python
# apps/api/routers/assembly_runs.py
from uuid import UUID
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from modules.assessment.errors import AssessmentError

router = APIRouter(prefix="/api/v1/assembly-runs", tags=["assembly"])


class StartRunBody(BaseModel):
    blueprint_version_id: UUID


class SelectSolutionBody(BaseModel):
    solution_index: int = Field(ge=0, le=2)


@router.post("", status_code=202)
def start(body: StartRunBody, request: Request,
          idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
    result = request.app.state.assembly_service.start(
        body.blueprint_version_id, request.state.actor_id, idempotency_key
    )
    return {"assembly_run_id": result.assembly_run_id, "status": result.status, "reused": result.reused}


@router.get("/{run_id}")
def get_run(run_id: UUID, request: Request):
    try:
        run = request.app.state.assembly_service.get(run_id, request.state.actor_id)
    except LookupError as exc:
        raise AssessmentError(404, "resource_not_found", "组卷运行不存在或无权访问") from exc
    diagnostics = dict(run.diagnostics)
    diagnostics["messages"] = list(diagnostics.get("messages", [])) + [
        *(f"{code} 无合格候选" for code in diagnostics.get("empty_slots", [])),
        *(f"覆盖不足：{target_id}" for target_id in diagnostics.get("coverage_shortages", [])),
        *(f"分值不可达：{code}" for code in diagnostics.get("score_mismatches", [])),
    ]
    return {
        "id": run.id, "status": run.status,
        "candidate_manifest_hash": run.candidate_manifest_hash,
        "solver_version": run.solver_version, "solver_config": run.solver_config,
        "solution_manifest": run.solution_manifest,
        "objective_breakdown": run.objective_breakdown,
        "diagnostics": diagnostics, "best_bound": run.best_bound,
        "optimality_gap_milli": run.optimality_gap_milli,
    }


@router.post("/{run_id}/select")
def select(run_id: UUID, body: SelectSolutionBody, request: Request,
           idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
    try:
        drafts = request.app.state.assembly_service.select(
            run_id, body.solution_index, request.state.actor_id, idempotency_key,
            request.app.state.paper_service, request.app.state.paper_validator,
        )
    except LookupError as exc:
        raise AssessmentError(404, "resource_not_found", "组卷运行不存在或无权访问") from exc
    except ValueError as exc:
        raise AssessmentError(409, "assembly_not_selectable", str(exc)) from exc
    return {"papers": [{"paper_version_id": p.paper_version_id, "status": p.status} for p in drafts]}
```

GET 不实例化也不调用 `AssemblySolver`，回放数据完全来自保存的 manifest。`FAILED`、`INFEASIBLE`、空 `TIMEOUT` 都没有可选索引，统一返回 409。在 `apps/api/main.py` 注册路由，并复用阶段 A 的教师身份与课程范围中间件：

```python
from apps.api.routers import assembly_runs
app.include_router(assembly_runs.router)
```

`request.state.actor_id` 只能由该中间件写入；`get` 与 `select` 再校验运行所有者，避免跨教师读取或选择方案。写路由调用共享 `parse_idempotency_key`，错误统一由 `AssessmentError` handler 返回 `error.code/message/details`，不再使用字符串 detail。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/integration/assessment/test_assembly_api_worker.py -q`
预期：PASS；幂等键复用同一运行，worker 重入不重新求解，GET 回放不调用求解器，空超时不能创建 paper。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/service.py apps/api/routers/assembly_runs.py apps/api/main.py apps/worker/assembly.py tests/integration/assessment/test_assembly_api_worker.py
git commit -m "feat: 贯通幂等组卷运行与方案选择接口"
```

### 任务 7：生成共享快照、显式锚题且独立落卷的 A/B 方案

**文件：**
- 修改：`modules/assessment/assembly/solver.py`
- 复用：`modules/assessment/models/paper.py`
- 测试：`tests/integration/assessment/test_ab_assembly.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/integration/assessment/test_ab_assembly.py
from uuid import UUID

from modules.assessment.assembly.contracts import RunStatus
from modules.assessment.assembly.solver import AssemblySolver
from tests.fixtures.assembly import make_blueprint, make_candidate, make_config, make_manifest, make_slot


def test_ab_only_shares_explicit_anchor():
    slots = (make_slot(1), make_slot(2))
    anchor = make_candidate(1)
    a_only = make_candidate(2)
    b_only = make_candidate(3)
    blueprint = make_blueprint(
        slots=slots, assembly_mode="ab", anchor_version_ids=(anchor.exercise_version_id,)
    )
    result = AssemblySolver().solve(
        blueprint, make_manifest(blueprint, [anchor, a_only, b_only]), make_config()
    )
    assert result.status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE, RunStatus.TIMEOUT}
    pair = result.solution_sets[0].variants
    assert [s.variant_label for s in pair] == ["A", "B"]
    a_ids = {s.exercise_version_id for s in pair[0].selections}
    b_ids = {s.exercise_version_id for s in pair[1].selections}
    assert a_ids & b_ids <= set(blueprint.anchor_version_ids)
    assert pair[1].structure_report["non_anchor_overlap"] == 0
    assert {"section_delta", "kind_delta", "concept_delta", "difficulty_delta",
            "cognitive_delta", "duration_delta", "total_score_delta"} <= set(pair[1].structure_report)


def test_ab_infeasible_has_no_partial_paper_set():
    blueprint = make_blueprint(slots=(make_slot(1), make_slot(2)), assembly_mode="ab")
    result = AssemblySolver().solve(
        blueprint, make_manifest(blueprint, [make_candidate(1)]), make_config()
    )
    assert result.status == RunStatus.INFEASIBLE
    assert result.solution_sets == ()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_ab_assembly.py -q`
预期：FAIL；当前结果只有 `single`，且 peer 尚未施加非锚题去重。

- [ ] **步骤 3：编写最少实现代码**

复用 `modules/assessment/models/paper.py` 已有的 `variant_group_id`、`variant_label`、`assembly_run_id` 和 `selected_solution_index`，不新增 A/B 表。将任务 3 的 `solve` 改名为 `_solve_variant`，增加 `variant_label` 参数，并用以下入口组合 A/B；单卷路径仍调用同一个求解实现。

```python
# modules/assessment/assembly/solver.py
from dataclasses import asdict, replace


def solve(self, blueprint, manifest, config, peer=None):
    if blueprint.assembly_mode != "ab":
        return self._solve_variant(blueprint, manifest, config, peer, "single")
    a = self._solve_variant(blueprint, manifest, config, None, "A")
    if not a.solution_sets:
        return a
    pairs = []
    b_status = RunStatus.INFEASIBLE
    for a_set in a.solution_sets:
        b = self._solve_variant(blueprint, manifest, config, a_set.variants[0], "B")
        b_status = b.status
        if not b.solution_sets:
            continue
        b_solution = b.solution_sets[0].variants[0]
        overlap = self._non_anchor_overlap(a_set.variants[0], b_solution, blueprint.anchor_version_ids)
        b_solution = replace(b_solution, structure_report={
            **self._ab_structure_report(a_set.variants[0], b_solution, manifest, blueprint),
            "non_anchor_overlap": overlap,
            "statistical_equivalence_claimed": 0,
        })
        b_solution = replace(b_solution, manifest_hash=canonical_hash({
            **asdict(b_solution), "manifest_hash": None
        }))
        pairs.append(make_solution_set((a_set.variants[0], b_solution)))
    status = RunStatus.TIMEOUT if RunStatus.TIMEOUT in {a.status, b_status} else (
        a.status if pairs else RunStatus.INFEASIBLE
    )
    return SolveResult(status, tuple(pairs[:3]), a.diagnostics, a.best_bound, a.optimality_gap_milli)


def _non_anchor_overlap(self, a, b, anchors):
    anchor_set = set(anchors)
    a_items = {s.exercise_version_id: s for s in a.selections}
    overlap = 0
    for selected in b.selections:
        if selected.exercise_version_id in anchor_set:
            continue
        for previous in a_items.values():
            if selected.stable_exercise_id == previous.stable_exercise_id \
                    or selected.exercise_family_id == previous.exercise_family_id:
                overlap += 1
    return overlap


def _ab_structure_report(self, a, b, manifest, blueprint):
    from collections import Counter
    candidates = {c.exercise_version_id: c for c in manifest.candidates}
    slots = {s.code: s for s in blueprint.slots}

    def stats(solution):
        result = {"section": Counter(), "kind": Counter(), "concept": Counter(),
                  "difficulty": Counter(), "cognitive": Counter()}
        seconds = 0
        score = 0
        for selected in solution.selections:
            candidate = candidates[selected.exercise_version_id]
            result["section"][slots[selected.slot_code].section_code] += 1
            result["kind"][candidate.exercise_kind] += 1
            result["difficulty"][candidate.difficulty or 0] += 1
            result["cognitive"][candidate.cognitive_level or 0] += 1
            for concept in candidate.concept_weights:
                result["concept"][str(concept.concept_id)] += concept.weight
            seconds += selected.estimated_seconds or 0
            score += selected.score_hundredths
        return result, seconds, score

    a_stats, a_seconds, a_score = stats(a)
    b_stats, b_seconds, b_score = stats(b)
    report = {}
    for name in a_stats:
        keys = set(a_stats[name]) | set(b_stats[name])
        report[f"{name}_delta"] = sum(abs(a_stats[name][k] - b_stats[name][k]) for k in keys)
    report["duration_delta"] = abs(a_seconds - b_seconds)
    report["total_score_delta"] = abs(a_score - b_score)
    return report
```

在 `_build_model` 返回前加入 B 卷排除约束。锚题按显式 exercise_version 放行，其余稳定题与同族题均禁止重复。

```python
if peer is not None:
    anchors = set(blueprint.anchor_version_ids)
    peer_stable = {s.stable_exercise_id for s in peer.selections
                   if s.exercise_version_id not in anchors}
    peer_families = {s.exercise_family_id for s in peer.selections
                     if s.exercise_version_id not in anchors}
    for (_, version_id), var in variables.items():
        candidate = candidates[version_id]
        if version_id not in anchors and (
            candidate.stable_exercise_id in peer_stable
            or candidate.exercise_family_id in peer_families
        ):
            model.Add(var == 0)
```

将 `_solve_variant` 内的 `variant = peer.variant_label if peer else "single"` 改为使用传入的 `variant_label`。A/B 每对只占一个方案序号；`SolutionSet.variants=(A, B)`，worker 对两者分别调用 `PaperValidationService.validate`，任务 5 的落卷门禁分别创建两个草稿。界面只显示「结构匹配的 A/B 卷」。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/integration/assessment/test_ab_assembly.py tests/integration/assessment/test_materialize.py -q`
预期：PASS；除锚题外稳定题与同族题不重叠，A/B 各自有 manifest，B 不可行时整对不返回、不创建半套 paper。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assembly/solver.py tests/integration/assessment/test_ab_assembly.py
git commit -m "feat: 生成显式锚题的 AB 固定卷方案"
```

### 任务 8：复用阶段 A 曝光事件并阻断候选与发布冲突

**文件：**
- 创建：`modules/assessment/exposure.py`
- 修改：`modules/assessment/assignment.py`
- 复用：`modules/assessment/models/exposure.py`
- 测试：`tests/integration/assessment/test_exposure.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/integration/assessment/test_exposure.py
from datetime import UTC, datetime
from uuid import UUID

import pytest
from modules.assessment.assembly.contracts import ExposureConflictError
from modules.assessment.exposure import ExposureService
from modules.assessment.models.exposure import ItemExposureEvent
from tests.fixtures.assembly import make_candidate


def test_practice_answer_release_blocks_formal_candidate(db_session):
    candidate = make_candidate(1)
    db_session.add(ItemExposureEvent(
        exercise_version_id=candidate.exercise_version_id,
        exercise_instance_id=None, exercise_family_id=candidate.exercise_family_id,
        paper_version_id=None, assignment_id=None, scope_offering_id=UUID(int=102),
        event_kind="practice_answer_released", visible_from=datetime(2026, 11, 1, tzinfo=UTC),
        occurred_at=datetime(2026, 11, 1, tzinfo=UTC), metadata_hash="a" * 64,
    ))
    db_session.commit()
    with pytest.raises(ExposureConflictError, match="practice_answer_released"):
        ExposureService(db_session).assert_no_conflict(
            purpose="formal", scope_offering_id=UUID(int=102),
            visible_from=datetime(2026, 11, 5, tzinfo=UTC), candidate=candidate,
        )


def test_assignment_publish_checks_and_records_in_one_transaction(assignment_context):
    context = assignment_context
    context.assignment_service.publish(context.session, assignment_id=context.assignment_id,
        teacher_id=UUID(int=88), base_version=1, idempotency_key="publish-once", now=context.now)
    context.assignment_service.publish(context.session, assignment_id=context.assignment_id,
        teacher_id=UUID(int=88), base_version=1, idempotency_key="publish-once", now=context.now)
    assert context.exposure.secure_event_count(context.assignment_id) == 1
    context.exposure.insert_practice_release_for_assignment_paper(context.assignment_id)
    with pytest.raises(ExposureConflictError):
        context.assignment_service.publish(context.session, assignment_id=context.other_assignment_id,
            teacher_id=UUID(int=88), base_version=1, idempotency_key="publish-twice", now=context.now)
    assert context.exposure.secure_event_count(context.other_assignment_id) == 0
```

`assignment_context` 使用阶段 A 的真实 `AssignmentService`、已审核 paper 和 assignment fixture，并提供 session、now、actor_id、purpose、offering_id；测试只在既有发布入口注入冲突事件，不调用新建的阶段 B 发布接口。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_exposure.py -q`
预期：FAIL；`modules.assessment.exposure` 不存在，发布事务尚未执行曝光终检。

- [ ] **步骤 3：编写最少实现代码**

不执行任何 exposure DDL。`ItemExposureEvent` 的字段、约束和迁移归阶段 A 所有；INSERT 省略 `id`，复用阶段 A 数据库默认的 UUIDv7 生成器。

```python
# modules/assessment/exposure.py
from sqlalchemy import text
from modules.assessment.assembly.contracts import ExposureConflictError


class ExposureService:
    def __init__(self, session):
        self.session = session

    def assert_no_conflict(self, *, purpose, scope_offering_id, visible_from,
                           candidate=None, paper_version_id=None, assignment_id=None):
        if (candidate is None) == (paper_version_id is None):
            raise ValueError("provide exactly one candidate or paper_version_id")
        if purpose != "formal":
            return
        if candidate is not None:
            rows = self.session.execute(text("""
                SELECT event_kind, scope_offering_id, visible_from
                FROM assessment.item_exposure_event
                WHERE exercise_family_id = :family_id
                  AND event_kind IN ('practice_answer_released', 'formal_answer_released', 'secure_assigned', 'student_opened')
            """), {"family_id": candidate.exercise_family_id}).mappings()
        else:
            rows = self.session.execute(text("""
                SELECT e.event_kind, e.scope_offering_id, e.visible_from,
                       event_paper.variant_group_id AS event_variant_group_id,
                       target.variant_group_id AS target_variant_group_id
                FROM assessment.paper_version target
                JOIN assessment.paper_slot s ON s.paper_version_id = target.id
                JOIN exercise.exercise_version ev ON ev.id = s.exercise_version_id
                JOIN assessment.item_exposure_event e
                  ON e.exercise_family_id = ev.exercise_family_id
                  OR e.exercise_version_id = s.exercise_version_id
                LEFT JOIN assessment.paper_version event_paper ON event_paper.id = e.paper_version_id
                WHERE target.id = :paper_version_id
                  AND e.event_kind IN ('practice_answer_released', 'formal_answer_released', 'secure_assigned', 'student_opened')
            """), {"paper_version_id": paper_version_id}).mappings()
        for event in rows:
            if event["event_kind"] in {"practice_answer_released", "formal_answer_released"}:
                raise ExposureConflictError(event["event_kind"])
            same_ab_group = event.get("target_variant_group_id") is not None \
                and event.get("event_variant_group_id") == event.get("target_variant_group_id")
            if same_ab_group:
                continue
            if event["scope_offering_id"] != scope_offering_id \
                    or event["visible_from"] != visible_from:
                raise ExposureConflictError("different_secure_scope_or_window")

    def record_secure_assignment(self, *, paper_version_id, assignment_id,
                                 scope_offering_id, visible_from):
        self.session.execute(text("""
            INSERT INTO assessment.item_exposure_event (
                id, exercise_version_id, exercise_instance_id, exercise_family_id,
                paper_version_id, assignment_id, scope_offering_id, event_kind,
                visible_from, occurred_at, metadata_hash
            )
            SELECT uuid_generate_v4(), s.exercise_version_id, s.exercise_instance_id,
                   ev.exercise_family_id, s.paper_version_id, :assignment_id,
                   :scope_offering_id, 'secure_assigned', :visible_from, now(),
                   encode(sha256(convert_to(s.content_hash, 'UTF8')), 'hex')
            FROM assessment.paper_slot s
            JOIN exercise.exercise_version ev ON ev.id = s.exercise_version_id
            WHERE s.paper_version_id = :paper_version_id
              AND NOT EXISTS (
                  SELECT 1 FROM assessment.item_exposure_event e
                  WHERE e.assignment_id = :assignment_id
                    AND e.exercise_version_id = s.exercise_version_id
                    AND e.event_kind = 'secure_assigned'
              )
        """), dict(assignment_id=assignment_id, paper_version_id=paper_version_id,
                   scope_offering_id=scope_offering_id, visible_from=visible_from))

    def record_practice_answer_release(self, *, paper_version_id, scope_offering_id, visible_from):
        self.session.execute(text("""
            INSERT INTO assessment.item_exposure_event (
                id, exercise_version_id, exercise_instance_id, exercise_family_id,
                paper_version_id, assignment_id, scope_offering_id, event_kind,
                visible_from, occurred_at, metadata_hash
            )
            SELECT uuid_generate_v4(), s.exercise_version_id, s.exercise_instance_id,
                   ev.exercise_family_id, s.paper_version_id, NULL,
                   :scope_offering_id, 'practice_answer_released', :visible_from, now(),
                   encode(sha256(convert_to(s.content_hash, 'UTF8')), 'hex')
            FROM assessment.paper_slot s
            JOIN exercise.exercise_version ev ON ev.id = s.exercise_version_id
            WHERE s.paper_version_id = :paper_version_id
              AND NOT EXISTS (
                  SELECT 1 FROM assessment.item_exposure_event e
                  WHERE e.paper_version_id = s.paper_version_id
                    AND e.exercise_version_id = s.exercise_version_id
                    AND e.event_kind = 'practice_answer_released'
              )
        """), dict(paper_version_id=paper_version_id,
                   scope_offering_id=scope_offering_id, visible_from=visible_from))

    def record_practice_answer_released(self, *, exercise_version_id, exercise_instance_id,
                                        paper_attempt_id):
        self.session.execute(text("""
            INSERT INTO assessment.item_exposure_event (
                id, exercise_version_id, exercise_instance_id, exercise_family_id,
                paper_version_id, assignment_id, scope_offering_id, event_kind,
                visible_from, occurred_at, metadata_hash
            )
            SELECT :id, s.exercise_version_id, s.exercise_instance_id,
                   ev.exercise_family_id, pa.paper_version_id, a.id, a.offering_id,
                   'practice_answer_released', now(), now(),
                   encode(sha256(convert_to(s.content_hash, 'UTF8')), 'hex')
            FROM assessment.paper_attempt pa
            JOIN assessment.assignment a ON a.id = pa.assignment_id
            JOIN assessment.paper_slot s ON s.paper_version_id = pa.paper_version_id
            JOIN exercise.exercise_version ev ON ev.id = s.exercise_version_id
            WHERE pa.id = :paper_attempt_id
              AND s.exercise_version_id = :exercise_version_id
              AND s.exercise_instance_id IS NOT DISTINCT FROM :exercise_instance_id
              AND NOT EXISTS (
                  SELECT 1 FROM assessment.item_exposure_event e
                  WHERE e.assignment_id = a.id AND e.exercise_version_id = s.exercise_version_id
                    AND e.exercise_instance_id IS NOT DISTINCT FROM s.exercise_instance_id
                    AND e.event_kind = 'practice_answer_released'
              )
        """), {"id": uuid4(), "paper_attempt_id": paper_attempt_id,
               "exercise_version_id": exercise_version_id,
               "exercise_instance_id": exercise_instance_id})
```

`record_practice_answer_released` 只在阶段 D 已实际开放解析后调用，并与 Artifact 授权/反馈记录处于同一事务；重复打开不新增事件。生产主键统一由基础 UUIDv7 工厂生成，测试中的 `uuid4()` 仅用于内存示例，落地时替换为 `modules.identity` 的 UUIDv7 生成器。

在既有 `AssignmentService.publish` 的 paper 终检之后、提交事务之前加入以下组合；`assignment` 尚未发布成功时异常会回滚事件，重复发布由既有幂等键和 `NOT EXISTS` 共同避免重复事件。

```python
self.exposure.assert_no_conflict(
    purpose=paper.purpose,
    scope_offering_id=assignment.offering_id,
    visible_from=assignment.visible_from,
    paper_version_id=assignment.paper_version_id,
    assignment_id=assignment.id,
)
if paper.purpose == "formal":
    self.exposure.record_secure_assignment(
        paper_version_id=assignment.paper_version_id,
        assignment_id=assignment.id,
        scope_offering_id=assignment.offering_id,
        visible_from=assignment.visible_from,
    )
```

练习解析的实际开放界面属于在线练习范围；本阶段只提供 `record_practice_answer_release` 组合供既有发布动作调用，确保事件与内容发布使用同一事务。候选快照的逐题调用已在任务 1 落地，发布终检在本任务落地。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/integration/assessment/test_exposure.py tests/unit/assessment/test_candidate.py -q`
预期：PASS；练习答案公开题在候选与正式卷发布两处均被阻断，发布失败无曝光事件残留，幂等发布只有 1 组事件。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/exposure.py modules/assessment/assignment.py tests/integration/assessment/test_exposure.py
git commit -m "feat: 在候选与作业发布终检阻断曝光冲突"
```

### 任务 9：实现教师方案比较与明确状态 UI

**文件：**
- 创建：`apps/web/components/assessment/AssemblySolutionComparator.tsx`
- 测试：`apps/web/components/assessment/AssemblySolutionComparator.test.tsx`

- [ ] **步骤 1：编写失败的测试**

```tsx
// apps/web/components/assessment/AssemblySolutionComparator.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AssemblySolutionComparator, AssemblyRunView } from "./AssemblySolutionComparator";

const baseRun: AssemblyRunView = {
  id: "run-1", status: "feasible", candidate_manifest_hash: "abc",
  solver_version: "9.11.4210", diagnostics: { messages: [] }, solution_manifest: [],
};

describe("AssemblySolutionComparator", () => {
  it("does not call FEASIBLE proven optimal", () => {
    render(<AssemblySolutionComparator run={{ ...baseRun, solution_manifest: [solutionSet()] }} onSelect={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("可行，未证明最优");
    expect(screen.queryByText("已证明最优")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "采用方案 1" })).toBeEnabled();
  });

  it("disables selection for timeout without a solution", () => {
    render(<AssemblySolutionComparator run={{ ...baseRun, status: "timeout" }} onSelect={vi.fn()} />);
    expect(screen.getByText("超时，无可行解；未创建试卷")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /采用方案/ })).not.toBeInTheDocument();
  });

  it("shows infeasible diagnostics without a relax action", () => {
    render(<AssemblySolutionComparator run={{
      ...baseRun, status: "infeasible",
      diagnostics: { messages: ["A1 无合格候选"], suggestions: ["增加候选"] },
    }} onSelect={vi.fn()} />);
    expect(screen.getByText("原硬约束不可行；未自动放松")).toBeInTheDocument();
    expect(screen.getByText("A1 无合格候选")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /放松/ })).not.toBeInTheDocument();
  });

  it("labels AB as structural rather than statistical equivalence", () => {
    const pair = solutionSet();
    pair.variants.push({ ...pair.variants[0], variant_label: "B" });
    pair.variants[0].variant_label = "A";
    render(<AssemblySolutionComparator run={{ ...baseRun, solution_manifest: [pair] }} onSelect={vi.fn()} />);
    expect(screen.getByText("结构匹配的 A/B 卷；不代表统计等值")).toBeInTheDocument();
  });
});

function solutionSet() {
  return {
    manifest_hash: "set-1",
    variants: [{
      variant_label: "single", manifest_hash: "solution-1", optimal_proven: false,
      solver_status: "feasible" as const, objective_value: 12,
      selections: [{ slot_code: "A1", exercise_version_id: "ex-1", score_hundredths: 1000, estimated_seconds: 300, difficulty: null }],
      objective_terms: [{ name: "duration", raw_value: { seconds: 300 }, deviation: 2, penalty: 12 }],
      structure_report: {},
    }],
  };
}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test -- AssemblySolutionComparator.test.tsx`
预期：FAIL；组件文件不存在。

- [ ] **步骤 3：编写最少实现代码**

```tsx
// apps/web/components/assessment/AssemblySolutionComparator.tsx
import React from "react";

export type RunStatus = "queued" | "running" | "optimal" | "feasible" | "infeasible" | "timeout" | "failed" | "cancelled";
export interface ObjectiveTermView { name: string; raw_value: Record<string, number>; deviation: number; penalty: number }
export interface SelectionView { slot_code: string; exercise_version_id: string; score_hundredths: number; estimated_seconds: number | null; difficulty: number | null }
export interface SolutionView {
  variant_label: string; manifest_hash: string; optimal_proven: boolean;
  solver_status: RunStatus; objective_value: number;
  selections: SelectionView[]; objective_terms: ObjectiveTermView[];
  structure_report: Record<string, number>;
}
export interface SolutionSetView { manifest_hash: string; variants: SolutionView[] }
export interface AssemblyRunView {
  id: string; status: RunStatus; candidate_manifest_hash: string; solver_version: string;
  diagnostics: { messages?: string[]; suggestions?: string[] };
  solution_manifest: SolutionSetView[];
}

function formatScore(hundredths: number) {
  const whole = Math.trunc(hundredths / 100);
  const fraction = String(Math.abs(hundredths % 100)).padStart(2, "0");
  return `${whole}.${fraction}`;
}

function statusText(run: AssemblyRunView) {
  if (run.status === "optimal") return "首个方案已证明最优；备选方案不继承最优标签";
  if (run.status === "feasible") return "可行，未证明最优";
  if (run.status === "infeasible") return "原硬约束不可行；未自动放松";
  if (run.status === "timeout") return run.solution_manifest.length
    ? "求解超时；保留的方案可行，未证明最优" : "超时，无可行解；未创建试卷";
  if (run.status === "failed") return "求解或独立整卷复验失败；未创建试卷";
  if (run.status === "cancelled") return "已取消；未创建试卷";
  return run.status === "running" ? "正在求解" : "等待求解";
}

export function AssemblySolutionComparator({ run, onSelect }: {
  run: AssemblyRunView;
  onSelect: (solutionIndex: number) => void;
}) {
  const canSelect = ["optimal", "feasible", "timeout"].includes(run.status)
    && run.solution_manifest.length > 0;
  return <section aria-labelledby="assembly-title">
    <h2 id="assembly-title">约束组卷方案比较</h2>
    <p role="status">{statusText(run)}</p>
    <p>候选快照：<code>{run.candidate_manifest_hash}</code>；OR-Tools：{run.solver_version}</p>
    {(run.diagnostics.messages ?? []).map(message => <p key={message} role="alert">{message}</p>)}
    {(run.diagnostics.suggestions ?? []).map(item => <p key={item}>建议：{item}</p>)}
    {run.solution_manifest.slice(0, 3).map((set, index) => <article key={set.manifest_hash}>
      <h3>方案 {index + 1}</h3>
      {set.variants.length === 2 && <p>结构匹配的 A/B 卷；不代表统计等值</p>}
      {set.variants.map(solution => <div key={solution.manifest_hash}>
        <h4>{solution.variant_label === "single" ? "固定卷" : `${solution.variant_label} 卷`}</h4>
        <p>{solution.optimal_proven ? "已证明最优" : "可行，未证明最优"}</p>
        <table><caption>槽位与固定题版本</caption><thead><tr><th>槽位</th><th>题目版本</th><th>分值</th><th>标称难度</th><th>预计秒数</th></tr></thead>
          <tbody>{solution.selections.map(slot => <tr key={slot.slot_code}>
            <td>{slot.slot_code}</td><td>{slot.exercise_version_id}</td>
            <td>{formatScore(slot.score_hundredths)}</td>
            <td>{slot.difficulty ?? "未知"}</td>
            <td>{slot.estimated_seconds ?? "未知"}</td>
          </tr>)}</tbody></table>
        <table><caption>软目标分解（不代替硬约束校验）</caption><thead><tr><th>目标</th><th>原始值</th><th>偏差</th><th>罚分</th></tr></thead>
          <tbody>{solution.objective_terms.map(term => <tr key={term.name}>
            <td>{term.name}</td><td>{JSON.stringify(term.raw_value)}</td><td>{term.deviation}</td><td>{term.penalty}</td>
          </tr>)}</tbody></table>
      </div>)}
      <button disabled={!canSelect} onClick={() => onSelect(index)}>采用方案 {index + 1}</button>
    </article>)}
  </section>;
}
```

组件不把硬约束和软目标合成绿色总分；未知难度和未知秒数都显示「未知」。采用按钮只在保存了可行方案时出现，`INFEASIBLE` 与空 `TIMEOUT` 没有 paper 创建入口。分值用整数余数格式化，不经过 JavaScript 浮点运算。

- [ ] **步骤 4：运行测试验证通过**

运行：`npm --prefix apps/web run test -- AssemblySolutionComparator.test.tsx`
预期：PASS；4 个用例通过，状态文本、诊断、选择门禁和 A/B 结构声明均正确。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/components/assessment/AssemblySolutionComparator.tsx apps/web/components/assessment/AssemblySolutionComparator.test.tsx
git commit -m "feat: 增加教师组卷方案比较视图"
```

### 任务 10：完成端到端验收与 1,000 候选/50 槽位受控压测

**文件：**
- 创建：`scripts/load_assembly.py`
- 创建：`tests/e2e/test_constraint_assembly.py`
- 修改：`tests/integration/assessment/test_assembly_api_worker.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/e2e/test_constraint_assembly.py
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from modules.assessment.assembly.contracts import RunStatus, SolveResult


def test_saved_result_manifest_is_the_replay_source():
    saved = json.loads(Path("artifacts/assembly-load.json").read_text(encoding="utf-8"))
    replayed = TypeAdapter(SolveResult).validate_python(saved["result"])
    assert replayed.status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE, RunStatus.TIMEOUT}
    assert replayed.solution_sets
    assert len(replayed.solution_sets) <= 3
    assert saved["candidate_manifest_hash"] == saved["input"]["manifest_hash"]
    for solution_set in replayed.solution_sets:
        assert len(solution_set.variants[0].selections) == 50


@pytest.mark.parametrize("ineligible_kind", [
    "unreviewed", "revoked", "license_expired", "hidden_evaluation", "private_draft"
])
def test_stage_a_ineligible_items_never_enter_snapshot(ineligible_kind, candidate_gate_context):
    context = candidate_gate_context
    exercise_version_id = context.make_item(ineligible_kind)
    eligible_ids = {c.exercise_version_id for c in context.eligibility.list_eligible(context.blueprint_version_id)}
    assert exercise_version_id not in eligible_ids
    manifest = context.candidate_service.snapshot(
        context.actor_id, context.blueprint_version_id, context.purpose, context.offering_id)
    assert exercise_version_id not in {c.exercise_version_id for c in manifest.candidates}
```

在 `test_assembly_api_worker.py` 增加并发重入用例：

```python
from concurrent.futures import ThreadPoolExecutor


def test_twenty_duplicate_deliveries_execute_once(assembly_context):
    context = assembly_context
    run_id = context.service.start(UUID(int=100), UUID(int=88), "concurrent-key").assembly_run_id

    def deliver(_):
        with context.new_session() as session:
            return execute_assembly(
                session, run_id, context.solver, context.validator, context.paper_service
            )

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(deliver, range(20)))
    assert context.solver.calls == 1
    assert len(context.run(run_id).solution_manifest) == 1
    assert context.paper_service.requests == []
```

只有第一个 delivery 能把 `queued` 原子推进为 `running`；每个线程使用独立数据库 session。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/e2e/test_constraint_assembly.py tests/integration/assessment/test_assembly_api_worker.py -q`
预期：FAIL；`artifacts/assembly-load.json` 不存在，候选门禁与 worker 并发重入断言尚未全部接通。

- [ ] **步骤 3：编写最少实现代码**

```python
# scripts/load_assembly.py
import argparse
import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from modules.assessment.assembly.contracts import (
    BlueprintSnapshot, Candidate, HardConstraints, RunStatus, SlotSpec,
    SoftObjectives, SolverConfig, make_candidate_manifest,
)
from modules.assessment.assembly.solver import AssemblySolver
from modules.assessment.db import SessionLocal
from modules.assessment.validation import PaperValidationService


def build_load(candidates_count=1000, slot_count=50):
    slots = tuple(SlotSpec(
        slot_id=UUID(int=1000 + i), code=f"A{i + 1}", section_code="A",
        score=Decimal("2.00"), estimated_seconds=300, allowed_kinds=("expression",),
    ) for i in range(slot_count))
    candidates = []
    for i in range(candidates_count):
        candidates.append(Candidate(
            exercise_version_id=UUID(int=i + 1), stable_exercise_id=UUID(int=10000 + i),
            exercise_family_id=UUID(int=20000 + i // 2), source_release_id=UUID(int=30000),
            decision_seq=i + 1, exercise_kind="expression", dependency_hash=f"d-{i}",
            content_hash=f"c-{i}", difficulty=1 + i % 5, cognitive_level=1 + i % 4,
            estimated_seconds=300, conflict_group_ids=(UUID(int=40000 + i // 2),),
        ))
    blueprint = BlueprintSnapshot(
        blueprint_version_id=UUID(int=100), course_id=UUID(int=101), offering_id=UUID(int=102),
        release_context_id=UUID(int=103), purpose="coursework", total_score=Decimal("100.00"),
        duration_seconds=15000, slots=slots,
        hard_constraints=HardConstraints(section_scores={"A": Decimal("100.00")}, max_duration_seconds=15000),
        soft_objectives=SoftObjectives(target_duration_seconds=15000, duration_weight=1),
    )
    return blueprint, make_candidate_manifest(blueprint, candidates, [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/assembly-load.json")
    parser.add_argument("--time-limit-seconds", type=int, default=2)
    args = parser.parse_args()
    blueprint, manifest = build_load()
    config = SolverConfig(time_limit_seconds=args.time_limit_seconds, max_solutions=3, num_workers=1)
    started = time.monotonic()
    result = AssemblySolver().solve(blueprint, manifest, config)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert result.status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE, RunStatus.TIMEOUT}
    assert result.solution_sets, "design load must produce a feasible paper"
    assert len(result.solution_sets) <= 3
    with SessionLocal() as session:
        seed_synthetic_release(session, blueprint, manifest)
        session.commit()
        validator = PaperValidationService(session)
        for solution_set in result.solution_sets:
            solution = solution_set.variants[0]
            assert len(solution.selections) == 50
            assert sum(s.score_hundredths for s in solution.selections) == 10000
            assert len({s.stable_exercise_id for s in solution.selections}) == 50
            report = validator.validate(
                blueprint=blueprint, candidate_manifest=manifest, solution=solution, peer=None
            )
            assert report.valid, report.issues
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "load": {"candidates": 1000, "slots": 50, "elapsed_ms": elapsed_ms},
        "candidate_manifest_hash": manifest.manifest_hash,
        "input": asdict(manifest), "config": asdict(config), "result": asdict(result),
    }, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({"status": result.status, "elapsed_ms": elapsed_ms,
                      "solutions": len(result.solution_sets)}))


if __name__ == "__main__":
    main()
```

`seed_synthetic_release(session, blueprint, manifest)` 在同一脚本的测试 schema 中创建固定 Release、合成 exercise/answer/rubric、具体实例和人工审核决策；它使用稳定 UUID 与内容哈希，结束后只清理该测试 schema。真实校验器不得直接对未落库的内存候选假报通过。

压测断言只检查受控超时、状态语义、方案数、50 槽位、精确总分、去重和独立整卷复验，不设生产 P95 承诺。执行：

```bash
python -I scripts/load_assembly.py --time-limit-seconds 2 --output artifacts/assembly-load.json
```

阶段 A 的 `candidate_gate_context.make_item` 分别生成未审核、审核撤销、许可失效、隐藏评测与私人草稿题；真实 `CandidateEligibilityService` 必须先排除，任务 1 的 `CandidateService.snapshot` 再固定结果。参数化题 fixture 另验证 `exercise_instance_id`、seed、题干和验证清单哈希在 manifest 中不变；难度或时长未知时 DTO 保留 `None`，UI 显示未知，不写 0。

- [ ] **步骤 4：运行测试验证通过**

运行：

```bash
pytest tests/e2e/test_constraint_assembly.py tests/integration/assessment/test_assembly_api_worker.py -q
pytest tests/unit/assessment tests/integration/assessment -q
npm --prefix apps/web run test -- AssemblySolutionComparator.test.tsx
alembic upgrade 20261105_090000_assessment_assembly
```

预期：PASS；20 个重复 delivery 只求解 1 次，所有已落卷方案硬约束违反数为 0，1,000 候选/50 槽位在受控时限内返回明确状态，保存 manifest 可直接回放，迁移停在统一 revision。

- [ ] **步骤 5：Commit**

```bash
git add scripts/load_assembly.py tests/e2e/test_constraint_assembly.py tests/integration/assessment/test_assembly_api_worker.py
git commit -m "test: 验收约束组卷幂等与千题受控负载"
```

## 3. 验收追踪与计划自检

| 设计要求 | 对应任务与证据 |
|---|---|
| 候选资格、固定 Release、证据哈希、缺失数据 | 任务 1、10；真实负向 fixture 与稳定 manifest 哈希 |
| 槽位、总分、分节分、题型、覆盖、必选、去重、冲突、时长 | 任务 2；CP-SAT 线性整数约束测试 |
| 难度、时长、认知、近期使用、曝光、教师偏好、A/B 结构软目标 | 任务 3、7；逐项目标原值、偏差、整数罚分 |
| `OPTIMAL / FEASIBLE / INFEASIBLE / TIMEOUT` 语义 | 任务 3、6、9；求解器、API、UI 三层测试 |
| 不可行诊断不修改原蓝图 | 任务 4；诊断副本无 `solution_sets`，原硬约束不变 |
| 最多 3 个不同 manifest | 任务 3；数据库 CHECK 与不同哈希测试 |
| 自动方案与手工卷使用同一独立整卷校验 | 任务 5、6、10；阶段 A validator 在落卷前逐方案调用 |
| 空超时不创建 paper，回放不重跑求解器 | 任务 6、10；409、空调用计数和保存 manifest 回放 |
| A/B 共享快照、显式锚题、独立 manifest 与审核 | 任务 5、7；双 variant 复验、双草稿、非锚重叠为 0 |
| 候选与发布曝光冲突 | 任务 1、8；同一 `ExposureService` 两个入口及事务回滚 |
| worker 幂等与受控压测 | 任务 6、10；单运行、单结果、1,000 候选/50 槽位 |
| 迁移归属 | 任务 1；唯一 assembly migration，固定 down_revision，upgrade/downgrade 测试 |

自检结果：10 个任务均包含失败测试、失败命令、具体最少实现、通过命令和独立 commit；共享 DTO、服务签名、状态、单位和文件路径前后一致。`item_exposure_event` 只在阶段 A 迁移中出现，本计划没有 exposure DDL，也没有在线 attempt 或 QTI 工作项。所有 paper 创建都经过保存 manifest、独立整卷复验和阶段 A `PaperService`，不存在自动放松硬约束或把 `FEASIBLE` 标为最优的路径。
