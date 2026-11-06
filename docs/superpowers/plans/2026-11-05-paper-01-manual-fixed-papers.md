# 阶段 A：手工固定卷与打印实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在数学知识库前置能力之上，交付一门课可实际使用的「版本化蓝图 → 合格候选 → 手工固定卷 → 独立校验 → 三类打印预览 → 整卷双复核 → 固定作业投影 → 撤回」闭环。

**架构：** `modules/assessment` 以应用服务编排 PostgreSQL 事务，复用前置计划任务 1—6 已建立的身份、权限、exercise、governance、Release Context、job/outbox，不复制题目或审核真源。`paper_version` 在批准前可编辑、批准后内容不可变；assignment 发布在同一事务内重新校验资格、双审、渲染与曝光，并从 slot 固定投影。Next.js 只调用 `/api/v1` 契约，打印 worker 只处理已授权渲染请求，学生卷使用独立白名单输入。

**技术栈：** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16、Decimal/NUMERIC(8,2)、Celery、Jinja2、WeasyPrint、pypdf、Next.js、React、TypeScript、Tailwind、pytest、Vitest、Testing Library、Playwright。

---

## 0. 范围、前置与不变量

### 0.1 本计划交付

- 手工蓝图版本、固定 Release Context 候选快照与资格拒绝原因。
- `paper_version / paper_section / paper_slot`、草稿替换/调序/调分、独立整卷校验。
- 学生卷、答案卷、量规卷的分离输入、分离权限、PDF 产物与导出审计。
- 正式卷两名不同授权人类复核；以 `review_manifest_hash` 聚合整卷内容与全部复核证据，不以单个 `review_decision_item_id` 代表整卷通过。
- 打印 assignment 与不可变 `assignment_item`，保留 `release_context_id / source_release_id / exercise_version_id / exercise_instance_id`。
- 完整 `attempt_policy_version` 和 `item_exposure_event`，供阶段 C/D 直接引用；本阶段不实现任何在线作答或评分行为。
- 基础曝光事件、发布终检、严重撤回、历史证据只读回放。

### 0.2 明确排除

- 不创建 `assembly_run`，不引入 OR-Tools/CP-SAT、软目标求解、A/B 卷或运行时随机题。
- 不创建 attempt、response revision、submission、在线保存/交卷、练习逐题检查、CAS 评分、grade 或 QTI 路由。
- 不重建身份、Release、exercise、answer、solution、rubric、review_decision；不依赖模型网关、向量检索或基础计划任务 7—9。
- `modules/assessment/models/grading.py` 只声明评分边界，不定义评分表；阶段 C/D 只使用本阶段已建的策略与曝光表，不重复建表。

### 0.3 全计划强制规则

1. 所有分值在 Python 中使用 `Decimal`，数据库使用 `NUMERIC(8,2)`；API JSON 用两位小数字符串，禁止二进制浮点求和。
2. `PaperStatus` 只允许 `draft / in_review / approved / withdrawn`。退回修改是追加审核事实后回到 `draft`，不增加第五种状态。
3. 所有写接口需要人类用户、课程/班级权限和 `Idempotency-Key`；草稿编辑另带 `base_version=edit_seq`。
4. 批准后的题目、分值、顺序、策略摘要和依赖哈希不可 UPDATE；普通纠错创建新 paper 版本，严重纠错走撤回。
5. 学生渲染 DTO 只含题号、题干、公开资产、作答区域、说明、页码与版本短码；`answer / solution / rubric / answer_test / hidden_note` 在数据组装前即被排除。
6. 正式卷需要两名不同、无内容贡献且具有课程范围权限的人类复核者；任一 manifest 变化使原审核失效。
7. assignment 发布校验 `visible_at <= due_at <= hard_close_at`，使用课程范围 advisory lock，并在同一事务内重查最新终态决策、许可、撤回、曝光、双审与打印渲染。
8. 统一入口为 `BlueprintService.create_version`、`CandidateService.snapshot`、`PaperService.create_manual_draft`、`PaperValidationService.validate`、`PaperReviewService.submit`、`PaperReviewService.approve`、`AssignmentService.publish`、`PaperRenderService.render`、`ExposureService.assert_no_conflict`。
9. 审计只记录 ID、哈希、操作者、时间、范围与理由，不记录完整答案、量规或密钥。

## 1. 文件结构

```text
AIForTeaching/
├─ apps/
│  ├─ api/
│  │  ├─ main.py                         # 只注册阶段 A 路由
│  │  └─ routers/
│  │     ├─ paper_blueprints.py         # 蓝图版本与候选快照
│  │     ├─ papers.py                   # 草稿、编辑、校验、双审、撤回
│  │     ├─ paper_renders.py            # 三类渲染请求与授权下载
│  │     └─ assignments.py              # 打印作业草稿与发布
│  ├─ web/
│  │  ├─ app/teacher/papers/page.tsx     # 教师固定卷工作台
│  │  ├─ components/assessment/
│  │  │  ├─ BlueprintForm.tsx           # 蓝图与分节槽位输入
│  │  │  ├─ CandidatePicker.tsx         # 合格候选与排除原因
│  │  │  ├─ ManualPaperEditor.tsx       # 键盘可完成的替换/调序/调分
│  │  │  └─ PaperOperations.tsx         # 校验、双审、打印、发布、撤回
│  │  ├─ lib/assessment-api.ts          # 类型安全 API 与幂等键
│  │  ├─ types/assessment.ts            # 与 JSON Schema 对齐的 DTO
│  │  ├─ vitest.config.ts               # 外部 assessment 测试目录
│  │  └─ playwright.assessment.config.ts
│  └─ worker/render_paper.py             # DB job 驱动的打印渲染消费者
├─ modules/
│  ├─ agent/
│  │  ├─ models.py                      # 最小通用 Artifact/ArtifactVersion
│  │  └─ artifacts.py                   # 注册、对象存储与访问授权，无 LLM
│  └─ assessment/
│     ├─ models/
│     │  ├─ assembly.py                 # 蓝图、候选快照、assignment 投影
│     │  ├─ paper.py                    # paper/section/slot/review/render
│     │  ├─ attempt.py                  # 完整 attempt_policy_version，无 attempt
│     │  ├─ grading.py                  # 阶段 A 空评分边界
│     │  └─ exposure.py                 # item_exposure_event
│     ├─ artifacts.py                   # paper_render 到通用 Artifact 的适配
│     ├─ blueprints.py                  # BlueprintService
│     ├─ candidates.py                  # CandidateService 与资格查询
│     ├─ exposure.py                    # ExposureService
│     ├─ papers.py                      # PaperService 与不可变 manifest
│     ├─ validation.py                  # PaperValidationService
│     ├─ rendering.py                   # 白名单投影与 PaperRenderService
│     ├─ review.py                      # PaperReviewService
│     ├─ assignments.py                 # AssignmentService
│     ├─ security.py                    # 权限端口与错误映射
│     ├─ hashing.py                     # Decimal 安全 canonical JSON/SHA-256
│     └─ domain.py                      # 服务间冻结 dataclass 与枚举
├─ packages/contracts/assessment/
│  ├─ index.ts                          # Web 共享类型
│  ├─ blueprint.schema.json
│  ├─ paper.schema.json
│  ├─ assignment.schema.json
│  └─ render.schema.json
├─ db/migrations/versions/
│  └─ 20261105_080000_assessment_paper.py # 阶段 A 唯一迁移，含条件 Artifact 基线
└─ tests/
   ├─ fixtures/assessment/              # 合成数学固定卷、字体与 PDF 样例
   ├─ unit/assessment/                  # 哈希、资格、校验、审核、白名单、Web
   ├─ integration/assessment/           # 迁移、RLS、事务、固定投影、对象存储
   └─ e2e/assessment/                   # 真实 100 分卷与浏览器闭环
```

## 2. 共享契约

以下类型在任务 1 创建，后续代码中的同名类型均直接从此文件导入，而不是重新定义近似结构。`decision_seqs` 的键是依赖类型加版本 ID；`latest_decisions` 是该键对应的最新终态。所有 UUID 在 JSON 中为字符串，所有分值为两位小数字符串。

```python
# modules/assessment/domain.py
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping
from uuid import UUID
from modules.assessment.types import PaperStatus

Purpose = Literal["formal", "coursework", "practice"]
RenderKind = Literal["student", "answer", "rubric"]

@dataclass(frozen=True)
class BlueprintSlot:
    code: str
    score: Decimal
    exercise_kinds: tuple[str, ...]
    required_concept_ids: tuple[UUID, ...]
    learning_outcome_ids: tuple[UUID, ...]
    difficulty_label: str | None
    estimated_seconds: int | None
    mandatory_exercise_version_ids: tuple[UUID, ...]
    excluded_exercise_version_ids: tuple[UUID, ...]

@dataclass(frozen=True)
class BlueprintSection:
    code: str
    title: str
    instruction: str
    slots: tuple[BlueprintSlot, ...]

@dataclass(frozen=True)
class BlueprintContent:
    schema_version: Literal["paper-blueprint/v1"]
    purpose: Purpose
    course_id: UUID
    offering_id: UUID | None
    release_context_id: UUID
    total_score: Decimal
    duration_seconds: int
    sections: tuple[BlueprintSection, ...]
    hard_constraints: Mapping[str, object]
    soft_objectives: Mapping[str, object]
    review_policy: Literal["formal_dual_review", "single_review"]

@dataclass(frozen=True)
class CandidateEvidence:
    release_context_id: UUID
    source_release_id: UUID
    exercise_id: UUID
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    exercise_family_id: UUID
    exercise_kind: str
    concept_ids: tuple[UUID, ...]
    learning_outcome_ids: tuple[UUID, ...]
    difficulty_label: str | None
    estimated_seconds: int | None
    decision_seqs: Mapping[str, int]
    latest_decisions: Mapping[str, str]
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

@dataclass(frozen=True)
class SlotSelection:
    slot_code: str
    exercise_version_id: UUID

@dataclass(frozen=True)
class PaperSlotView:
    id: UUID
    section_code: str
    code: str
    slot_no: int
    exercise_id: UUID | None
    exercise_version_id: UUID | None
    exercise_instance_id: UUID | None
    exercise_family_id: UUID | None
    source_release_id: UUID | None
    score: Decimal
    estimated_seconds: int | None
    display_config: Mapping[str, object]
    dependency_manifest_hash: str | None

@dataclass(frozen=True)
class PaperSectionView:
    code: str
    title: str
    instruction: str
    sort_order: int
    section_score: Decimal
    slots: tuple[PaperSlotView, ...]

@dataclass(frozen=True)
class PaperView:
    id: UUID
    blueprint_version_id: UUID
    release_context_id: UUID
    course_id: UUID
    offering_id: UUID | None
    purpose: Purpose
    status: PaperStatus
    total_score: Decimal
    duration_seconds: int
    edit_seq: int
    manifest_hash: str
    review_manifest_hash: str | None
    sections: tuple[PaperSectionView, ...]

@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    slot_id: UUID | None = None

@dataclass(frozen=True)
class ExposureEvent:
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    exercise_family_id: UUID
    scope_offering_id: UUID | None
    event_kind: Literal["secure_assigned", "student_opened", "practice_answer_released", "formal_answer_released", "withdrawn"]
    visible_from: datetime
    occurred_at: datetime

@dataclass(frozen=True)
class ReviewEvidence:
    reviewer_user_id: UUID
    decision_seq: int
    manifest_hash: str
    decision: Literal["approved", "rejected", "revoked"]
    human: bool
    authorized: bool
    contributor: bool

@dataclass(frozen=True)
class StudentSlotProjection:
    slot_id: UUID
    question_no: str
    statement_ast: Mapping[str, object]
    public_assets: tuple[Mapping[str, str], ...]
    response_kind: str
    answer_space_lines: int

@dataclass(frozen=True)
class StudentPaperProjection:
    paper_version_id: UUID
    version_short_code: str
    course_title: str
    instructions: str
    slots: tuple[StudentSlotProjection, ...]
```

## 3. 实施任务

### 任务 1：最小通用 Artifact、共享契约与阶段 A 数据基线

**交付物：** 从基础计划任务 9 抽出的通用 Artifact 注册/对象存储/授权访问能力，以及供全部固定卷任务使用的共享类型和唯一 Alembic 迁移。该任务不调用模型网关；若执行时 `agent.artifact_version` 已由并行工作建立，迁移只做列与约束兼容检查，服务只做接口兼容测试，绝不重复建表。

**文件：**
- 创建：`modules/agent/models.py`
- 创建：`modules/agent/artifacts.py`
- 创建：`modules/assessment/hashing.py`
- 创建：`modules/assessment/domain.py`
- 创建：`db/migrations/versions/20261105_080000_assessment_paper.py`
- 测试：`tests/unit/assessment/test_artifacts.py`
- 测试：`tests/integration/assessment/test_assessment_migration.py`

- [ ] **步骤 1：编写失败的 Artifact 与迁移测试**

```python
# tests/unit/assessment/test_artifacts.py
from hashlib import sha256
from uuid import uuid4
import pytest
from modules.agent.artifacts import ArtifactMetadata, ArtifactService, MemoryObjectStore
from modules.assessment.security import StaticAuthorizer

def test_artifact_round_trip_preserves_security_metadata():
    store = MemoryObjectStore()
    auth = StaticAuthorizer({uuid4(): {"artifact.write", "artifact.read"}})
    actor = next(iter(auth.grants))
    service = ArtifactService(store, auth)
    context = uuid4()
    body = b"%PDF-1.7 synthetic"
    metadata = ArtifactMetadata(
        release_context_id=context, visibility="private",
        contains_solution=True, contains_answer_test=False,
        mime_type="application/pdf", media_kind="paper_render",
    )
    registered = service.register(actor_id=actor, body=body, metadata=metadata)
    assert registered.output_hash == sha256(body).hexdigest()
    assert registered.release_context_id == context
    assert service.open(actor_id=actor, artifact_version_id=registered.id) == body
    with pytest.raises(PermissionError):
        service.open(actor_id=uuid4(), artifact_version_id=registered.id)

def test_artifact_registration_is_deterministically_addressed_but_not_public():
    store = MemoryObjectStore()
    actor = uuid4()
    service = ArtifactService(store, StaticAuthorizer({actor: {"artifact.write"}}))
    metadata = ArtifactMetadata(uuid4(), "private", False, False, "text/plain", "paper_render")
    version = service.register(actor, b"safe", metadata)
    assert version.storage_uri.startswith("s3://private-artifacts/")
    assert "signed" not in version.storage_uri
```

