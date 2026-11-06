# 组卷共享契约与工程前置实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为固定卷、约束组卷、在线作答、练习评分和 QTI 计划建立可复用的类型、JSON Schema、权限适配、幂等、安全投影、测试和 CI 契约。

**架构：** 本计划只在数学知识库基础工程之上增加 `assessment` 共享层，不重复实现身份、教材、习题、审核或 Release。后端以 Pydantic 模型生成跨语言 JSON Schema，Python/TypeScript 共用错误与状态语义；数据库、worker 和前端任务在后续分计划中实现。

**技术栈：** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16、Next.js/React/TypeScript、pytest、jsonschema、Vitest、GitHub Actions。

---

## 0. 文件结构与职责

```text
scripts/
├─ check-paper-prerequisites.py       # 检查基础工程和共享接口是否存在
├─ export-assessment-contracts.py     # 从 Pydantic 导出 JSON Schema
└─ check-assessment-contracts.py      # 检查 Schema 漂移和学生敏感字段
modules/assessment/
├─ __init__.py                        # 只导出稳定公共类型
├─ types.py                           # 枚举、哈希、Decimal、时间工具
├─ contracts.py                       # 跨模块 Pydantic 契约
├─ errors.py                          # 统一业务异常和错误响应
├─ auth.py                            # assessment 权限上下文与检查
├─ governance.py                      # Release/Review/撤回适配接口
├─ projections.py                     # 学生安全投影白名单
├─ idempotency.py                     # 请求键解析与存储接口
└─ jobs.py                            # worker/outbox 调度接口
apps/api/
├─ deps_assessment.py                 # Actor、Idempotency-Key、Clock 依赖
└─ assessment_errors.py               # FastAPI 异常处理器注册
apps/web/lib/assessment-api.ts        # 前端请求与错误规范
apps/web/components/assessment/
├─ AssessmentStatusBadge.tsx          # 状态显示
└─ ValidationIssueList.tsx            # 硬/软约束问题显示
packages/contracts/assessment/         # 生成的版本化 JSON Schema
tests/unit/assessment/                # 无数据库单元测试
tests/integration/assessment/         # 数据库与治理适配测试
tests/contract/assessment/            # Schema 与跨端契约测试
.github/workflows/assessment.yml       # 独立质量门禁
```

后续计划统一使用以下接口，不得改名：

- `BlueprintService.create_version`
- `CandidateService.snapshot`
- `PaperService.create_manual_draft`
- `PaperValidationService.validate`
- `PaperReviewService.submit / approve`
- `AssignmentService.publish`
- `PaperRenderService.render`
- `AssemblyService.start`
- `AssemblySolver.solve`
- `AttemptService.start`
- `ResponseService.save`
- `SubmissionService.finalize`
- `DeadlineWorker.finalize_due`
- `PracticeService.check`
- `GradingService.evaluate_submission / publish_grade`
- `ExposureService.assert_no_conflict`

## 1. 前置依赖守卫

**文件：**
- 创建：`scripts/check-paper-prerequisites.py`
- 测试：`tests/unit/assessment/test_prerequisites.py`

- [ ] **步骤 1：编写失败测试**

```python
# tests/unit/assessment/test_prerequisites.py
from pathlib import Path
from scripts.check_paper_prerequisites import REQUIRED_FILES, missing_prerequisites


def test_reports_every_missing_foundation_file(tmp_path: Path):
    missing = missing_prerequisites(tmp_path)
    assert "apps/api/main.py" in missing
    assert "modules/exercise/models.py" in missing
    assert "modules/governance/service.py" in missing


def test_accepts_complete_foundation(tmp_path: Path):
    for name in REQUIRED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# foundation fixture\n", encoding="utf-8")
    assert missing_prerequisites(tmp_path) == []
```

在测试顶部从脚本导入 `REQUIRED_FILES`，保证测试和检查器使用同一清单。

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_prerequisites.py -q`  
预期：FAIL，提示模块 `scripts.check_paper_prerequisites` 不存在。

- [ ] **步骤 3：实现依赖检查器**

```python
# scripts/check_paper_prerequisites.py
from pathlib import Path

REQUIRED_FILES = (
    "apps/api/main.py",
    "apps/worker/main.py",
    "modules/identity/models.py",
    "modules/exercise/models.py",
    "modules/governance/models.py",
    "modules/governance/service.py",
    "modules/governance/hashing.py",
    "modules/ops/models.py",
    "modules/ops/service.py",
    "db/migrations/env.py",
)


