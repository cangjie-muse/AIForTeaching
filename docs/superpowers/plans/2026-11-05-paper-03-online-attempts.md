# 固定卷在线作答（阶段 C）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 交付固定 assignment 的在线开始、安全投影、自动保存、断线重连、多标签冲突、服务端截止、幂等交卷、到期收卷、提交回执与撤回，不实现练习逐题反馈或正式评分。

**架构：** PostgreSQL 是 attempt、revision、submission、幂等和 outbox 的唯一真源；FastAPI 只调用应用服务，Next.js 只接收白名单 DTO。保存与交卷使用 attempt 行锁，到期 worker 和手动交卷共用 `SubmissionService.finalize`；最终快照只选择 `server_received_at <= effective_deadline` 的最后修订，submission、submission_item、审计和 outbox 在同一事务落库。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL 16、Celery、Pydantic 2、pytest、Next.js、React、TypeScript、Vitest、Testing Library、Playwright。

---

## 0. 前置边界与全局不变量

### 0.1 复用的上游产物

数学基础计划和固定卷阶段 A/B 已提供身份/范围权限、`PaperVersion/PaperSection/PaperSlot`、Release 终检、canonical JSON＋SHA-256、通用 `ops.idempotency_key/outbox_event/job`、审计和测试数据库。**阶段 A 已创建 `assessment.attempt_policy_version` 与 `assessment.item_exposure_event`，并在 `modules/assessment/models/attempt.py` 提供 `AttemptPolicyVersion`；本计划只解析和校验策略，绝不重复创建这两张表。**

上游接口固定为：

- `ReleaseGate.lock_and_verify_paper(session, paper_version_id, assignment_id)`：与撤回使用同一范围锁，事务内检查 approved、许可、依赖、曝光和当前撤回。
- `IdempotencyRunner.run(session, actor_id, operation, key, request, handler)`：actor＋operation＋key 唯一，同键不同请求哈希抛 `IdempotencyConflict`，重放首次结果。
- `OutboxWriter.emit(session, event_type, aggregate_id, dedup_key, payload)`：dedup_key 唯一，至少一次投递。
- `tests/fixtures/assessment.py` 的 `assessment_bundle`：两个固定槽位、approved paper、draft assignment、policy、教师、学生和秘密答案 fixture。

### 0.2 共享服务接口

在 `modules/assessment/services/ports.py` 原样定义以下 Protocol；所有 `now` 都是调用方注入的 UTC aware datetime。

```python
class AttemptService(Protocol):
    def start(self, session: Session, *, assignment_id: UUID, student_id: UUID,
              idempotency_key: str, now: datetime) -> StudentAttemptDTO: ...
    def get(self, session: Session, *, attempt_id: UUID,
            student_id: UUID, now: datetime) -> StudentAttemptDTO: ...

class ResponseService(Protocol):
    def save(self, session: Session, *, attempt_id: UUID, slot_id: UUID,
             student_id: UUID, client_revision_id: UUID, base_revision_no: int,
             raw_response: dict, client_sent_at: datetime | None,
             idempotency_key: str, now: datetime) -> SaveResponseResult: ...

class SubmissionService(Protocol):
    def finalize(self, session: Session, *, attempt_id: UUID, actor: AttemptActor,
                 reason: SubmissionReason, idempotency_key: str,
                 now: datetime) -> SubmissionReceiptDTO: ...
    def receipt_for_student(self, session: Session, *, submission_id: UUID,
                            student_id: UUID) -> SubmissionReceiptDTO: ...

class DeadlineWorker(Protocol):
    def finalize_due(self, session: Session, *, now: datetime,
                     batch_size: int = 100) -> DeadlineBatchResult: ...

class AssignmentService(Protocol):
    def publish(self, session: Session, *, assignment_id: UUID, teacher_id: UUID,
                base_version: int, idempotency_key: str,
                now: datetime) -> PublishedAssignmentDTO: ...
    def withdraw(self, session: Session, *, assignment_id: UUID, actor: AttemptActor,
                 reason: str, idempotency_key: str, now: datetime) -> None: ...
    def apply_revocation(self, session: Session, *, assignment_id: UUID,
                         reason: str, now: datetime) -> None: ...
```

### 0.3 不可违反的规则

1. 同 assignment＋student 有 `in_progress/finalizing` attempt 时，开始必须返回原 attempt；新幂等键不消耗次数。
2. 保存身份为 `attempt_id＋slot_id＋client_revision_id`；`base_revision_no` 按槽比较，过期返回 409 `response_conflict` 和服务端最新本人响应。
3. 服务端时间唯一权威；快照资格是 **`server_received_at <= effective_deadline`**。只读宽限不延长新答案写入。
4. finalize 先锁 attempt；已有 submission 返回同一回执。提交后的新保存不得改变快照、manifest 或评阅输入。
5. 学生 DTO 白名单不含 `answer/solution/rubric/answer_test`，不序列化 exercise ORM 全对象。
6. 撤回只冻结和留证，不删除 attempt、revision、submission；本人历史回执仍可读。
7. 不添加 `/checks`、正误、提示、解析、answer_test/CAS/LLM 调用或 `assessment.grade` 写入；`submission_item.evaluation_status='pending'`。

## 1. 文件结构

```text
AIForTeaching/
├─ db/migrations/versions/
│  ├─ 20261105_100000_assessment_attempts.py       # attempt/revision/快照/约束/RLS
│  └─ 20261105_101000_assessment_attempt_due.py    # 到期扫描索引
├─ modules/assessment/
│  ├─ contracts.py                                # DTO、错误、纯时间策略
│  ├─ models/attempt.py                           # 在01策略模型旁添加attempt/revision
│  ├─ models/assignment.py                        # assignment固定项扩展
│  ├─ models/submission.py                        # submission快照
│  ├─ projection.py                               # 学生白名单读取
│  ├─ input_normalization.py                      # 结构校验，不判断正误
│  ├─ observability.py                            # 审计白名单与低基数指标
│  └─ services/{ports,assignments,attempts,responses,submissions,deadlines}.py
├─ apps/api/routers/attempts.py                    # assignment/attempt/submission API
├─ apps/worker/tasks/assessment.py                 # 到期扫描入口
├─ apps/web/
│  ├─ app/attempts/[attemptId]/page.tsx
│  ├─ components/assessment/{AttemptRunner,SaveStatusIndicator}.tsx
│  ├─ components/assessment/*.test.tsx
│  ├─ hooks/{useAttemptSaver.ts,useAttemptSaver.test.tsx}
│  ├─ lib/assessment/attempts.ts
│  └─ tests/e2e/online-attempt.spec.ts
├─ packages/contracts/assessment-online-attempts.v1.json
└─ tests/
   ├─ unit/assessment/test_deadline_policy.py
   ├─ integration/assessment/test_{migrations,projection,start,save,finalize,worker,api,security,concurrency}.py
   └─ e2e/seed_online_attempt.py
```