```python
# tests/integration/assessment/test_assessment_migration.py
from sqlalchemy import inspect, text

def test_assessment_tables_numeric_policy_exposure_and_artifact_compatibility(db_engine):
    names = set(inspect(db_engine).get_table_names(schema="assessment"))
    assert {"paper_blueprint_version", "candidate_snapshot", "paper_version",
            "paper_section", "paper_slot", "paper_review_round", "paper_render",
            "attempt_policy_version", "item_exposure_event", "assignment",
            "assignment_item"} <= names
    with db_engine.connect() as conn:
        score_type = conn.execute(text("""
          SELECT data_type || ':' || numeric_precision || ':' || numeric_scale
          FROM information_schema.columns
          WHERE table_schema='assessment' AND table_name='paper_slot' AND column_name='score'
        """)).scalar_one()
        assert score_type == "numeric:8:2"
        artifact_columns = {c["name"] for c in inspect(db_engine).get_columns(
            "artifact_version", schema="agent")}
        assert {"release_context_id", "visibility", "contains_solution",
                "contains_answer_test", "output_hash", "storage_uri"} <= artifact_columns

def test_candidate_manifest_is_unique_per_blueprint_not_global(db_engine):
    # 同一候选清单可复用于两个蓝图；同一蓝图重复清单仍由复合唯一键去重。
    constraints = inspect(db_engine).get_unique_constraints(
        "candidate_snapshot", schema="assessment")
    assert any(set(item["column_names"]) == {
        "blueprint_version_id", "candidate_manifest_hash"} for item in constraints)

def test_non_owner_student_role_cannot_read_confidential_render(student_db):
    with student_db.connect() as conn:
        conn.execute(text("SET LOCAL app.course_ids = ''"))
        conn.execute(text("SET LOCAL app.assessment_service = 'off'"))
        assert conn.execute(text("SELECT count(*) FROM assessment.paper_render")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM assessment.paper_version")).scalar_one() == 0
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_artifacts.py tests/integration/assessment/test_assessment_migration.py -q`

预期：FAIL；首先出现 `ModuleNotFoundError: modules.agent.artifacts`，迁移测试明确报告 assessment 表不存在，而不是连接或 fixture 错误。

- [ ] **步骤 3：编写最少实现与唯一迁移**

先在共享基线工作区运行 `alembic heads`。只有输出恰好一个 revision 才继续；把该行的实际原值写入新文件的 `down_revision`。以下示例假设该命令实际输出为 `9f3c7a2e1b64`，执行时以本机共享基线命令输出为准，不沿用此示例值。

```python
# db/migrations/versions/20261105_080000_assessment_paper.py
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261105_080000_assessment_paper"
down_revision = "9f3c7a2e1b64"  # 本行必须是执行时 alembic heads 的实际唯一输出
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS assessment")
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS ops;
    CREATE TABLE IF NOT EXISTS ops.migration_owned_object (
      migration_revision text NOT NULL, object_schema text NOT NULL, object_name text NOT NULL,
      PRIMARY KEY(migration_revision,object_schema,object_name)
    );
    """)
    created_artifact = not inspector.has_table("artifact_version", schema="agent")
    if created_artifact:
        op.execute("""
        CREATE TABLE agent.artifact (
          id uuid PRIMARY KEY, artifact_kind text NOT NULL,
          created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE agent.artifact_version (
          id uuid PRIMARY KEY, artifact_id uuid NOT NULL REFERENCES agent.artifact(id),
          version_no integer NOT NULL, release_context_id uuid NOT NULL,
          visibility text NOT NULL CHECK (visibility IN ('private','class_shared','course_published')),
          contains_solution boolean NOT NULL, contains_answer_test boolean NOT NULL,
          media_kind text NOT NULL, mime_type text NOT NULL, storage_uri text NOT NULL,
          byte_size bigint NOT NULL CHECK (byte_size >= 0), output_hash char(64) NOT NULL,
          created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(artifact_id, version_no), UNIQUE(output_hash, media_kind, created_by)
        );
        CREATE TABLE agent.artifact_access_event (
          id uuid PRIMARY KEY, artifact_version_id uuid NOT NULL REFERENCES agent.artifact_version(id),
          actor_id uuid NOT NULL, action text NOT NULL CHECK (action IN ('open','download')),
          occurred_at timestamptz NOT NULL DEFAULT now()
        );
        """)
        for object_name in ("artifact", "artifact_version", "artifact_access_event"):
            op.execute(sa.text("""
              INSERT INTO ops.migration_owned_object(migration_revision,object_schema,object_name)
              VALUES (:revision,'agent',:name) ON CONFLICT DO NOTHING
            """), {"revision": revision, "name": object_name})
    else:
        required = {"release_context_id", "visibility", "contains_solution",
                    "contains_answer_test", "output_hash", "storage_uri"}
        actual = {c["name"] for c in inspector.get_columns("artifact_version", schema="agent")}
        missing = required - actual
        if missing:
            raise RuntimeError(f"existing agent.artifact_version incompatible: {sorted(missing)}")

    op.execute("""
    CREATE TABLE assessment.paper_blueprint_version (
      id uuid PRIMARY KEY, blueprint_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
      course_id uuid NOT NULL, offering_id uuid NULL, release_context_id uuid NOT NULL,
      purpose text NOT NULL CHECK(purpose IN ('formal','coursework','practice')),
      assembly_mode text NOT NULL CHECK(assembly_mode IN ('manual','constraint_assisted')),
      total_score numeric(8,2) NOT NULL CHECK(total_score>0), duration_seconds integer NOT NULL CHECK(duration_seconds>0),
      blueprint_ast jsonb NOT NULL, base_version_id uuid NULL REFERENCES assessment.paper_blueprint_version(id),
      content_hash char(64) NOT NULL, change_set_id uuid NOT NULL, created_by uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(blueprint_id,version_no), UNIQUE(content_hash,blueprint_id)
    );
    CREATE TABLE assessment.candidate_snapshot (
      id uuid PRIMARY KEY, blueprint_version_id uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
      candidate_manifest jsonb NOT NULL, candidate_manifest_hash char(64) NOT NULL,
      UNIQUE(blueprint_version_id,candidate_manifest_hash),
      created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE assessment.paper_version (
      id uuid PRIMARY KEY, paper_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
      blueprint_version_id uuid NOT NULL REFERENCES assessment.paper_blueprint_version(id),
      candidate_snapshot_id uuid NOT NULL REFERENCES assessment.candidate_snapshot(id),
      assembly_run_id uuid NULL, selected_solution_index integer NULL,
      base_paper_version_id uuid NULL REFERENCES assessment.paper_version(id),
      course_id uuid NOT NULL, offering_id uuid NULL, release_context_id uuid NOT NULL,
      paper_kind text NOT NULL CHECK(paper_kind IN ('formal','coursework','practice')),
      variant_group_id uuid NULL, variant_label text NULL,
      total_score numeric(8,2) NOT NULL, duration_seconds integer NOT NULL,
      status text NOT NULL CHECK(status IN ('draft','in_review','approved','withdrawn')),
      edit_seq integer NOT NULL DEFAULT 1, manifest_hash char(64) NOT NULL,
      submitted_manifest_hash char(64) NULL, review_manifest_hash char(64) NULL,
      created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), approved_at timestamptz NULL,
      withdrawn_by uuid NULL, withdrawn_at timestamptz NULL, withdrawal_reason text NULL,
      UNIQUE(paper_id,version_no), UNIQUE(manifest_hash,paper_id),
      CONSTRAINT ck_paper_manual_only CHECK(assembly_run_id IS NULL AND selected_solution_index IS NULL),
      CHECK(status<>'approved' OR approved_at IS NOT NULL),
      CHECK((status='withdrawn')=(withdrawn_at IS NOT NULL))
    );
    CREATE TABLE assessment.paper_section (
      id uuid PRIMARY KEY, paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
      code text NOT NULL, title text NOT NULL, instruction text NOT NULL, sort_order integer NOT NULL,
      section_score numeric(8,2) NOT NULL, display_config jsonb NOT NULL DEFAULT '{}',
      content_hash char(64) NOT NULL, UNIQUE(paper_version_id,code), UNIQUE(paper_version_id,sort_order)
    );
    CREATE TABLE assessment.paper_slot (
      id uuid PRIMARY KEY, section_id uuid NOT NULL REFERENCES assessment.paper_section(id),
      code text NOT NULL, slot_no integer NOT NULL, exercise_id uuid NULL, exercise_version_id uuid NULL,
      exercise_instance_id uuid NULL, exercise_family_id uuid NULL, source_release_id uuid NULL,
      score numeric(8,2) NOT NULL CHECK(score>=0), estimated_seconds integer NULL,
      display_config jsonb NOT NULL DEFAULT '{}', dependency_manifest_hash char(64) NULL,
      content_hash char(64) NOT NULL, UNIQUE(section_id,slot_no), UNIQUE(section_id,code),
      CHECK(exercise_instance_id IS NULL OR exercise_version_id IS NOT NULL)
    );
    CREATE TABLE assessment.paper_review_round (
      id uuid PRIMARY KEY, paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
      manifest_hash char(64) NOT NULL, required_approvals integer NOT NULL DEFAULT 2 CHECK(required_approvals>=1),
      status text NOT NULL CHECK(status IN ('open','approved','returned','invalidated')),
      submitted_by uuid NOT NULL, submitted_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz NULL,
      UNIQUE(paper_version_id,manifest_hash)
    );
    CREATE TABLE assessment.attempt_policy_version (
      id uuid PRIMARY KEY, policy_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
      policy_kind text NOT NULL CHECK(policy_kind IN ('formal','coursework','practice')),
      content_hash char(64) NOT NULL UNIQUE, max_attempts integer NULL CHECK(max_attempts IS NULL OR max_attempts>0),
      behavior text NOT NULL CHECK(behavior IN ('deferred','adaptive_practice')),
      time_limit_seconds integer NULL, grace_seconds integer NOT NULL DEFAULT 0,
      late_policy text NOT NULL CHECK(late_policy IN ('reject','read_only_grace','auto_submit')),
      show_syntax_errors boolean NOT NULL, show_math_interpretation boolean NOT NULL,
      show_input_preview boolean NOT NULL, correctness_release text NOT NULL,
      score_release text NOT NULL, hint_release text NOT NULL, solution_release text NOT NULL,
      answer_release text NOT NULL, allow_item_check boolean NOT NULL,
      max_hint_level integer NOT NULL DEFAULT 0, allow_retry boolean NOT NULL,
      practice_penalty numeric(5,2) NOT NULL DEFAULT 0, allow_flag boolean NOT NULL,
      allow_review boolean NOT NULL, allow_upload boolean NOT NULL,
      autosave_debounce_ms integer NOT NULL DEFAULT 800, autosave_max_interval_ms integer NOT NULL DEFAULT 5000,
      conflict_policy text NOT NULL DEFAULT 'per_slot', created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(policy_id,version_no), CHECK(max_attempts IS NOT NULL OR policy_kind='practice')
    );
    CREATE TABLE assessment.item_exposure_event (
      id uuid PRIMARY KEY, exercise_version_id uuid NOT NULL, exercise_instance_id uuid NULL,
      exercise_family_id uuid NOT NULL, paper_version_id uuid NULL REFERENCES assessment.paper_version(id),
      assignment_id uuid NULL, scope_offering_id uuid NULL,
      event_kind text NOT NULL CHECK(event_kind IN ('secure_assigned','student_opened','practice_answer_released','formal_answer_released','withdrawn')),
      visible_from timestamptz NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(), metadata_hash char(64) NOT NULL
    );
    CREATE INDEX exposure_family_kind_idx ON assessment.item_exposure_event(exercise_family_id,event_kind,occurred_at DESC);
    CREATE TABLE assessment.paper_render (
      id uuid PRIMARY KEY, paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
      render_kind text NOT NULL CHECK(render_kind IN ('student','answer','rubric')),
      paper_manifest_hash char(64) NOT NULL, template_version text NOT NULL, language_code text NOT NULL,
      artifact_version_id uuid NOT NULL REFERENCES agent.artifact_version(id),
      renderer_version text NOT NULL, font_manifest_hash char(64) NOT NULL,
      security_manifest_hash char(64) NOT NULL, output_hash char(64) NOT NULL,
      status text NOT NULL CHECK(status IN ('ready','failed','blocked')),
      created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(paper_version_id,render_kind,output_hash)
    );
    CREATE TABLE assessment.assignment (
      id uuid PRIMARY KEY, course_id uuid NOT NULL, offering_id uuid NOT NULL,
      paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
      attempt_policy_version_id uuid NOT NULL REFERENCES assessment.attempt_policy_version(id),
      release_context_id uuid NOT NULL, visible_at timestamptz NOT NULL, due_at timestamptz NOT NULL,
      hard_close_at timestamptz NOT NULL, distribution_kind text NOT NULL CHECK(distribution_kind='print'),
      status text NOT NULL CHECK(status IN ('draft','scheduled','published','closed','withdrawn')),
      created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz NULL,
      CHECK(visible_at<=due_at AND due_at<=hard_close_at)
    );
    CREATE TABLE assessment.assignment_item (
      id uuid PRIMARY KEY, assignment_id uuid NOT NULL REFERENCES assessment.assignment(id),
      paper_slot_id uuid NOT NULL REFERENCES assessment.paper_slot(id), ord integer NOT NULL,
      release_context_id uuid NOT NULL, source_release_id uuid NOT NULL,
      exercise_version_id uuid NOT NULL, exercise_instance_id uuid NULL,
      score numeric(8,2) NOT NULL, content_hash char(64) NOT NULL,
      UNIQUE(assignment_id,paper_slot_id), UNIQUE(assignment_id,ord)
    );
    ALTER TABLE assessment.item_exposure_event
      ADD CONSTRAINT exposure_assignment_fk FOREIGN KEY(assignment_id)
      REFERENCES assessment.assignment(id);
    """)
    op.execute("""
    CREATE OR REPLACE FUNCTION assessment.guard_fixed_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME='paper_version' THEN
        IF OLD.status='withdrawn' THEN RAISE EXCEPTION 'withdrawn paper is immutable'; END IF;
        IF OLD.status='approved' AND NEW.status<>'withdrawn' THEN RAISE EXCEPTION 'approved paper is immutable'; END IF;
        IF OLD.status<>'draft' AND (NEW.manifest_hash<>OLD.manifest_hash OR NEW.edit_seq<>OLD.edit_seq) THEN
          RAISE EXCEPTION 'submitted manifest is immutable';
        END IF;
        IF NEW.status='approved' AND (OLD.status<>'in_review' OR NEW.review_manifest_hash IS NULL) THEN
          RAISE EXCEPTION 'dual review manifest required';
        END IF;
      ELSIF TG_TABLE_NAME='assignment_item' THEN
        RAISE EXCEPTION 'assignment_item is append-only';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER paper_version_guard BEFORE UPDATE ON assessment.paper_version
      FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();
    CREATE TRIGGER assignment_item_guard BEFORE UPDATE OR DELETE ON assessment.assignment_item
      FOR EACH ROW EXECUTE FUNCTION assessment.guard_fixed_projection();
    ALTER TABLE assessment.paper_version ENABLE ROW LEVEL SECURITY;
    ALTER TABLE assessment.paper_version FORCE ROW LEVEL SECURITY;
    CREATE POLICY paper_course_scope ON assessment.paper_version USING (
      current_setting('app.assessment_service',true)='on'
      OR course_id::text=ANY(string_to_array(current_setting('app.course_ids',true),','))
    );
    ALTER TABLE assessment.paper_section ENABLE ROW LEVEL SECURITY;
    ALTER TABLE assessment.paper_section FORCE ROW LEVEL SECURITY;
    CREATE POLICY section_course_scope ON assessment.paper_section USING (
      current_setting('app.assessment_service',true)='on' OR EXISTS (
        SELECT 1 FROM assessment.paper_version p WHERE p.id=paper_version_id
        AND p.course_id::text=ANY(string_to_array(current_setting('app.course_ids',true),',')))
    );
    ALTER TABLE assessment.paper_slot ENABLE ROW LEVEL SECURITY;
    ALTER TABLE assessment.paper_slot FORCE ROW LEVEL SECURITY;
    CREATE POLICY slot_course_scope ON assessment.paper_slot USING (
      current_setting('app.assessment_service',true)='on' OR EXISTS (
        SELECT 1 FROM assessment.paper_section s JOIN assessment.paper_version p ON p.id=s.paper_version_id
        WHERE s.id=section_id AND p.course_id::text=ANY(string_to_array(current_setting('app.course_ids',true),',')))
    );
    ALTER TABLE assessment.paper_render ENABLE ROW LEVEL SECURITY;
    ALTER TABLE assessment.paper_render FORCE ROW LEVEL SECURITY;
    CREATE POLICY render_service_only ON assessment.paper_render USING (
      current_setting('app.assessment_service',true)='on'
    );
    """)

def downgrade():
    bind = op.get_bind()
    owned = bind.execute(sa.text("""
      SELECT 1 FROM ops.migration_owned_object
      WHERE migration_revision=:revision AND object_schema='agent' AND object_name='artifact_version'
    """), {"revision": revision}).scalar() is not None
    op.execute("DROP SCHEMA assessment CASCADE")
    if owned:
        op.execute("DROP TABLE agent.artifact_access_event, agent.artifact_version, agent.artifact CASCADE")
    op.execute(sa.text("DELETE FROM ops.migration_owned_object WHERE migration_revision=:revision"),
               {"revision": revision})
```