def missing_prerequisites(root: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (root / name).is_file()]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    missing = missing_prerequisites(root)
    if missing:
        raise SystemExit(
            "先完成数学知识库基础任务 1—6 及 ops job/outbox/idempotency 基线；缺少：\n" + "\n".join(missing)
        )
    print("paper prerequisites: ok")


if __name__ == "__main__":
    main()
```

若基础计划最终调整了文件位置，只修改 `REQUIRED_FILES` 和对应基础计划，禁止创建同名空壳绕过检查。

- [ ] **步骤 4：运行验证**

运行：`pytest tests/unit/assessment/test_prerequisites.py -q`  
预期：PASS。

运行：`python -I scripts/check-paper-prerequisites.py`  
预期：基础工程未完成时明确失败并列出缺失文件；基础任务 1—6 完成后输出 `paper prerequisites: ok`。

- [ ] **步骤 5：提交**

```bash
git add scripts/check-paper-prerequisites.py tests/unit/assessment/test_prerequisites.py
git commit -m "test: add assessment foundation prerequisite guard"
```

## 2. 共享枚举、哈希、分值与时钟

**文件：**
- 创建：`modules/assessment/types.py`
- 测试：`tests/unit/assessment/test_types.py`

- [ ] **步骤 1：编写失败测试**

```python
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from modules.assessment.types import (
    PaperStatus, AttemptStatus, canonical_bytes, content_hash,
    score_to_cents, cents_to_score, FrozenClock,
)


def test_canonical_hash_ignores_json_key_order():
    assert content_hash({"b": 1, "a": 2}) == content_hash({"a": 2, "b": 1})
    assert canonical_bytes({"中文": "连续"}) == b'{"\xe4\xb8\xad\xe6\x96\x87":"\xe8\xbf\x9e\xe7\xbb\xad"}'
    assert canonical_bytes({"score": Decimal("12.50")}) == b'{"score":"12.50"}'


def test_score_uses_exact_hundredths():
    assert score_to_cents(Decimal("12.50")) == 1250
    assert cents_to_score(1250) == Decimal("12.50")
    with pytest.raises(ValueError):
        score_to_cents(Decimal("0.001"))


def test_shared_status_and_clock():
    assert PaperStatus.APPROVED.value == "approved"
    assert AttemptStatus.IN_PROGRESS.value == "in_progress"
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert FrozenClock(now).now() == now
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 11, 5))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_types.py -q`  
预期：FAIL，`modules.assessment.types` 不存在。

- [ ] **步骤 3：实现共享类型**

```python
# modules/assessment/types.py
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import hashlib
import json
from typing import Any
from uuid import UUID


class PaperStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    WITHDRAWN = "withdrawn"


class AttemptStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    SUBMITTED = "submitted"
    GRADING = "grading"
    GRADED = "graded"
    RETURNED = "returned"
    CLOSED_UNSUBMITTED = "closed_unsubmitted"
    WITHDRAWN = "withdrawn"


