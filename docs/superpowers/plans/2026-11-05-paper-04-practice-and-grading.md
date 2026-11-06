# 阶段 D：在线练习与评分闭环实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在既有固定卷与在线作答链路上交付绑定 `response_revision` 的练习检查、学习投影、受控提示与解析、正式 submission 预评、教师量规评分、独立成绩发布、追加改分、申诉与重评闭环，且练习和 AI 均不能写正式成绩。

**架构：** `PracticeService`、`GradingService`、`RegradeService` 只编排阶段 A—C 已交付的 `AttemptService`、`ResponseService`、`SubmissionService`，不重复实现保存、交卷或 submission 快照。`answer_test_run` 保存确定性评价证据，`learning.exercise_attempt` 只保存练习学习投影；正式成绩只由教师确认产生 `grade_revision`，再由独立发布动作公开。反馈 Artifact 每次打开都重新经过 PDP，解析额外经过 `ExposureService` 的正式卷冲突检查。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL 16、pytest、Next.js/React/TypeScript、Vitest、Testing Library、既有 CAS 沙箱与 Artifact/Policy/Exposure 服务。

---

## 0. 范围与不变量

### 本阶段交付

- 固定练习卷逐题检查：绑定具体 `response_revision`，先检查 `parse_status`，再运行固定 `answer_test`。
- `true / false / fail` 三分；CAS `fail`、超时、定义域不确定和引擎异常都显示「暂不能判定」，不打 0 分。
- 每次幂等检查生成一条不可变 `learning.exercise_attempt`；自动保存不检查、不生成学习投影、不新增 paper attempt。
- 分级提示与解析按固定 policy 开放；旧 Artifact 链接、签名 URL 续期和重新打开页面都重新授权。
- 正式 submission 预评、rubric 教师评分、已确认分组建议、独立发布、追加 grade revision、学生申诉、regrade run、撤回后的显式成绩处理。
- 教师复核工作台、学生反馈视图、数据库/API/UI/端到端正负向测试。

### 复用边界

- `AttemptService` 负责 attempt 状态、次数、模式、截止和撤回门禁；`ResponseService` 负责自动保存、revision 幂等和冲突；`SubmissionService` 负责最终快照。本计划不重复实现这些方法。
- `attempt_policy_version` 与 `item_exposure_event` 由阶段 A 创建，`ExposureService` 由阶段 B 实现；本阶段只实现反馈策略判定与曝光阻断。
- `PolicyDecisionPoint`、outbox、审计、模型网关、CAS 沙箱均复用既有实现。
- 不实现运行时随机卷、在线监考、IRT、AI 自动发布成绩或第二套 submission/grade 真源。

### 不变量

1. 练习路径没有 `Grade`/`GradeRevision` 写入；学习投影不是交卷回执，也不是正式成绩。
2. 预评只写 `answer_test_run` 与 `submission_item.suggested_score/evaluation_status/requires_human`。
3. 每个进入 grade revision 的 submission_item 都有当前教师签署的 `ItemGradeDecision`；自动预评和 AI 建议也必须由教师逐项确认。
4. `publish_grade` 是独立动作；教师确认不等于学生可见，正式发布前重新检查复核完整性、当前策略和撤回。
5. 正式预评中，`false` 才可确定性建议 0 分；`fail`、超时、定义域不确定、引擎异常、空答、无效输入、证明/开放题和低置信度 OCR 的建议分均为 `NULL` 并转人工。练习中的无效输入只反馈「输入无效、请修正」，不进入教师人工队列，建议分同样为 `NULL`。
6. 改分只追加 revision；申诉不修改 submission；重评未发布前学生仍看到旧发布版本及「重评中」状态。
7. 学生反馈始终携带 response revision；已有更新 revision 时标明「旧输入反馈」，不覆盖新输入。

## 1. 文件结构

```text
AIForTeaching/
├─ modules/
│  ├─ assessment/
│  │  ├─ models/grading.py                 # 答案测试、评分决定、成绩版本、申诉、重评、AI建议模型
│  │  └─ services/
│  │     ├─ contracts.py                   # 阶段D DTO、CAS/AI端口与错误类型
│  │     ├─ practice.py                    # PracticeService.check，绑定revision并投影学习记录
│  │     ├─ feedback.py                    # 提示/解析Artifact重检与Exposure阻断
│  │     ├─ grading.py                     # 预评、教师确认、独立发布、撤回成绩约束
│  │     └─ regrade.py                     # 申诉与RegradeService.create_run/逐项复核
│  └─ learning/models.py                   # 仅在学习模型已存在时补两个来源字段；不存在时不新建第二真源
├─ apps/
│  ├─ api/
│  │  ├─ main.py                           # 注册practice/grading路由
│  │  ├─ deps.py                           # 仅组合既有服务与阶段D服务
│  │  └─ routers/{practice,grading}.py     # 学生练习、教师评分、发布、申诉、重评、反馈DTO
│  ├─ web/
│  │  └─ src/components/
│  │     ├─ TeacherReviewWorkspace.tsx     # CAS失败、rubric、AI建议、确认与分开发布
│  │     └─ StudentFeedbackView.tsx        # revision反馈、成绩版本、申诉、撤回说明
│  └─ worker/main.py                       # 调用已有预评入口，不直接写grade
├─ db/migrations/versions/
│  └─ 20261105_110000_assessment_grading.py # 唯一步骤D迁移；兼容既有learning.exercise_attempt
└─ tests/
   ├─ unit/assessment/                     # 练习、评价、评分、发布、重评、撤回
   ├─ integration/assessment/              # 迁移、PostgreSQL约束、API、权限、Artifact
   └─ e2e/test_practice_to_grading.py       # 练习与正式评分闭环及策略矩阵
```

## 2. 共享接口与类型契约

以下上游接口只调用，不在阶段 D 再实现。`UUID` 均为 `uuid.UUID`，分值均为 `decimal.Decimal`，时间均为带时区 `datetime`。

```python
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

ParseStatus = Literal["valid", "invalid", "unsupported"]
AnswerOutcome = Literal["true", "false", "fail"]
AttemptMode = Literal["formal", "coursework", "practice"]

class ActorContext(Protocol):
    user_id: UUID
    permissions: frozenset[str]
    offering_ids: frozenset[UUID]

class PracticePolicy(Protocol):
    version_id: UUID
    checks_enabled: bool
    correctness_enabled: bool
    max_hint_level: int
    solution_enabled: bool
    solution_requires_valid_response: bool

class AnswerTestContext(Protocol):
    answer_test_version_id: UUID
    definition: dict[str, object]
    engine_name: str
    engine_version: str

class AttemptSlotContext(Protocol):
    paper_slot_id: UUID
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    answer_test: AnswerTestContext | None
    rubric_version_id: UUID
    max_score: Decimal
    response_kind: str

class AttemptContext(Protocol):
    id: UUID
    student_id: UUID
    paper_version_id: UUID
    mode: AttemptMode
    status: str
    practice_policy: PracticePolicy
    slots: tuple[AttemptSlotContext, ...]

class ResponseRevisionContext(Protocol):
    id: UUID
    attempt_id: UUID
    paper_slot_id: UUID
    revision_no: int
    raw_response: dict[str, object]
    normalized_response: dict[str, object] | None
    parse_status: ParseStatus
    response_hash: str

class SubmissionItemContext(Protocol):
    id: UUID
    slot: AttemptSlotContext
    response: ResponseRevisionContext | None
    ocr_confidence: Decimal | None

class RevocationContext(Protocol):
    active: bool
    paper_slot_id: UUID | None
    revocation_event_id: UUID | None

class SubmissionContext(Protocol):
    id: UUID
    attempt_id: UUID
    student_id: UUID
    response_manifest_hash: str
    items: tuple[SubmissionItemContext, ...]
    revocation: RevocationContext

class AttemptService(Protocol):
    def require_attempt(self, attempt_id: UUID, actor: ActorContext) -> AttemptContext: ...

class ResponseService(Protocol):
    def require_revision(self, attempt_id: UUID, paper_slot_id: UUID,
                         response_revision_id: UUID, actor: ActorContext) -> ResponseRevisionContext: ...
    def latest_revision_no(self, attempt_id: UUID, paper_slot_id: UUID) -> int: ...

class SubmissionService(Protocol):
    def require_submission(self, submission_id: UUID, actor: ActorContext,
                           action: Literal["evaluate", "grade", "publish", "appeal", "view"]) -> SubmissionContext: ...
```

`ArtifactDecision` 含 `allowed: bool`、`deny_code: str | None`、`artifact_version_id`、`exercise_version_id`、`exercise_instance_id`、`contains_solution: bool`、`download_url: str | None`。共享调用为：

```python
PolicyDecisionPoint.check_artifact(
    actor=actor,
    artifact_version_id=artifact_version_id,
    activity_attempt_id=attempt_id,
    response_revision_id=response_revision_id,
    action="open",
) -> ArtifactDecision

ExposureService.assert_solution_release(
    exercise_version_id=exercise_version_id,
    exercise_instance_id=exercise_instance_id,
    paper_version_id=paper_version_id,
) -> None

ExposureService.record_practice_answer_released(
    exercise_version_id=exercise_version_id,
    exercise_instance_id=exercise_instance_id,
    paper_attempt_id=paper_attempt_id,
) -> None
```

阶段 D 对外服务签名固定为：

```python
PracticeService.check(command: PracticeCheckCommand, actor: ActorContext) -> PracticeCheckResult
GradingService.evaluate_submission(submission_id: UUID, actor: ActorContext) -> EvaluationSummary
GradingService.publish_grade(grade_revision_id: UUID, actor: ActorContext, idempotency_key: UUID) -> GradeRevision
RegradeService.create_run(command: CreateRegradeRunCommand, actor: ActorContext) -> RegradeRunView
```

`PracticeCheckCommand` 含 `attempt_id/paper_slot_id/response_revision_id/client_check_id: UUID`、`requested_hint_level: int`、`reveal_solution: bool`。`PracticeCheckResult` 含 `learning_exercise_attempt_id`、`response_revision_id`、`revision_no`、`latest_revision_no`、`superseded`、`input_status`、`outcome: AnswerOutcome | None`、`requires_human`、`hint_artifact_ids: tuple[UUID, ...]`、`solution_artifact_id: UUID | None`。无效输入的 `outcome=None`，不调用 CAS。

`AnswerTestResult` 含 `outcome`、`assumptions: tuple[str, ...]`、`branch: str`、`failure_kind: str | None`；`AnswerTestRunner.run(response, answer_test) -> AnswerTestResult` 是既有沙箱适配器端口。`CASFailure(code="cas_fail")`、`CASTimeout(code="cas_timeout")`、`DomainUncertain(code="domain_uncertain")`、`CASEngineError(code="cas_engine_error")` 是 `contracts.py` 中定义的四个异常，服务统一映射为 `fail`。