降级只 drop 本迁移拥有的 assessment 对象；Artifact 若由本迁移新建，迁移测试在空库中通过依赖顺序显式 drop `agent.artifact_access_event`、`agent.artifact_version`、`agent.artifact` 后验证可重建。若检测到 Artifact 为并行既有对象，兼容测试断言 downgrade 不删除它。为让归属可判定，在同一迁移增加 `ops.migration_owned_object` 记录三行 Artifact 对象归属，downgrade 只删除这些登记对象。

```python
# modules/assessment/hashing.py
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from uuid import UUID
import json

def canonical_hash(value: object) -> str:
    def normalize(v):
        if is_dataclass(v):
            return normalize(asdict(v))
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, Decimal):
            return format(v, "f")
        if isinstance(v, UUID):
            return str(v)
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if isinstance(v, dict):
            return {str(k): normalize(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            return [normalize(x) for x in v]
        return v
    payload = json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
```

```python
# modules/agent/artifacts.py
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4
from modules.assessment.hashing import canonical_hash

@dataclass(frozen=True)
class ArtifactMetadata:
    release_context_id: UUID
    visibility: str
    contains_solution: bool
    contains_answer_test: bool
    mime_type: str
    media_kind: str

class MemoryObjectStore:
    def __init__(self): self.objects = {}
    def put(self, key: str, body: bytes): self.objects[key] = body
    def get(self, key: str): return self.objects[key]

class ArtifactService:
    def __init__(self, store, authorizer, repository=None):
        self.store, self.auth = store, authorizer
        self.rows = {} if repository is None else repository
    def register(self, actor_id: UUID, body: bytes, metadata: ArtifactMetadata):
        self.auth.require(actor_id, "artifact.write")
        if metadata.visibility not in {"private", "class_shared", "course_published"}:
            raise ValueError("invalid visibility")
        digest = sha256(body).hexdigest()
        key = f"{metadata.media_kind}/{digest}"
        self.store.put(key, body)
        row = type("ArtifactVersionRow", (), {})()
        row.id, row.version_no = uuid4(), 1
        row.release_context_id = metadata.release_context_id
        row.visibility, row.contains_solution = metadata.visibility, metadata.contains_solution
        row.contains_answer_test, row.output_hash = metadata.contains_answer_test, digest
        row.storage_uri = f"s3://private-artifacts/{key}"
        self.rows[row.id] = (row, key)
        return row
    def open(self, actor_id: UUID, artifact_version_id: UUID) -> bytes:
        self.auth.require(actor_id, "artifact.read")
        row, key = self.rows[artifact_version_id]
        return self.store.get(key)
```

```python
# modules/assessment/security.py
class StaticAuthorizer:
    def __init__(self, grants):
        self.grants = grants
        self.service_accounts = set()
    def require(self, actor_id, action, **scope):
        if action not in self.grants.get(actor_id, set()):
            raise PermissionError(action)
    def is_human(self, actor_id):
        return actor_id not in self.service_accounts
```

`StaticAuthorizer` 是仅测试使用的内存端口实现；生产适配器调用前置身份 Policy Decision Point，并在 `class_shared` 时核对 offering、`course_published` 时核对 Release Context。SQLAlchemy repository 使用同一 `ArtifactService` 方法签名，在事务内写 `agent.artifact/version/access_event`，S3 适配器使用相同 `put/get`。

- [ ] **步骤 4：运行迁移升降级与测试确认通过**

运行：

```bash
alembic upgrade head
pytest tests/unit/assessment/test_artifacts.py tests/integration/assessment/test_assessment_migration.py -q
alembic downgrade -1
alembic upgrade head
pytest tests/integration/assessment/test_assessment_migration.py -q
```

预期：全部 PASS；空库可升降级重建，策略与曝光表已存在，分值类型为 `numeric:8:2`，既有 Artifact 兼容检查不重复建表，负向 Artifact 访问被拒绝。

- [ ] **步骤 5：Commit**

```bash
git add modules/agent/models.py modules/agent/artifacts.py modules/assessment/hashing.py modules/assessment/security.py modules/assessment/domain.py db/migrations/versions/20261105_080000_assessment_paper.py tests/unit/assessment/test_artifacts.py tests/integration/assessment/test_assessment_migration.py
git commit -m "feat: add assessment schema and minimal artifact boundary"
```

### 任务 2：版本化手工蓝图与 API

**交付物：** 教师能创建不可变手工蓝图版本；蓝图固定课程、班级、Release Context、总分、时长、分节槽位与双审策略，JSON 键顺序不影响内容哈希。`BlueprintContent` 等类型来自任务 1 的共享契约。

**文件：**
- 创建：`modules/assessment/blueprints.py`
- 创建：`apps/api/routers/paper_blueprints.py`
- 创建：`packages/contracts/assessment/blueprint.schema.json`
- 测试：`tests/unit/assessment/test_blueprints.py`
- 测试：`tests/integration/assessment/test_blueprint_api.py`

- [ ] **步骤 1：编写失败的蓝图版本测试**

```python
from decimal import Decimal
from uuid import uuid4
import pytest
from modules.assessment.blueprints import BlueprintService
from modules.assessment.security import StaticAuthorizer
from modules.assessment.domain import BlueprintContent, BlueprintSection, BlueprintSlot

def content():
    course, context = uuid4(), uuid4()
    return BlueprintContent(
        "paper-blueprint/v1", "formal", course, uuid4(), context,
        Decimal("100.00"), 7200,
        (BlueprintSection("A", "基础", "写出证明", (
            BlueprintSlot("A1", Decimal("40.00"), ("proof",), (), (), None, 1800, (), ()),
            BlueprintSlot("A2", Decimal("60.00"), ("expression",), (), (), None, 2400, (), ()),
        )),), {"coverage_required": True}, {}, "formal_dual_review",
    )

def test_blueprint_version_is_hashed_and_decimal_scores_are_exact():
    actor = uuid4()
    service = BlueprintService(repository=None, authorizer=StaticAuthorizer({actor: {"paper_blueprint.write"}}))
    value = content()
    first = service.create_version(actor_id=actor, content=value, blueprint_id=None, base_version_id=None)
    second = service.create_version(actor_id=actor, content=value, blueprint_id=None, base_version_id=None)
    assert first.version_no == second.version_no == 1
    assert first.total_score == Decimal("100.00")
    assert sum(s.score for s in first.content.sections[0].slots) == Decimal("100.00")
    assert first.content_hash == second.content_hash

def test_blueprint_rejects_fractional_cent_and_score_mismatch():
    bad_slot = BlueprintSlot("A1", Decimal("0.001"), (), (), (), None, None, (), ())
    with pytest.raises(ValueError, match="two decimal places"):
        BlueprintService.validate_content(bad_slot.score, Decimal("1.00"))
    with pytest.raises(ValueError, match="total_score"):
        BlueprintService.validate_content(Decimal("90.00"), Decimal("100.00"))
```

集成 API 测试另用真实身份 fixture，断言学生和无课程范围教师收到 403，重复 Idempotency-Key 返回同一蓝图版本，同键不同 payload 返回 409。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_blueprints.py tests/integration/assessment/test_blueprint_api.py -q`

预期：FAIL，`BlueprintService` 不存在；不得先通过放宽总分或改用 float 让测试变绿。

- [ ] **步骤 3：实现最少蓝图服务、schema 与路由**

```python
# modules/assessment/blueprints.py
from dataclasses import asdict
from decimal import Decimal
from uuid import UUID, uuid4
from modules.assessment.hashing import canonical_hash
from modules.assessment.models.assembly import PaperBlueprintVersion
from modules.assessment.domain import BlueprintContent

class BlueprintService:
    def __init__(self, repository, authorizer, id_factory=uuid4):
        self.repository, self.authorizer, self.id_factory = repository, authorizer, id_factory

    @staticmethod
    def validate_content(actual: Decimal, expected: Decimal):
        if actual != actual.quantize(Decimal("0.01")):
            raise ValueError("scores must have at most two decimal places")
        if actual != expected:
            raise ValueError("total_score must equal slot score sum")

    def create_version(self, *, actor_id: UUID, content: BlueprintContent,
                       blueprint_id: UUID | None, base_version_id: UUID | None):
        self.authorizer.require(actor_id, "paper_blueprint.write", course_id=content.course_id,
                                offering_id=content.offering_id)
        scores = [slot.score for section in content.sections for slot in section.slots]
        slot_codes = [slot.code for section in content.sections for slot in section.slots]
        if not scores or len({s.code for s in content.sections}) != len(content.sections):
            raise ValueError("sections must be non-empty and uniquely coded")
        if len(set(slot_codes)) != len(slot_codes):
            raise ValueError("slot codes must be globally unique")
        for section in content.sections:
            if len({s.code for s in section.slots}) != len(section.slots):
                raise ValueError("slot codes must be unique within section")
        self.validate_content(sum(scores, Decimal("0.00")), content.total_score)
        if content.purpose == "formal" and (content.offering_id is None or content.review_policy != "formal_dual_review"):
            raise ValueError("formal paper requires offering and dual review")
        stable_id = blueprint_id or self.id_factory()
        previous = self.repository.latest(stable_id) if self.repository else None
        version_no = 1 if previous is None else previous.version_no + 1
        if base_version_id and (previous is None or base_version_id != previous.id):
            raise ValueError("stale base_version_id")
        row = PaperBlueprintVersion(
            id=self.id_factory(), blueprint_id=stable_id, version_no=version_no,
            course_id=content.course_id, offering_id=content.offering_id,
            release_context_id=content.release_context_id, purpose=content.purpose,
            assembly_mode="manual", total_score=content.total_score,
            duration_seconds=content.duration_seconds, blueprint_ast=asdict(content),
            base_version_id=base_version_id,
            content_hash=canonical_hash(asdict(content)), change_set_id=self.id_factory(),
            created_by=actor_id,
        )
        return self.repository.add(row) if self.repository else row
```

`blueprint.schema.json` 将 `total_score` 与 `score` 定义为 `^\\d+\\.\\d{2}$` 字符串，`purpose/review_policy` 使用共享枚举，`additionalProperties=false`；FastAPI 的 Pydantic 模型在字段 validator 中转 `Decimal` 并构造 `BlueprintContent`。路由只暴露 `POST /api/v1/paper-blueprints/{blueprint_id}/versions` 和 `GET /api/v1/paper-blueprint-versions/{id}`，事务内消费幂等键；不注册 assembly-run 或求解路由。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_blueprints.py tests/integration/assessment/test_blueprint_api.py -q`

预期：PASS；覆盖哈希稳定、分值精确、stale base、正式卷双审策略、权限与幂等。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/blueprints.py modules/assessment/models/assembly.py apps/api/routers/paper_blueprints.py packages/contracts/assessment/blueprint.schema.json tests/unit/assessment/test_blueprints.py tests/integration/assessment/test_blueprint_api.py
git commit -m "feat: version manual paper blueprints"
```

### 任务 3：固定 Release 候选资格与基础曝光门禁

**交付物：** `CandidateService.snapshot` 只接收固定 Release Context 内完整审核的候选，保存含 decision_seq 的不可变清单和逐项拒绝原因；`ExposureService.assert_no_conflict` 同时服务候选预检与发布终检。共享 `CandidateEvidence/ExposureEvent` 来自任务 1。

**文件：**
- 创建：`modules/assessment/candidates.py`
- 创建：`modules/assessment/exposure.py`
- 创建：`modules/assessment/models/exposure.py`
- 修改：`apps/api/routers/paper_blueprints.py`
- 测试：`tests/unit/assessment/test_candidates.py`
- 测试：`tests/integration/assessment/test_candidate_eligibility.py`

- [ ] **步骤 1：编写失败的资格与曝光测试**

```python
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from modules.assessment.candidates import CandidateService, MemoryCandidateSource
from modules.assessment.exposure import ExposureConflict, ExposureService
from modules.assessment.domain import CandidateEvidence, ExposureEvent