def canonical_json_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime is not canonical")
        return value.isoformat()
    raise TypeError(f"unsupported canonical type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=canonical_json_value,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def score_to_cents(value: Decimal) -> int:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized != value:
        raise ValueError("score supports at most two decimal places")
    return int(quantized * 100)


def cents_to_score(value: int) -> Decimal:
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


class FrozenClock:
    def __init__(self, value: datetime):
        if value.tzinfo is None:
            raise ValueError("clock requires timezone-aware datetime")
        self.value = value

    def now(self) -> datetime:
        return self.value
```

生产时钟统一返回带时区 UTC；禁止在领域代码中直接使用裸 `datetime.now()`。

- [ ] **步骤 4：运行验证**

运行：`pytest tests/unit/assessment/test_types.py -q`  
预期：PASS，并验证中文 canonical JSON、分值精度和状态字符串。

- [ ] **步骤 5：提交**

```bash
git add modules/assessment/types.py tests/unit/assessment/test_types.py
git commit -m "feat: add shared assessment value types"
```

## 3. 跨端 Pydantic 契约与 JSON Schema

**文件：**
- 创建：`modules/assessment/contracts.py`
- 创建：`scripts/export-assessment-contracts.py`
- 创建：`tests/contract/assessment/test_schema_export.py`

- [ ] **步骤 1：编写失败契约测试**

```python
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from modules.assessment.contracts import CONTRACT_MODELS


def test_every_public_contract_has_forbidden_extra_fields():
    for model in CONTRACT_MODELS.values():
        assert model.model_config.get("extra") == "forbid"


def test_student_projection_schema_has_no_answer_fields():
    schema = json.loads(Path(
        "packages/contracts/assessment/student-paper-projection-v1.schema.json"
    ).read_text(encoding="utf-8"))
    encoded = json.dumps(schema)
    for forbidden in ("answer_spec", "solution_ast", "rubric_version_id", "answer_test"):
        assert forbidden not in encoded


def test_blueprint_schema_rejects_unknown_constraint():
    schema = json.loads(Path(
        "packages/contracts/assessment/paper-blueprint-v1.schema.json"
    ).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    payload = {
        "schema_version": "paper-blueprint/v1",
        "purpose": "practice",
        "course_id": "018f0000-0000-7000-8000-000000000001",
        "offering_id": None,
        "release_context_id": "018f0000-0000-7000-8000-000000000002",
        "total_score": "10.00",
        "duration_seconds": 600,
        "sections": [],
        "hard_constraints": {"unexpected": True},
        "soft_objectives": {},
        "review_policy": "single_teacher",
    }
    assert list(validator.iter_errors(payload))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/contract/assessment/test_schema_export.py -q`  
预期：FAIL，契约模块和 Schema 文件不存在。

- [ ] **步骤 3：实现契约模型**

```python
# modules/assessment/contracts.py
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class BlueprintSlot(ContractModel):
    code: str = Field(min_length=1, max_length=32)
    score: Decimal = Field(gt=0, decimal_places=2)
    exercise_kinds: list[str]
    required_concept_ids: list[UUID]
    learning_outcome_ids: list[UUID]
    difficulty_label: Literal["easy", "medium", "hard", "unknown"]
    estimated_seconds: int | None = Field(ge=0)
    mandatory_exercise_version_ids: list[UUID] = Field(default_factory=list)
    excluded_exercise_version_ids: list[UUID] = Field(default_factory=list)


class BlueprintSection(ContractModel):
    code: str
    title: str
    instruction: str
    slots: list[BlueprintSlot]


class HardConstraints(ContractModel):
    max_total_seconds: int | None = Field(default=None, ge=0)
    allow_anchor_reuse: bool = False
    exercise_family_conflicts: list[list[UUID]] = Field(default_factory=list)


class PaperBlueprintV1(ContractModel):
    schema_version: Literal["paper-blueprint/v1"]
    purpose: Literal["formal", "coursework", "practice"]
    course_id: UUID
    offering_id: UUID | None
    release_context_id: UUID
    total_score: Decimal = Field(gt=0, decimal_places=2)
    duration_seconds: int = Field(gt=0)
    sections: list[BlueprintSection]
    hard_constraints: HardConstraints
    soft_objectives: dict[str, int]
    review_policy: Literal["single_teacher", "formal_dual_review"]


class CandidateEntryV1(ContractModel):
    exercise_version_id: UUID
    exercise_instance_id: UUID | None
    source_release_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_decision_seq: int = Field(gt=0)
    dependency_decision_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    exercise_kind: str
    concept_ids: list[UUID]
    difficulty_label: Literal["easy", "medium", "hard", "unknown"]
    estimated_seconds: int | None = Field(ge=0)
    exercise_family_id: UUID | None


class CandidateManifestV1(ContractModel):
    release_context_id: UUID
    candidates: list[CandidateEntryV1]
    candidate_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttemptPolicyV1(ContractModel):
    schema_version: Literal["attempt-policy/v1"]
    policy_kind: Literal["formal", "coursework", "practice"]
    max_attempts: int | None = Field(ge=1)
    behavior: Literal["deferred", "adaptive_practice"]
    late_policy: Literal["auto_submit", "readonly_grace", "reject_late"]
    show_input_validation: bool
    show_correctness: Literal["never", "after_check", "after_submission", "teacher_release"]
    solution_release: Literal["never", "after_valid_attempt", "after_due", "teacher_release"]
    save_debounce_ms: int = Field(default=2000, ge=200, le=10000)
    max_save_interval_seconds: int = Field(default=30, ge=5, le=300)


class ResponseRevisionV1(ContractModel):
    schema_version: Literal["response-revision/v1"]
    attempt_id: UUID
    paper_slot_id: UUID
    client_revision_id: UUID
    base_revision_no: int = Field(ge=0)
    raw_response: dict
    client_sent_at: str | None


class SubmissionReceiptV1(ContractModel):
    schema_version: Literal["submission-receipt/v1"]
    submission_id: UUID
    attempt_id: UUID
    finalized_at: str
    item_count: int = Field(ge=0)
    unanswered_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    response_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_reason: Literal["student", "deadline"]


class StudentSlotProjectionV1(ContractModel):
    slot_id: UUID
    slot_no: int
    stem: dict
    public_assets: list[dict]
    input_kind: str
    raw_response: dict | None
    parse_status: Literal["valid", "invalid", "unsupported"] | None
    revision_no: int = Field(ge=0)


class StudentPaperProjectionV1(ContractModel):
    schema_version: Literal["student-paper-projection/v1"]
    attempt_id: UUID
    paper_version_id: UUID
    server_time: str
    server_deadline_at: str | None
    slots: list[StudentSlotProjectionV1]


CONTRACT_MODELS = {
    "paper-blueprint-v1": PaperBlueprintV1,
    "candidate-manifest-v1": CandidateManifestV1,
    "attempt-policy-v1": AttemptPolicyV1,
    "response-revision-v1": ResponseRevisionV1,
    "submission-receipt-v1": SubmissionReceiptV1,
    "student-paper-projection-v1": StudentPaperProjectionV1,
}
```

列表默认值在领域代码中通过工厂函数构造，禁止在运行时跨实例共享并修改这些列表。

- [ ] **步骤 4：实现确定性导出器**

```python
# scripts/export-assessment-contracts.py
import json
from pathlib import Path
from modules.assessment.contracts import CONTRACT_MODELS


def export_contracts(root: Path) -> None:
    target = root / "packages/contracts/assessment"
    target.mkdir(parents=True, exist_ok=True)
    for name, model in CONTRACT_MODELS.items():
        schema = model.model_json_schema()
        path = target / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    export_contracts(Path(__file__).resolve().parents[1])
```

运行：`python scripts/export-assessment-contracts.py` 生成 6 个 Schema 文件。

- [ ] **步骤 5：运行验证**

运行：`pytest tests/contract/assessment/test_schema_export.py -q`  
预期：PASS。额外字段被拒绝，学生 Schema 不含答案相关字段。

- [ ] **步骤 6：提交**

```bash
git add modules/assessment/contracts.py scripts/export-assessment-contracts.py packages/contracts/assessment tests/contract/assessment
git commit -m "feat: define versioned assessment contracts"
```

## 4. 统一错误、Actor 与幂等请求键

**文件：**
- 创建：`modules/assessment/errors.py`
- 创建：`modules/assessment/auth.py`
- 创建：`modules/assessment/idempotency.py`
- 创建：`apps/api/deps_assessment.py`
- 创建：`apps/api/assessment_errors.py`
- 测试：`tests/unit/assessment/test_api_contracts.py`

- [ ] **步骤 1：编写失败测试**

```python
import pytest
from uuid import uuid4
from modules.assessment.auth import AssessmentActor, require_scope
from modules.assessment.errors import AssessmentError
from modules.assessment.idempotency import parse_idempotency_key


def test_scope_denial_is_explicit():
    actor = AssessmentActor(
        user_id=uuid4(), course_ids=frozenset(), offering_ids=frozenset(),
        permissions=frozenset({"paper.edit"}),
    )
    with pytest.raises(AssessmentError) as exc:
        require_scope(actor, "paper.edit", course_id=uuid4(), offering_id=None)
    assert exc.value.status_code == 403
    assert exc.value.code == "policy_denied"


def test_idempotency_key_is_required_and_bounded():
    assert parse_idempotency_key("abc-123") == "abc-123"
    for value in (None, "", "x" * 129):
        with pytest.raises(AssessmentError):
            parse_idempotency_key(value)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_api_contracts.py -q`  
预期：FAIL，错误和权限模块不存在。

- [ ] **步骤 3：实现错误、权限和请求键**

```python
# modules/assessment/errors.py
class AssessmentError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
```

```python
# modules/assessment/auth.py
from dataclasses import dataclass
from uuid import UUID
from modules.assessment.errors import AssessmentError


@dataclass(frozen=True)
class AssessmentActor:
    user_id: UUID
    course_ids: frozenset[UUID]
    offering_ids: frozenset[UUID]
    permissions: frozenset[str]


def require_scope(actor: AssessmentActor, permission: str, course_id: UUID, offering_id: UUID | None):
    allowed_scopes = actor.offering_ids if offering_id else actor.course_ids
    scope = offering_id or course_id
    if permission not in actor.permissions or scope not in allowed_scopes:
        raise AssessmentError(403, "policy_denied", "当前身份或范围无权执行此操作")
```

```python
# modules/assessment/idempotency.py
import re
from modules.assessment.errors import AssessmentError

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def parse_idempotency_key(value: str | None) -> str:
    if value is None or not KEY_PATTERN.fullmatch(value):
        raise AssessmentError(422, "idempotency_key_invalid", "需要 1—128 位安全幂等键")
    return value
```

在 `apps/api/assessment_errors.py` 注册 `AssessmentError`，返回与其 `status_code` 一致的 `JSONResponse(content=exc.payload())`。在 `deps_assessment.py` 中把学校身份上下文映射为 `AssessmentActor`，不得信任客户端提交的 `permissions`、`course_ids` 或 `offering_ids`。

- [ ] **步骤 4：运行验证**

运行：`pytest tests/unit/assessment/test_api_contracts.py -q`  
预期：PASS。再添加 API 测试，验证 403、409、410、422 的响应均为同一 `error.code/message/details` 结构。

- [ ] **步骤 5：提交**

```bash
git add modules/assessment/{errors,auth,idempotency}.py apps/api/{deps_assessment,assessment_errors}.py tests/unit/assessment/test_api_contracts.py
git commit -m "feat: standardize assessment API errors and authorization"
```

## 5. 治理适配器与测试替身

**文件：**
- 创建：`modules/assessment/governance.py`
- 创建：`tests/unit/assessment/test_governance_adapter.py`
- 创建：`tests/integration/assessment/test_governance_adapter.py`
- 创建：`tests/unit/assessment/fakes.py`

- [ ] **步骤 1：编写失败测试**

```python
from uuid import uuid4
import pytest
from modules.assessment.governance import FakeGovernance, CandidateDecision
from modules.assessment.errors import AssessmentError


def test_revoked_candidate_cannot_be_approved():
    governance = FakeGovernance()
    version = uuid4()
    governance.decisions[version] = CandidateDecision("revoked", 9, "hash")
    with pytest.raises(AssessmentError) as exc:
        governance.assert_candidate_valid(version, None, "hash")
    assert exc.value.code == "review_invalid"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_governance_adapter.py -q`  
预期：FAIL，治理适配接口不存在。

- [ ] **步骤 3：定义统一适配接口**

```python
# modules/assessment/governance.py
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID
from modules.assessment.errors import AssessmentError


@dataclass(frozen=True)
class CandidateDecision:
    decision: str
    decision_seq: int
    content_hash: str


class AssessmentGovernance(Protocol):
    def assert_candidate_valid(self, exercise_version_id: UUID, exercise_instance_id: UUID | None,
                               expected_content_hash: str) -> CandidateDecision: ...

    def submit_paper_review(self, paper_version_id: UUID, manifest_hash: str,
                            reviewer_user_id: UUID) -> str: ...

    def approve_paper(self, paper_version_id: UUID, manifest_hash: str,
                      reviewer_user_id: UUID, required_reviews: int) -> str: ...

    def assert_not_revoked(self, entity_type: str, entity_id: UUID) -> None: ...


class FakeGovernance:
    def __init__(self):
        self.decisions: dict[UUID, CandidateDecision] = {}

    def assert_candidate_valid(self, exercise_version_id, exercise_instance_id,
                               expected_content_hash):
        decision = self.decisions.get(exercise_instance_id or exercise_version_id)
        if (decision is None or decision.decision != "approved"
                or decision.content_hash != expected_content_hash):
            raise AssessmentError(422, "review_invalid", "题目审核无效或内容哈希已变化")
        return decision
```

生产适配器使用以下最新终态查询；`CandidateService` 还必须调用依赖闭包、许可和撤回检查，不能把单条题干决策当成完整候选资格：

```python
from sqlalchemy import text
from sqlalchemy.orm import Session

LATEST_DECISION_SQL = """
SELECT decision, decision_seq, content_hash
FROM governance.review_decision_item
WHERE entity_type = :entity_type AND entity_version_id = :entity_id
ORDER BY decision_seq DESC
LIMIT 1
"""

class SqlAlchemyAssessmentGovernance:
    def __init__(self, session: Session):
        self.session = session

    def assert_candidate_valid(self, exercise_version_id, exercise_instance_id,
                               expected_content_hash):
        entity_type = "exercise_instance" if exercise_instance_id else "exercise_version"
        entity_id = exercise_instance_id or exercise_version_id
        row = self.session.execute(text(LATEST_DECISION_SQL), {
            "entity_type": entity_type, "entity_id": entity_id,
        }).mappings().first()
        if (row is None or row["decision"] != "approved"
                or row["content_hash"] != expected_content_hash):
            raise AssessmentError(422, "review_invalid", "题目审核无效或内容哈希已变化")
        return CandidateDecision(row["decision"], row["decision_seq"], row["content_hash"])
```

`submit_paper_review` 返回 review batch ID；`approve_paper` 返回 `review_manifest_hash`，双审时必须包含两个不同授权复核者的 decision_seq。发布终检另行查询 `dependency_decision_manifest`、许可和 `revocation_event`，不能只查历史 approved。

- [ ] **步骤 4：运行数据库适配验证**

在 `tests/integration/assessment/test_governance_adapter.py` 覆盖：历史 approved 后 revoked、贡献者自审、双审同人重复、内容哈希变化、许可失效和撤回。非 owner 数据库角色执行测试。

运行：`pytest tests/integration/assessment/test_governance_adapter.py -q`  
预期：全部 PASS，任何无效状态均为默认拒绝。

- [ ] **步骤 5：提交**

```bash
git add modules/assessment/governance.py tests/unit/assessment/test_governance_adapter.py tests/integration/assessment/test_governance_adapter.py tests/unit/assessment/fakes.py
git commit -m "feat: connect assessment publishing to governance"
```

## 6. 学生投影白名单与敏感字段扫描

**文件：**
- 创建：`modules/assessment/projections.py`
- 创建：`tests/security/assessment/test_student_projection.py`

- [ ] **步骤 1：编写失败安全测试**

```python
import pytest
from modules.assessment.projections import project_student_slot, assert_no_sensitive_fields


def test_projection_removes_all_server_only_material():
    slot = {
        "slot_id": "018f0000-0000-7000-8000-000000000010",
        "slot_no": 1,
        "stem": {"text": "证明连续函数的性质"},
        "public_assets": [],
        "input_kind": "proof",
        "answer_spec": {"hidden": True},
        "solution_ast": {"steps": ["hidden"]},
        "rubric_version_id": "018f0000-0000-7000-8000-000000000011",
        "answer_test": "hidden",
        "raw_response": {"answer": "学生自己的作答"},
        "parse_status": "valid",
        "revision_no": 0,
    }
    projected = project_student_slot(slot)
    assert set(projected) == {
        "slot_id", "slot_no", "stem", "public_assets",
        "input_kind", "raw_response", "parse_status", "revision_no",
    }
    assert_no_sensitive_fields(projected)


def test_nested_teacher_answer_is_blocked_but_student_answer_is_allowed():
    assert_no_sensitive_fields({"raw_response": {"answer": "x+1"}})
    with pytest.raises(AssertionError, match="correct_answer"):
        assert_no_sensitive_fields({"stem": {"correct_answer": "x+1"}})
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/security/assessment/test_student_projection.py -q`  
预期：FAIL，投影模块不存在。

- [ ] **步骤 3：实现递归白名单和扫描**

```python
# modules/assessment/projections.py
SENSITIVE_KEYS = {
    "answer", "answer_spec", "answer_version", "answer_version_id",
    "correct_answer", "teacher_answer", "standard_answer",
    "solution", "solution_ast", "solution_version_id",
    "rubric", "rubric_id", "rubric_version_id", "grading_notes",
    "answer_test", "answer_test_version", "answer_test_version_id",
    "pass_feedback", "fail_feedback", "hidden_test", "gold_answer",
    "gold_evidence_manifest",
}
STUDENT_OWN_RESPONSE_KEYS = {"current_response", "raw_response", "normalized_response"}
STUDENT_SLOT_FIELDS = (
    "slot_id", "slot_no", "stem", "public_assets",
    "input_kind", "raw_response", "parse_status", "revision_no",
)


def project_student_slot(slot: dict) -> dict:
    return {name: slot[name] for name in STUDENT_SLOT_FIELDS}


def assert_no_sensitive_fields(payload) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in STUDENT_OWN_RESPONSE_KEYS:
                continue
            if key in SENSITIVE_KEYS:
                raise AssertionError(f"sensitive student field: {key}")
            assert_no_sensitive_fields(value)
    elif isinstance(payload, list):
        for value in payload:
            assert_no_sensitive_fields(value)
```

API 必须从专用学生查询构造输入，不能先读取完整 ORM 再依赖此函数保护；本函数是第二道防线。

- [ ] **步骤 4：运行验证**

运行：`pytest tests/security/assessment/test_student_projection.py -q`  
预期：PASS，并验证嵌套 list/dict 中的 `gold_answer` 也会触发失败。

- [ ] **步骤 5：提交**

```bash
git add modules/assessment/projections.py tests/security/assessment/test_student_projection.py
git commit -m "test: enforce assessment student projection allowlist"
```

## 7. 前端共享请求与状态组件

**文件：**
- 创建：`apps/web/lib/assessment-api.ts`
- 创建：`apps/web/components/assessment/AssessmentStatusBadge.tsx`
- 创建：`apps/web/components/assessment/ValidationIssueList.tsx`
- 测试：`apps/web/components/assessment/__tests__/ValidationIssueList.test.tsx`

- [ ] **步骤 1：编写失败前端测试**

```tsx
import { render, screen } from "@testing-library/react";
import { ValidationIssueList } from "../ValidationIssueList";

test("硬约束与软目标不混为一个通过分数", () => {
  render(<ValidationIssueList issues={[
    { code: "TOTAL_SCORE", severity: "hard", message: "总分差 5 分" },
    { code: "DIFFICULTY", severity: "soft", message: "难度偏离目标" },
  ]} />);
  expect(screen.getByText("硬约束 1")).toBeInTheDocument();
  expect(screen.getByText("软目标 1")).toBeInTheDocument();
  expect(screen.queryByText("全部通过")).not.toBeInTheDocument();
});
```

- [ ] **步骤 2：运行测试确认失败**

运行：`npm --prefix apps/web test -- ValidationIssueList`  
预期：FAIL，组件不存在。

- [ ] **步骤 3：实现请求封装与组件**

```ts
// apps/web/lib/assessment-api.ts
export type AssessmentError = {
  error: { code: string; message: string; details: Record<string, unknown> };
};

export async function assessmentFetch<T>(
  input: string, init: RequestInit & { idempotencyKey?: string } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
  const response = await fetch(input, { ...init, headers });
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error.message), {
    status: response.status, code: body.error.code, details: body.error.details,
  });
  return body as T;
}
```

```tsx
// apps/web/components/assessment/ValidationIssueList.tsx
export type ValidationIssue = { code: string; severity: "hard" | "soft"; message: string };