## 2. 实施任务

### 任务 1：迁移链、在线作答表与数据库约束

**文件：**
- 创建：`db/migrations/versions/20261105_100000_assessment_attempts.py`
- 修改：`modules/assessment/models/attempt.py`
- 创建：`modules/assessment/models/assignment.py`
- 创建：`modules/assessment/models/submission.py`
- 创建：`tests/integration/assessment/test_migrations.py`
- 修改：`tests/fixtures/assessment.py`

- [ ] **步骤 1：编写失败的测试**

```python
import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from modules.assessment.models.attempt import AttemptPolicyVersion, PaperAttempt

def test_upgrade_downgrade_and_one_active_attempt(alembic_config, session, assessment_bundle, fixed_now):
    command.upgrade(alembic_config, "20261105_100000_assessment_attempts")
    assert "paper_attempt" in inspect(session.bind).get_table_names(schema="assessment")
    policy = session.get(AttemptPolicyVersion, assessment_bundle.policy_id)
    first = PaperAttempt.new(assessment_bundle, policy, attempt_no=1, now=fixed_now)
    session.add(first)
    session.flush()
    session.add(PaperAttempt.new(assessment_bundle, policy, attempt_no=2, now=fixed_now))
    with pytest.raises(IntegrityError, match="uq_paper_attempt_one_active"):
        session.flush()
    session.rollback()
    command.downgrade(alembic_config, "20261105_090000_assessment_assembly")
    assert "paper_attempt" not in inspect(session.bind).get_table_names(schema="assessment")
    assert "attempt_policy_version" in inspect(session.bind).get_table_names(schema="assessment")
    command.upgrade(alembic_config, "20261105_100000_assessment_attempts")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_migrations.py -q`