def candidate():
    now = uuid4()
    return CandidateEvidence(
        now, uuid4(), uuid4(), uuid4(), None, uuid4(), "expression",
        (), (), "medium", 600, {"exercise": 7, "answer": 8, "rubric": 9},
        {"exercise": "approved", "answer": "approved", "rubric": "approved"},
        "a" * 64, "b" * 64, True, True, False, False, False,
        False, True, True, True, True, True, False,
    )

@pytest.mark.parametrize("field", ["in_context", "license_valid", "dependencies_complete", "render_ready", "language_pair_valid", "purpose_allowed"])
def test_ineligible_candidate_never_enters_snapshot(field):
    row = replace(candidate(), **{field: False})
    snap = CandidateService(MemoryCandidateSource([row], [])).snapshot(
        actor_id=uuid4(), blueprint_version_id=uuid4(), purpose="formal", offering_id=uuid4())
    assert snap.accepted == ()
    assert snap.rejected[0].exercise_version_id == row.exercise_version_id

def test_revoked_or_missing_instance_is_rejected():
    revoked = replace(candidate(), latest_decisions={"exercise": "approved", "answer": "revoked", "rubric": "approved"})
    instance = replace(candidate(), parameterized=True, exercise_instance_id=None, instance_approved=False)
    snap = CandidateService(MemoryCandidateSource([revoked, instance], [])).snapshot(
        uuid4(), uuid4(), "formal", uuid4())
    assert len(snap.rejected) == 2 and snap.accepted == ()

def test_released_practice_answer_blocks_formal_use_by_family():
    row = candidate()
    event = ExposureEvent(row.exercise_version_id, None, row.exercise_family_id,
                          uuid4(), "practice_answer_released", datetime.now(UTC), datetime.now(UTC))
    with pytest.raises(ExposureConflict, match="practice_answer_released"):
        ExposureService.assert_no_conflict("formal", uuid4(), (row,), (event,), datetime.now(UTC))
```

集成测试在真实 Release Context 中分别放入 approved→revoked、许可失效、隐藏评测、私人草稿、缺 answer/rubric 决策、参数化未审实例六类 fixture，断言 SQL 查询不会把它们变成 accepted，并验证新 Release 不改变旧 snapshot 哈希。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_candidates.py tests/integration/assessment/test_candidate_eligibility.py -q`

预期：FAIL，`CandidateService` 与 `ExposureService` 不存在。

- [ ] **步骤 3：实现最少资格判定、快照与曝光服务**

```python
# modules/assessment/exposure.py
from datetime import datetime
from uuid import UUID
from modules.assessment.domain import CandidateEvidence, ExposureEvent

class ExposureConflict(ValueError): pass

class ExposureService:
    @staticmethod
    def assert_no_conflict(purpose: str, offering_id: UUID | None,
                           candidates: tuple[CandidateEvidence, ...],
                           events: tuple[ExposureEvent, ...], visible_from: datetime):
        withdrawn = {e.exercise_family_id for e in events if e.event_kind == "withdrawn"}
        for item in candidates:
            related = [e for e in events if e.exercise_family_id == item.exercise_family_id]
            if purpose == "formal":
                leaked = next((e for e in related if e.event_kind in {
                    "practice_answer_released", "formal_answer_released"}), None)
                if leaked:
                    raise ExposureConflict(f"{leaked.event_kind}: {item.exercise_version_id}")
                other_class = next((e for e in related if e.event_kind in {"secure_assigned", "student_opened"}
                                    and e.scope_offering_id not in {None, offering_id}
                                    and e.exercise_family_id not in withdrawn), None)
                if other_class:
                    raise ExposureConflict(f"overlapping secure offering: {item.exercise_version_id}")
```

```python
# modules/assessment/candidates.py
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4
from modules.assessment.exposure import ExposureConflict, ExposureService
from modules.assessment.hashing import canonical_hash
from modules.assessment.domain import CandidateEvidence

@dataclass(frozen=True)
class RejectedCandidate:
    exercise_version_id: UUID
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class CandidateSnapshotView:
    id: UUID
    blueprint_version_id: UUID
    accepted: tuple[CandidateEvidence, ...]
    rejected: tuple[RejectedCandidate, ...]
    candidate_manifest_hash: str

class MemoryCandidateSource:
    def __init__(self, rows, events): self.rows, self.events = rows, events
    def load_evidence(self, blueprint_version_id): return self.rows
    def load_exposure(self, rows): return self.events

class CandidateService:
    def __init__(self, source, repository=None): self.source, self.repository = source, repository

    @staticmethod
    def eligibility_errors(c: CandidateEvidence) -> tuple[str, ...]:
        checks = {
            "outside_release_context": c.in_context,
            "license_invalid": c.license_valid,
            "revoked": not c.revoked,
            "hidden_evaluation": not c.hidden_evaluation,
            "private_draft": not c.private_draft,
            "latest_decision_not_approved": bool(c.decision_seqs) and all(
                c.latest_decisions.get(k) == "approved" for k in c.decision_seqs),
            "missing_approved_instance": not c.parameterized or (
                c.exercise_instance_id is not None and c.instance_approved),
            "dependencies_incomplete": c.dependencies_complete,
            "render_not_ready": c.render_ready,
            "language_pair_invalid": c.language_pair_valid,
            "purpose_conflict": c.purpose_allowed,
        }
        return tuple(name for name, valid in checks.items() if not valid)

    def snapshot(self, actor_id: UUID, blueprint_version_id: UUID,
                 purpose: str, offering_id: UUID | None) -> CandidateSnapshotView:
        rows = tuple(self.source.load_evidence(blueprint_version_id))
        events = tuple(self.source.load_exposure(rows))
        accepted_rows, rejected_rows = [], []
        for candidate in rows:
            reasons = list(self.eligibility_errors(candidate))
            if not reasons:
                try:
                    ExposureService.assert_no_conflict(purpose, offering_id, (candidate,), events, datetime.now(UTC))
                except ExposureConflict:
                    reasons.append("exposure_conflict")
            (rejected_rows if reasons else accepted_rows).append((candidate, tuple(reasons)))
        accepted = tuple(c for c, _ in accepted_rows)
        rejected = tuple(RejectedCandidate(c.exercise_version_id, reasons) for c, reasons in rejected_rows)
        manifest = {"accepted": [asdict(c) for c in sorted(accepted, key=lambda x: str(x.exercise_version_id))],
                    "rejected": [asdict(r) for r in sorted(rejected, key=lambda x: str(x.exercise_version_id))],
                    "exposure_events": [asdict(e) for e in sorted(events, key=lambda x: (str(x.exercise_family_id), x.event_kind, x.occurred_at))]}
        snap = CandidateSnapshotView(uuid4(), blueprint_version_id, accepted, rejected, canonical_hash(manifest))
        if self.repository:
            self.repository.add(snap, manifest, actor_id)
        return snap
```

生产 source 使用固定 `release_context_id` 沿 base 链解析 `release_item`，通过窗口函数 `row_number() OVER (PARTITION BY dependency_key ORDER BY decision_seq DESC)` 取最新终态，并逐项核对答案、解析、量规、实例、资产、数学约定和许可；禁止读取实体的“当前最新版本”。`GET /api/v1/paper-blueprint-versions/{id}/candidates` 返回 accepted、拒绝原因与 manifest hash，不返回答案正文。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_candidates.py tests/integration/assessment/test_candidate_eligibility.py -q`

预期：PASS；六类不合格对象均被拒绝，快照在 Release 更新后保持原哈希，练习答案与跨班保密曝光阻断正式候选。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/candidates.py modules/assessment/exposure.py modules/assessment/models/exposure.py apps/api/routers/paper_blueprints.py tests/unit/assessment/test_candidates.py tests/integration/assessment/test_candidate_eligibility.py
git commit -m "feat: snapshot eligible paper candidates with exposure gates"
```

### 任务 4：手工建卷、替换、调序与调分

**交付物：** `PaperService.create_manual_draft` 从蓝图和候选快照建立 paper/section/slot；同一服务支持草稿替换候选、键盘调序和 Decimal 调分。所有编辑携带 `base_version=edit_seq`，每次修改重算 manifest 并清除旧审核哈希；批准卷只能克隆新草稿，不能原位改题。

**文件：**
- 创建：`modules/assessment/papers.py`
- 创建：`modules/assessment/models/paper.py`
- 创建：`apps/api/routers/papers.py`
- 创建：`packages/contracts/assessment/paper.schema.json`
- 测试：`tests/unit/assessment/test_papers.py`
- 测试：`tests/integration/assessment/test_paper_editing.py`

- [ ] **步骤 1：编写失败的建卷与编辑测试**

```python
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
import pytest
from modules.assessment.papers import EditOperation, PaperService, StalePaperVersion

def test_manual_draft_tracks_candidate_identity_and_edit_sequence(paper_factory):
    blueprint, snapshot = paper_factory()
    service = PaperService(repository=None)
    chosen = snapshot.accepted[0]
    paper = service.create_manual_draft(
        actor_id=uuid4(), blueprint_version_id=uuid4(), blueprint=blueprint,
        snapshot=snapshot, selections=(slot_selection("A1", chosen),), paper_id=None)
    slot = paper.sections[0].slots[0]
    assert slot.exercise_version_id == chosen.exercise_version_id
    assert slot.exercise_instance_id == chosen.exercise_instance_id
    assert slot.source_release_id == chosen.source_release_id
    edited = service.edit_draft(paper, base_version=1, snapshot=snapshot, operations=(
        EditOperation("score", slot.id, score=Decimal("45.50")),))
    assert edited.edit_seq == 2
    assert edited.sections[0].slots[0].score == Decimal("45.50")
    assert edited.manifest_hash != paper.manifest_hash
    with pytest.raises(StalePaperVersion):
        service.edit_draft(edited, base_version=1, snapshot=snapshot, operations=())

def test_non_draft_and_cross_snapshot_replacement_are_rejected(paper_factory):
    blueprint, snapshot = paper_factory()
    service = PaperService(repository=None)
    paper = service.create_manual_draft(uuid4(), uuid4(), blueprint, snapshot,
                                        (slot_selection("A1", snapshot.accepted[0]),), None)
    approved = replace(paper, status="approved")
    with pytest.raises(ValueError, match="draft"):
        service.edit_draft(approved, 1, snapshot, ())
    stranger = replace(snapshot.accepted[0], exercise_version_id=uuid4())
    with pytest.raises(ValueError, match="candidate snapshot"):
        service.edit_draft(paper, 1, snapshot,
            (EditOperation("replace", paper.sections[0].slots[0].id, candidate=stranger),))
```

`paper_factory` 在测试文件中构造一个 A1 槽位、100 分蓝图和两个 `CandidateEvidence`；`slot_selection(code, candidate)` 返回共享 `SlotSelection`。集成测试使用 SQLAlchemy repository 验证 section/sort_order 唯一约束、edit_seq 乐观锁、approved trigger 和 approved 克隆产生 `(paper_id, version_no+1)`。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_papers.py tests/integration/assessment/test_paper_editing.py -q`

预期：FAIL，`PaperService` 与 `EditOperation` 不存在。

- [ ] **步骤 3：实现最少 PaperService 与 PATCH 契约**

```python
# modules/assessment/papers.py
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from uuid import UUID, uuid4
from modules.assessment.hashing import canonical_hash
from modules.assessment.domain import (
    BlueprintContent, PaperSectionView, PaperSlotView, PaperStatus, PaperView, SlotSelection,
)
from modules.assessment.candidates import CandidateSnapshotView

class StalePaperVersion(ValueError): pass

@dataclass(frozen=True)
class EditOperation:
    kind: str
    slot_id: UUID
    candidate: object | None = None
    target_slot_no: int | None = None
    score: Decimal | None = None

class PaperService:
    def __init__(self, repository): self.repository = repository

    @staticmethod
    def manifest(paper: PaperView) -> str:
        value = asdict(paper)
        value.update(status="draft", review_manifest_hash=None, edit_seq=0)
        return canonical_hash(value)

    def create_manual_draft(self, actor_id: UUID, blueprint_version_id: UUID,
                            blueprint: BlueprintContent, snapshot: CandidateSnapshotView,
                            selections: tuple[SlotSelection, ...], paper_id: UUID | None):
        by_version = {c.exercise_version_id: c for c in snapshot.accepted}
        try:
            chosen = {s.slot_code: by_version[s.exercise_version_id] for s in selections}
        except KeyError as exc:
            raise ValueError("candidate snapshot") from exc
        sections = []
        for order, section in enumerate(blueprint.sections, 1):
            slots = []
            for number, spec in enumerate(section.slots, 1):
                c = chosen.get(spec.code)
                slots.append(PaperSlotView(uuid4(), section.code, spec.code, number,
                    c.exercise_id if c else None, c.exercise_version_id if c else None,
                    c.exercise_instance_id if c else None, c.exercise_family_id if c else None,
                    c.source_release_id if c else None, spec.score,
                    c.estimated_seconds if c else spec.estimated_seconds,
                    {"language_pair": "fixed", "option_order": "fixed"},
                    c.dependency_manifest_hash if c else None))
            sections.append(PaperSectionView(section.code, section.title, section.instruction,
                order, sum((s.score for s in slots), Decimal("0.00")), tuple(slots)))
        paper = PaperView(uuid4(), blueprint_version_id, blueprint.release_context_id,
            blueprint.course_id, blueprint.offering_id, blueprint.purpose, PaperStatus.DRAFT,
            blueprint.total_score, blueprint.duration_seconds, 1, "", None, tuple(sections))
        paper = replace(paper, manifest_hash=self.manifest(paper))
        return self.repository.create(paper, snapshot.id, actor_id, paper_id) if self.repository else paper

    def edit_draft(self, paper: PaperView, base_version: int,
                   snapshot: CandidateSnapshotView, operations: tuple[EditOperation, ...]):
        if paper.status != PaperStatus.DRAFT: raise ValueError("only draft can be edited")
        if paper.edit_seq != base_version: raise StalePaperVersion("stale_version")
        candidates = {c.exercise_version_id: c for c in snapshot.accepted}
        sections = list(paper.sections)
        for op in operations:
            for si, section in enumerate(sections):
                slots = list(section.slots)
                for i, slot in enumerate(slots):
                    if slot.id != op.slot_id: continue
                    if op.kind == "replace":
                        c = candidates.get(op.candidate.exercise_version_id)
                        if c is None or c.exercise_version_id not in candidates:
                            raise ValueError("candidate snapshot does not contain replacement")
                        slots[i] = replace(slot, exercise_id=c.exercise_id,
                            exercise_version_id=c.exercise_version_id,
                            exercise_instance_id=c.exercise_instance_id,
                            exercise_family_id=c.exercise_family_id,
                            source_release_id=c.source_release_id,
                            dependency_manifest_hash=c.dependency_manifest_hash)
                    elif op.kind == "move":
                        if not 1 <= op.target_slot_no <= len(slots): raise ValueError("invalid slot order")
                        moved = slots.pop(i); slots.insert(op.target_slot_no - 1, moved)
                        slots = [replace(s, slot_no=n) for n, s in enumerate(slots, 1)]
                    elif op.kind == "score":
                        if op.score is None or op.score != op.score.quantize(Decimal("0.01")):
                            raise ValueError("score must use two decimal places")
                        slots[i] = replace(slot, score=op.score)
                    else: raise ValueError("unknown edit operation")
                sections[si] = replace(section, slots=tuple(slots),
                    section_score=sum((s.score for s in slots), Decimal("0.00")))
        edited = replace(paper, sections=tuple(sections), edit_seq=paper.edit_seq + 1,
                         review_manifest_hash=None)
        edited = replace(edited, manifest_hash=self.manifest(edited))
        return self.repository.save_edit(edited, base_version) if self.repository else edited