export function ValidationIssueList({ issues }: { issues: ValidationIssue[] }) {
  const hard = issues.filter((issue) => issue.severity === "hard");
  const soft = issues.filter((issue) => issue.severity === "soft");
  return <div>
    <section aria-label="硬约束"><h3>硬约束 {hard.length}</h3>
      {hard.map((issue) => <p role="alert" key={issue.code}>{issue.message}</p>)}</section>
    <section aria-label="软目标"><h3>软目标 {soft.length}</h3>
      {soft.map((issue) => <p key={issue.code}>{issue.message}</p>)}</section>
    {issues.length === 0 && <p>全部通过</p>}
  </div>;
}
```

`AssessmentStatusBadge` 使用文本映射显示全部 `PaperStatus` 和 `AttemptStatus`，不能只靠颜色区分。

- [ ] **步骤 4：运行验证**

运行：`npm --prefix apps/web test -- AssessmentStatusBadge ValidationIssueList`  
预期：PASS，键盘和屏幕阅读器能读到硬/软问题数量。

- [ ] **步骤 5：提交**

```bash
git add apps/web/lib/assessment-api.ts apps/web/components/assessment
git commit -m "feat: add shared assessment web contracts"
```

## 8. Outbox 调度与可控时钟接口

**文件：**
- 创建：`modules/assessment/jobs.py`
- 创建：`tests/unit/assessment/test_jobs.py`
- 创建：`tests/integration/assessment/test_outbox_dispatch.py`

- [ ] **步骤 1：编写失败测试**

```python
from modules.assessment.jobs import InMemoryJobDispatcher