预期：FAIL，Alembic 找不到 `20261105_100000_assessment_attempts`，且 `PaperAttempt` 不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
revision = "20261105_100000_assessment_attempts"
down_revision = "20261105_090000_assessment_assembly"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
      CREATE TABLE assessment.paper_attempt (
        id uuid PRIMARY KEY, assignment_id uuid NOT NULL REFERENCES assessment.assignment(id),
        paper_version_id uuid NOT NULL REFERENCES assessment.paper_version(id),
        student_id uuid NOT NULL REFERENCES iam.user_account(id),
        attempt_policy_version_id uuid NOT NULL REFERENCES assessment.attempt_policy_version(id),
        attempt_no integer NOT NULL, mode text NOT NULL, status text NOT NULL,
        started_at timestamptz NOT NULL, server_deadline_at timestamptz NOT NULL,
        worker_due_at timestamptz NOT NULL, last_server_revision integer NOT NULL DEFAULT 0,
        response_manifest_hash text, submitted_at timestamptz, close_reason text,
        created_at timestamptz NOT NULL
      );
      CREATE UNIQUE INDEX uq_paper_attempt_one_active
        ON assessment.paper_attempt(assignment_id, student_id)
        WHERE status IN ('in_progress', 'finalizing');
      CREATE UNIQUE INDEX uq_paper_attempt_no
        ON assessment.paper_attempt(assignment_id, student_id, attempt_no);
    """)

def downgrade():
    op.execute("DROP TABLE assessment.paper_attempt CASCADE")
```

完整迁移再创建 `paper_attempt_item`、`response_revision`，扩展 assignment/assignment_item/submission/submission_item。必须将阶段 A `assignment.distribution_kind` 的 CHECK 从仅 `print` 扩展为 `print / online / both`，并包含 `(attempt_id,client_revision_id)`、`(attempt_id,paper_slot_id,revision_no)`、`submission.attempt_id`、`(submission_id,assignment_item_id)` 唯一约束，assignment 时间 `visible_at<=due_at<=hard_close_at`，以及正 revision CHECK。submission 保存 item/unanswered/invalid 计数和 receipt_hash；submission_item 保存 raw/normalized response、parse_status、rubric_version_id、answer_test_run_id、evaluation_status、suggested_score、requires_human。

`downgrade` 按依赖逆序只删除本迁移对象，不删除阶段 A 的 policy/exposure 表；恢复 distribution CHECK 前若仍存在 online/both assignment 或任何 attempt/submission，必须阻断降级，不能静默改成 print。attempt_item 和已发布 assignment_item 禁止 UPDATE/DELETE；attempt/response/submission 启用 FORCE RLS，非 owner 学生仅能访问本人行。测试还需验证空库 upgrade 到 head、无在线数据时逐级 downgrade 到 assembly、再升级均成功，以及有在线数据时 downgrade 明确失败。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_migrations.py -q`

预期：PASS；迁移链、唯一约束、不可变触发器、upgrade/downgrade 和非 superuser RLS 负向用例全部通过。

- [ ] **步骤 5：Commit**

```bash
git add db/migrations/versions/20261105_100000_assessment_attempts.py modules/assessment/models tests/fixtures/assessment.py tests/integration/assessment/test_migrations.py
git commit -m "feat: add assessment attempt persistence"
```

### 任务 2：共享 DTO、策略解析与学生安全重连投影

**文件：**
- 修改：`modules/assessment/contracts.py`
- 创建：`modules/assessment/services/ports.py`
- 创建：`modules/assessment/projection.py`
- 修改：`modules/assessment/services/attempts.py`
- 创建：`packages/contracts/assessment-online-attempts.v1.json`
- 创建：`tests/unit/assessment/test_deadline_policy.py`
- 创建：`tests/integration/assessment/test_projection.py`

- [ ] **步骤 1：编写失败的测试**

```python
import json
from datetime import datetime, timedelta, timezone
from modules.assessment.contracts import AssignmentWindow, LatePolicy, PolicyTiming, compute_deadline
from modules.assessment.services.attempts import AttemptService

def test_closed_deadline_and_read_only_grace():
    start = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)
    due = start + timedelta(hours=2)
    result = compute_deadline(start, AssignmentWindow(start, due, due + timedelta(minutes=20)),
        PolicyTiming(False, None, LatePolicy.READ_ONLY_GRACE, 600))
    assert result.effective_deadline == due
    assert result.worker_due_at == due + timedelta(minutes=10)
    assert result.accepts_revision_at(due)
    assert not result.accepts_revision_at(due + timedelta(microseconds=1))

def test_reconnect_returns_latest_without_secret_fields(session, started_attempt, fixed_now):
    dto = AttemptService().get(session, attempt_id=started_attempt.attempt_id,
        student_id=started_attempt.student_id, now=fixed_now)
    assert dto.slots[0].revision_no == 2
    assert dto.slots[0].raw_response == {"text": "第二版"}
    encoded = json.dumps(dto.model_dump(mode="json"), ensure_ascii=False)
    assert started_attempt.secret_answer not in encoded
    for forbidden in ("answer", "solution", "rubric", "answer_test"):
        assert f'"{forbidden}":' not in encoded
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/unit/assessment/test_deadline_policy.py tests/integration/assessment/test_projection.py -q`

预期：FAIL，`contracts.py`、`projection.py` 和 `AttemptService.get` 不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
@dataclass(frozen=True)
class DeadlineWindow:
    effective_deadline: datetime
    worker_due_at: datetime
    def accepts_revision_at(self, received_at: datetime) -> bool:
        return received_at <= self.effective_deadline

def compute_deadline(started_at, assignment, policy):
    deadline = min(assignment.due_at, assignment.hard_close_at)
    if policy.time_limited:
        if not policy.duration_seconds or policy.duration_seconds <= 0:
            raise ValueError("timed policy requires positive duration")
        deadline = min(deadline, started_at + timedelta(seconds=policy.duration_seconds))
    grace = policy.grace_seconds if policy.late_policy is LatePolicy.READ_ONLY_GRACE else 0
    return DeadlineWindow(deadline, min(deadline + timedelta(seconds=grace), assignment.hard_close_at))
```

定义第 0.2 节 Protocol，以及 `StudentSlotDTO(slot_id,number,stem,public_assets,input_kind,raw_response,parse_status,revision_no)`、`StudentAttemptDTO(attempt_id,assignment_id,paper_version_id,status,assignment_status,attempt_no,started_at,server_now,server_deadline_at,worker_due_at,save_debounce_ms,max_save_interval_ms,late_policy,slots,submission_receipt)`、`SaveResponseResult(attempt_id,slot_id,client_revision_id,revision_no,server_received_at,raw_response,parse_status)`、`SubmissionReceiptDTO(submission_id,attempt_id,finalized_at,item_count,unanswered_count,invalid_count,response_manifest_hash,receipt_hash,submission_reason)`、`PublishedAssignmentDTO(assignment_id,status,version_no,item_ids)`、`DeadlineBatchResult(finalized_ids,closed_ids,replayed_ids)`、`AttemptActor(student_id,is_worker)` 和 `PolicyDenied/ResourceNotFound/ResponseConflict/AttemptFinalized/Withdrawn/AttemptLimitExceeded`。JSON Schema 禁止 additionalProperties。

`validate_policy_version(policy: AttemptPolicyVersion) -> None` 在 start 前执行：max_attempts 为 NULL 仅允许 practice，非 NULL 必须大于等于 1；duration_seconds 在 time_limited 时必须为正；grace_seconds 仅在 read_only_grace 可大于 0；保存参数必须满足 `0 < save_debounce_ms <= max_save_interval_ms`。校验失败抛 PolicyDenied("invalid_attempt_policy")，不会修改阶段 A 的策略行。

`StudentProjectionRepository.latest_slots` 从 `paper_attempt_item` 显式 SELECT slot_id、slot_no、已清理 `student_stem`、安全 public_assets、input_kind，并用 LATERAL 取每槽最新 revision；禁止 SELECT exercise ORM 全对象。`AttemptService.get` 先校验 owner，再组装 DTO；非 owner 抛 ResourceNotFound。活动 attempt 已撤回时抛 Withdrawn；已提交 attempt 即使 assignment 撤回也返回 `submission_receipt` 和空 slots，不回传题干。进行中的重连返回服务端最新 revision，不在服务端合并浏览器草稿。`SaveResponseResult.empty` 用 revision_no=0、raw_response=None 表示尚无任何服务端修订，仅供初始 base 冲突使用。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/unit/assessment/test_deadline_policy.py tests/integration/assessment/test_projection.py -q`

预期：PASS；限时、due、hard_close、截止等号、微秒后拒绝、只读宽限、最新 revision、越权 404 和敏感值扫描通过。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/contracts.py modules/assessment/services/ports.py modules/assessment/projection.py modules/assessment/services/attempts.py packages/contracts/assessment-online-attempts.v1.json tests/unit/assessment/test_deadline_policy.py tests/integration/assessment/test_projection.py
git commit -m "feat: define attempt policy and safe student projection"
```

### 任务 3：AssignmentService.publish 与重复开始固定 attempt

**文件：**
- 创建：`modules/assessment/services/assignments.py`
- 修改：`modules/assessment/services/attempts.py`
- 创建：`tests/integration/assessment/test_start.py`

- [ ] **步骤 1：编写失败的测试**

```python
import pytest
from modules.assessment.contracts import AttemptLimitExceeded
from modules.assessment.models.attempt import PaperAttempt
from modules.assessment.models.assignment import AssignmentItem
from modules.assessment.services.assignments import AssignmentService
from modules.assessment.services.attempts import AttemptService

def test_publish_then_new_keys_reuse_active_attempt(session, assessment_bundle, teacher_id, fixed_now):
    published = AssignmentService().publish(session, assignment_id=assessment_bundle.assignment_id,
        teacher_id=teacher_id, base_version=1, idempotency_key="publish-1", now=fixed_now)
    assert len(published.item_ids) == 2
    first = AttemptService().start(session, assignment_id=assessment_bundle.assignment_id,
        student_id=assessment_bundle.student_id, idempotency_key="start-a", now=fixed_now)
    second = AttemptService().start(session, assignment_id=assessment_bundle.assignment_id,
        student_id=assessment_bundle.student_id, idempotency_key="start-b", now=fixed_now)
    assert second.attempt_id == first.attempt_id
    assert session.query(PaperAttempt).filter_by(assignment_id=assessment_bundle.assignment_id).count() == 1
    assert session.query(AssignmentItem).filter_by(assignment_id=assessment_bundle.assignment_id).count() == 2
    session.get(PaperAttempt, first.attempt_id).status = "closed_unsubmitted"
    session.flush()
    with pytest.raises(AttemptLimitExceeded):
        AttemptService().start(session, assignment_id=assessment_bundle.assignment_id,
            student_id=assessment_bundle.student_id, idempotency_key="start-c", now=fixed_now)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_start.py -q`

预期：FAIL，publish/start 方法不存在。

- [ ] **步骤 3：编写最少实现代码**

publish 在 assignment `FOR UPDATE` 锁内验证教师、base_version、时间，再调用 `ReleaseGate.lock_and_verify_paper`，按 section/slot 顺序一次性插入 assignment_item；重复 publish 读取并返回原 item，绝不删除重建。item 固定 release_context、source_release、exercise_version、exercise_instance、rubric、安全资产和 input_kind。start 改用 assignment `FOR SHARE`，允许不同学生并行开始，同时与 publish/withdraw 的 `FOR UPDATE` 互斥。

start 的核心顺序必须如下，活动 attempt 检查位于开放时间和次数之前：

```python
assignment = session.scalar(select(Assignment).where(Assignment.id == assignment_id).with_for_share())
policy_service.require_assigned_student(session, student_id, assignment.id)
if assignment.status == "withdrawn":
    raise Withdrawn(str(assignment.id))
active = session.scalar(select(PaperAttempt).where(
    PaperAttempt.assignment_id == assignment.id,
    PaperAttempt.student_id == student_id,
    PaperAttempt.status.in_(["in_progress", "finalizing"])).with_for_update())
if active:
    return self.get(session, attempt_id=active.id, student_id=student_id, now=now)
if assignment.status != "published" or not assignment.visible_at <= now <= assignment.hard_close_at:
    raise PolicyDenied("assignment_not_open")
used = session.scalar(select(func.count(PaperAttempt.id)).where(
    PaperAttempt.assignment_id == assignment.id, PaperAttempt.student_id == student_id))
max_attempts = assignment.attempt_policy_version.max_attempts
if max_attempts is not None and used >= max_attempts:
    raise AttemptLimitExceeded(str(assignment.id))
attempt = PaperAttempt.create_with_fixed_items(assignment, student_id, used + 1, now)
session.add(attempt)
session.flush()
return self.get(session, attempt_id=attempt.id, student_id=student_id, now=now)
```

`create_with_fixed_items` 使用任务 2 的 `compute_deadline` 并复制 assignment_item 到 attempt_item。公开 publish/start 均经 IdempotencyRunner；paper 必须 approved 且无当前撤回。并行开始由任务 9 的并发测试继续加压。

`modules/assessment/models/attempt.py` 的固定接口为 `PaperAttempt.create_with_fixed_items(assignment: Assignment, student_id: UUID, attempt_no: int, now: datetime) -> PaperAttempt`。任务 1 测试使用的 `PaperAttempt.new(bundle, policy, attempt_no, now)` 是同文件中的简单构造器，只设置 fixture 给定 ID、policy、attempt_no、mode=coursework、status=in_progress、三个时间等于 now；不得把它用于生产开始流程。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_start.py -q`

预期：PASS；同键/新键恢复、次数耗尽、未开放、未分配、撤回、重复 publish、新 Release 不改变固定项均有断言。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/assignments.py modules/assessment/services/attempts.py tests/integration/assessment/test_start.py
git commit -m "feat: publish and start fixed assessment attempts"
```

### 任务 4：幂等自动保存、base_revision 冲突与非法输入留证

**文件：**
- 创建：`modules/assessment/input_normalization.py`
- 创建：`modules/assessment/services/responses.py`
- 创建：`tests/integration/assessment/test_save.py`

- [ ] **步骤 1：编写失败的测试**

```python
from datetime import timedelta
from uuid import uuid4
import pytest
from modules.assessment.contracts import AttemptFinalized, ResponseConflict
from modules.assessment.services.responses import ResponseService

def test_save_retry_stale_base_and_deadline(session, started_attempt, fixed_now):
    slot_id = started_attempt.slots[0].slot_id
    client_id = uuid4()
    command = dict(attempt_id=started_attempt.attempt_id, slot_id=slot_id,
        student_id=started_attempt.student_id, client_revision_id=client_id,
        base_revision_no=0, raw_response={"text": "x+1"}, client_sent_at=None,
        idempotency_key=str(client_id), now=started_attempt.server_deadline_at)
    first = ResponseService().save(session, **command)
    assert ResponseService().save(session, **command).revision_no == first.revision_no == 1
    with pytest.raises(ResponseConflict) as error:
        ResponseService().save(session, **{**command, "client_revision_id": uuid4(),
            "idempotency_key": "stale-tab", "raw_response": {"text": "旧标签"}})
    assert error.value.server_result.revision_no == 1
    with pytest.raises(AttemptFinalized):
        ResponseService().save(session, **{**command, "client_revision_id": uuid4(),
            "idempotency_key": "late", "base_revision_no": 1,
            "now": started_attempt.server_deadline_at + timedelta(microseconds=1)})
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_save.py -q`

预期：FAIL，`ResponseService.save` 不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
attempt = session.scalar(select(PaperAttempt).where(
    PaperAttempt.id == attempt_id, PaperAttempt.student_id == student_id).with_for_update())
if attempt is None:
    raise ResourceNotFound(str(attempt_id))
previous = session.scalar(select(ResponseRevision).where(
    ResponseRevision.attempt_id == attempt.id,
    ResponseRevision.client_revision_id == client_revision_id))
if previous:
    if previous.paper_slot_id != slot_id or previous.raw_response != raw_response:
        raise IdempotencyConflict(str(client_revision_id))
    return previous.to_save_result()
if attempt.assignment.status == "withdrawn" or attempt.status == "withdrawn":
    raise Withdrawn(str(attempt.id))
if attempt.status != "in_progress" or now > attempt.server_deadline_at:
    raise AttemptFinalized(str(attempt.id))
latest = latest_slot_revision(session, attempt.id, slot_id)
current = latest.revision_no if latest else 0
if current != base_revision_no:
    raise ResponseConflict(latest.to_save_result() if latest else SaveResponseResult.empty(attempt.id, slot_id))
normalized, parse_status = ResponseNormalizer.normalize(raw_response)
revision = ResponseRevision.create(attempt.id, slot_id, client_revision_id,
    base_revision_no, current + 1, raw_response, normalized, parse_status,
    client_sent_at, now)
session.add(revision)
session.flush()
return revision.to_save_result()
```

保存前确认 slot 属于 attempt_item；`ResponseNormalizer` 仅做 NFC、JSON 结构和括号配对，保留 raw_response，返回 valid/invalid/unsupported，绝不判断数学正误或调用 CAS/LLM。response_hash 覆盖 raw＋normalized；`last_server_revision` 递增，相同 client_revision_id 不新增行。公开 save 同时受通用 Idempotency-Key 保护。

`latest_slot_revision(session, attempt_id: UUID, slot_id: UUID) -> ResponseRevision | None` 是 responses.py 内按 revision_no DESC LIMIT 1 的共享查询；`ResponseRevision.create` 接收代码中的全部参数并生成 UUID、server_received_at 和 response_hash。测试中的 `ResponseRevision.fixture` 在 `tests/fixtures/assessment.py` 定义为带默认 UUID/client_revision_id/base_revision_no/parse_status/response_hash 的测试工厂，任务 1 将该工厂加入 fixture 文件。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_save.py -q`

预期：PASS；重试只有一行、多标签 409、截止等号成功、之后失败、非法表达式原文与 parse_status 保留、伪造 slot 返回 404。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/input_normalization.py modules/assessment/services/responses.py tests/integration/assessment/test_save.py
git commit -m "feat: save idempotent response revisions"
```

### 任务 5：幂等交卷、不可变 submission_item 与提交回执

**文件：**
- 创建：`modules/assessment/services/submissions.py`
- 创建：`tests/integration/assessment/test_finalize.py`

- [ ] **步骤 1：编写失败的测试**

```python
from datetime import timedelta
from modules.assessment.models.attempt import PaperAttempt, ResponseRevision
from modules.assessment.models.submission import Submission
from modules.assessment.contracts import SubmissionReason
from modules.assessment.services.submissions import SubmissionService

def test_finalize_is_idempotent_and_excludes_late_revision(session, started_attempt, student_actor, fixed_now):
    attempt = session.get(PaperAttempt, started_attempt.attempt_id)
    session.add(ResponseRevision.fixture(attempt_id=attempt.id,
        paper_slot_id=started_attempt.slots[0].slot_id, revision_no=2,
        raw_response={"text": "截止后"},
        received_at=attempt.server_deadline_at + timedelta(microseconds=1)))
    session.flush()
    first = SubmissionService().finalize(session, attempt_id=attempt.id, actor=student_actor,
        reason=SubmissionReason.STUDENT, idempotency_key="submit-a", now=fixed_now)
    second = SubmissionService().finalize(session, attempt_id=attempt.id, actor=student_actor,
        reason=SubmissionReason.DEADLINE, idempotency_key="submit-b", now=fixed_now)
    assert second.submission_id == first.submission_id
    assert second.receipt_hash == first.receipt_hash
    assert session.query(Submission).filter_by(attempt_id=attempt.id).count() == 1
    assert first.item_count == 2
    assert first.response_manifest_hash == attempt.response_manifest_hash
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_finalize.py -q`

预期：FAIL，`SubmissionService.finalize` 不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
attempt = session.scalar(select(PaperAttempt).where(PaperAttempt.id == attempt_id).with_for_update())
if attempt is None or (not actor.is_worker and attempt.student_id != actor.student_id):
    raise ResourceNotFound(str(attempt_id))
existing = session.scalar(select(Submission).where(Submission.attempt_id == attempt.id))
if existing:
    return SubmissionService.receipt(existing)
if attempt.status == "withdrawn" or attempt.assignment.status == "withdrawn":
    raise Withdrawn(str(attempt.id))
if attempt.attempt_policy_version.late_policy == "reject_late" and now > attempt.server_deadline_at:
    raise AttemptFinalized(str(attempt.id))
attempt.status = "finalizing"
rows = session.execute(text("""
  SELECT DISTINCT ON (paper_slot_id) * FROM assessment.response_revision
  WHERE attempt_id=:attempt_id AND server_received_at<=:effective_deadline
  ORDER BY paper_slot_id, revision_no DESC