```

`create_manual_draft` 对 selections 中不属于快照的 ID 直接抛 `ValueError("candidate snapshot")`。SQL repository 在同一事务写 paper、section、slot，`UPDATE ... WHERE id=:id AND edit_seq=:base AND status='draft'` 影响行数必须为 1；编辑时将旧 `submitted_manifest_hash` 清空并把仍开放的 review round 标为 `invalidated`。

路由提供 `POST /api/v1/paper-versions`、`PATCH /api/v1/paper-versions/{id}/slots`、`GET /api/v1/paper-versions/{id}`；请求 `base_version` 映射 edit_seq，操作 kind 只允许 `replace/move/score`，不暴露 status 字段。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_papers.py tests/integration/assessment/test_paper_editing.py -q`

预期：PASS；草稿可保存总分错误的中间状态，旧 edit_seq 返回 409，approved 内容 UPDATE 被数据库拒绝，替换永远来自同一 candidate snapshot。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/papers.py modules/assessment/models/paper.py apps/api/routers/papers.py modules/assessment/domain.py packages/contracts/assessment/paper.schema.json tests/unit/assessment/test_papers.py tests/integration/assessment/test_paper_editing.py
git commit -m "feat: create and edit manual fixed paper drafts"
```

### 任务 5：独立整卷校验器

**交付物：** `PaperValidationService.validate` 不依赖任何组卷算法，直接读取蓝图、paper/section/slot、当前候选证据和曝光事件，返回机器可读硬约束错误；草稿可保存错误，提交审核与 assignment 发布必须零错误。

**文件：**
- 创建：`modules/assessment/validation.py`
- 修改：`apps/api/routers/papers.py`
- 测试：`tests/unit/assessment/test_paper_validation.py`
- 测试：`tests/integration/assessment/test_paper_validation.py`

- [ ] **步骤 1：编写失败的独立校验测试**

```python
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4
from modules.assessment.validation import PaperValidationService

def issue_codes(result): return {i.code for i in result}

def test_score_slot_and_duplicate_rules_are_independent(paper_factory):
    blueprint, snapshot = paper_factory()
    paper = make_two_slot_paper(blueprint, snapshot)
    duplicate = snapshot.accepted[0]
    paper = replace(paper, sections=(replace(paper.sections[0], slots=(
        paper.sections[0].slots[0], replace(paper.sections[0].slots[1],
            exercise_id=duplicate.exercise_id, exercise_version_id=duplicate.exercise_version_id,
            score=Decimal("1.00")))),))
    result = PaperValidationService.validate(paper, blueprint, snapshot.accepted, (), now=utc_now())
    assert {"section_score_mismatch", "total_score_mismatch", "duplicate_exercise"} <= issue_codes(result)

def test_missing_time_is_not_zero_and_constraints_are_checked(paper_factory):
    blueprint, snapshot = paper_factory()
    blueprint = replace(blueprint, hard_constraints={"duration_required": True, "duration_max_seconds": 100})
    paper = make_two_slot_paper(blueprint, snapshot)
    slots = paper.sections[0].slots
    paper = replace(paper, sections=(replace(paper.sections[0], slots=(
        replace(slots[0], estimated_seconds=None), slots[1])),))
    result = PaperValidationService.validate(paper, blueprint, snapshot.accepted, (), now=utc_now())
    assert "estimated_seconds_missing" in issue_codes(result)
    assert "duration_exceeded" not in issue_codes(result)  # 缺失时不得虚构总时长结论

def test_current_revocation_and_released_answer_block_validation(paper_factory):
    blueprint, snapshot = paper_factory()
    paper = make_two_slot_paper(blueprint, snapshot)
    revoked = replace(snapshot.accepted[0], revoked=True)
    current = (revoked,) + snapshot.accepted[1:]
    result = PaperValidationService.validate(paper, blueprint, current, (), now=utc_now())
    assert "candidate_ineligible" in issue_codes(result)
```

`make_two_slot_paper` 在测试文件中使用共享 `PaperView/PaperSectionView/PaperSlotView` 构造两个不同 exercise_id 的 40+60 分槽位；`utc_now` 返回固定带时区时间。集成测试再覆盖必选/排除题、concept/outcome 覆盖、同族/共享材料/泄题组、语言配对和发布时曝光冲突。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_paper_validation.py tests/integration/assessment/test_paper_validation.py -q`

预期：FAIL，`PaperValidationService` 不存在。

- [ ] **步骤 3：实现最少确定性校验器**

```python
# modules/assessment/validation.py
from collections import Counter
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from modules.assessment.candidates import CandidateService
from modules.assessment.exposure import ExposureConflict, ExposureService
from modules.assessment.domain import BlueprintContent, CandidateEvidence, PaperView, ValidationIssue

class PaperValidationService:
    @staticmethod
    def validate(paper: PaperView, blueprint: BlueprintContent,
                 current_candidates: tuple[CandidateEvidence, ...],
                 exposure_events: tuple, *, now: datetime) -> tuple[ValidationIssue, ...]:
        issues = []
        slots = [s for section in paper.sections for s in section.slots]
        specs = {s.code: s for section in blueprint.sections for s in section.slots}
        blueprint_sections = {section.code: section for section in blueprint.sections}
        candidates = {c.exercise_version_id: c for c in current_candidates}
        for section in paper.sections:
            section_sum = sum((s.score for s in section.slots), Decimal("0.00"))
            if section_sum != section.section_score:
                issues.append(ValidationIssue("section_score_mismatch", section.code))
            target = sum((s.score for s in blueprint_sections[section.code].slots), Decimal("0.00"))
            if section_sum != target:
                issues.append(ValidationIssue("blueprint_section_score_mismatch", section.code))
            # 槽位权威分允许教师调整，因此 section_score 与 slot 之和必须一致，蓝图只用于覆盖规则。
            if not section.slots: issues.append(ValidationIssue("empty_section", section.code))
        total = sum((s.score for s in slots), Decimal("0.00"))
        if total != paper.total_score or total != blueprint.total_score:
            issues.append(ValidationIssue("total_score_mismatch", str(total)))
        if any(s.exercise_version_id is None for s in slots):
            issues.append(ValidationIssue("slot_unfilled", "every slot needs one candidate"))
        ids = [s.exercise_id for s in slots if s.exercise_id]
        families = [s.exercise_family_id for s in slots if s.exercise_family_id]
        issues += [ValidationIssue("duplicate_exercise", str(x)) for x, n in Counter(ids).items() if n > 1]
        issues += [ValidationIssue("exercise_family_conflict", str(x)) for x, n in Counter(families).items() if n > 1]
        for slot in slots:
            if slot.exercise_version_id is None: continue
            c = candidates.get(slot.exercise_version_id)
            if c is None or CandidateService.eligibility_errors(c):
                issues.append(ValidationIssue("candidate_ineligible", str(slot.exercise_version_id), slot.id))
                continue
            spec = specs[slot.code]
            if c.exercise_kind not in spec.exercise_kinds:
                issues.append(ValidationIssue("exercise_kind_mismatch", spec.code, slot.id))
            if not set(spec.required_concept_ids) <= set(c.concept_ids):
                issues.append(ValidationIssue("concept_coverage_missing", spec.code, slot.id))
            if not set(spec.learning_outcome_ids) <= set(c.learning_outcome_ids):
                issues.append(ValidationIssue("outcome_coverage_missing", spec.code, slot.id))
            if c.exercise_version_id in spec.excluded_exercise_version_ids:
                issues.append(ValidationIssue("excluded_exercise", spec.code, slot.id))
        selected = {s.exercise_version_id for s in slots}
        for spec in specs.values():
            for required in spec.mandatory_exercise_version_ids:
                if required not in selected:
                    issues.append(ValidationIssue("mandatory_exercise_missing", str(required)))
        if blueprint.hard_constraints.get("duration_required"):
            if any(s.estimated_seconds is None for s in slots):
                issues.append(ValidationIssue("estimated_seconds_missing", "duration cannot treat unknown as zero"))
            else:
                maximum = int(blueprint.hard_constraints.get("duration_max_seconds", paper.duration_seconds))
                if sum(s.estimated_seconds for s in slots) > maximum:
                    issues.append(ValidationIssue("duration_exceeded", str(maximum)))
        for group_key, code in (("conflict_groups", "shared_material_conflict"), ("leak_groups", "mutual_answer_leak")):
            for group in blueprint.hard_constraints.get(group_key, []):
                if len(selected.intersection({UUID(x) for x in group})) > 1:
                    issues.append(ValidationIssue(code, ",".join(group)))
        try:
            selected_candidates = tuple(candidates[s.exercise_version_id] for s in slots
                                        if s.exercise_version_id in candidates)
            ExposureService.assert_no_conflict(paper.purpose, paper.offering_id,
                                               selected_candidates, tuple(exposure_events), now)
        except ExposureConflict as exc:
            issues.append(ValidationIssue("exposure_conflict", str(exc)))
        return tuple(issues)
```

`POST /api/v1/paper-versions/{id}/validate` 每次重新获取当前决策与曝光，将同一结果写入 `governance.quality_check/finding`，响应包含 `manifest_hash` 和有序 issue 列表；前端不能把 issue 标记为已接受。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_paper_validation.py tests/integration/assessment/test_paper_validation.py -q`

预期：PASS；100.00 分使用 Decimal 精确比较，缺失时长显示未知，已发布卷硬约束错误数为 0。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/validation.py apps/api/routers/papers.py tests/unit/assessment/test_paper_validation.py tests/integration/assessment/test_paper_validation.py
git commit -m "feat: validate whole fixed papers independently"
```

### 任务 6：学生卷、答案卷、量规卷分离渲染与 worker

**交付物：** `PaperRenderService.render` 为同一 paper_version 生成三个独立 PDF 和 `paper_render`，学生输入在数据层白名单化；复用任务 1 Artifact，不依赖 agent run。教师请求进入 DB job/outbox，worker 只能渲染，不能审核或发布。

**文件：**
- 创建：`modules/assessment/rendering.py`
- 创建：`modules/assessment/artifacts.py`
- 创建：`apps/api/routers/paper_renders.py`
- 创建：`apps/worker/render_paper.py`
- 创建：`packages/contracts/assessment/render.schema.json`
- 测试：`tests/unit/assessment/test_rendering.py`
- 测试：`tests/integration/assessment/test_render_security.py`

- [ ] **步骤 1：编写失败的白名单与 PDF 测试**

```python
from uuid import uuid4
import pytest
from modules.assessment.rendering import PaperRenderService, build_student_projection

def test_student_projection_drops_all_sensitive_keys_before_template():
    row = {"slot_id": uuid4(), "question_no": "1", "statement_ast": {"text": "证明连续"},
           "public_assets": [], "response_kind": "proof", "answer_space_lines": 8,
           "answer": "SECRET", "rubric": {"steps": [5]}, "answer_test": "x=1",
           "solution": "SECRET", "hidden_note": "SECRET"}
    projection = build_student_projection(uuid4(), "ABC123", "数学分析 I", "闭卷", [row])
    dumped = repr(projection)
    assert "SECRET" not in dumped and "rubric" not in dumped and "answer_test" not in dumped
    assert projection.slots[0].statement_ast == {"text": "证明连续"}

def test_render_kinds_have_distinct_artifact_security_flags(render_factory):
    paper, source, artifacts, auth = render_factory()
    service = PaperRenderService(source, artifacts, auth, renderer=identity_pdf)
    student = service.render(uuid4(), paper.id, "student")
    answer = service.render(uuid4(), paper.id, "answer")
    rubric = service.render(uuid4(), paper.id, "rubric")
    assert student.contains_solution is False and student.contains_answer_test is False
    assert answer.contains_solution is True and rubric.contains_solution is True
    assert len({student.output_hash, answer.output_hash, rubric.output_hash}) == 3
```

`identity_pdf` 是测试 renderer，接收模板上下文并返回以 `%PDF-1.4` 开头的合成字节；集成测试用真实 WeasyPrint 和 pypdf 在三份 PDF 的文本层、metadata、annotations、attachments 中扫描 `SECRET_ANSWER`，只允许答案/量规卷出现，学生卷及授权日志不得出现。撤回 paper 后三种新渲染均返回 410。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_rendering.py tests/integration/assessment/test_render_security.py -q`

预期：FAIL，白名单函数与渲染服务不存在。

- [ ] **步骤 3：实现最少白名单、渲染服务与 worker 入口**

```python
# modules/assessment/rendering.py
from uuid import UUID
from modules.agent.artifacts import ArtifactMetadata
from modules.assessment.hashing import canonical_hash
from modules.assessment.domain import StudentPaperProjection, StudentSlotProjection

STUDENT_KEYS = ("slot_id", "question_no", "statement_ast", "public_assets", "response_kind", "answer_space_lines")

def build_student_projection(paper_id: UUID, short_code: str, course_title: str,
                             instructions: str, rows: list[dict]) -> StudentPaperProjection:
    slots = tuple(StudentSlotProjection(**{key: row[key] for key in STUDENT_KEYS}) for row in rows)
    return StudentPaperProjection(paper_id, short_code, course_title, instructions, slots)