def test_job_dispatch_is_idempotent_per_business_key():
    dispatcher = InMemoryJobDispatcher()
    first = dispatcher.enqueue("submission.evaluate", "sub-1", {"submission_id": "sub-1"})
    second = dispatcher.enqueue("submission.evaluate", "sub-1", {"submission_id": "sub-1"})
    assert first == second
    assert len(dispatcher.jobs) == 1
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/unit/assessment/test_jobs.py -q`  
预期：FAIL，调度模块不存在。

- [ ] **步骤 3：实现接口和内存替身**

```python
# modules/assessment/jobs.py
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class JobRef:
    job_id: str


class JobDispatcher(Protocol):
    def enqueue(self, kind: str, business_key: str, payload: dict) -> JobRef: ...


class InMemoryJobDispatcher:
    def __init__(self):
        self.jobs: dict[tuple[str, str], JobRef] = {}

    def enqueue(self, kind: str, business_key: str, payload: dict) -> JobRef:
        key = (kind, business_key)
        if key not in self.jobs:
            self.jobs[key] = JobRef(f"{kind}:{business_key}")
        return self.jobs[key]
```

生产 `OutboxJobDispatcher` 必须在业务事务内写 `ops.outbox_event`，`(kind, business_key)` 唯一；不能在数据库提交前先调用 Celery。时间依赖注入 `Clock.now()`，生产使用 UTC，测试使用 `FrozenClock`。

- [ ] **步骤 4：运行验证**

运行：`pytest tests/unit/assessment/test_jobs.py tests/integration/assessment/test_outbox_dispatch.py -q`  
预期：内存测试与数据库唯一/回滚测试均 PASS；业务事务回滚时没有 outbox 事件。

- [ ] **步骤 5：提交**

```bash
git add modules/assessment/jobs.py tests/unit/assessment/test_jobs.py tests/integration/assessment/test_outbox_dispatch.py
git commit -m "feat: add transactional assessment job dispatch"
```

## 9. CI 与 Schema 漂移门禁

**文件：**
- 创建：`scripts/check-assessment-contracts.py`
- 创建：`.github/workflows/assessment.yml`
- 测试：`tests/contract/assessment/test_no_schema_drift.py`

- [ ] **步骤 1：编写失败漂移测试**

```python
import subprocess
from pathlib import Path