`ItemDecisionInput` 含 `submission_item_id`、`rubric_version_id`、`criterion_scores: dict[str, Decimal]`、`score: Decimal | None`、`rationale: str`、`cancelled: bool`；`ConfirmGradeCommand` 含 `submission_id`、`base_revision_no: int`、`decisions: tuple[ItemDecisionInput, ...]`、`change_reason`、`student_message`、`regrade_run_id: UUID | None`、`withdrawal_action: Literal["cancel_item", "close_assignment"] | None`。教师确认签名为 `GradingService.confirm_grade(command, actor) -> GradeRevision`。

`EvaluationItemResult` 含 `submission_item_id`、`outcome: AnswerOutcome | None`、`suggested_score: Decimal | None`、`requires_human: bool`、`answer_test_run_id: UUID | None`；`EvaluationSummary` 含 `submission_id` 与 `items: tuple[EvaluationItemResult, ...]`。

`CreateRegradeRunCommand` 含 `submission_id`、`appeal_id: UUID | None`、`new_answer_test_version_id: UUID | None`、`new_rubric_version_id: UUID | None`、`reason: str`。`RegradeRunView` 含 `run_id`、`submission_id`、`status` 与逐项 `RegradeItemView`；后者含 `submission_item_id`、`old_score/new_score: Decimal | None`、`old_outcome/new_outcome: AnswerOutcome | None`、`requires_human`、`status: Literal["pending", "confirmed"]`。

错误类型统一为 `PolicyDenied(code="policy_denied")`、`ResponseConflict(code="response_conflict")`、`WithdrawnError(code="withdrawn")`、`ReviewIncomplete(code="review_incomplete")`、`EvaluationUnavailable(code="evaluation_unavailable")`、`ExposureConflict(code="exposure_conflict")`；路由层负责映射，服务层不返回 HTTP 对象。

## 3. 实施任务

### 任务 1：阶段 D 持久化、兼容迁移与追加版本约束

**文件：**
- 创建：`modules/assessment/models/grading.py`
- 创建：`modules/assessment/services/contracts.py`
- 条件修改：`modules/learning/models.py`
- 创建：`db/migrations/versions/20261105_110000_assessment_grading.py`
- 测试：`tests/integration/assessment/test_grading_migration.py`
- 测试：`tests/integration/assessment/test_grade_revision_append_only.py`

- [ ] **步骤 1：编写失败的迁移与追加版本测试**

`checked_practice_response` fixture 在本任务中直接向 learning 表插入一条带来源的学习投影，不调用任务 2 的 `PracticeService`；`practice_response_revision` 使用阶段 C 已保存的练习 revision，且对应 attempt 没有 submission。这样任务 1 能独立验证学习表边界。

```python
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
import pytest


def upgrade(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


def test_upgrade_creates_learning_projection_when_absent(empty_migration_db):
    cfg = Config("alembic.ini")
    upgrade(cfg, "20261105_101000_assessment_attempt_due")
    upgrade(cfg, "20261105_110000_assessment_grading")
    columns = {c["name"] for c in inspect(empty_migration_db).get_columns(
        "exercise_attempt", schema="learning")}
    assert {"id", "exercise_version_id", "exercise_instance_id",
            "source_response_revision_id", "paper_attempt_id",
            "input_status", "outcome", "engine_version",
            "feedback_policy_version_id", "created_at"} <= columns
    command.downgrade(cfg, "20261105_101000_assessment_attempt_due")
    assert not inspect(empty_migration_db).has_table("exercise_attempt", schema="learning")


def test_upgrade_only_extends_existing_learning_projection(migration_db_with_learning_attempt):
    cfg = Config("alembic.ini")
    upgrade(cfg, "20261105_110000_assessment_grading")
    columns = {c["name"] for c in inspect(migration_db_with_learning_attempt).get_columns(
        "exercise_attempt", schema="learning")}
    assert {"legacy_learning_field", "source_response_revision_id", "paper_attempt_id"} <= columns
    command.downgrade(cfg, "20261105_101000_assessment_attempt_due")
    remaining = {c["name"] for c in inspect(migration_db_with_learning_attempt).get_columns(
        "exercise_attempt", schema="learning")}
    assert "legacy_learning_field" in remaining
    assert "source_response_revision_id" not in remaining


def test_grade_revision_score_cannot_be_updated(db_session, confirmed_grade_revision):
    with pytest.raises(Exception, match="grade_revision_score_is_append_only"):
        db_session.execute(text(
            "UPDATE assessment.grade_revision SET total_score = 0 WHERE id = :id"),
            {"id": confirmed_grade_revision.id})


def test_learning_projection_is_not_submission_or_grade(db_session, checked_practice_response):
    row = db_session.execute(text("""
        SELECT ea.source_response_revision_id, ea.paper_attempt_id,
               COUNT(s.id) AS submission_count, COUNT(g.id) AS grade_count
        FROM learning.exercise_attempt ea
        LEFT JOIN assessment.submission s ON s.attempt_id = ea.paper_attempt_id
        LEFT JOIN assessment.grade g ON g.submission_id = s.id
        WHERE ea.id = :id GROUP BY ea.id
    """), {"id": checked_practice_response.learning_exercise_attempt_id}).one()
    assert row.submission_count == 0
    assert row.grade_count == 0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_grading_migration.py tests/integration/assessment/test_grade_revision_append_only.py -q`

预期：FAIL；Alembic 报 `KeyError: '20261105_110000_assessment_grading'`，表明迁移尚不存在。

- [ ] **步骤 3：编写最少模型与迁移实现**

`grading.py` 定义 `AnswerTestRun`、`ItemGradeDecision`、`Grade`、`GradeRevision`、`GradeAppeal`、`RegradeRun`、`RegradeItem`、`GradingSuggestion`。关键约束如下，字段使用 SQLAlchemy 2 `Mapped/mapped_column`，JSON 使用 PostgreSQL `JSONB`：