class PaperRenderService:
    def __init__(self, source, artifacts, authorizer, renderer, repository=None):
        self.source, self.artifacts, self.auth = source, artifacts, authorizer
        self.renderer, self.repository = renderer, repository

    def render(self, actor_id: UUID, paper_version_id: UUID, render_kind: str):
        paper = self.source.paper(paper_version_id)
        if paper.status == "withdrawn": raise LookupError("withdrawn")
        self.auth.require(actor_id, f"paper.render.{render_kind}", course_id=paper.course_id,
                          offering_id=paper.offering_id)
        if render_kind == "student":
            projection = build_student_projection(paper_version_id, paper.manifest_hash[:8],
                self.source.course_title(paper.course_id), "按题号作答", self.source.student_rows(paper_version_id))
            context, contains_solution = projection, False
        elif render_kind == "answer":
            context, contains_solution = self.source.answer_context(paper_version_id), True
        elif render_kind == "rubric":
            context, contains_solution = self.source.rubric_context(paper_version_id), True
        else:
            raise ValueError("invalid render_kind")
        security_hash = canonical_hash({"kind": render_kind, "input": context})
        body = self.renderer(template="paper-v1", kind=render_kind, context=context)
        artifact = self.artifacts.register(actor_id, body, ArtifactMetadata(
            paper.release_context_id, "private", contains_solution, False,
            "application/pdf", "paper_render"))
        if self.repository:
            self.repository.add_render(paper_version_id, render_kind, artifact, security_hash, actor_id)
        return artifact
```

生产 source 的 `student_rows` 是显式 SQL SELECT，不用 ORM 全对象序列化；答案/量规 context 也不读取 answer_test。Jinja2 模板只使用转义文本、MathML 和授权公开资产；WeasyPrint 使用固定 `paper-v1`、容器内锁定字体和 `font_manifest_hash`。pypdf 重写 metadata 为课程名、版本短码、render_kind，不复制源正文到日志。

```python
# apps/worker/render_paper.py
from celery import shared_task

@shared_task(bind=True, autoretry_for=(TimeoutError,), max_retries=3)
def render_paper(self, job_id: str):
    from apps.worker.container import build_render_service, jobs
    with jobs.claim_once(job_id, worker_id=self.request.id) as job:
        if job is None:
            return {"status": "duplicate"}
        artifact = build_render_service().render(job.requested_by, job.paper_version_id, job.render_kind)
        jobs.mark_done(job_id, artifact.id)
        return {"status": "ready", "artifact_version_id": str(artifact.id)}
```

`jobs.claim_once` 使用 `ops.job` 行锁与 lease，`jobs.mark_done` 在同一数据库事务写完成状态和 outbox 回执；`build_render_service` 只装配本地内容 repository、Artifact 与 PDF renderer，不装配模型网关。

`POST /api/v1/paper-versions/{id}/renders` 校验权限后在同事务创建 `ops.job/outbox_event`；`paper_render.paper_manifest_hash` 固定本次输入，`ready_render_kinds` 只返回与当前 paper manifest 相同的渲染，草稿编辑后三份旧预览自动失去提交审核与发布资格。`apps/worker/render_paper.py` 通过 `job_id` 调用同一服务并用唯一 event ID 去重。`GET /api/v1/paper-renders/{id}/download` 重新校验 paper 未撤回、render 权限和 Artifact 权限，写访问审计后返回短时 URL；学生在线可见不产生下载权限。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_rendering.py tests/integration/assessment/test_render_security.py -q`

预期：PASS；三份输出哈希不同，学生 PDF 各通道无答案/量规/answer_test，worker 重试不重复 Artifact，越权与撤回下载被拒绝。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/rendering.py modules/assessment/artifacts.py apps/api/routers/paper_renders.py apps/worker/render_paper.py packages/contracts/assessment/render.schema.json tests/unit/assessment/test_rendering.py tests/integration/assessment/test_render_security.py
git commit -m "feat: render fixed papers into isolated print artifacts"
```

### 任务 7：整卷提交与双复核证据哈希

**交付物：** `PaperReviewService.submit` 只在独立校验通过且三类渲染 ready 后冻结 submitted manifest；`PaperReviewService.approve` 逐人追加 governance 决策，正式卷须两名不同授权、非贡献者的人类复核，最终聚合为 `review_manifest_hash`。paper 不保存单个 review_decision_item_id。

**文件：**
- 创建：`modules/assessment/review.py`
- 修改：`apps/api/routers/papers.py`
- 测试：`tests/unit/assessment/test_paper_review.py`
- 测试：`tests/integration/assessment/test_paper_dual_review.py`

- [ ] **步骤 1：编写失败的双审与失效测试**

```python
from uuid import uuid4
import pytest
from modules.assessment.review import PaperReviewService

def test_formal_paper_requires_two_distinct_noncontributor_humans(review_factory):
    paper, store, auth = review_factory(required=2, status="draft")
    service = PaperReviewService(store, auth, validator=lambda *_: ())
    service.submit(uuid4(), paper)
    first, second = uuid4(), uuid4()
    service.approve(first, paper.id)
    assert store.paper.status == "in_review" and store.paper.review_manifest_hash is None
    service.approve(second, paper.id)
    assert store.paper.status == "approved"
    assert store.review_manifest()["manifest_hash"] == paper.manifest_hash
    assert len(store.review_manifest()["decisions"]) == 2

def test_same_reviewer_worker_or_contributor_cannot_complete_dual_review(review_factory):
    paper, store, auth = review_factory(required=2)
    service = PaperReviewService(store, auth, validator=lambda *_: ())
    actor = uuid4()
    service.approve(actor, paper.id)
    service.approve(actor, paper.id)
    assert len(store.latest_decisions(paper.id, paper.manifest_hash)) == 1
    assert store.paper.status == "in_review"
    store.contributors.add(paper.manifest_hash)
    with pytest.raises(PermissionError, match="contributor"):
        service.approve(uuid4(), paper.id)
    auth.service_accounts.add(uuid4())
    worker = next(iter(auth.service_accounts))
    with pytest.raises(PermissionError, match="human"):
        service.approve(worker, paper.id)

def test_manifest_change_invalidates_prior_approval(review_factory):
    paper, store, auth = review_factory(required=2)
    service = PaperReviewService(store, auth, validator=lambda *_: ())
    service.approve(uuid4(), paper.id)
    store.paper.manifest_hash = "f" * 64
    with pytest.raises(ValueError, match="manifest"):
        service.approve(uuid4(), paper.id)
```

`review_factory` 在测试文件按 `status` 参数建立 paper（默认 `in_review`）、已冻结 review round、三类 ready render 和内存 governance store；`store.review_manifest()` 返回服务写入的哈希输入。集成测试使用两个真实人类账号和 `governance.review_decision/item`，检查 decision_seq、贡献者、课程范围、重复审批幂等、退回后 draft 以及内容修改使开放 round invalidated。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_paper_review.py tests/integration/assessment/test_paper_dual_review.py -q`

预期：FAIL，`PaperReviewService` 不存在。

- [ ] **步骤 3：实现最少双审服务**

```python
# modules/assessment/review.py
from dataclasses import replace
from uuid import UUID
from modules.assessment.hashing import canonical_hash
from modules.assessment.domain import ReviewEvidence

class PaperReviewService:
    def __init__(self, store, authorizer, validator):
        self.store, self.auth, self.validator = store, authorizer, validator

    def submit(self, actor_id: UUID, paper):
        self.auth.require(actor_id, "paper.submit", course_id=paper.course_id, offering_id=paper.offering_id)
        if paper.status != "draft": raise ValueError("only draft can be submitted")
        issues = self.validator(paper)
        if issues: raise ValueError(f"hard_constraint_violation: {issues}")
        if set(self.store.ready_render_kinds(paper.id)) != {"student", "answer", "rubric"}:
            raise ValueError("three current renders required")
        self.store.open_round(paper.id, paper.manifest_hash, required=2 if paper.purpose == "formal" else 1,
                              submitted_by=actor_id)
        self.store.set_status(paper.id, "in_review", submitted_manifest_hash=paper.manifest_hash)

    @staticmethod
    def review_manifest(paper, required: int, decisions: tuple[ReviewEvidence, ...]):
        return {"paper_version_id": str(paper.id), "manifest_hash": paper.manifest_hash,
                "required_approvals": required,
                "decisions": [{"reviewer": str(d.reviewer_user_id), "decision_seq": d.decision_seq}
                              for d in sorted(decisions, key=lambda d: str(d.reviewer_user_id))]}

    def approve(self, actor_id: UUID, paper_id: UUID):
        paper = self.store.paper(paper_id)
        self.auth.require(actor_id, "paper.review", course_id=paper.course_id, offering_id=paper.offering_id)
        if not self.auth.is_human(actor_id): raise PermissionError("human reviewer required")
        if paper.status != "in_review" or self.store.submitted_hash(paper_id) != paper.manifest_hash:
            raise ValueError("review manifest changed")
        if self.store.is_contributor(actor_id, paper.manifest_hash): raise PermissionError("contributor cannot review")
        round_ = self.store.open_round_for(paper_id, paper.manifest_hash)
        previous = self.store.latest_decisions(paper_id, paper.manifest_hash)
        if any(d.reviewer_user_id == actor_id and d.decision == "approved" for d in previous):
            return
        seq = self.store.append_governance_decision(actor_id, paper_id, paper.manifest_hash, "approved")
        decisions = previous + (ReviewEvidence(actor_id, seq, paper.manifest_hash, "approved", True, True, False),)
        valid = tuple(d for d in decisions if d.decision == "approved" and d.human and d.authorized
                      and not d.contributor and d.manifest_hash == paper.manifest_hash)
        distinct = {d.reviewer_user_id: d for d in valid}
        if len(distinct) < round_.required_approvals: return
        manifest = self.review_manifest(paper, round_.required_approvals, tuple(distinct.values()))
        self.store.approve(paper_id, review_manifest_hash=canonical_hash(manifest), manifest=manifest)
```

生产 store 将每次签署写为 `review_decision + review_decision_item(entity_type='paper_version', entity_version_id=paper_id, dependency_hash=manifest_hash)`，同时保存授权与资格快照；`approve` 使用 paper 行锁，发布时按相同 `review_manifest` 规则重算并核对最新 decision_seq。`request_changes` 追加 rejected 事实、关闭 round、状态回 draft，不删除历史。路由为 `POST /submit-review`、`POST /approve`、`POST /request-changes`，worker 身份在依赖层即被拒绝。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/unit/assessment/test_paper_review.py tests/integration/assessment/test_paper_dual_review.py -q`

预期：PASS；一审后仍不可发布，同人/贡献者/worker 不能完成双审，内容哈希变化使全部旧复核失效。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/review.py apps/api/routers/papers.py tests/unit/assessment/test_paper_review.py tests/integration/assessment/test_paper_dual_review.py
git commit -m "feat: require manifest-bound dual paper review"
```

### 任务 8：assignment 固定投影、发布终检与严重撤回

**交付物：** `AssignmentService.publish` 固定完整 `attempt_policy_version`、paper、Release Context、时间范围并生成不可变 assignment_item；同事务写曝光事件。`PaperService.withdraw` 立即阻断新发布和新下载、撤回相关 assignment，但保留全部历史投影和审计。

**文件：**
- 创建：`modules/assessment/assignments.py`
- 创建：`modules/assessment/models/attempt.py`
- 创建：`modules/assessment/models/grading.py`
- 修改：`modules/assessment/papers.py`
- 创建：`apps/api/routers/assignments.py`
- 创建：`packages/contracts/assessment/assignment.schema.json`
- 测试：`tests/integration/assessment/test_assignment_projection.py`
- 测试：`tests/integration/assessment/test_withdrawal.py`

- [ ] **步骤 1：编写失败的发布、漂移与撤回测试**

```python
from datetime import timedelta
import pytest
from modules.assessment.assignments import AssignmentService, TimeWindow

def test_publish_validates_time_and_freezes_all_source_ids(assignment_factory):
    paper, policy, store, auth = approved_dual_reviewed_fixture()
    service = AssignmentService(store, auth, validator=lambda *_: ())
    times = TimeWindow(visible=utc_now(), due=utc_now()+timedelta(days=1),
                       hard_close=utc_now()+timedelta(days=2))
    assignment = service.create_draft(uuid4(), paper.id, policy.id, paper.offering_id, times)
    service.publish(store.session, assignment_id=assignment.id, teacher_id=uuid4(),
                    base_version=1, idempotency_key=f"publish-{assignment.id}", now=utc_now())
    items = store.items(assignment.id)
    assert {(i.release_context_id, i.source_release_id, i.exercise_version_id, i.exercise_instance_id)
            for i in items} == {(s.release_context_id, s.source_release_id, s.exercise_version_id,
                                 s.exercise_instance_id) for s in store.slot_sources(paper.id)}
    store.install_new_release_without_changing_slots()
    assert [i.content_hash for i in store.items(assignment.id)] == [i.content_hash for i in items]
    with pytest.raises(Exception, match="append-only"):
        store.mutate_first_item(assignment.id)

def test_invalid_time_missing_review_or_render_cannot_publish(assignment_factory):
    paper, policy, store, auth = approved_dual_reviewed_fixture()
    service = AssignmentService(store, auth, validator=lambda *_: ())
    with pytest.raises(ValueError, match="visible_at"):
        service.create_draft(uuid4(), paper.id, policy.id, paper.offering_id,
                             TimeWindow(utc_now(), utc_now()-timedelta(days=1), utc_now()))
    store.paper.review_manifest_hash = None
    assignment = service.create_draft(uuid4(), paper.id, policy.id, paper.offering_id,
                                      valid_time_window())
    with pytest.raises(ValueError, match="review"):
        service.publish(store.session, assignment_id=assignment.id, teacher_id=uuid4(),
                    base_version=1, idempotency_key=f"publish-{assignment.id}", now=utc_now())

def test_withdraw_blocks_publish_and_download_but_preserves_items(assignment_factory):
    paper, policy, store, auth = approved_dual_reviewed_fixture()
    assignment = published_assignment_fixture(store, paper, policy)
    before = store.items(assignment.id)
    store.papers.withdraw(uuid4(), paper.id, "权威答案错误")
    assert store.paper.status == "withdrawn" and store.assignment.status == "withdrawn"
    assert store.items(assignment.id) == before
    assert store.exposure_events(paper.id)[-1].event_kind == "withdrawn"
    with pytest.raises(LookupError, match="withdrawn"):
        store.render_download(uuid4(), paper.id, "student")
```

`approved_dual_reviewed_fixture` 建立 approved paper、两名有效决策、student ready render、完整打印策略；`TimeWindow(visible,due,hard_close)` 是测试 dataclass，`valid_time_window` 返回递增时间。所有 fixture 函数在 `tests/integration/assessment/conftest.py` 中实现，并同时供任务 10 端到端测试复用。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/integration/assessment/test_assignment_projection.py tests/integration/assessment/test_withdrawal.py -q`

预期：FAIL，`AssignmentService` 不存在，paper 尚无 withdraw 方法。

- [ ] **步骤 3：实现最少发布事务与撤回**

```python
# modules/assessment/assignments.py
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from modules.assessment.hashing import canonical_hash