def test_generated_contracts_are_current():
    result = subprocess.run(
        ["python", "scripts/check-assessment-contracts.py"],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **步骤 2：运行测试确认失败**

运行：`pytest tests/contract/assessment/test_no_schema_drift.py -q`  
预期：FAIL，检查器不存在。

- [ ] **步骤 3：实现检查器与 CI**

```python
# scripts/check-assessment-contracts.py
import json
from pathlib import Path
from modules.assessment.contracts import CONTRACT_MODELS
from modules.assessment.projections import assert_no_sensitive_fields


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    for name, model in CONTRACT_MODELS.items():
        path = root / f"packages/contracts/assessment/{name}.schema.json"
        expected = model.model_json_schema()
        actual = json.loads(path.read_text(encoding="utf-8"))
        if actual != expected:
            raise SystemExit(f"schema drift: {name}; rerun export-assessment-contracts.py")
        if name == "student-paper-projection-v1":
            assert_no_sensitive_fields(actual.get("properties", {}))
    print("assessment contracts: current")


if __name__ == "__main__":
    main()
```

```yaml
# .github/workflows/assessment.yml
name: assessment
on:
  pull_request:
    paths: ["modules/assessment/**", "apps/**", "packages/contracts/assessment/**", "tests/**/assessment/**"]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: python -m pip install -e ".[dev]"
      - run: python scripts/check-paper-prerequisites.py
      - run: python scripts/check-assessment-contracts.py
      - run: pytest tests/unit/assessment tests/contract/assessment tests/security/assessment -q
      - run: npm --prefix apps/web ci
      - run: npm --prefix apps/web test -- --run
```

集成工作流另外启动 PostgreSQL/MinIO/Redis service，并执行 `pytest tests/integration/assessment tests/e2e/assessment -q`；不得用 unit job 绿色代替数据库和端到端结果。

- [ ] **步骤 4：运行验证**

运行：`python scripts/check-assessment-contracts.py && pytest tests/contract/assessment -q`  
预期：PASS。人工修改一个生成 Schema 后，检查器必须退出非零；恢复文件后再提交。

- [ ] **步骤 5：提交**

```bash
git add scripts/check-assessment-contracts.py .github/workflows/assessment.yml tests/contract/assessment/test_no_schema_drift.py
git commit -m "ci: enforce assessment schema and security contracts"
```

## 10. 共享计划验收与后续入口

- [ ] **步骤 1：运行全部共享测试**

```bash
python -I scripts/check-paper-prerequisites.py
python scripts/export-assessment-contracts.py
python scripts/check-assessment-contracts.py
pytest tests/unit/assessment tests/contract/assessment tests/integration/assessment tests/security/assessment -q
npm --prefix apps/web test -- --run
```

预期：全部 PASS；任何数据库或前端测试未运行都必须在交接中明确写出，不能宣称完成。

- [ ] **步骤 2：核对跨计划接口**

确认后续计划只使用本文件第 0 节列出的服务名；`review_manifest_hash` 支持双审，`StudentPaperProjectionV1` 不含答案，`SubmissionReceiptV1` 不含成绩，`AttemptStatus` 覆盖终态和撤回。

- [ ] **步骤 3：提交共享基线**

```bash
git add modules/assessment apps/api apps/web packages/contracts/assessment scripts tests .github/workflows/assessment.yml
git commit -m "feat: establish assessment implementation contracts"
```

后续执行顺序：阶段 A 手工固定卷 → 阶段 B 约束组卷 → 阶段 C 在线作答 → 阶段 D 练习评分 → 阶段 E QTI 与上线验收。每个阶段必须独立通过测试和审查，不能以后续功能掩盖前一阶段失败。