```python
class AnswerTestRun(Base):
    __tablename__ = "answer_test_run"
    __table_args__ = (
        CheckConstraint("outcome IN ('true','false','fail')", name="answer_outcome_valid"),
        CheckConstraint("(outcome = 'fail') = (failure_kind IS NOT NULL)", name="fail_has_reason"),
        Index("uq_formal_answer_run", "submission_item_id", "response_revision_id",
              "answer_test_version_id", unique=True,
              postgresql_where=text("submission_item_id IS NOT NULL")),
        {"schema": "assessment"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    response_revision_id: Mapped[UUID] = mapped_column(ForeignKey("assessment.response_revision.id"))
    answer_test_version_id: Mapped[UUID] = mapped_column()
    submission_item_id: Mapped[UUID | None] = mapped_column(ForeignKey("assessment.submission_item.id"))
    learning_exercise_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning.exercise_attempt.id"), unique=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    engine_name: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(128))
    assumptions: Mapped[dict] = mapped_column(JSONB)
    branch: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(8))
    failure_kind: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

`ItemGradeDecision` 字段为 `id/submission_item_id/grade_revision_id/decision_seq/rubric_version_id/criterion_scores/score/rationale/decided_by/decided_at/regrade_item_id/supersedes_id`，唯一键 `(submission_item_id, decision_seq)`，`score` 可空且 `cancelled` 由 `score IS NULL` 表示。`Grade` 字段为 `id/submission_id/status/current_published_revision_id`，`submission_id` 唯一。`GradeRevision` 字段为 `id/grade_id/submission_id/revision_no/previous_revision_id/total_score/status/change_reason/student_message/regrade_run_id/withdrawal_action/created_by/created_at/published_by/published_at`，唯一键 `(grade_id, revision_no)`。`GradeAppeal` 保存 `submission_id/student_id/grade_revision_id/reason/status/resolution/created_at/resolved_at`。`RegradeRun` 与 `RegradeItem` 保存新旧答案/rubric、逐项新旧结果与教师确认状态；`GradingSuggestion` 只保存建议、模型版本与教师采纳状态，没有 grade 外键写入权限。

若 `LearningExerciseAttempt` 模型已存在，只在其上映射 `source_response_revision_id` 与 `paper_attempt_id`，已有结果字段通过属性别名提供 `input_status/outcome/engine_version/feedback_policy_version_id`，不新增列；若模型和表都不存在，则在 `modules/learning/models.py` 新建映射，字段与下面的最小建表语句一致。`append_item_decision` 必须把每个决定写到当前 `grade_revision_id`，发布时只能读取该 revision 的决定集合。

迁移开头必须精确为：

```python
revision = "20261105_110000_assessment_grading"
down_revision = "20261105_101000_assessment_attempt_due"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("exercise_attempt", schema="learning"):
        op.create_table(
            "exercise_attempt",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("exercise_version_id", sa.Uuid(), nullable=False),
            sa.Column("exercise_instance_id", sa.Uuid(), nullable=True),
            sa.Column("input_status", sa.String(16), nullable=False),
            sa.Column("outcome", sa.String(8), nullable=True),
            sa.Column("engine_version", sa.String(128), nullable=True),
            sa.Column("feedback_policy_version_id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            schema="learning",
        )
        op.execute("COMMENT ON TABLE learning.exercise_attempt IS 'created_by:20261105_110000_assessment_grading'")
    op.add_column("exercise_attempt", sa.Column("source_response_revision_id", sa.Uuid(),
                  sa.ForeignKey("assessment.response_revision.id"), nullable=True), schema="learning")
    op.add_column("exercise_attempt", sa.Column("paper_attempt_id", sa.Uuid(),
                  sa.ForeignKey("assessment.paper_attempt.id"), nullable=True), schema="learning")
    op.create_index("ix_learning_attempt_response", "exercise_attempt",
                    ["source_response_revision_id"], schema="learning")
    op.create_index("ix_learning_attempt_paper_attempt", "exercise_attempt",
                    ["paper_attempt_id"], schema="learning")
```

新建表的两列先允许 NULL，以兼容既有学习记录；阶段 D 新投影在服务层强制非空并加 `source_response_revision_id IS NOT NULL` 的部分索引。已有表只增加这两列与索引，不添加其他业务列。新建 grading 表与触发器也在同一迁移中创建。`downgrade()` 检查表注释：有本迁移标记才 drop 表；无标记时，在 learning 对象上只移除本迁移增加的两列、索引和投影保护触发器，保留原表及其他字段。追加约束触发器为：

```sql
CREATE OR REPLACE FUNCTION assessment.guard_grade_revision_score() RETURNS trigger AS $$
BEGIN
  IF NEW.grade_id IS DISTINCT FROM OLD.grade_id
     OR NEW.submission_id IS DISTINCT FROM OLD.submission_id
     OR NEW.revision_no IS DISTINCT FROM OLD.revision_no
     OR NEW.previous_revision_id IS DISTINCT FROM OLD.previous_revision_id
     OR NEW.total_score IS DISTINCT FROM OLD.total_score
     OR NEW.change_reason IS DISTINCT FROM OLD.change_reason
     OR NEW.student_message IS DISTINCT FROM OLD.student_message
     OR NEW.regrade_run_id IS DISTINCT FROM OLD.regrade_run_id
     OR NEW.withdrawal_action IS DISTINCT FROM OLD.withdrawal_action
     OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
    RAISE EXCEPTION 'grade_revision_score_is_append_only';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

为 `ItemGradeDecision` 建立禁止 UPDATE/DELETE 触发器。学习投影保护触发器仅当 `OLD.source_response_revision_id IS NOT NULL OR OLD.paper_attempt_id IS NOT NULL` 时拒绝 UPDATE/DELETE，避免锁死既有表的历史学习记录；阶段 D 新投影服务层始终同时写这两个来源字段。只允许 grade revision 的发布状态、发布者、发布时间变化。

- [ ] **步骤 4：运行迁移与约束测试验证通过**

运行：`pytest tests/integration/assessment/test_grading_migration.py tests/integration/assessment/test_grade_revision_append_only.py -q`

预期：PASS；覆盖 learning 表不存在、已存在、upgrade/downgrade、成绩不可原位改分和学习投影不产生 submission/grade。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/models/grading.py modules/assessment/services/contracts.py modules/learning/models.py db/migrations/versions/20261105_110000_assessment_grading.py tests/integration/assessment/test_grading_migration.py tests/integration/assessment/test_grade_revision_append_only.py
git commit -m "feat: 建立阶段D评分与学习投影持久化边界"
```

### 任务 2：练习逐题检查、revision 绑定与学习投影

**文件：**
- 创建：`modules/assessment/services/practice.py`
- 修改：`modules/assessment/services/contracts.py`
- 测试：`tests/unit/assessment/test_practice_check.py`
- 测试：`tests/integration/assessment/test_autosave_does_not_check.py`

- [ ] **步骤 1：编写失败的练习检查测试**

```python
import pytest
from uuid import uuid4
from modules.learning.models import LearningExerciseAttempt
from modules.assessment.services.contracts import (
    CASFailure, CASTimeout, DomainUncertain, CASEngineError, PracticeCheckCommand)


def command(revision, **changes):
    values = dict(attempt_id=revision.attempt_id, paper_slot_id=revision.paper_slot_id,
                  response_revision_id=revision.id, client_check_id=uuid4(),
                  requested_hint_level=0, reveal_solution=False)
    values.update(changes)
    return PracticeCheckCommand(**values)


def test_valid_response_runs_answer_test_and_projects_exact_revision(
        practice_service, valid_practice_revision, runner, db_session, student_actor):
    valid_practice_revision.parse_status = "valid"
    runner.result.outcome = "true"
    result = practice_service.check(command(valid_practice_revision), student_actor)
    assert result.response_revision_id == valid_practice_revision.id
    assert result.revision_no == valid_practice_revision.revision_no
    assert result.outcome == "true"
    assert runner.calls[0].response.id == valid_practice_revision.id
    assert db_session.get(LearningExerciseAttempt, result.learning_exercise_attempt_id) is not None


def test_invalid_input_never_calls_cas(practice_service, invalid_practice_revision, runner, student_actor):
    result = practice_service.check(command(invalid_practice_revision), student_actor)
    assert result.input_status == "invalid"
    assert result.outcome is None
    assert result.requires_human is False
    assert runner.calls == []


@pytest.mark.parametrize("error", [CASFailure("fail"), CASTimeout("timeout"),
                                  DomainUncertain("domain"), CASEngineError("engine")])
def test_cas_uncertainty_is_fail_not_false(practice_service, valid_practice_revision,
                                           runner, student_actor, error):
    runner.error = error
    result = practice_service.check(command(valid_practice_revision), student_actor)
    assert result.outcome == "fail"
    assert result.requires_human is True


def test_old_revision_is_explicitly_marked(practice_service, superseded_practice_revision, student_actor):
    result = practice_service.check(command(superseded_practice_revision), student_actor)
    assert result.superseded is True
    assert result.latest_revision_no > result.revision_no


def test_check_retry_returns_same_learning_projection(practice_service, valid_practice_revision, student_actor):
    cmd = command(valid_practice_revision)
    first = practice_service.check(cmd, student_actor)
    second = practice_service.check(cmd, student_actor)
    assert second.learning_exercise_attempt_id == first.learning_exercise_attempt_id
```

集成测试调用阶段 C 的真实保存入口，再统计学习投影与 paper attempt：

```python
def test_autosave_does_not_check_or_consume_attempt(
        response_service, practice_attempt, student_actor, practice_check_spy, db_session):
    before_attempts = count_paper_attempts(db_session, practice_attempt.id)
    before_learning = count_learning_attempts(db_session, practice_attempt.id)
    saved = response_service.save(**valid_save_command(practice_attempt, student_actor))
    assert saved.parse_status == "valid"
    assert practice_check_spy.calls == []
    assert count_learning_attempts(db_session, practice_attempt.id) == before_learning
    assert count_paper_attempts(db_session, practice_attempt.id) == before_attempts
```

`valid_save_command(practice_attempt, student_actor)` 返回阶段 C `ResponseService.save` 所需的完整关键字参数，包括 attempt_id、slot_id、student_id、client_revision_id、base_revision_no、raw_response、client_sent_at、idempotency_key 和 now；本阶段不改写其签名。

- [ ] **步骤 2：运行测试验证失败**

预期：FAIL，报 `ModuleNotFoundError: No module named 'modules.assessment.services.practice'`。

- [ ] **步骤 3：编写最少 PracticeService 实现**

在 `contracts.py` 增加既有幂等存储的端口：`IdempotencyPort.get(scope: str, key: UUID, request_hash: str) -> dict[str, str] | None` 与 `put(scope, key, request_hash, response: dict[str, str]) -> None`。请求哈希覆盖 attempt、slot、response revision、hint 和 solution 请求；同键不同哈希抛 `ResponseConflict`。

```python
class PracticeService:
    def __init__(self, attempts: AttemptService, responses: ResponseService,
                 runner: AnswerTestRunner, idempotency: IdempotencyPort,
                 session: Session, clock: Callable[[], datetime]):
        self.attempts, self.responses, self.runner = attempts, responses, runner
        self.idempotency, self.session, self.clock = idempotency, session, clock

    def check(self, command: PracticeCheckCommand, actor: ActorContext) -> PracticeCheckResult:
        request_hash = canonical_check_hash(command)
        attempt = self.attempts.require_attempt(command.attempt_id, actor)
        if attempt.mode != "practice" or not attempt.practice_policy.checks_enabled:
            raise PolicyDenied()
        slot = require_slot(attempt, command.paper_slot_id)
        revision = self.responses.require_revision(command.attempt_id, command.paper_slot_id,
                                                   command.response_revision_id, actor)
        cached = self.idempotency.get("practice_check", command.client_check_id, request_hash)
        if cached:
            return self._result(UUID(cached["learning_exercise_attempt_id"]),
                                UUID(cached["response_revision_id"]), actor)
        learning = LearningExerciseAttempt(
            id=uuid4(), exercise_version_id=slot.exercise_version_id,
            exercise_instance_id=slot.exercise_instance_id,
            source_response_revision_id=revision.id, paper_attempt_id=attempt.id,
            input_status=revision.parse_status, outcome=None, engine_version=None,
            feedback_policy_version_id=attempt.practice_policy.version_id,
            created_at=self.clock())
        self.session.add(learning)
        self.session.flush()
        outcome = None
        requires_human = False
        if revision.parse_status == "valid":
            if slot.answer_test is None:
                outcome, failure_kind = "fail", "answer_test_missing"
            else:
                try:
                    tested = self.runner.run(revision, slot.answer_test)
                    outcome, failure_kind = tested.outcome, tested.failure_kind
                    self._save_run(learning, revision, slot.answer_test, tested)
                except (CASFailure, CASTimeout, DomainUncertain, CASEngineError) as exc:
                    outcome, failure_kind = "fail", exc.code
                    self._save_failed_run(learning, revision, slot.answer_test, failure_kind)
            learning.outcome = outcome
            learning.engine_version = slot.answer_test.engine_version if slot.answer_test else None
            requires_human = outcome == "fail"
        self.idempotency.put("practice_check", command.client_check_id, request_hash, {
            "learning_exercise_attempt_id": str(learning.id),
            "response_revision_id": str(revision.id)})
        self.session.commit()
        return self._result(learning.id, revision.id, actor)
```

`_result` 重新读取学习投影和 `latest_revision_no`，计算 `superseded = latest > revision_no`，并只返回当前 policy 允许的 `outcome`；关闭正误时返回 `outcome=None`，但保留学习投影中的真实确定性结果。`IdempotencyPort.put` 与学习投影、answer_test_run 使用同一数据库事务，禁止先 commit 再写幂等记录。本任务 `hint_artifact_ids=()`、`solution_artifact_id=None`，由任务 3 接入。`_save_run` 固定保存 `response_hash`、引擎版本、assumptions、branch 和 outcome，不能用模型生成文本替代。

- [ ] **步骤 4：运行练习与自动保存测试验证通过**

运行：`pytest tests/unit/assessment/test_practice_check.py tests/integration/assessment/test_autosave_does_not_check.py -q`

预期：PASS；无效输入 runner 调用数为 0，四类 CAS 异常均为 `fail`，重试只有一条学习投影，自动保存无检查且 attempt 数不变。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/practice.py modules/assessment/services/contracts.py tests/unit/assessment/test_practice_check.py tests/integration/assessment/test_autosave_does_not_check.py
git commit -m "feat: 练习检查绑定响应版本并投影学习记录"
```

### 任务 3：提示、解析策略与 Artifact/曝光重检

**文件：**
- 创建：`modules/assessment/services/feedback.py`
- 修改：`modules/assessment/services/practice.py`
- 测试：`tests/unit/assessment/test_feedback_policy.py`
- 测试：`tests/integration/assessment/test_feedback_artifact_recheck.py`

- [ ] **步骤 1：编写失败的反馈策略与旧链接测试**

```python
def test_hint_level_and_solution_follow_fixed_policy(feedback_service, practice_revision,
                                                     practice_policy, student_actor):
    practice_policy.max_hint_level = 2
    practice_policy.solution_requires_valid_response = True
    allowed = feedback_service.for_check(check_command(practice_revision, hint=2, solution=True),
                                         student_actor)
    assert [a.level for a in allowed.hints] == [1, 2]
    assert allowed.solution is not None
    with pytest.raises(PolicyDenied):
        feedback_service.for_check(check_command(practice_revision, hint=3), student_actor)


def test_invalid_response_cannot_unlock_solution(feedback_service, invalid_practice_revision, student_actor):
    with pytest.raises(PolicyDenied):
        feedback_service.for_check(check_command(invalid_practice_revision, solution=True), student_actor)


def test_old_artifact_url_is_rechecked(feedback_service, closed_feedback_policy,
                                       old_artifact_url, student_actor):
    with pytest.raises(PolicyDenied):
        feedback_service.open_artifact(open_artifact_command(old_artifact_url), student_actor)


def test_exposure_service_blocks_practice_solution_used_by_secure_formal(
        feedback_service, policy_allowed_solution_artifact, exposure_conflict, student_actor):
    with pytest.raises(ExposureConflict):
        feedback_service.open_artifact(
            open_artifact_command(policy_allowed_solution_artifact), student_actor)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_feedback_policy.py tests/integration/assessment/test_feedback_artifact_recheck.py -q`

预期：FAIL，报 `ModuleNotFoundError: No module named 'modules.assessment.services.feedback'`。

- [ ] **步骤 3：编写最少反馈服务并接入检查结果**

```python
class FeedbackService:
    def __init__(self, attempts: AttemptService, responses: ResponseService,
                 catalog: FeedbackCatalog, pdp: PolicyDecisionPoint,
                 exposure: ExposureService):
        self.attempts, self.responses, self.catalog = attempts, responses, catalog
        self.pdp, self.exposure = pdp, exposure

    def for_check(self, command: PracticeCheckCommand, actor: ActorContext) -> FeedbackSelection:
        attempt = self.attempts.require_attempt(command.attempt_id, actor)
        revision = self.responses.require_revision(command.attempt_id, command.paper_slot_id,
                                                   command.response_revision_id, actor)
        policy = attempt.practice_policy
        if not 0 <= command.requested_hint_level <= policy.max_hint_level:
            raise PolicyDenied()
        if command.reveal_solution:
            if not policy.solution_enabled:
                raise PolicyDenied()
            if policy.solution_requires_valid_response and revision.parse_status != "valid":
                raise PolicyDenied()
        slot = require_slot(attempt, command.paper_slot_id)
        return self.catalog.select(slot, revision, command.requested_hint_level,
                                   command.reveal_solution, policy.version_id)

    def open_artifact(self, command: OpenArtifactCommand, actor: ActorContext) -> ArtifactOpenResult:
        attempt = self.attempts.require_attempt(command.attempt_id, actor)
        revision = self.responses.require_revision(command.attempt_id, command.paper_slot_id,
                                                   command.response_revision_id, actor)
        decision = self.pdp.check_artifact(
            actor=actor, artifact_version_id=command.artifact_version_id,
            activity_attempt_id=attempt.id, response_revision_id=revision.id, action="open")
        if not decision.allowed:
            raise PolicyDenied(decision.deny_code)
        if decision.contains_solution:
            self.exposure.assert_solution_release(
                exercise_version_id=decision.exercise_version_id,
                exercise_instance_id=decision.exercise_instance_id,
                paper_version_id=attempt.paper_version_id)
            self.exposure.record_practice_answer_released(
                exercise_version_id=decision.exercise_version_id,
                exercise_instance_id=decision.exercise_instance_id,
                paper_attempt_id=attempt.id)
        latest = self.responses.latest_revision_no(attempt.id, revision.paper_slot_id)
        return ArtifactOpenResult(decision.artifact_version_id, decision.download_url,
                                  revision.id, revision.revision_no, latest, latest > revision.revision_no)
```

`OpenArtifactCommand` 含 `attempt_id/paper_slot_id/response_revision_id/artifact_version_id: UUID`；`ArtifactOpenResult` 含 `artifact_version_id/download_url/response_revision_id/revision_no/latest_revision_no/superseded`。`FeedbackArtifact` 含 `artifact_version_id` 与 `level`，`FeedbackSelection` 含 `hints: tuple[FeedbackArtifact, ...]` 和 `solution: FeedbackArtifact | None`。

`FeedbackCatalog` 是只读端口：`select(slot, revision, hint_level, reveal_solution, policy_version_id) -> FeedbackSelection`，从固定题目的 hint/solution 版本生成 Artifact ID，不读取学生卷答案字段。`PracticeService.check` 调用 `FeedbackService.for_check` 并填入 Artifact ID；打开、下载、签名 URL 续期都路由到 `open_artifact`，不得只校验 URL 签名。PDP 先判权限/时间/反馈策略，`ExposureService` 再阻断练习解析与保密正式卷冲突；只有允许实际打开解析时才写 `practice_answer_released`，拒绝路径不写曝光成功事件。

- [ ] **步骤 4：运行反馈与安全测试验证通过**

运行：`pytest tests/unit/assessment/test_feedback_policy.py tests/integration/assessment/test_feedback_artifact_recheck.py -q`

预期：PASS；越级提示和无效输入解析被拒绝，旧链接重新判定，PDP 允许但曝光冲突仍无下载 URL。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/feedback.py modules/assessment/services/practice.py tests/unit/assessment/test_feedback_policy.py tests/integration/assessment/test_feedback_artifact_recheck.py
git commit -m "feat: 练习反馈重检Artifact并阻断正式卷曝光"
```

### 任务 4：正式 submission 确定性预评与人工分派

**文件：**
- 创建：`modules/assessment/services/grading.py`
- 测试：`tests/unit/assessment/test_evaluate_submission.py`
- 测试：`tests/integration/assessment/test_evaluation_persistence.py`

- [ ] **步骤 1：编写失败的预评分派测试**

```python
def test_true_false_fail_have_distinct_score_semantics(
        grading_service, submission_with_three_objective_items, runner, teacher_actor, db_session):
    runner.results = [answer_true(), answer_false(), answer_fail("domain_uncertain")]
    summary = grading_service.evaluate_submission(submission_with_three_objective_items.id, teacher_actor)
    assert [i.outcome for i in summary.items] == ["true", "false", "fail"]
    assert summary.items[0].suggested_score == Decimal("5.00")
    assert summary.items[1].suggested_score == Decimal("0.00")
    assert summary.items[2].suggested_score is None
    assert summary.items[2].requires_human is True
    assert count_grade_revisions(db_session, submission_with_three_objective_items.id) == 0


@pytest.mark.parametrize("case", ["empty", "proof", "open", "invalid", "low_ocr", "missing_answer_test"])
def test_non_deterministic_items_require_human_without_zero(
        grading_service, submission_case, runner, teacher_actor, case):
    submission = submission_case(case)
    summary = grading_service.evaluate_submission(submission.id, teacher_actor)
    assert summary.items[0].suggested_score is None
    assert summary.items[0].requires_human is True
    assert runner.calls == []


def test_evaluation_retry_reuses_runs(grading_service, submitted_submission, teacher_actor, db_session):
    first = grading_service.evaluate_submission(submitted_submission.id, teacher_actor)
    second = grading_service.evaluate_submission(submitted_submission.id, teacher_actor)
    assert first == second
    assert count_answer_test_runs(db_session, submitted_submission.id) == len(first.items)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_evaluate_submission.py tests/integration/assessment/test_evaluation_persistence.py -q`

预期：FAIL，报 `ModuleNotFoundError: No module named 'modules.assessment.services.grading'`。

- [ ] **步骤 3：编写最少 GradingService 预评实现**

```python
class GradingService:
    def __init__(self, submissions: SubmissionService, runner: AnswerTestRunner,
                 session: Session, clock: Callable[[], datetime],
                 advisor: GroupingAdvisor | None = None):
        self.submissions, self.runner, self.session = submissions, runner, session
        self.clock, self.advisor = clock, advisor

    def evaluate_submission(self, submission_id: UUID, actor: ActorContext) -> EvaluationSummary:
        submission = self.submissions.require_submission(submission_id, actor, "evaluate")
        results = []
        for item in submission.items:
            existing = find_formal_run(self.session, item)
            if existing:
                results.append(evaluation_from_run(item, existing))
                continue
            result = self._evaluate_item(item)
            persist_item_evaluation(self.session, item, result, self.clock())
            results.append(result)
        self.session.commit()
        return EvaluationSummary(submission.id, tuple(results))

    def _evaluate_item(self, item: SubmissionItemContext) -> EvaluationItemResult:
        response = item.response
        if (response is None or item.slot.response_kind in {"proof", "open"}
                or response.parse_status != "valid"
                or item.slot.answer_test is None
                or (item.ocr_confidence is not None and item.ocr_confidence < Decimal("0.80"))):
            return EvaluationItemResult(item.id, None, None, True, None)
        try:
            tested = self.runner.run(response, item.slot.answer_test)
        except (CASFailure, CASTimeout, DomainUncertain, CASEngineError) as exc:
            tested = failed_answer_test(exc.code)
        run = new_formal_run(item, tested, self.clock())
        self.session.add(run)
        self.session.flush()
        if tested.outcome == "true":
            suggested, human = item.slot.max_score, False
        elif tested.outcome == "false":
            suggested, human = Decimal("0.00"), False
        else:
            suggested, human = None, True
        return EvaluationItemResult(item.id, tested.outcome, suggested, human, run.id)
```

`persist_item_evaluation` 只更新既有 `submission_item` 的 `answer_test_run_id/evaluation_status/suggested_score/requires_human`，并插入包含输入哈希、引擎、假设、分支和失败原因的 `AnswerTestRun`。`evaluation_status` 只取 `evaluated/requires_human`；重试按 `(submission_item_id,response_revision_id,answer_test_version_id)` 复用。服务没有 `session.add(Grade...)` 路径。

- [ ] **步骤 4：运行预评与持久化测试验证通过**

运行：`pytest tests/unit/assessment/test_evaluate_submission.py tests/integration/assessment/test_evaluation_persistence.py -q`

预期：PASS；`false` 才建议 0，`fail` 与五类人工项建议分为 NULL，预评重试不重复运行且 grade revision 数为 0。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/grading.py tests/unit/assessment/test_evaluate_submission.py tests/integration/assessment/test_evaluation_persistence.py
git commit -m "feat: 正式提交确定性预评并将不确定项转人工"
```

### 任务 5：rubric 教师确认、分组建议边界与追加 grade revision

**文件：**
- 修改：`modules/assessment/services/contracts.py`
- 修改：`modules/assessment/services/grading.py`
- 测试：`tests/unit/assessment/test_teacher_grading.py`
- 测试：`tests/integration/assessment/test_ai_cannot_write_grade.py`

- [ ] **步骤 1：编写失败的教师评分与 AI 边界测试**

```python
def test_every_item_requires_current_teacher_rubric_decision(
        grading_service, evaluated_submission, teacher_actor):
    command = confirm_command(evaluated_submission, include_only_first_item=True)
    with pytest.raises(ReviewIncomplete):
        grading_service.confirm_grade(command, teacher_actor)


def test_teacher_confirmation_appends_but_does_not_publish(
        grading_service, evaluated_submission, teacher_actor, db_session):
    revision = grading_service.confirm_grade(
        confirm_command(evaluated_submission), teacher_actor)
    assert revision.revision_no == 1
    assert revision.status == "confirmed"
    assert revision.published_at is None
    decisions = item_decisions(db_session, evaluated_submission.id)
    assert len(decisions) == len(evaluated_submission.items)
    assert {d.decided_by for d in decisions} == {teacher_actor.user_id}


def test_rubric_criteria_must_sum_to_item_score(grading_service, evaluated_submission, teacher_actor):
    command = confirm_command(evaluated_submission, criterion_delta=Decimal("0.01"))
    with pytest.raises(ValueError, match="rubric_score_mismatch"):
        grading_service.confirm_grade(command, teacher_actor)


def test_ai_grouping_is_only_a_suggestion(grading_service, evaluated_submission,
                                          ai_advisor, teacher_actor, db_session):
    ai_advisor.suggestions = [group_all_items(evaluated_submission)]
    suggestions = grading_service.suggest_groups(evaluated_submission.id, teacher_actor)
    assert suggestions[0].status == "proposed"
    assert count_grade_revisions(db_session, evaluated_submission.id) == 0
    assert count_item_decisions(db_session, evaluated_submission.id) == 0


def test_worker_database_role_cannot_insert_formal_grade(ai_db_session, evaluated_submission):
    with pytest.raises(Exception, match="permission denied"):
        ai_db_session.execute(text("""
            INSERT INTO assessment.grade_revision
              (id, grade_id, submission_id, revision_no, total_score, status, created_by, created_at)
            VALUES (gen_random_uuid(), gen_random_uuid(), :submission_id, 1, 0, 'confirmed',
                    gen_random_uuid(), now())
        """), {"submission_id": evaluated_submission.id})


def test_second_confirmation_preserves_previous_revision(
        grading_service, confirmed_grade, teacher_actor, db_session):
    changed = confirm_command(confirmed_grade.submission, base_revision_no=1,
                              change_reason="教师复核后调整步骤分")
    second = grading_service.confirm_grade(changed, teacher_actor)
    first = db_session.get(type(second), confirmed_grade.id)
    assert second.revision_no == 2
    assert second.previous_revision_id == first.id
    assert first.total_score == confirmed_grade.total_score
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_teacher_grading.py tests/integration/assessment/test_ai_cannot_write_grade.py -q`

预期：FAIL，报 `AttributeError: 'GradingService' object has no attribute 'confirm_grade'`。

- [ ] **步骤 3：编写最少教师确认与建议实现**

在 `contracts.py` 定义 `RubricDefinition.criteria: dict[str, Decimal]`、`RubricRepository.get(version_id) -> RubricDefinition`、`GroupingSuggestionInput(group_key, submission_item_ids, rationale, model_version)` 与 `GroupingAdvisor.suggest(submission) -> tuple[GroupingSuggestionInput, ...]`。给 `GradingService` 注入 `rubrics: RubricRepository`。

```python
def suggest_groups(self, submission_id: UUID, actor: ActorContext):
    submission = self.submissions.require_submission(submission_id, actor, "grade")
    if self.advisor is None:
        return ()
    rows = [GradingSuggestion(id=uuid4(), submission_id=submission.id,
            group_key=s.group_key, submission_item_ids=list(map(str, s.submission_item_ids)),
            rationale=s.rationale, model_version=s.model_version,
            status="proposed", created_at=self.clock())
            for s in self.advisor.suggest(submission)]
    self.session.add_all(rows)
    self.session.commit()
    return rows


def confirm_grade(self, command: ConfirmGradeCommand, actor: ActorContext) -> GradeRevision:
    submission = self.submissions.require_submission(command.submission_id, actor, "grade")
    expected_ids = {item.id for item in submission.items}
    decision_ids = {d.submission_item_id for d in command.decisions}
    if decision_ids != expected_ids or len(command.decisions) != len(expected_ids):
        raise ReviewIncomplete("every_submission_item_requires_teacher_decision")
    total = Decimal("0.00")
    for decision in command.decisions:
        item = require_submission_item(submission, decision.submission_item_id)
        rubric = self.rubrics.get(decision.rubric_version_id)
        if decision.cancelled:
            if not submission.revocation.active:
                raise PolicyDenied("cancelled_item_requires_active_revocation")
            if submission.revocation.paper_slot_id not in {None, item.slot.paper_slot_id}:
                raise PolicyDenied("cancelled_item_revocation_mismatch")
            if command.withdrawal_action is None:
                raise ValueError("cancelled_item_requires_withdrawal_action")
            if decision.score is not None or not command.student_message:
                raise ValueError("cancelled_item_requires_empty_score_and_student_message")
            continue
        if decision.score is None or set(decision.criterion_scores) != set(rubric.criteria):
            raise ValueError("rubric_score_mismatch")
        for code, score in decision.criterion_scores.items():
            if score < 0 or score > rubric.criteria[code]:
                raise ValueError("rubric_criterion_out_of_range")
        if sum(decision.criterion_scores.values(), Decimal("0.00")) != decision.score:
            raise ValueError("rubric_score_mismatch")
        if decision.score > item.slot.max_score:
            raise ValueError("item_score_out_of_range")
        total += decision.score
    grade = get_or_create_grade(self.session, submission.id)
    latest = latest_grade_revision(self.session, grade.id)
    current_no = latest.revision_no if latest else 0
    if command.base_revision_no != current_no:
        raise ResponseConflict("stale_grade_revision")
    revision = GradeRevision(id=uuid4(), grade_id=grade.id, submission_id=submission.id,
        revision_no=current_no + 1, previous_revision_id=latest.id if latest else None,
        total_score=total, status="confirmed", change_reason=command.change_reason,
        student_message=command.student_message, regrade_run_id=command.regrade_run_id,
        withdrawal_action=command.withdrawal_action, created_by=actor.user_id,
        created_at=self.clock())
    self.session.add(revision)
    self.session.flush()
    for decision in command.decisions:
        append_item_decision(self.session, decision, revision, actor.user_id, self.clock())
    self.session.commit()
    return revision
```

`append_item_decision` 的 `decision_seq` 取该 item 当前最大值加 1，教师 ID 永远来自 `actor.user_id`，建议中的模型 ID 不能成为签署人。分组采纳只在前端生成一组显式 `ItemDecisionInput`；后端仍逐项校验 rubric、分值与完整性。首次确认创建 revision 1，改分使用当前 `base_revision_no` 追加，旧分数字段不更新。

- [ ] **步骤 4：运行教师评分与 AI 负向测试验证通过**

运行：`pytest tests/unit/assessment/test_teacher_grading.py tests/integration/assessment/test_ai_cannot_write_grade.py -q`

预期：PASS；缺一项即拒绝确认，AI 建议后 grade/decision 数均为 0，教师确认不发布，第二次确认保留第一版分数。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/contracts.py modules/assessment/services/grading.py tests/unit/assessment/test_teacher_grading.py tests/integration/assessment/test_ai_cannot_write_grade.py
git commit -m "feat: 教师按量规确认成绩并隔离AI分组建议"
```

### 任务 6：独立成绩发布、幂等与撤回影响

**文件：**
- 修改：`modules/assessment/services/grading.py`
- 测试：`tests/unit/assessment/test_publish_grade.py`
- 测试：`tests/integration/assessment/test_withdrawn_grading.py`

- [ ] **步骤 1：编写失败的发布与撤回测试**

```python
def test_confirmation_and_publication_are_separate_actions(
        grading_service, confirmed_grade_revision, teacher_actor, student_feedback_query):
    assert student_feedback_query(confirmed_grade_revision.submission_id).grade is None
    published = grading_service.publish_grade(
        confirmed_grade_revision.id, teacher_actor, uuid4())
    assert published.status == "published"
    assert student_feedback_query(confirmed_grade_revision.submission_id).grade.id == published.id


def test_worker_or_ai_actor_cannot_publish(grading_service, confirmed_grade_revision, worker_actor):
    with pytest.raises(PolicyDenied):
        grading_service.publish_grade(confirmed_grade_revision.id, worker_actor, uuid4())


def test_publish_retry_returns_same_revision(grading_service, confirmed_grade_revision, teacher_actor):
    key = uuid4()
    first = grading_service.publish_grade(confirmed_grade_revision.id, teacher_actor, key)
    second = grading_service.publish_grade(confirmed_grade_revision.id, teacher_actor, key)
    assert first.id == second.id
    assert count_grade_publication_events(first.id) == 1


def test_withdrawal_blocks_old_revision_until_explicit_resolution(
        grading_service, confirmed_grade_on_withdrawn_item, teacher_actor):
    with pytest.raises(WithdrawnError):
        grading_service.publish_grade(confirmed_grade_on_withdrawn_item.id, teacher_actor, uuid4())
    resolved = grading_service.confirm_grade(
        withdrawal_resolution_command(confirmed_grade_on_withdrawn_item,
            action="cancel_item", student_message="该题因内容撤回取消，总分已调整为95分"),
        teacher_actor)
    published = grading_service.publish_grade(resolved.id, teacher_actor, uuid4())
    assert published.total_score == Decimal("95.00")
    assert published.withdrawal_action == "cancel_item"


def test_close_assignment_requires_zero_and_visible_explanation(
        grading_service, submission_with_active_withdrawal, teacher_actor):
    command = withdrawal_resolution_command(submission_with_active_withdrawal,
        action="close_assignment", student_message="")
    with pytest.raises(ValueError, match="withdrawal_resolution_required"):
        grading_service.confirm_grade(command, teacher_actor)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_publish_grade.py tests/integration/assessment/test_withdrawn_grading.py -q`

预期：FAIL，报 `AttributeError: 'GradingService' object has no attribute 'publish_grade'`。

- [ ] **步骤 3：编写最少发布与撤回门禁**

给服务注入既有 `OutboxPort.append(event_type, aggregate_id, payload)` 与任务 2 的 `IdempotencyPort`。`SubmissionService.require_submission(..., "publish")` 负责当前用户范围与当前成绩策略门禁，服务再检查人工完整性和撤回。

```python
def publish_grade(self, grade_revision_id: UUID, actor: ActorContext,
                  idempotency_key: UUID) -> GradeRevision:
    cached = self.idempotency.get("grade_publish", idempotency_key,
                                  canonical_hash({"revision": str(grade_revision_id)}))
    revision = self.session.get(GradeRevision, grade_revision_id)
    if revision is None:
        raise LookupError("grade_revision_not_found")
    submission = self.submissions.require_submission(revision.submission_id, actor, "publish")
    if cached:
        return self.session.get(GradeRevision, UUID(cached["grade_revision_id"]))
    if revision.status not in {"confirmed", "published"}:
        raise ReviewIncomplete("grade_revision_not_teacher_confirmed")
    decisions = item_decisions_for_revision(self.session, revision.id)
    if {d.submission_item_id for d in decisions} != {i.id for i in submission.items}:
        raise ReviewIncomplete("every_submission_item_requires_teacher_decision")
    if revision.created_by is None or any(d.decided_by != revision.created_by for d in decisions):
        raise ReviewIncomplete("teacher_signature_mismatch")
    if submission.revocation.active:
        if not revision.withdrawal_action or not revision.change_reason or not revision.student_message:
            raise WithdrawnError("withdrawal_resolution_required")
        validate_withdrawal_resolution(revision, decisions, submission.revocation)
    if revision.status == "confirmed":
        revision.status = "published"
        revision.published_by = actor.user_id
        revision.published_at = self.clock()
        grade = self.session.get(Grade, revision.grade_id)
        grade.status = "published"
        grade.current_published_revision_id = revision.id
        self.outbox.append("assessment.grade_published", grade.id, {
            "grade_revision_id": str(revision.id), "submission_id": str(submission.id)})
    self.session.commit()
    self.idempotency.put("grade_publish", idempotency_key,
        canonical_hash({"revision": str(grade_revision_id)}),
        {"grade_revision_id": str(revision.id)})
    return revision
```

`validate_withdrawal_resolution` 规则固定：`cancel_item` 必须至少一项 `score IS NULL`，总分等于其余教师决定之和，且取消项对应当前撤回 slot；`close_assignment` 必须全部取消且总分为 0。撤回不会删除 submission_item，旧发布 revision 保持可读；新 revision 发布前学生仍看到旧版及撤回/重评状态。

- [ ] **步骤 4：运行发布与撤回测试验证通过**

运行：`pytest tests/unit/assessment/test_publish_grade.py tests/integration/assessment/test_withdrawn_grading.py -q`

预期：PASS；确认后学生不可见，worker 发布被拒绝，重复发布只有一个 outbox 事件，撤回必须显式取消并重新计算总分。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/grading.py tests/unit/assessment/test_publish_grade.py tests/integration/assessment/test_withdrawn_grading.py
git commit -m "feat: 独立发布成绩并约束撤回后的改分"
```

### 任务 7：学生申诉、regrade run 与改分发布闭环

**文件：**
- 创建：`modules/assessment/services/regrade.py`
- 修改：`modules/assessment/services/grading.py`
- 测试：`tests/unit/assessment/test_regrade.py`
- 测试：`tests/integration/assessment/test_appeal_regrade_flow.py`

- [ ] **步骤 1：编写失败的申诉与重评测试**

```python
def test_appeal_does_not_mutate_submission(regrade_service, published_grade,
                                           student_actor, db_session):
    before = submission_manifest(db_session, published_grade.submission_id)
    appeal = regrade_service.create_appeal(CreateAppealCommand(
        submission_id=published_grade.submission_id,
        grade_revision_id=published_grade.id, reason="第二步等价变形应得步骤分"), student_actor)
    assert appeal.status == "open"
    assert submission_manifest(db_session, published_grade.submission_id) == before


def test_regrade_run_compares_without_overwriting_published_grade(
        regrade_service, published_grade, corrected_answer_test, teacher_actor, db_session):
    view = regrade_service.create_run(CreateRegradeRunCommand(
        submission_id=published_grade.submission_id, appeal_id=None,
        new_answer_test_version_id=corrected_answer_test.id,
        new_rubric_version_id=None, reason="修正答案测试定义域"), teacher_actor)
    assert view.status == "pending"
    assert current_published_revision(db_session, published_grade.submission_id) == published_grade.id


def test_regrade_fail_is_human_not_zero(regrade_service, published_grade,
                                        corrected_but_failing_answer_test, teacher_actor):
    view = regrade_service.create_run(CreateRegradeRunCommand(
        submission_id=published_grade.submission_id, appeal_id=None,
        new_answer_test_version_id=corrected_but_failing_answer_test.id,
        new_rubric_version_id=None, reason="核对定义域"), teacher_actor)
    assert view.items[0].new_outcome == "fail"
    assert view.items[0].new_score is None
    assert view.items[0].requires_human is True


def test_confirmed_regrade_appends_and_resolves_appeal_only_after_publish(
        regrade_service, grading_service, open_appeal, teacher_actor, student_feedback_query):
    run = regrade_service.create_run(regrade_command_for_appeal(open_appeal), teacher_actor)
    for item in run.items:
        regrade_service.confirm_item(confirm_regrade_item_command(item), teacher_actor)
    revision = grading_service.confirm_grade(
        regrade_grade_command(run, base_revision_no=1), teacher_actor)
    assert student_feedback_query(run.submission_id).grade.revision_no == 1
    assert student_feedback_query(run.submission_id).regrade_status == "in_review"
    grading_service.publish_grade(revision.id, teacher_actor, uuid4())
    feedback = student_feedback_query(run.submission_id)
    assert feedback.grade.revision_no == 2
    assert feedback.appeal_status == "resolved"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_regrade.py tests/integration/assessment/test_appeal_regrade_flow.py -q`

预期：FAIL，报 `ModuleNotFoundError: No module named 'modules.assessment.services.regrade'`。

- [ ] **步骤 3：编写最少 RegradeService 与发布联动**

`CreateAppealCommand` 定义 `submission_id/grade_revision_id/reason`；`create_appeal` 使用 `require_submission(..., "appeal")`，验证学生本人和已发布 revision，只插入 `GradeAppeal`。`RegradeService` 注入 `SubmissionService`、`AnswerTestRunner`、SQLAlchemy session 和 clock。

```python
class RegradeService:
    def create_run(self, command: CreateRegradeRunCommand,
                   actor: ActorContext) -> RegradeRunView:
        submission = self.submissions.require_submission(command.submission_id, actor, "grade")
        run = RegradeRun(id=uuid4(), submission_id=submission.id, appeal_id=command.appeal_id,
            new_answer_test_version_id=command.new_answer_test_version_id,
            new_rubric_version_id=command.new_rubric_version_id,
            reason=command.reason, status="pending", created_by=actor.user_id,
            created_at=self.clock())
        self.session.add(run)
        self.session.flush()
        for item in submission.items:
            old = current_item_decision(self.session, item.id)
            new_outcome, new_score, requires_human = self._preview(item, command)
            self.session.add(RegradeItem(id=uuid4(), regrade_run_id=run.id,
                submission_item_id=item.id, old_score=old.score if old else None,
                old_outcome=current_outcome(self.session, item.id),
                new_score=new_score, new_outcome=new_outcome,
                requires_human=requires_human, status="pending"))
        if command.appeal_id:
            appeal = self.session.get(GradeAppeal, command.appeal_id)
            appeal.status = "in_review"
        self.session.commit()
        return regrade_view(run.id, self.session)

    def _preview(self, item: SubmissionItemContext, command: CreateRegradeRunCommand):
        if command.new_answer_test_version_id is None:
            return current_outcome(self.session, item.id), None, True
        answer_test = load_approved_answer_test(command.new_answer_test_version_id)
        try:
            tested = self.runner.run(item.response, answer_test)
        except (CASFailure, CASTimeout, DomainUncertain, CASEngineError):
            return "fail", None, True
        if tested.outcome == "fail":
            return "fail", None, True
        return tested.outcome, item.slot.max_score if tested.outcome == "true" else Decimal("0.00"), False
```

`confirm_item` 只允许授权教师写入 `RegradeItem.status/confirmed_score/criterion_scores/rationale/confirmed_by/confirmed_at`，`fail` 项也必须显式评分，不能自动填 0。所有 item confirmed 后，`GradingService.confirm_grade` 校验 `regrade_run_id` 属于同一 submission、决定来自该 run 且逐项已确认，再追加 revision。`publish_grade` 成功后把关联 appeal 置为 `resolved` 并写 `resolution=新revision ID`；仅创建 run 或确认新 revision 不改变旧发布成绩。

- [ ] **步骤 4：运行申诉与重评测试验证通过**

运行：`pytest tests/unit/assessment/test_regrade.py tests/integration/assessment/test_appeal_regrade_flow.py -q`

预期：PASS；申诉不改变提交哈希，重评 `fail` 不打 0，新 revision 发布前旧成绩可见，发布后申诉才 resolved。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/regrade.py modules/assessment/services/grading.py tests/unit/assessment/test_regrade.py tests/integration/assessment/test_appeal_regrade_flow.py
git commit -m "feat: 申诉与重评通过追加成绩版本闭环"
```

### 任务 8：practice/grading API、安全 DTO 与统一错误语义

**文件：**
- 创建：`apps/api/routers/practice.py`
- 创建：`apps/api/routers/grading.py`
- 修改：`apps/api/deps.py`
- 测试：`tests/integration/assessment/test_practice_api.py`
- 测试：`tests/integration/assessment/test_grading_api.py`

- [ ] **步骤 1：编写失败的 API 契约与负向测试**

```python
def test_practice_check_response_identifies_revision_and_hides_answer_test(
        client, student_headers, practice_revision, monkeypatch):
    response = client.post(
        f"/attempts/{practice_revision.attempt_id}/checks/{practice_revision.paper_slot_id}",
        headers={**student_headers, "Idempotency-Key": str(uuid4())},
        json={"response_revision_id": str(practice_revision.id),
              "requested_hint_level": 0, "reveal_solution": False})
    assert response.status_code == 200
    body = response.json()
    assert body["response_revision_id"] == str(practice_revision.id)
    assert body["revision_no"] == practice_revision.revision_no
    assert "answer_test" not in body and "rubric" not in body and "answer" not in body


def test_fail_is_successful_check_with_indeterminate_result(client, student_headers, cas_fail_revision):
    response = client.post(check_url(cas_fail_revision), headers=student_headers,
                           json=check_json(cas_fail_revision))
    assert response.status_code == 200
    assert response.json()["outcome"] == "fail"
    assert "suggested_score" not in response.json()


def test_old_artifact_open_is_denied_by_current_policy(client, student_headers, old_feedback_artifact):
    response = client.post(open_artifact_url(old_feedback_artifact),
                           headers=student_headers, json=open_artifact_json(old_feedback_artifact))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "policy_denied"


def test_prevaluation_never_exposes_suggested_score_to_student(
        client, student_headers, evaluated_submission):
    response = client.get(f"/submissions/{evaluated_submission.id}/feedback", headers=student_headers)
    assert response.status_code == 200
    assert response.json()["grade"] is None
    assert "suggested_score" not in response.text


def test_worker_cannot_call_grade_or_publish(client, worker_headers, evaluated_submission):
    confirmed = client.post(f"/submissions/{evaluated_submission.id}/grades",
                            headers=worker_headers, json=teacher_grade_json(evaluated_submission))
    assert confirmed.status_code == 403


def test_appeal_and_regrade_use_revision_resources(client, student_headers, teacher_headers,
                                                   published_grade, teacher_grade_json):
    appeal = client.post(f"/submissions/{published_grade.submission_id}/regrade-requests",
        headers=student_headers, json={"grade_revision_id": str(published_grade.id), "reason": "步骤分"})
    assert appeal.status_code == 201
    run = client.post("/regrade-runs", headers=teacher_headers,
        json={"submission_id": str(published_grade.submission_id),
              "appeal_id": appeal.json()["id"], "new_answer_test_version_id": None,
              "new_rubric_version_id": str(published_grade.rubric_version_id), "reason": "复核量规"})
    assert run.status_code == 201
    assert run.json()["status"] == "pending"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/integration/assessment/test_practice_api.py tests/integration/assessment/test_grading_api.py -q`

预期：FAIL；路由尚未注册，练习检查返回 404，grading router 导入失败。

- [ ] **步骤 3：编写最少路由与 DTO 实现**

`practice.py` 的请求模型只允许 revision、提示级别和解析布尔值；`client_check_id` 直接取 `Idempotency-Key`，防止路径与幂等身份分裂。

```python
class PracticeCheckRequest(BaseModel):
    response_revision_id: UUID
    requested_hint_level: int = Field(default=0, ge=0, le=20)
    reveal_solution: bool = False

class OpenFeedbackArtifactRequest(BaseModel):
    paper_slot_id: UUID
    response_revision_id: UUID

@router.post("/attempts/{attempt_id}/checks/{paper_slot_id}")
def check(attempt_id: UUID, paper_slot_id: UUID, body: PracticeCheckRequest,
          idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
          actor: Annotated[ActorContext, Depends(current_actor)],
          service: Annotated[PracticeService, Depends(get_practice_service)]):
    return service.check(PracticeCheckCommand(attempt_id=attempt_id,
        paper_slot_id=paper_slot_id, response_revision_id=body.response_revision_id,
        client_check_id=idempotency_key, requested_hint_level=body.requested_hint_level,
        reveal_solution=body.reveal_solution), actor)

@router.post("/attempts/{attempt_id}/feedback-artifacts/{artifact_version_id}/open")
def open_artifact(attempt_id: UUID, artifact_version_id: UUID,
                  body: OpenFeedbackArtifactRequest,
                  actor: Annotated[ActorContext, Depends(current_actor)],
                  service: Annotated[FeedbackService, Depends(get_feedback_service)]):
    return service.open_artifact(OpenArtifactCommand(attempt_id=attempt_id,
        paper_slot_id=body.paper_slot_id, response_revision_id=body.response_revision_id,
        artifact_version_id=artifact_version_id), actor)
```

`grading.py` 注册以下精确路由并直接调用共享服务：

```python
@router.post("/submissions/{submission_id}/evaluate", response_model=EvaluationSummary)
def evaluate(submission_id: UUID, actor: ActorContext = Depends(current_actor),
             service: GradingService = Depends(get_grading_service)):
    return service.evaluate_submission(submission_id, actor)

@router.post("/submissions/{submission_id}/grades", response_model=GradeRevisionResponse, status_code=201)
def confirm(submission_id: UUID, body: ConfirmGradeRequest,
            actor: ActorContext = Depends(current_actor),
            service: GradingService = Depends(get_grading_service)):
    return service.confirm_grade(body.to_command(submission_id), actor)

@router.post("/grade-revisions/{grade_revision_id}/publish", response_model=GradeRevisionResponse)
def publish(grade_revision_id: UUID,
            idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
            actor: ActorContext = Depends(current_actor),
            service: GradingService = Depends(get_grading_service)):
    return service.publish_grade(grade_revision_id, actor, idempotency_key)

@router.post("/submissions/{submission_id}/regrade-requests", status_code=201)
def appeal(submission_id: UUID, body: CreateAppealRequest,
           actor: ActorContext = Depends(current_actor),
           service: RegradeService = Depends(get_regrade_service)):
    return service.create_appeal(body.to_command(submission_id), actor)

@router.post("/regrade-runs", response_model=RegradeRunView, status_code=201)
def regrade(body: CreateRegradeRunRequest, actor: ActorContext = Depends(current_actor),
            service: RegradeService = Depends(get_regrade_service)):
    return service.create_run(body.to_command(), actor)
```

`ConfirmGradeRequest` 明确包含 `base_revision_no/decisions/change_reason/student_message/regrade_run_id/withdrawal_action`；每个 decision 包含 rubric、criterion scores、score、rationale、cancelled。`GradeRevisionResponse` 只含 `id/submission_id/revision_no/previous_revision_id/total_score/status/change_reason/student_message/regrade_run_id/withdrawal_action/created_by/created_at/published_by/published_at`。

`evaluate/confirm/appeal/regrade` 四个写路由都增加 `dependencies=[Depends(require_idempotency_key)]`；该依赖从 `Idempotency-Key` 读取 UUID，在身份与范围校验后通过既有 `ops.idempotency_key` 重放相同请求结果，同键不同请求哈希返回 409。`publish` 使用上面显式的 key 参数，GET 反馈不要求写幂等键。

学生 `GET /submissions/{id}/feedback` 使用独立 `StudentFeedbackResponse`，只含已发布 grade revision、题目版本、当前可见 rubric、appeal/regrade 状态和策略允许的练习反馈，不序列化 ORM 对象、`suggested_score`、answer_test 或教师批注。

全局映射为 `PolicyDenied/ExposureConflict -> 403`，资源隐藏 -> 404，`ResponseConflict -> 409`，`WithdrawnError -> 410`，请求结构/rubric -> 422，`EvaluationUnavailable -> 503`。CAS 不确定结果本身返回 200 + `fail`，提交已成功时预评故障不得改成「提交失败」。

- [ ] **步骤 4：运行 API 测试验证通过**

运行：`pytest tests/integration/assessment/test_practice_api.py tests/integration/assessment/test_grading_api.py -q`

预期：PASS；练习 DTO 无答案字段，学生看不到建议分，worker 不能确认/发布，Artifact 旧链接按当前策略返回 403。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/routers/practice.py apps/api/routers/grading.py apps/api/deps.py tests/integration/assessment/test_practice_api.py tests/integration/assessment/test_grading_api.py
git commit -m "feat: 开放练习评分与申诉重评API"
```

### 任务 9：TeacherReviewWorkspace 教师复核与分开发布

**文件：**
- 创建：`apps/web/components/assessment/TeacherReviewWorkspace.tsx`
- 测试：`apps/web/components/assessment/__tests__/TeacherReviewWorkspace.test.tsx`

- [ ] **步骤 1：编写失败的教师工作台测试**

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { TeacherReviewWorkspace } from "../TeacherReviewWorkspace";

it("CAS失败显示待人工且不是0分", () => {
  render(<TeacherReviewWorkspace items={[reviewItem({ outcome: "fail", suggestedScore: null,
    requiresHuman: true })]} suggestions={[]} onConfirm={vi.fn()} onPublish={vi.fn()} />);
  expect(screen.getByText("暂不能判定，需教师评分")).toBeInTheDocument();
  expect(screen.getByLabelText("题目得分")).toHaveValue(null);
});

it("AI分组建议只能填入草稿，确认与发布是两个动作", () => {
  const onConfirm = vi.fn(); const onPublish = vi.fn();
  const { rerender } = render(<TeacherReviewWorkspace items={[reviewItem()]} 
    suggestions={[groupSuggestion()]} onConfirm={onConfirm} onPublish={onPublish} />);
  expect(screen.getByText("AI 建议，未计入正式成绩")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "采纳为本组评分草稿" }));
  expect(onConfirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "教师确认成绩" }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
  expect(onPublish).not.toHaveBeenCalled();
  rerender(<TeacherReviewWorkspace items={[reviewItem()]} suggestions={[]} confirmedRevisionId="rev-1"
    onConfirm={onConfirm} onPublish={onPublish} />);
  fireEvent.click(screen.getByRole("button", { name: "发布成绩" }));
  expect(onPublish).toHaveBeenCalledWith("rev-1");
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test -- src/components/__tests__/TeacherReviewWorkspace.test.tsx`

预期：FAIL，报 `Cannot find module '../TeacherReviewWorkspace'`。

- [ ] **步骤 3：编写最少教师组件**

```tsx
export type ReviewItem = {
  submissionItemId: string; outcome: "true" | "false" | "fail" | null;
  suggestedScore: string | null; requiresHuman: boolean; maxScore: string;
};
export type GroupSuggestion = { id: string; label: string; score: string };

export function TeacherReviewWorkspace({ items, suggestions, confirmedRevisionId,
  onConfirm, onPublish }: {
  items: ReviewItem[]; suggestions: GroupSuggestion[]; confirmedRevisionId?: string;
  onConfirm: (decisions: Array<{ submissionItemId: string; score: string }>) => void;
  onPublish: (revisionId: string) => void;
}) {
  const [scores, setScores] = useState<Record<string, string>>({});
  return <section aria-label="教师复核工作台">
    {items.map(item => <article key={item.submissionItemId}>
      {item.outcome === "fail" && <p role="alert">暂不能判定，需教师评分</p>}
      <label>题目得分<input aria-label="题目得分" type="number" min="0" max={item.maxScore}
        value={scores[item.submissionItemId] ?? ""} onChange={e => setScores(
          {...scores, [item.submissionItemId]: e.target.value})} /></label>
    </article>)}
    {suggestions.map(s => <aside key={s.id}><p>AI 建议，未计入正式成绩</p>
      <button onClick={() => setScores(Object.fromEntries(items.map(
        i => [i.submissionItemId, s.score])))}>采纳为本组评分草稿</button></aside>)}
    <button disabled={!items.every(i => scores[i.submissionItemId] !== undefined)}
      onClick={() => onConfirm(items.map(i => ({ submissionItemId: i.submissionItemId,
        score: scores[i.submissionItemId] })))}>教师确认成绩</button>
    <button disabled={!confirmedRevisionId}
      onClick={() => confirmedRevisionId && onPublish(confirmedRevisionId)}>发布成绩</button>
  </section>;
}
```

实际 API 适配器把分数字符串转 DTO，并提交每项 rubric criterion、rationale、base revision；组件不能把 AI 建议标记成教师决定，也不能在确认成功后自动调用发布。CAS fail 的输入保持空值，教师必须输入有效 rubric 分才能确认。

- [ ] **步骤 4：运行组件测试验证通过**

运行：`npm --prefix apps/web run test -- src/components/__tests__/TeacherReviewWorkspace.test.tsx`

预期：PASS；CAS fail 不显示 0，采纳建议不确认，确认不发布，只有独立发布按钮调用 `onPublish`。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/components/assessment/TeacherReviewWorkspace.tsx apps/web/components/assessment/__tests__/TeacherReviewWorkspace.test.tsx
git commit -m "feat: 教师复核工作台分离AI建议确认与发布"
```

### 任务 10：StudentFeedbackView revision 反馈、成绩版本与申诉

**文件：**
- 创建：`apps/web/components/assessment/StudentFeedbackView.tsx`
- 测试：`apps/web/components/assessment/__tests__/StudentFeedbackView.test.tsx`

- [ ] **步骤 1：编写失败的学生反馈测试**

```tsx
it("旧revision反馈不覆盖当前输入", () => {
  render(<StudentFeedbackView feedback={practiceFeedback({ revisionNo: 2,
    latestRevisionNo: 3, superseded: true, outcome: "false" })} onAppeal={vi.fn()} />);
  expect(screen.getByText("此反馈属于第 2 次输入；当前已保存第 3 次输入")).toBeInTheDocument();
});

it("CAS失败显示暂不能判定且不打0", () => {
  render(<StudentFeedbackView feedback={practiceFeedback({ outcome: "fail" })} onAppeal={vi.fn()} />);
  expect(screen.getByText("暂不能判定")).toBeInTheDocument();
  expect(screen.queryByText("0 分")).not.toBeInTheDocument();
});

it("正式反馈显示成绩版本量规申诉和撤回说明", () => {
  const onAppeal = vi.fn();
  render(<StudentFeedbackView feedback={publishedFeedback({ revisionNo: 2,
    regradeStatus: "in_review", withdrawalMessage: "该题撤回，总分调整为95分" })}
    onAppeal={onAppeal} />);
  expect(screen.getByText("成绩版本 2")).toBeInTheDocument();
  expect(screen.getByText("重评中，当前仍显示上一已发布成绩")).toBeInTheDocument();
  expect(screen.getByText("该题撤回，总分调整为95分")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "申请复核" }));
  expect(onAppeal).toHaveBeenCalledTimes(1);
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test -- src/components/__tests__/StudentFeedbackView.test.tsx`

预期：FAIL，报 `Cannot find module '../StudentFeedbackView'`。

- [ ] **步骤 3：编写最少学生组件**

```tsx
export type StudentFeedback = {
  mode: "practice" | "formal"; outcome?: "true" | "false" | "fail" | null;
  revisionNo?: number; latestRevisionNo?: number; superseded?: boolean;
  score?: string; gradeRevisionNo?: number; regradeStatus?: string;
  rubricUrl?: string; withdrawalMessage?: string;
};

export function StudentFeedbackView({ feedback, onAppeal }: {
  feedback: StudentFeedback; onAppeal: () => void;
}) {
  return <section aria-live="polite" aria-label="作答反馈">
    {feedback.superseded && <p role="status">此反馈属于第 {feedback.revisionNo} 次输入；
      当前已保存第 {feedback.latestRevisionNo} 次输入</p>}
    {feedback.mode === "practice" && feedback.outcome === "true" && <p>回答正确</p>}
    {feedback.mode === "practice" && feedback.outcome === "false" && <p>回答不正确</p>}
    {feedback.outcome === "fail" && <p>暂不能判定</p>}
    {feedback.mode === "formal" && <>
      <h2>成绩版本 {feedback.gradeRevisionNo}</h2><p>{feedback.score} 分</p>
      {feedback.regradeStatus === "in_review" && <p>重评中，当前仍显示上一已发布成绩</p>}
      {feedback.withdrawalMessage && <p role="alert">{feedback.withdrawalMessage}</p>}
      {feedback.rubricUrl && <a href={feedback.rubricUrl}>查看评分量规</a>}
      <button onClick={onAppeal}>申请复核</button>
    </>}
  </section>;
}
```

练习解析按钮使用任务 8 的 open 端点，不直接复用缓存 URL；打开失败显示「当前策略不允许查看或内容存在曝光冲突」。无效输入显示输入错误，不显示「回答不正确」；成绩未发布时不渲染建议分或空白成绩卡。

- [ ] **步骤 4：运行组件测试验证通过**

运行：`npm --prefix apps/web run test -- src/components/__tests__/StudentFeedbackView.test.tsx`

预期：PASS；旧 revision 有明确文本，CAS fail 无 0 分，正式成绩版本、量规、重评状态、撤回说明与申诉入口可见。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/components/assessment/StudentFeedbackView.tsx apps/web/components/assessment/__tests__/StudentFeedbackView.test.tsx
git commit -m "feat: 学生反馈标明响应与成绩版本并支持申诉"
```

### 任务 11：端到端策略矩阵、worker 组合与全量验收

**文件：**
- 修改：`apps/api/main.py`
- 修改：`apps/worker/main.py`
- 修改：`apps/api/deps.py`
- 测试：`tests/e2e/test_practice_to_grading.py`

- [ ] **步骤 1：编写失败的端到端验收**

```python
def test_practice_and_formal_closed_loop(e2e, runner):
    practice = e2e.start_practice(student="s1")
    saved = e2e.save(practice, "x+1")
    checked = e2e.check(practice, saved.response_revision_id)
    assert checked.outcome == "true"
    assert e2e.learning_projection(checked).source_response_revision_id == saved.response_revision_id
    assert e2e.formal_grade_count(practice) == 0

    formal = e2e.submit_formal(student="s1", responses=["x+1", "证明略"])
    runner.force("domain_uncertain", item=0)
    evaluated = e2e.evaluate(formal)
    assert evaluated.items[0].outcome == "fail"
    assert evaluated.items[0].suggested_score is None
    assert evaluated.items[1].requires_human is True
    assert e2e.student_feedback(formal).grade is None

    confirmed = e2e.teacher_confirm_all_with_rubric(formal)
    assert e2e.student_feedback(formal).grade is None
    e2e.publish(confirmed)
    assert e2e.student_feedback(formal).grade.revision_no == 1
    appeal = e2e.appeal(formal, "申请复核定义域与证明步骤")
    run = e2e.create_regrade(formal, appeal.id)
    e2e.confirm_regrade_items(run)
    revised = e2e.teacher_confirm_regrade(run, base_revision_no=1)
    assert e2e.student_feedback(formal).grade.revision_no == 1
    e2e.publish(revised)
    assert e2e.student_feedback(formal).grade.revision_no == 2
    assert e2e.student_feedback(formal).appeal_status == "resolved"


def test_policy_and_exposure_matrix(e2e):
    e2e.assert_formal_immediate_correctness_denied()
    e2e.assert_coursework_feedback_waits_for_release()
    e2e.assert_practice_hints_obey_level()
    e2e.assert_autosave_creates_no_learning_projection()
    e2e.assert_model_gateway_failure_does_not_block_deterministic_evaluation()
    e2e.assert_old_artifact_url_rechecked()
    e2e.assert_practice_solution_conflicting_with_secure_formal_is_blocked()
    e2e.assert_withdrawn_item_requires_explicit_grade_resolution()
```

- [ ] **步骤 2：运行端到端测试验证失败**

运行：`pytest tests/e2e/test_practice_to_grading.py -q`

预期：FAIL；组合根尚未注册 practice/grading 路由，首个练习请求返回 404。

- [ ] **步骤 3：编写最少组合根与 worker 接线**

```python
# apps/api/main.py
from apps.api.routers import grading, practice
app.include_router(practice.router, prefix="/api", tags=["practice"])
app.include_router(grading.router, prefix="/api", tags=["grading"])

# apps/worker/main.py
@celery_app.task(name="assessment.evaluate_submission", autoretry_for=(EvaluationUnavailable,),
                 retry_backoff=True, max_retries=5)
def evaluate_submission(submission_id: str) -> None:
    with worker_session() as session:
        service = build_grading_service(session)
        service.evaluate_submission(UUID(submission_id), worker_actor())
```

`build_grading_service` 注入既有 `SubmissionService`、CAS runner、rubric 只读仓库、outbox 与幂等适配器；worker 身份只有 `evaluate` 权限，没有 `grade/publish` 权限。`deps.py` 为学生/教师请求构造相同领域服务，但 actor 来自请求身份。不得新增保存或交卷端点，不得让 worker 直接访问 `GradeRevision`。

- [ ] **步骤 4：运行端到端与阶段 D 全量测试验证通过**

运行：

```bash
pytest tests/unit/assessment tests/integration/assessment tests/e2e/test_practice_to_grading.py -q
npm --prefix apps/web run test -- src/components/__tests__/TeacherReviewWorkspace.test.tsx src/components/__tests__/StudentFeedbackView.test.tsx
alembic upgrade head
alembic downgrade 20261105_101000_assessment_attempt_due
alembic upgrade head
```

预期：全部 PASS；策略矩阵覆盖正式/作业/练习，CAS fail、学习投影、Artifact 重检、曝光阻断、教师确认、独立发布、申诉、重评、撤回和迁移往返均通过。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/main.py apps/api/deps.py apps/worker/main.py tests/e2e/test_practice_to_grading.py
git commit -m "test: 验收在线练习到教师评分发布完整闭环"
```

## 4. 规格覆盖与计划自检

| 规格/约束 | 对应任务 |
|---|---|
| 第10节 response revision、submission_item、learning projection、grade revision | 任务1、2、4、5 |
| 第11节自动保存与提交边界复用 | 任务2、8、11 |
| 第12节练习三分、提示解析、正式预评、rubric、教师发布、申诉 | 任务2—7 |
| 第14节 Artifact 重检、曝光、学生安全 DTO、评价数据范围 | 任务3、8、10 |
| 第15节检查、成绩、申诉 API 与错误语义 | 任务8 |
| 第16节 CAS 不可用、旧响应、曝光冲突、撤回、重评旧成绩 | 任务2—7、11 |
| 第17节 CAS fail/转人工/发布延迟/申诉审计所需记录 | 任务1、4、6、7、11 |
| 第18节第22—28项及成绩教师确认发布率100% | 任务2、3、5、6、8、11 |
| 数学基础计划的模块化单体、PostgreSQL真源、人工门禁、TDD | 任务1—11 |

- 所有服务签名、DTO 字段、上游方法与 CAS/AI/只读仓库端口均在第2节或对应任务中定义；上游 Attempt/Response/Submission 只调用不重建。
- 每个任务都有独立失败测试、失败命令与预期、最小实现、通过命令和提交步骤。
- 迁移固定为 `20261105_110000_assessment_grading`，直接依赖 `20261105_101000_assessment_attempt_due`；`attempt_policy_version`、`item_exposure_event` 不重复建表。
- `learning.exercise_attempt` 按存在与否分别走创建或两列扩展路径，downgrade 均验证；学习记录不产生 submission 或 grade。
- AI 和 worker 均无正式成绩写入路径；`fail` 与人工项建议分始终为 NULL；练习自动保存与显式检查完全分离。