@dataclass(frozen=True)
class TimeWindow:
    visible: datetime
    due: datetime
    hard_close: datetime

@dataclass(frozen=True)
class SlotSource:
    paper_slot_id: UUID
    ord: int
    release_context_id: UUID
    source_release_id: UUID
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    score: Decimal

class AssignmentService:
    def __init__(self, store, authorizer, validator): self.store, self.auth, self.validator = store, authorizer, validator

    def create_draft(self, actor_id: UUID, paper_id: UUID, policy_version_id: UUID,
                     offering_id: UUID, times: TimeWindow):
        paper = self.store.paper(paper_id)
        self.auth.require(actor_id, "assignment.write", course_id=paper.course_id, offering_id=offering_id)
        if not times.visible <= times.due <= times.hard_close:
            raise ValueError("visible_at <= due_at <= hard_close_at required")
        policy = self.store.policy(policy_version_id)
        if policy.policy_kind not in {"formal", "coursework", "practice"} or policy.behavior != "deferred":
            raise ValueError("deferred assessment policy required")
        return self.store.create_assignment(
            actor_id, paper, policy, offering_id, times, distribution_kind="print")

    def publish(self, session, *, assignment_id: UUID, teacher_id: UUID,
                base_version: int, idempotency_key: str, now: datetime):
        assignment = self.store.assignment(assignment_id)
        self.auth.require(teacher_id, "assignment.publish", course_id=assignment.course_id,
                          offering_id=assignment.offering_id)
        if assignment.edit_seq != base_version:
            raise ValueError("stale_assignment_version")
        request = {"assignment_id": str(assignment_id), "base_version": base_version}
        with self.store.idempotency.run(session, teacher_id, "assignment.publish",
                                        idempotency_key, request):
            with self.store.transaction():
                self.store.lock_course_scope(assignment.course_id, assignment.offering_id)
                assignment = self.store.assignment_for_update(assignment_id)
                paper = self.store.paper(assignment.paper_version_id)
                if paper.status != "approved" or not paper.review_manifest_hash:
                    raise ValueError("dual review required")
                if assignment.offering_id != paper.offering_id or assignment.release_context_id != paper.release_context_id:
                    raise ValueError("assignment scope mismatch")
                if self.store.recompute_review_manifest(paper) != paper.review_manifest_hash:
                    raise ValueError("review decisions stale")
                issues = self.validator(paper)
                if issues:
                    raise ValueError(f"publish validation failed: {issues}")
                if "student" not in self.store.ready_render_kinds(paper.id):
                    raise ValueError("student print render required")
                sources = self.store.slot_sources(paper.id)
                self.store.assert_current_candidates_and_exposure(paper, sources, assignment.visible_at)
                for source in sources:
                    self.store.insert_item(assignment.id, source, canonical_hash(source))
                if paper.purpose == "formal":
                    self.store.insert_secure_assigned_events(assignment, sources)
                self.store.mark_published(assignment.id)
                self.store.audit("assignment.published", teacher_id, assignment.id, paper.manifest_hash)
                return self.store.published_view(assignment.id)
```

`slot_sources` 每行显式包含 assignment_id 之外的 `paper_slot_id/ord/release_context_id/source_release_id/exercise_version_id/exercise_instance_id/score/content_hash`。`store.idempotency.run` 以 teacher＋operation＋key 唯一并校验请求哈希，重复请求直接返回首次 `PublishedAssignmentDTO`；若发现 assignment_item 已存在，则返回既有发布视图而不删除重建。`now` 写入 published_at 和审计，曝光使用 assignment.visible_at。范围锁与 governance 撤销使用同一锁顺序，撤销先提交时发布重查必然失败。

```python
# modules/assessment/papers.py：以下方法缩进位于既有 PaperService 内
    def withdraw(self, actor_id: UUID, paper_version_id: UUID, reason: str):
        if len(reason.strip()) < 8: raise ValueError("withdrawal reason required")
        with self.repository.transaction():
            paper = self.repository.paper_for_update(paper_version_id)
            self.repository.authorizer.require(actor_id, "paper.withdraw",
                course_id=paper.course_id, offering_id=paper.offering_id)
            if paper.status != "approved": raise ValueError("only approved paper can be withdrawn")
            self.repository.mark_withdrawn(paper, actor_id, reason)
            self.repository.withdraw_assignments(paper.id)
            self.repository.insert_withdrawal_exposure(paper)
            self.repository.append_governance_revocation(paper, actor_id, reason)
            self.repository.audit("paper.withdrawn", actor_id, paper.id, paper.manifest_hash)
```

`models/attempt.py` 只定义 `AttemptPolicyVersion` 映射，完整字段与任务 1 迁移一致；`models/grading.py` 仅写模块 docstring「阶段 A 不定义 submission、evaluation 或 grade」。路由只含 `POST /assignments`、`POST /assignments/{id}/publish`、`POST /paper-versions/{id}/withdraw`；没有 attempts、checks、submit、grades 或 QTI 路径。另有 `GET /api/v1/audit/paper-versions/{id}`，仅 `paper.audit` 课程负责人可读取标记 `historical=true` 的固定 manifest、撤回原因和 assignment_item 哈希，不提供普通 Artifact 下载或内容替换。

- [ ] **步骤 4：运行测试确认通过**

运行：`pytest tests/integration/assessment/test_assignment_projection.py tests/integration/assessment/test_withdrawal.py -q`

预期：PASS；新 Release 不改变 assignment_item，时间/范围/审核/渲染/曝光任一失败都零投影，撤回后历史 item 可审计但发布和下载被拒绝。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/assignments.py modules/assessment/papers.py modules/assessment/models/attempt.py modules/assessment/models/grading.py apps/api/routers/assignments.py packages/contracts/assessment/assignment.schema.json tests/integration/assessment/conftest.py tests/integration/assessment/test_assignment_projection.py tests/integration/assessment/test_withdrawal.py
git commit -m "feat: publish fixed print assignments and support withdrawal"
```

### 任务 9：Next.js 蓝图、候选与手工编辑器

**交付物：** 教师在同一工作台创建蓝图、查看候选排除原因、选择候选、替换、调序、调分并运行校验；所有操作可用键盘完成，拖拽不是唯一入口，分值始终使用字符串。

**文件：**
- 创建：`apps/web/types/assessment.ts`
- 创建：`apps/web/lib/assessment-api.ts`
- 创建：`apps/web/components/assessment/BlueprintForm.tsx`
- 创建：`apps/web/components/assessment/CandidatePicker.tsx`
- 创建：`apps/web/components/assessment/ManualPaperEditor.tsx`
- 创建：`apps/web/app/teacher/papers/page.tsx`
- 创建：`apps/web/vitest.config.ts`
- 测试：`tests/unit/assessment/web/manual-editor.test.tsx`
- 测试：`tests/unit/assessment/web/blueprint-form.test.tsx`
- 测试：`tests/unit/assessment/web/setup.ts`

- [ ] **步骤 1：编写失败的教师编辑器测试**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ManualPaperEditor } from "@/components/assessment/ManualPaperEditor";

const slots = [
  { id: "s1", code: "A1", score: "40.00", exerciseVersionId: "e1" },
  { id: "s2", code: "A2", score: "60.00", exerciseVersionId: "e2" },
];
const candidates = [
  { exerciseVersionId: "e1", title: "连续性", reasons: [] },
  { exerciseVersionId: "e2", title: "一致连续", reasons: [] },
  { exerciseVersionId: "e3", title: "未审题", reasons: ["latest_decision_not_approved"] },
];