"""), {"attempt_id": attempt.id, "effective_deadline": attempt.server_deadline_at}).mappings().all()
submission = Submission.create_snapshot(attempt, reason, now, rows)
session.add(submission)
attempt.status = "submitted"
attempt.submitted_at = now
attempt.response_manifest_hash = submission.response_manifest_hash
outbox.emit(session, "assessment.submission.finalized.v1", str(submission.id),
    f"submission:{submission.id}:finalized", {"submission_id": str(submission.id),
    "attempt_id": str(attempt.id), "response_manifest_hash": submission.response_manifest_hash})
session.flush()
return SubmissionService.receipt(submission)
```

`create_snapshot` 为每个 attempt_item 都创建一项，左连接不到 revision 也保留未答；每行固定 assignment_item、response_revision、rubric、raw/normalized response 和 parse_status，answer_test_run_id/suggested_score 为 NULL、evaluation_status=pending；`requires_human` 按固定题型和评阅策略计算，证明、开放、未知题型必须为 True，客观可解析题才允许为 False，禁止一律写 False。manifest 按 slot UUID 排序并只含 assignment_item_id、response_revision_id、response_hash。回执含 submission_id、attempt_id、服务端 finalized_at、item/unanswered/invalid 计数、manifest_hash、reason 和 receipt_hash。

`Submission.create_snapshot(attempt: PaperAttempt, reason: SubmissionReason, now: datetime, eligible_rows: list[RowMapping]) -> Submission` 是唯一快照构造入口；`SubmissionService.receipt(submission: Submission) -> SubmissionReceiptDTO` 从持久化计数和哈希构造回执，公开读取走第 0.2 节 `receipt_for_student`。

reject_late 由任务 6 worker 持久化 closed_unsubmitted；手动逾期只拒绝，不在异常中提交状态变更。提交后重复旧保存可重放原 client_revision_id，新保存拒绝，manifest 不变。finalize 不执行或等待评分。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_finalize.py -q`

预期：PASS；微秒边界、空卷、invalid、重复提交、唯一 outbox、提交后保存和回执哈希重算全部通过。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/submissions.py tests/integration/assessment/test_finalize.py
git commit -m "feat: finalize immutable submissions and receipts"
```

### 任务 6：到期 worker、手动竞态与撤回流

**文件：**
- 创建：`modules/assessment/services/deadlines.py`
- 创建：`apps/worker/tasks/assessment.py`
- 修改：`modules/assessment/services/assignments.py`
- 创建：`tests/integration/assessment/test_worker.py`

- [ ] **步骤 1：编写失败的测试**

```python
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from modules.assessment.models.submission import Submission
from modules.assessment.contracts import SubmissionReason
from modules.assessment.services.deadlines import DeadlineWorker
from modules.assessment.services.submissions import SubmissionService

def test_manual_and_due_worker_make_one_submission(session_factory, started_attempt, student_actor):
    barrier = Barrier(2)
    def manual():
        with session_factory() as session:
            barrier.wait()
            return SubmissionService().finalize(session, attempt_id=started_attempt.attempt_id,
                actor=student_actor, reason=SubmissionReason.STUDENT,
                idempotency_key="manual-race", now=started_attempt.worker_due_at)
    def automatic():
        with session_factory() as session:
            barrier.wait()
            DeadlineWorker().finalize_due(session, now=started_attempt.worker_due_at)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(manual), pool.submit(automatic)]
        receipt = futures[0].result()
        futures[1].result()
    with session_factory() as session:
        assert session.query(Submission).filter_by(attempt_id=started_attempt.attempt_id).count() == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_worker.py -q`

预期：FAIL，`DeadlineWorker.finalize_due` 不存在。

- [ ] **步骤 3：编写最少实现代码**

```python
due_ids = session.scalars(select(PaperAttempt.id).where(
    PaperAttempt.status == "in_progress", PaperAttempt.worker_due_at <= now
).order_by(PaperAttempt.worker_due_at, PaperAttempt.id).limit(batch_size)
 .with_for_update(skip_locked=True)).all()
for attempt_id in due_ids:
    attempt = session.get(PaperAttempt, attempt_id, with_for_update=True)
    if attempt.attempt_policy_version.late_policy == "reject_late":
        attempt.status = "closed_unsubmitted"
        attempt.close_reason = "deadline_rejected"
        closed.append(str(attempt.id))
        continue
    already = session.scalar(select(Submission.id).where(Submission.attempt_id == attempt.id))
    receipt = submissions.finalize(session, attempt_id=attempt.id,
        actor=AttemptActor(student_id=None, is_worker=True), reason=SubmissionReason.DEADLINE,
        idempotency_key=f"deadline:{attempt.id}:{attempt.worker_due_at.isoformat()}", now=now)
    (replayed if already else finalized).append(receipt.submission_id)
```

Celery 每 5 秒在独立事务调用 `finalize_due(batch_size=100)`，失败回滚后重试，不调用模型。auto_submit 到期收空卷；read_only_grace 到 worker_due_at 才收且保存仍止於 server_deadline_at；reject_late 无 submission。

`AssignmentService.withdraw` 锁 assignment 和活动 attempt，将 assignment 置 withdrawn、活动 attempt 置 withdrawn/close_reason=assignment_withdrawn，已提交 attempt 不变；同一事务写审计和 `assessment.assignment.withdrawn.v1` outbox。`apply_revocation` 供治理严重撤回事件复用同一内部方法。start/save/finalize 对 withdrawn 抛 Withdrawn；活动 attempt 的 get 同样拒绝，已提交 attempt 的 get 与 `receipt_for_student` 仍按 owner 返回历史回执。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_worker.py -q`

预期：PASS；手动/worker 仅一个 submission/outbox，三种到期策略、worker 崩溃重试、撤回冻结、撤回后历史回执和证据保留通过。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/services/deadlines.py modules/assessment/services/assignments.py modules/assessment/services/submissions.py apps/worker/tasks/assessment.py tests/integration/assessment/test_worker.py
git commit -m "feat: close due and withdrawn attempts safely"
```

### 任务 7：FastAPI 契约与统一错误语义

**文件：**
- 创建：`apps/api/routers/attempts.py`
- 修改：`apps/api/main.py`
- 创建：`tests/integration/assessment/test_api.py`

- [ ] **步骤 1：编写失败的测试**

```python
from uuid import uuid4

def test_start_save_conflict_submit_and_receipt(api_client, published_assignment):
    started = api_client.post(f"/api/v1/assignments/{published_assignment.id}/attempts",
        headers={"Idempotency-Key": "api-start-key"})
    assert started.status_code == 200
    attempt = started.json()
    slot_id = attempt["slots"][0]["slot_id"]
    revision_id = str(uuid4())
    saved = api_client.put(f"/api/v1/attempts/{attempt['attempt_id']}/responses/{slot_id}",
        headers={"Idempotency-Key": revision_id}, json={"client_revision_id": revision_id,
        "base_revision_no": 0, "raw_response": {"text": "x+1"}, "client_sent_at": None})
    assert saved.status_code == 200
    stale = api_client.put(f"/api/v1/attempts/{attempt['attempt_id']}/responses/{slot_id}",
        headers={"Idempotency-Key": "stale-api-key"}, json={"client_revision_id": str(uuid4()),
        "base_revision_no": 0, "raw_response": {"text": "旧"}, "client_sent_at": None})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "response_conflict"
    assert stale.json()["error"]["details"]["server_result"]["revision_no"] == 1
    submitted = api_client.post(f"/api/v1/attempts/{attempt['attempt_id']}/submit",
        headers={"Idempotency-Key": "api-submit-key"})
    assert submitted.status_code == 200
    assert api_client.get(f"/api/v1/submissions/{submitted.json()['submission_id']}").status_code == 200
    assert api_client.post(f"/api/v1/attempts/{attempt['attempt_id']}/checks/{slot_id}").status_code == 404
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_api.py -q`

预期：FAIL，业务路由返回 404。

- [ ] **步骤 3：编写最少实现代码**

```python
@router.post("/api/v1/assignments/{assignment_id}/attempts", response_model=StudentAttemptDTO)
def start(assignment_id: UUID, request: Request,
          key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
          session: Session = Depends(get_session), actor: Actor = Depends(get_current_actor)):
    return AttemptService().start(session, assignment_id=assignment_id, student_id=actor.user_id,
        idempotency_key=key, now=request.state.server_now)

@router.put("/api/v1/attempts/{attempt_id}/responses/{slot_id}", response_model=SaveResponseResult)
def save(attempt_id: UUID, slot_id: UUID, body: SaveResponseRequest, request: Request,
         key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
         session: Session = Depends(get_session), actor: Actor = Depends(get_current_actor)):
    if key != str(body.client_revision_id):
        raise RequestValidationError("Idempotency-Key must equal client_revision_id")
    return ResponseService().save(session, attempt_id=attempt_id, slot_id=slot_id,
        student_id=actor.user_id, client_revision_id=body.client_revision_id,
        base_revision_no=body.base_revision_no, raw_response=body.raw_response,
        client_sent_at=body.client_sent_at, idempotency_key=key, now=request.state.server_now)
```

同文件实现 GET attempt、POST submit、GET submission、POST assignment publish/withdraw。错误映射固定为 ResourceNotFound→404、PolicyDenied→403、ResponseConflict/StaleVersion/AttemptFinalized→409、Withdrawn→410、结构输入→422；409 conflict 在统一错误结构的 `error.details.server_result` 携带服务端结果。所有写接口要求 Idempotency-Key，并调用共享 `parse_idempotency_key` 校验字符与长度；publish 另要求 base_version，withdraw 要求非空 reason。OpenAPI 与任务 2 JSON Schema 一致，错误堆栈不含题干或答案。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_api.py -q`

预期：PASS；缺幂等键、同键不同 body、越权 ID、教师范围、撤回 410 和无 checks 路由均通过。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/routers/attempts.py apps/api/main.py tests/integration/assessment/test_api.py
git commit -m "feat: expose online attempt APIs"
```

### 任务 8：Next.js 作答台、自动保存与可访问冲突界面

**文件：**
- 创建：`apps/web/lib/assessment/attempts.ts`
- 创建：`apps/web/hooks/useAttemptSaver.ts`
- 创建：`apps/web/hooks/useAttemptSaver.test.tsx`
- 创建：`apps/web/components/assessment/AttemptRunner.tsx`
- 创建：`apps/web/components/assessment/SaveStatusIndicator.tsx`
- 创建：`apps/web/components/assessment/AttemptRunner.test.tsx`

- [ ] **步骤 1：编写失败的测试**

```tsx
import { act, renderHook, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { useAttemptSaver } from "./useAttemptSaver";
import { AttemptRunner } from "../components/assessment/AttemptRunner";

it("持续输入按最大间隔保存且409不清空本地稿", async () => {
  vi.useFakeTimers();
  const client = fakeClientWithConflict();
  const { result } = renderHook(() => useAttemptSaver({ attempt: baseAttempt, client }));
  act(() => result.current.edit(baseAttempt.slots[0].slot_id, "本地未同步"));
  await act(async () => vi.advanceTimersByTimeAsync(baseAttempt.max_save_interval_ms));
  expect(result.current.status).toBe("conflict");
  expect(result.current.drafts[baseAttempt.slots[0].slot_id].text).toBe("本地未同步");
  expect(result.current.conflict?.server.raw_response.text).toBe("另一标签");
});

it("冲突必须显式解决并显示提交摘要", async () => {
  render(<AttemptRunner attempt={conflictAttempt} client={mockClient} />);
  await userEvent.click(screen.getByRole("button", { name: "保留本地内容并重试" }));
  expect(screen.getByDisplayValue("本地未同步内容")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "提交试卷" }));
  expect(screen.getByText("未答 1 题，无效输入 1 题")).toBeVisible();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test -- hooks/useAttemptSaver.test.tsx components/assessment/AttemptRunner.test.tsx`

预期：FAIL，hook 和组件不存在。

- [ ] **步骤 3：编写最少实现代码**

`useAttemptSaver` 暴露 `edit/flush/reconnect/useServer/keepLocal/submit` 和每槽 `SlotDraft{text,baseRevisionNo,clientRevisionId,dirty}`。edit 后 debounce 生效，首次 dirty 后 maxSaveIntervalMs 即使持续输入也 flush；成功才更新 base_revision 和 dirty。网络/5xx 按 250/500/1000/2000ms 最多重试四次，offline 保留内存稿，online 后 reconnect。409 同时保存 local/server，不自动覆盖；keepLocal 换新 client_revision_id 并以服务器 revision 为 base 重试。禁止 localStorage、sessionStorage、Service Worker 缓存，beforeunload 只提醒。

```tsx
const labels: Record<SaveStatus, string> = {
  saving: "正在保存", saved: "已保存至服务器", offline: "离线未同步",
  conflict: "保存冲突，请选择保留哪个版本", closed: "已关闭，不能继续保存",
};
export function SaveStatusIndicator({ status }: { status: SaveStatus }) {
  return <p role="status" aria-live="polite">{labels[status]}</p>;
}
```

AttemptRunner 用 label/fieldset 按题号渲染，只读 MathML 和带 alt 的公开资产；冲突按钮为“载入服务器版本”“保留本地内容并重试”。BroadcastChannel 通知另一标签 reconnect，本槽 dirty 时只进入 conflict。提交前 flush，dirty/offline/conflict 时列出未同步槽并禁用确认；摘要显示未答和 invalid，空卷可交。成功后替换为 submission_id、finalized_at、计数和 `data-testid="receipt-hash"`。重连 DTO 已含 submission_receipt 时直接显示回执；assignment_status=withdrawn 时同时显示“作业已撤回，历史回执仍可查看”。倒计时只展示，由 server_now 偏移计算，到期 GET 服务端状态。

- [ ] **步骤 4：运行测试验证通过**

运行：`npm --prefix apps/web run test -- hooks/useAttemptSaver.test.tsx components/assessment/AttemptRunner.test.tsx`

预期：PASS；debounce、最大间隔、有限重试、断线重连、409 双版本、键盘操作、aria-live、未同步阻止误交、空卷摘要和回执视图通过。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/lib/assessment/attempts.ts apps/web/hooks apps/web/components/assessment
git commit -m "feat: add resilient accessible attempt runner"
```

### 任务 9：安全、审计、100 并发与故障恢复

**文件：**
- 创建：`modules/assessment/observability.py`
- 修改：`modules/assessment/services/attempts.py`
- 修改：`modules/assessment/services/responses.py`
- 修改：`modules/assessment/services/submissions.py`
- 创建：`db/migrations/versions/20261105_101000_assessment_attempt_due.py`
- 创建：`tests/integration/assessment/test_security.py`
- 创建：`tests/integration/assessment/test_concurrency.py`

- [ ] **步骤 1：编写失败的测试**

```python
from concurrent.futures import ThreadPoolExecutor
from prometheus_client import REGISTRY
from modules.assessment.services.attempts import AttemptService

def test_twenty_parallel_new_keys_return_original(session_factory, published_assignment, student_id, fixed_now):
    def start(index):
        with session_factory() as session:
            dto = AttemptService().start(session, assignment_id=published_assignment.id,
                student_id=student_id, idempotency_key=f"parallel-{index}", now=fixed_now)
            session.commit()
            return dto.attempt_id
    with ThreadPoolExecutor(max_workers=20) as pool:
        ids = list(pool.map(start, range(20)))
    assert len(set(ids)) == 1

def test_logs_have_no_raw_response_and_metrics_have_no_uuid(api_client, started_attempt, caplog):
    response = save_with_raw_response(api_client, started_attempt, {"text": "私密作答"})
    assert response.status_code == 200
    assert "私密作答" not in "\n".join(record.getMessage() for record in caplog.records)
    assert REGISTRY.get_sample_value("assessment_response_saves_total", {"result": "success"}) == 1.0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/integration/assessment/test_concurrency.py::test_twenty_parallel_new_keys_return_original tests/integration/assessment/test_security.py -q`

预期：FAIL，并行开始触发唯一约束，且 `observability.py` 与成功指标不存在。

- [ ] **步骤 3：编写最少实现代码**

在任务 3 的 active 查询之前加 assignment＋student advisory lock，锁后重新查询 active 和次数：

```python
session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 1729))"),
    {"key": f"assessment-start:{assignment.id}:{student_id}"})
active = session.scalar(select(PaperAttempt).where(
    PaperAttempt.assignment_id == assignment.id, PaperAttempt.student_id == student_id,
    PaperAttempt.status.in_(["in_progress", "finalizing"])).with_for_update())
```

新迁移声明 `revision="20261105_101000_assessment_attempt_due"` 与 `down_revision="20261105_100000_assessment_attempts"`，创建 `(worker_due_at,id) WHERE status='in_progress'` 部分索引；生产用 CONCURRENTLY，测试使用普通索引路径，upgrade/downgrade 均测试。

`observability.py` 定义保存结果 counter、保存延迟 histogram、到期结果 counter、due backlog gauge；label 只用 success/conflict/closed/replayed，不用 UUID。审计 metadata 白名单只含 ID、revision_no、hash、status、reason、计数，日志不写 raw/normalized response。开始/保存/交卷/worker/撤回接入这些白名单。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/integration/assessment/test_security.py tests/integration/assessment/test_concurrency.py -q`

预期：PASS；20 个新键返回同一 attempt，100 学生并行 start/save/submit 无重复或丢失，手动/worker 竞态、worker 重试、RLS、API/HTML/日志敏感值扫描、XSS 清理和低基数指标通过。模型网关替换为必定抛错 stub 后保存与交卷仍成功。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/observability.py modules/assessment/services db/migrations/versions/20261105_101000_assessment_attempt_due.py tests/integration/assessment/test_security.py tests/integration/assessment/test_concurrency.py
git commit -m "fix: harden concurrent and secure attempt flows"
```

### 任务 10：浏览器端到端验收与页面接线

**文件：**
- 创建：`apps/web/app/attempts/[attemptId]/page.tsx`
- 创建：`apps/web/tests/e2e/online-attempt.spec.ts`
- 创建：`tests/e2e/seed_online_attempt.py`

- [ ] **步骤 1：编写失败的测试**

```ts
import { expect, test } from "@playwright/test";

test("断线续答、多标签冲突、提交回执与撤回", async ({ page, context, request }) => {
  await page.goto(`/attempts/${process.env.E2E_ATTEMPT_ID}`);
  await page.getByLabel("第 1 题").fill("断线前的答案");
  await expect(page.getByRole("status")).toHaveText("已保存至服务器");
  await context.setOffline(true);
  await page.getByLabel("第 1 题").fill("离线未同步");
  await expect(page.getByRole("status")).toHaveText("离线未同步");
  await context.setOffline(false);
  await expect(page.getByRole("status")).toHaveText("已保存至服务器");
  const other = await context.newPage();
  await other.goto(`/attempts/${process.env.E2E_ATTEMPT_ID}`);
  await other.getByLabel("第 1 题").fill("另一标签");
  await expect(other.getByRole("status")).toHaveText("已保存至服务器");
  await page.getByLabel("第 1 题").fill("本标签冲突");
  await expect(page.getByText("保存冲突，请选择保留哪个版本")).toBeVisible();
  await page.getByRole("button", { name: "保留本地内容并重试" }).click();
  await page.getByRole("button", { name: "提交试卷" }).click();
  await page.getByRole("button", { name: "确认提交" }).click();
  await expect(page.getByTestId("receipt-hash")).toHaveText(/^[a-f0-9]{64}$/);
  await request.post("/api/test-only/withdraw-seeded-assignment");
  await page.reload();
  await expect(page.getByText("作业已撤回，历史回执仍可查看")).toBeVisible();
});
```

测试专用撤回适配器只存在于 E2E 构建。另用短时限 fixture 越过 worker_due_at 后 reload，断言 worker 与手动重复提交得到相同回执。

- [ ] **步骤 2：运行测试验证失败**

运行：`python -I tests/e2e/seed_online_attempt.py && npm --prefix apps/web exec playwright test tests/e2e/online-attempt.spec.ts`

预期：FAIL，页面路由 404，找不到“第 1 题”。

- [ ] **步骤 3：编写最少实现代码**

```tsx
import { notFound } from "next/navigation";
import { AttemptRunner } from "@/components/assessment/AttemptRunner";
import { serverAttemptClient } from "@/lib/assessment/attempts";

export const dynamic = "force-dynamic";
export default async function AttemptPage({ params }: { params: Promise<{ attemptId: string }> }) {
  const { attemptId } = await params;
  const attempt = await serverAttemptClient.getAttempt(attemptId, { cache: "no-store" });
  if (!attempt) notFound();
  return <main><h1>在线作答</h1><AttemptRunner attempt={attempt} client={serverAttemptClient} /></main>;
}
```

SSR payload 只含学生 DTO，不预取 exercise ORM 或教师密钥。种子脚本只通过应用服务创建发布卷、attempt、学生会话和短时限 fixture，输出 E2E 环境变量；测试结束只清理测试 schema。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -I tests/e2e/seed_online_attempt.py && npm --prefix apps/web exec playwright test tests/e2e/online-attempt.spec.ts`

预期：PASS；真实 Chromium 完成开始→保存→断线→重连→双标签 409→显式解决→提交→回执→撤回，短时限 worker 与手动重复提交回执一致。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/app/attempts apps/web/tests/e2e/online-attempt.spec.ts tests/e2e/seed_online_attempt.py
git commit -m "test: verify browser attempt recovery and submission"
```

## 3. 总验收

```bash
alembic upgrade head
python -m pytest tests/unit/assessment tests/integration/assessment -q
python -m pytest tests/integration/assessment/test_concurrency.py -q
npm --prefix apps/web run test
npm --prefix apps/web run lint
python -I tests/e2e/seed_online_attempt.py
npm --prefix apps/web exec playwright test tests/e2e/online-attempt.spec.ts
```

最终确认：模型网关必定失败时保存、重连、提交、到期收卷和回执仍成功；没有 `/checks`、answer_test 执行、练习反馈或 grade 写入。100 并发是正确性负载，不构成生产 P95 承诺。

## 4. 计划自检

- **规格覆盖：** 第 9—11 节的版本固定、状态机、保存、重连、快照、到期与撤回由任务 1、3—6 覆盖；第 12 节只保留延迟反馈边界；第 14—15 节白名单、RLS、审计、API 和错误由任务 1、2、7、9 覆盖；第 16—18 节异常、100 并发、可访问性和学生 E2E 由任务 4—10 覆盖。
- **迁移归属：** 主迁移 `20261105_100000_assessment_attempts` 指向 `20261105_090000_assessment_assembly`；到期索引迁移 `20261105_101000_assessment_attempt_due` 指向 `20261105_100000_assessment_attempts`，阶段 D 从 `101000` 接续。policy/exposure 只引用阶段 A 表，升级与回滚均验证不删除上游表。
- **类型一致：** 五个共享服务签名集中在第 0.2 节；`effective_deadline` 对应 `paper_attempt.server_deadline_at`，DTO、保存 SQL 和快照 SQL 使用同一闭区间语义。
- **副作用一致：** 所有写接口使用 Idempotency-Key；活动 attempt、submission、client_revision 和 outbox 均有唯一约束，行锁与 advisory lock 负责并发兜底。
- **范围闭合：** 没有逐题反馈、答案开放、CAS/LLM 或正式 grade 任务，练习和模型服务不在保存/交卷调用链中。