describe("ManualPaperEditor", () => {
  it("supports keyboard move, decimal score and replacement", async () => {
    const user = userEvent.setup();
    const patch = vi.fn();
    render(<ManualPaperEditor paperId="p1" baseVersion={3} slots={slots}
      candidates={candidates} issues={[]} onPatch={patch} />);
    await user.click(screen.getByRole("button", { name: "下移 A1" }));
    expect(patch).toHaveBeenCalledWith({ base_version: 3,
      operations: [{ kind: "move", slot_id: "s1", target_slot_no: 2 }] });
    await user.selectOptions(screen.getByLabelText("替换 A1"), "e2");
    expect(patch).toHaveBeenLastCalledWith({ base_version: 3,
      operations: [{ kind: "replace", slot_id: "s1", exercise_version_id: "e2" }] });
    const score = screen.getByLabelText("A1 分值");
    await user.clear(score); await user.type(score, "45.50");
    await user.click(screen.getByRole("button", { name: "保存 A1 分值" }));
    expect(patch).toHaveBeenLastCalledWith({ base_version: 3,
      operations: [{ kind: "score", slot_id: "s1", score: "45.50" }] });
  });

  it("shows rejection text and validation issues without color-only status", () => {
    render(<ManualPaperEditor paperId="p1" baseVersion={3} slots={slots}
      candidates={candidates} issues={[{ code: "total_score_mismatch", message: "总分不等于 100.00" }]} onPatch={vi.fn()} />);
    expect(screen.getByText("latest_decision_not_approved")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("总分不等于 100.00");
  });
});
```

另在 `blueprint-form.test.tsx` 验证 40.00+60.00=100.00 的蓝图提交 payload、正式卷必须选择班级和双审；Vitest 配置使用 jsdom，并把 `@` 映射到 `apps/web`。

- [ ] **步骤 2：运行测试确认失败**

运行：`npm --prefix apps/web run test:assessment`

预期：FAIL，三个 assessment 组件与 `@/lib/assessment-api` 不存在；不是 TypeScript 配置或 jest-dom setup 错误。

- [ ] **步骤 3：实现最少共享 DTO、API 与键盘组件**

```tsx
// apps/web/components/assessment/ManualPaperEditor.tsx
"use client";
import { useState } from "react";

type Slot = { id: string; code: string; score: string; exerciseVersionId: string };
type Candidate = { exerciseVersionId: string; title: string; reasons: string[] };
type Patch = { base_version: number; operations: Array<Record<string, string | number>> };

export function ManualPaperEditor({ paperId, baseVersion, slots, candidates, issues, onPatch }: {
  paperId: string; baseVersion: number; slots: Slot[]; candidates: Candidate[];
  issues: Array<{ code: string; message: string }>; onPatch: (body: Patch) => void;
}) {
  const [scores, setScores] = useState<Record<string, string>>({});
  const eligible = candidates.filter(c => c.reasons.length === 0);
  return <section aria-label="手工试卷编辑器" data-paper-id={paperId}>
    {issues.length > 0 && <div role="alert"><strong>硬约束未满足</strong><ul>
      {issues.map(i => <li key={i.code}>{i.message}</li>)}</ul></div>}
    <ol>{slots.map((slot, index) => <li key={slot.id}>
      <span>{slot.code}</span>
      <button type="button" aria-label={`上移 ${slot.code}`} disabled={index === 0}
        onClick={() => onPatch({ base_version: baseVersion,
          operations: [{ kind: "move", slot_id: slot.id, target_slot_no: index }] })}>上移</button>
      <button type="button" aria-label={`下移 ${slot.code}`} disabled={index === slots.length - 1}
        onClick={() => onPatch({ base_version: baseVersion,
          operations: [{ kind: "move", slot_id: slot.id, target_slot_no: index + 2 }] })}>下移</button>
      <label htmlFor={`replace-${slot.id}`}>替换 {slot.code}</label>
      <select id={`replace-${slot.id}`} value={slot.exerciseVersionId}
        onChange={e => onPatch({ base_version: baseVersion, operations: [{
          kind: "replace", slot_id: slot.id, exercise_version_id: e.target.value }] })}>
        {eligible.map(c => <option key={c.exerciseVersionId} value={c.exerciseVersionId}>{c.title}</option>)}
      </select>
      <label htmlFor={`score-${slot.id}`}>{slot.code} 分值</label>
      <input id={`score-${slot.id}`} inputMode="decimal" pattern="\d+\.\d{2}"
        value={scores[slot.id] ?? slot.score}
        onChange={e => setScores({ ...scores, [slot.id]: e.target.value })} />
      <button type="button" onClick={() => onPatch({ base_version: baseVersion,
        operations: [{ kind: "score", slot_id: slot.id, score: scores[slot.id] ?? slot.score }] })}>
        保存 {slot.code} 分值</button>
    </li>)}</ol>
    <aside aria-label="候选排除原因"><ul>{candidates.filter(c => c.reasons.length > 0).map(c =>
      <li key={c.exerciseVersionId}>{c.title}<ul>{c.reasons.map(r => <li key={r}>{r}</li>)}</ul></li>)}</ul></aside>
  </section>;
}
```

```tsx
// apps/web/components/assessment/BlueprintForm.tsx
"use client";
type BlueprintFormValue = { courseId: string; offeringId: string; releaseContextId: string; totalScore: string;
  durationSeconds: number; slots: Array<{ code: string; score: string }> };
export function BlueprintForm({ value, onChange, onCreate }: {
  value: BlueprintFormValue; onChange: (value: BlueprintFormValue) => void;
  onCreate: (value: BlueprintFormValue) => void;
}) {
  return <form onSubmit={e => { e.preventDefault(); onCreate(value); }}>
    <label htmlFor="total-score">总分</label>
    <input id="total-score" required pattern="\d+\.\d{2}" value={value.totalScore}
      onChange={e => onChange({ ...value, totalScore: e.target.value })} />
    <label htmlFor="duration">时长（秒）</label>
    <input id="duration" type="number" min={1} value={value.durationSeconds}
      onChange={e => onChange({ ...value, durationSeconds: Number(e.target.value) })} />
    {value.slots.map((slot, index) => <div key={slot.code}>
      <label htmlFor={`blueprint-score-${slot.code}`}>{slot.code} 目标分</label>
      <input id={`blueprint-score-${slot.code}`} required pattern="\d+\.\d{2}" value={slot.score}
        onChange={e => onChange({ ...value, slots: value.slots.map((s, i) =>
          i === index ? { ...s, score: e.target.value } : s) })} />
    </div>)}
    <button type="submit">创建手工蓝图版本</button>
  </form>;
}

// apps/web/components/assessment/CandidatePicker.tsx
"use client";
export function CandidatePicker({ candidates, selected, onChange }: {
  candidates: Array<{ exerciseVersionId: string; title: string; reasons: string[] }>;
  selected: string[]; onChange: (ids: string[]) => void;
}) {
  return <fieldset><legend>合格候选</legend>{candidates.map(c => <div key={c.exerciseVersionId}>
    <label><input type="checkbox" disabled={c.reasons.length > 0}
      checked={selected.includes(c.exerciseVersionId)} onChange={e => onChange(e.target.checked
        ? [...selected, c.exerciseVersionId] : selected.filter(id => id !== c.exerciseVersionId))} />{c.title}</label>
    {c.reasons.length > 0 && <ul>{c.reasons.map(r => <li key={r}>{r}</li>)}</ul>}
  </div>)}</fieldset>;
}
```

`BlueprintForm` 的父组件以受控状态维护分节和槽位，所有新增、删除、调序按钮都调用同一 `onChange`；submit 调用 `createBlueprintVersion`，浏览器不计算或认可审核状态。`assessment-api.ts` 的写操作生成 `crypto.randomUUID()` 幂等键并传 `base_version`，409 时重新加载 paper 后显示冲突，不自动覆盖。

```ts
// apps/web/lib/assessment-api.ts
export async function apiWrite<T>(path: string, body: unknown, key = crypto.randomUUID()): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { method: "POST",
    headers: { "content-type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error((await response.json()).code ?? `HTTP ${response.status}`);
  return response.json() as Promise<T>;
}
export async function patchPaper(paperId: string, body: unknown) {
  const response = await fetch(`/api/v1/paper-versions/${paperId}/slots`, { method: "PATCH",
    headers: { "content-type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(body) });
  if (response.status === 409) throw new Error("stale_version");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
```

`page.tsx` 组合蓝图、候选和编辑器；从服务端读取当前课程权限，未授权不渲染操作按钮，但后端仍为最终门禁。

- [ ] **步骤 4：运行测试与静态检查确认通过**

运行：`npm --prefix apps/web run test:assessment && npm --prefix apps/web run lint && npm --prefix apps/web run typecheck`

预期：全部 PASS；键盘可完成替换/调序/调分，排除原因与硬约束使用文本，API 中无 float 分值或隐式覆盖。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/types/assessment.ts apps/web/lib/assessment-api.ts apps/web/components/assessment apps/web/app/teacher/papers/page.tsx apps/web/vitest.config.ts apps/web/package.json tests/unit/assessment/web
git commit -m "feat: add keyboard-accessible manual paper workbench"
```

### 任务 10：双审/打印/发布/撤回工作台与真实 100 分卷验收

**交付物：** 教师工作台完成独立校验、两人复核、三类预览、打印 assignment 发布和撤回；后端与浏览器共同验收一门《数学分析 I》真实语义的合成 100 分固定卷，不调用 LLM。

**文件：**
- 创建：`apps/web/components/assessment/PaperOperations.tsx`
- 创建：`apps/web/playwright.assessment.config.ts`
- 修改：`apps/web/app/teacher/papers/page.tsx`
- 修改：`apps/api/main.py`
- 测试：`tests/e2e/assessment/test_manual_fixed_paper.py`
- 测试：`tests/e2e/assessment/web/manual-fixed-paper.spec.ts`

- [ ] **步骤 1：编写失败的端到端测试**

```python
# tests/e2e/assessment/test_manual_fixed_paper.py
from decimal import Decimal

def test_real_100_point_manual_fixed_paper_full_lifecycle(stage_a_services):
    env = stage_a_services
    blueprint = env.blueprint_factory(scores=(Decimal("20.00"), Decimal("20.00"),
                                               Decimal("30.00"), Decimal("30.00")),
                                      course_title="数学分析 I")
    snapshot = env.candidates.snapshot(env.teacher, blueprint.id, "formal", blueprint.offering_id)
    assert not snapshot.rejected
    paper = env.papers.create_manual_draft(env.teacher, blueprint.id, blueprint.content, snapshot,
        selections=env.select_all(blueprint.content, snapshot), paper_id=None)
    assert env.validate(paper) == ()
    outputs = {}
    for kind in ("student", "answer", "rubric"):
        outputs[kind] = env.renders.render(env.teacher, paper.id, kind)
    assert "SECRET_ANSWER" not in env.pdf_text(outputs["student"].id)
    assert all(env.pdf_page_count(outputs[kind].id) > 0 for kind in outputs)
    env.review.submit(env.teacher, paper)
    env.review.approve(env.reviewer_one, paper.id)
    assert env.store.paper(paper.id).status == "in_review"
    env.review.approve(env.reviewer_two, paper.id)
    approved = env.store.paper(paper.id)
    assert approved.status == "approved" and approved.review_manifest_hash
    assignment = env.assignments.create_draft(env.teacher, paper.id, env.print_policy.id,
                                               blueprint.offering_id, env.valid_times())
    env.assignments.publish(env.teacher, assignment.id)
    items = env.store.items(assignment.id)
    assert len(items) == 4 and sum((i.score for i in items), Decimal("0.00")) == Decimal("100.00")
    env.papers.withdraw(env.course_owner, paper.id, "量规与权威答案不一致")
    assert env.store.paper(paper.id).status == "withdrawn"
    assert len(env.store.items(assignment.id)) == 4
```

`stage_a_services` 使用任务 8 的真实 PostgreSQL/MinIO/worker fixture；`blueprint_factory` 创建「连续定义、一致连续判别、极限计算、连续性证明」四个已审核题，分值之和 100.00；`select_all` 按槽位 code 返回共享 SlotSelection。测试断言学生 PDF 无 `SECRET_ANSWER`，三份 PDF 页码均非空。

```ts
// tests/e2e/assessment/web/manual-fixed-paper.spec.ts
import { test, expect, type Page } from "@playwright/test";

test("teacher validates, reviews, prints, publishes and withdraws", async ({ page }) => {
  await installPaperApiMock(page, { initialStatus: "draft", reviewers: 2 });
  await page.goto("/teacher/papers?paperVersionId=paper-100");
  await page.getByRole("button", { name: "运行整卷校验" }).click();
  await expect(page.getByRole("status")).toContainText("硬约束全部通过");
  await page.getByRole("button", { name: "提交整卷审核" }).click();
  await page.getByRole("button", { name: "确认本人复核" }).click();
  await expect(page.getByText("还需 1 名复核教师")).toBeVisible();
  await page.getByRole("button", { name: "确认本人复核" }).click();
  await expect(page.getByText("整卷双复核完成")).toBeVisible();
  for (const kind of ["学生卷", "答案卷", "量规卷"])
    await expect(page.getByRole("link", { name: `下载${kind}` })).toBeVisible();
  await page.getByRole("button", { name: "发布打印作业" }).click();
  await expect(page.getByRole("status")).toContainText("assignment 已发布");
  await page.getByRole("button", { name: "撤回试卷" }).click();
  await page.getByLabel("撤回原因").fill("量规与权威答案不一致");
  await page.getByRole("button", { name: "确认撤回" }).click();
  await expect(page.getByRole("alert")).toContainText("已撤回，历史版本只读");
  await expect(page.getByRole("button", { name: "发布打印作业" })).toBeDisabled();
});

async function installPaperApiMock(page: Page, options: { initialStatus: string; reviewers: number }) {
  let status = options.initialStatus;
  let approvals = 0;
  const renders = ["student", "answer", "rubric"].map(kind => ({ id: `render-${kind}`, kind }));
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: object, code = 200) => route.fulfill({
      status: code, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/validate")) return respond({ issues: [] });
    if (path.endsWith("/submit-review")) { status = "in_review"; return respond({ status }); }
    if (path.endsWith("/approve")) {
      approvals += 1; if (approvals >= options.reviewers) status = "approved";
      return respond({ status, approvals });
    }
    if (path.endsWith("/renders")) return respond({ job_id: `job-${renders.length}`, status: "ready" });
    if (path.includes("/assignments/") && path.endsWith("/publish")) return respond({ status: "published" });
    if (path.endsWith("/withdraw")) { status = "withdrawn"; return respond({ status }); }
    return respond({ id: "paper-100", assignmentId: "assignment-100", status, approvals,
      requiredApprovals: options.reviewers, renders });
  });
}
```

`installPaperApiMock` 只用于浏览器操作回归，真实后端正确性由上面的 PostgreSQL/MinIO 测试证明，不用 mock 替代后端验收。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/e2e/assessment/test_manual_fixed_paper.py -q` 与 `npm --prefix apps/web run test:assessment:e2e`

预期：后端在路由组合或完整生命周期断言处 FAIL；浏览器因 `PaperOperations` 不存在而 FAIL。失败日志必须对应阶段 A 功能，不得用跳过测试消除。

- [ ] **步骤 3：实现最少操作面板、页面组合与路由注册**

```tsx
// apps/web/components/assessment/PaperOperations.tsx
"use client";
import { useState } from "react";
import { apiWrite } from "@/lib/assessment-api";

const renderLabels: Record<string, string> = { student: "学生卷", answer: "答案卷", rubric: "量规卷" };

export function PaperOperations({ paperId, assignmentId, status, approvals, requiredApprovals, renders, onChanged }: {
  paperId: string; assignmentId: string; status: string; approvals: number; requiredApprovals: number;
  renders: Array<{ id: string; kind: string }>; onChanged: () => void;
}) {
  const [message, setMessage] = useState(""); const [reason, setReason] = useState("");
  const [withdrawing, setWithdrawing] = useState(false);
  async function action<T>(path: string, body: object = {}): Promise<T> {
    setMessage(""); const result = await apiWrite<T>(`/paper-versions/${paperId}/${path}`, body); onChanged(); return result;
  }
  async function validate() {
    const result = await action<{ issues: Array<{ message: string }> }>("validate");
    setMessage(result.issues.length ? result.issues.map(i => i.message).join("；") : "硬约束全部通过");
  }
  const withdrawn = status === "withdrawn";
  return <section aria-label="整卷审核与发布">
    <div role="status">{message || (withdrawn ? "已撤回" : approvals >= requiredApprovals ? "整卷双复核完成" : `还需 ${requiredApprovals-approvals} 名复核教师`)}</div>
    {withdrawn && <div role="alert">已撤回，历史版本只读</div>}
    <button disabled={withdrawn} onClick={validate}>运行整卷校验</button>
    <button disabled={withdrawn || status !== "draft"} onClick={() => action("submit-review")}>提交整卷审核</button>
    <button disabled={withdrawn || status !== "in_review"} onClick={() => action("approve")}>确认本人复核</button>
    <button disabled={withdrawn} onClick={() => Promise.all(["student", "answer", "rubric"].map(kind =>
      action("renders", { render_kind: kind }))).then(() => setMessage("三类打印预览已生成"))}>生成打印预览</button>
    <nav aria-label="打印产物">{renders.map(render =>
      <a key={render.id} href={`/api/v1/paper-renders/${render.id}/download`}>下载{renderLabels[render.kind]}</a>)}</nav>
    <button disabled={withdrawn || status !== "approved"}
      onClick={() => apiWrite(`/assignments/${assignmentId}/publish`, {}).then(() => setMessage("assignment 已发布"))}>发布打印作业</button>
    <button disabled={withdrawn || status !== "approved"} onClick={() => setWithdrawing(true)}>撤回试卷</button>
    {withdrawing && <><label htmlFor="withdraw-reason">撤回原因</label>
      <textarea id="withdraw-reason" value={reason} onChange={e => setReason(e.target.value)} />
      <button onClick={() => action("withdraw", { reason }).then(() => setWithdrawing(false))}>确认撤回</button></>}
  </section>;
}
```

发布按钮使用页面加载的草稿 assignment ID，并调用 `/assignments/{assignmentId}/publish`。校验响应有 issues 时逐条展示且不显示“全部通过”；双审人数从服务端 `review_manifest` 计算，前端不自行放宽正式卷 required=2。三份渲染先由「生成打印预览」按钮请求 job，页面轮询 job 状态，三类链接分别授权。

`apps/api/main.py` 只注册 blueprints、papers、paper_renders、assignments 四个 router；`page.tsx` 将真实 paper、assignment、review 和 render 状态传入操作面板。所有错误显示服务端 `code/message`，409 提示重新载入，410 显示历史只读，403 不暴露保密资源。

- [ ] **步骤 4：运行完整验收确认通过**

运行：

```bash
pytest tests/unit/assessment tests/integration/assessment tests/e2e/assessment/test_manual_fixed_paper.py -q
npm --prefix apps/web run test:assessment
npm --prefix apps/web run test:assessment:e2e
```

预期：全部 PASS；真实 100 分卷四题固定、双审前不可发布、发布后 assignment_item 完整、三类 PDF 分离、学生卷无敏感字段、撤回保留历史；浏览器所有主要状态有文本且键盘可操作。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/main.py apps/web/app/teacher/papers/page.tsx apps/web/components/assessment/PaperOperations.tsx apps/web/playwright.assessment.config.ts apps/web/package.json tests/e2e/assessment
git commit -m "test: accept the manual fixed paper lifecycle"
```

## 4. 全量回归顺序

```bash
alembic upgrade head
pytest tests/unit/assessment tests/integration/assessment -q
pytest tests/e2e/assessment/test_manual_fixed_paper.py -q
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run test:assessment
npm --prefix apps/web run test:assessment:e2e
```

数据库升级/降级测试只使用可丢弃测试库；不在共享开发库执行 downgrade。测试账号使用非 owner、非 superuser 数据库角色，RLS 负向用例不得由迁移账号代替。

## 5. 计划自检

| 规格要求 | 对应交付 |
|---|---|
| 最小通用 Artifact 不依赖 LLM | 任务 1 |
| 唯一 Alembic 文件、真实单头 down_revision、升降级 | 任务 1 |
| 蓝图版本与 Decimal 总分 | 任务 2 |
| 候选资格、固定 Release、decision_seq、曝光基础 | 任务 3 |
| paper_version/section/slot 与手工编辑 | 任务 4 |
| 独立整卷校验与硬约束 | 任务 5 |
| 学生/答案/量规分离渲染与学生白名单 | 任务 6 |
| 整卷双审与 review_manifest_hash | 任务 7 |
| 完整 attempt_policy_version、固定 assignment_item、时间终检、撤回 | 任务 1、8 |
| Next.js 教师工作台、键盘与端到端验收 | 任务 9、10 |
| CP-SAT、在线 attempt、练习评分、QTI 排除 | 范围 0.2 与路由负向约束 |

类型一致性检查：`BlueprintContent`、`CandidateEvidence`、`PaperView`、`ReviewEvidence`、`StudentPaperProjection` 只在任务 1 共享契约定义；各服务以相同字段名引用。`review_manifest_hash` 的输入始终含 paper manifest 与全部有效 reviewer/decision_seq，不存在单决策 ID 替代整卷审核的路径。`attempt_policy_version` 和 `item_exposure_event` 只在任务 1 迁移建表，后续阶段直接引用。
