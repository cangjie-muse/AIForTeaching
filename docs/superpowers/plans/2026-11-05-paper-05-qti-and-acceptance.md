# QTI 3 互操作与整套上线验收实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在阶段 A—D 已交付的固定卷、Release/Review 治理和在线作答链路上，完成 QTI 3 固定卷草稿导入与授权导出，并以可复现证据完成监控、告警、设计负载、安全、可访问性、真实试点、备份恢复和发布回滚验收。

**架构：** QTI 仅是交换边界：`importer.py` 生成新草稿，`exporter.py` 从固定 `paper_version` 快照生成包，`differences.py` 记录所有不可识别语义，`QtiService` 通过 `PaperService` 与 Release/Review 治理适配器执行，外部审核、成绩或发布状态均不进入本平台治理真源。上线验收以测试报告、扫描报告、staging 实测、真实试点签署和恢复/回滚演练为证据；所有容量数字和告警触发值只是设计验收负载或演练触发条件，不代表当前性能或生产 SLO。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL 16、defusedxml、pypdf、Pydantic 2、pytest、httpx、Prometheus client/PromQL、React/TypeScript、Vitest、Playwright、axe-core、k6、OWASP ZAP、Bandit、pip-audit、Trivy、Gitleaks、Docker Compose、pg_dump/pg_restore、MinIO Client。

---

## 0. 实施边界、前置条件与证据规则

### 0.1 前置条件

- 阶段 A—D 已提供可运行的 `apps/api`、`apps/web`、`modules/assessment`、`modules/governance`、数据库迁移和测试环境；本计划不重新实现蓝图、CP-SAT、attempt、评分或渲染主链路。
- 阶段 A—D 的 `PaperService` 若使用不同方法名，只在 QTI 适配层做签名适配；不得改变已批准 `paper_version` 的不可变边界。
- 真实试点使用已获授权的一门课程、真实任课教师、独立复核教师和一个真实教学班；证据只保存哈希、计数和签署，不把学生答案正文提交到 git。
- 阶段 A—D 的测试是上线门禁输入，不因本计划重点为 QTI 而跳过组卷、治理、在线作答、练习与评分回归。

### 0.2 明确排除

- 不实现 IRT、统计等值、自适应组卷、运行时随机练习卷或随机正式平行卷。
- 不实现在线监考、锁屏、摄像、屏幕监控或隐蔽按键监控。
- 不宣称 QTI 对 CAS、Lean、rubric、平台扩展或数学语义无损往返。
- 不用导入包中的外部 approved/published/grade 信息生成有效 Review、Release、assignment、submission 或 grade。
- 不把开发机或一次 staging 压测结果写成生产性能承诺；生产 P95/SLO 只能在目标硬件 staging 实测后由发布签署冻结。

### 0.3 验收证据规则

1. 自动门禁止读取人工预先写好的 `passed=true` 作为唯一证据；脚本必须根据命令退出码生成测试结果，人工步骤必须附真实签署文件及 SHA-256。
2. `var/acceptance/` 保存本地证据且不提交；仓库只提交收集器、schema、测试与 runbook。
3. 每份性能结果写入 `load_profile="design_acceptance"` 和 `performance_claim=false`；报告记录实际 p50/p95/p99，但正确性门禁不虚构延时指标。
4. QTI 差异报告中的 `blocking` 项必须阻止相关草稿提交整卷审核；`warning` 项仍需人工确认，不能被界面隐藏。
5. 跨课程导入必须重新校验目标课程范围、许可和 Release Context；导出源课程的审核状态不随包转移。

## 1. 文件结构

```text
AIForTeaching/
├─ apps/
│  ├─ api/
│  │  ├─ main.py                              # 注册 QTI router
│  │  ├─ deps.py                              # 注入 PaperService、治理和审计适配器
│  │  └─ routers/qti.py                       # 上传、报告查询、授权导出
│  └─ web/
│     ├─ app/teacher/papers/qti/page.tsx      # 教师导入/导出页
│     ├─ components/QtiImportReport.tsx       # 可访问的差异报告
│     ├─ components/assessment/SaveStatusIndicator.tsx # 保存状态无障碍修正
│     ├─ components/assessment/PaperEditor.tsx# 非拖拽调序与审核控件
│     └─ lib/qti.ts                           # 与 API 对齐的 TypeScript 类型
├─ modules/assessment/
│  ├─ qti/
│  │  ├─ contracts.py                         # 本计划全部共享类型和端口
│  │  ├─ differences.py                       # 差异收集、排序、阻断规则
│  │  ├─ importer.py                          # 安全解包与固定 Test/Section/Item 解析
│  │  ├─ exporter.py                          # 固定槽位导出与旁路 manifest
│  │  ├─ governance.py                        # Release/Review 治理适配器
│  │  └─ service.py                           # QtiService.import_package/export_paper
│  ├─ observability.py                        # 指标与结构化安全事件
│  └─ student_projection.py                   # 学生 DTO 白名单安全修正
├─ db/migrations/versions/
│  └─ 20261105_120000_assessment_qti.py       # qti_exchange 持久化，非空迁移
├─ packages/contracts/assessment/
│  ├─ qti-difference-report.schema.json
│  └─ pilot-evidence.schema.json
├─ tests/
│  ├─ unit/assessment/qti/                    # 差异、导入、导出单元测试
│  ├─ integration/assessment/qti/             # API、权限、治理和幂等测试
│  ├─ migrations/test_assessment_qti.py       # upgrade/downgrade 实测
│  ├─ performance/assessment/                 # 100 并发与 1000/50 设计负载
│  ├─ security/assessment/                    # 全链路边权、泄露、XSS、RLS、扫描
│  ├─ accessibility/assessment/               # 键盘、axe、阅读顺序
│  ├─ acceptance/assessment/                  # 真实试点与总上线门禁
│  └─ operations/assessment/                  # 恢复、发布与回滚演练
├─ infra/monitoring/
│  ├─ assessment-alerts.yml                   # 告警规则
│  └─ prometheus.yml                          # 测试与 staging scrape 配置
├─ scripts/operations/
│  ├─ assessment-backup.sh
│  ├─ assessment-restore.sh
│  ├─ assessment-release.sh
│  └─ assessment-launch-gate.py
└─ docs/operations/
   ├─ assessment-incidents.md                 # 告警响应与降级
   ├─ assessment-recovery.md                  # 备份、恢复、一致性核验
   ├─ assessment-release.md                   # 发布、回滚、go/no-go
   ├─ assessment-pilot.md                     # 真实试点执行脚本
   └─ assessment-accessibility.md             # NVDA/浏览器人工核验
```

`qti_exchange` 只保存交换方向、paper_version、操作者、包哈希、差异报告、artifact 引用、幂等键和时间，不保存外部审核或成绩状态。本阶段需要持久化报告和幂等记录，因此创建上述非空迁移；不得再创建空 QTI 迁移。

## 2. 实施任务

### 任务 1：QTI 共享契约、差异报告与持久化迁移

**文件：**
- 创建：`modules/assessment/qti/contracts.py`
- 创建：`modules/assessment/qti/differences.py`
- 创建：`db/migrations/versions/20261105_120000_assessment_qti.py`
- 创建：`packages/contracts/assessment/qti-difference-report.schema.json`
- 测试：`tests/unit/assessment/qti/test_differences.py`
- 测试：`tests/migrations/test_assessment_qti.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/qti/test_differences.py
from modules.assessment.qti.contracts import DifferenceKind, Severity
from modules.assessment.qti.differences import DifferenceCollector


def test_unknown_scoring_and_extension_are_reported_and_sorted():
    differences = DifferenceCollector()
    differences.add(
        path="/test/section/item[2]/responseProcessing",
        qname="qti:responseProcessing",
        kind=DifferenceKind.RESPONSE_PROCESSING,
        severity=Severity.BLOCKING,
        detail="custom response processing cannot be mapped",
        source_xml=b"<responseProcessing custom=\"true\"/>",
    )
    differences.add(
        path="/test/section/item[1]/vendorExtension",
        qname="vendor:extension",
        kind=DifferenceKind.EXTENSION,
        severity=Severity.WARNING,
        detail="unknown metadata extension",
        source_xml=b"<vendor:extension/>",
    )
    report = differences.finish(schema_version="qti-differences/v1")
    assert [entry.kind for entry in report.entries] == [
        DifferenceKind.EXTENSION,
        DifferenceKind.RESPONSE_PROCESSING,
    ]
    assert report.blocking_count == 1
    assert report.warning_count == 1
    assert all(len(entry.source_sha256) == 64 for entry in report.entries)
    assert report.can_submit_for_review is False
```

```python
# tests/migrations/test_assessment_qti.py
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_qti_migration_upgrade_and_downgrade(database_url, tmp_path):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "20261105_110000_assessment_grading")
    command.upgrade(config, "20261105_120000_assessment_qti")
    engine = create_engine(database_url)
    columns = {c["name"] for c in inspect(engine).get_columns("qti_exchange", schema="assessment")}
    assert {"id", "direction", "paper_version_id", "difference_report", "idempotency_key"} <= columns
    command.downgrade(config, "20261105_110000_assessment_grading")
    assert "qti_exchange" not in inspect(engine).get_table_names(schema="assessment")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/qti/test_differences.py tests/migrations/test_assessment_qti.py -q`
预期：FAIL；单元测试报 `ModuleNotFoundError: modules.assessment.qti`，迁移测试报找不到 revision `20261105_120000_assessment_qti`。

- [ ] **步骤 3：编写最少实现代码**

在 `contracts.py` 定义所有后续任务共用的类型；协议方法是注入边界，不允许在 QTI 核心另写 ORM 真源。

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias
from uuid import UUID

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

class Severity(StrEnum):
    WARNING = "warning"
    BLOCKING = "blocking"

class DifferenceKind(StrEnum):
    RESPONSE_PROCESSING = "response_processing"
    CAS = "cas"
    RUBRIC = "rubric"
    EXTENSION = "extension"
    MATH_SEMANTICS = "math_semantics"
    EXTERNAL_GOVERNANCE = "external_governance"
    EXTERNAL_RESULTS = "external_results"

@dataclass(frozen=True)
class DifferenceEntry:
    path: str
    qname: str
    kind: DifferenceKind
    severity: Severity
    detail: str
    source_sha256: str

@dataclass(frozen=True)
class QtiDifferenceReport:
    schema_version: str
    entries: tuple[DifferenceEntry, ...]
    blocking_count: int
    warning_count: int
    can_submit_for_review: bool

@dataclass(frozen=True)
class QtiImportContext:
    actor_id: UUID
    course_id: UUID
    offering_id: UUID | None
    release_context_id: UUID
    idempotency_key: str

@dataclass(frozen=True)
class ImportedChoice:
    identifier: str
    text: str

@dataclass(frozen=True)
class ImportedItem:
    identifier: str
    title: str
    prompt: str
    interaction: Literal["single_choice", "multiple_choice", "text_entry", "extended_text", "unsupported"]
    choices: tuple[ImportedChoice, ...]
    correct_response: tuple[str, ...]
    response_processing_xml: str | None
    rubric_xml: str | None
    extension_xml: tuple[str, ...]
    source_sha256: str

@dataclass(frozen=True)
class ImportedSlot:
    code: str
    item: ImportedItem
    score: Decimal

@dataclass(frozen=True)
class ImportedSection:
    code: str
    title: str
    instruction: str
    slots: tuple[ImportedSlot, ...]

@dataclass(frozen=True)
class ImportedPaper:
    title: str
    sections: tuple[ImportedSection, ...]
    duration_seconds: int | None

@dataclass(frozen=True)
class PaperDraftResult:
    paper_version_id: UUID
    qti_exchange_id: UUID
    status: Literal["draft"]
    review_status: Literal["unreviewed"]

@dataclass(frozen=True)
class QtiImportResult:
    paper_version_id: UUID
    qti_exchange_id: UUID
    status: Literal["draft"]
    review_status: Literal["unreviewed"]
    report: QtiDifferenceReport

@dataclass(frozen=True)
class PaperExportSnapshot:
    paper_version_id: UUID
    manifest_hash: str
    release_context_id: UUID
    paper: ImportedPaper
    policy: dict[str, JsonValue]

@dataclass(frozen=True)
class QtiExportResult:
    content: bytes
    output_sha256: str
    qti_exchange_id: UUID
    report: QtiDifferenceReport

class PaperService(Protocol):
    def submit_review(self, *, paper_version_id: UUID, actor_id: UUID, base_version: int) -> None: ...
    def import_qti_draft(self, *, paper: ImportedPaper, context: QtiImportContext,
                         report: QtiDifferenceReport, source_sha256: str) -> PaperDraftResult: ...
    def get_qti_export_snapshot(self, *, paper_version_id: UUID, actor_id: UUID) -> PaperExportSnapshot: ...
    def record_qti_export(self, *, snapshot: PaperExportSnapshot, actor_id: UUID,
                          output_sha256: str, report: QtiDifferenceReport,
                          idempotency_key: str) -> UUID: ...
    def get_qti_import_result(self, *, qti_exchange_id: UUID, actor_id: UUID) -> QtiImportResult: ...

class ReleaseGovernancePort(Protocol):
    def assert_import_target(self, *, context: QtiImportContext) -> None: ...
    def assert_export_allowed(self, *, paper_version_id: UUID, actor_id: UUID,
                              include_answers: bool) -> None: ...

class ReviewGovernancePort(Protocol):
    def assert_current_paper_approval(self, *, paper_version_id: UUID) -> None: ...
```

在 `differences.py` 使用规范化排序和哈希，报告不保存不受控 XML 正文。

```python
import hashlib
from .contracts import DifferenceEntry, DifferenceKind, QtiDifferenceReport, Severity

class DifferenceCollector:
    def __init__(self) -> None:
        self._entries: list[DifferenceEntry] = []

    def add(self, *, path: str, qname: str, kind: DifferenceKind,
            severity: Severity, detail: str, source_xml: bytes) -> None:
        self._entries.append(DifferenceEntry(path, qname, kind, severity, detail,
            hashlib.sha256(source_xml).hexdigest()))

    def finish(self, *, schema_version: str) -> QtiDifferenceReport:
        entries = tuple(sorted(self._entries, key=lambda e: (e.path, e.kind, e.qname)))
        blocking = sum(e.severity is Severity.BLOCKING for e in entries)
        warning = sum(e.severity is Severity.WARNING for e in entries)
        return QtiDifferenceReport(schema_version, entries, blocking, warning, blocking == 0)
```

迁移的 `down_revision` 必须精确为 `20261105_110000_assessment_grading`，`upgrade()` 创建表、JSONB CHECK、方向 CHECK 和 `(actor_id, idempotency_key, direction)` 唯一索引，`downgrade()` 仅删除本表及索引。

```python
revision = "20261105_120000_assessment_qti"
down_revision = "20261105_110000_assessment_grading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assessment.qti_exchange (
            id uuid PRIMARY KEY,
            direction text NOT NULL CHECK (direction IN ('import', 'export')),
            paper_version_id uuid NOT NULL,
            actor_id uuid NOT NULL,
            source_package_sha256 char(64),
            output_sha256 char(64),
            difference_report jsonb NOT NULL,
            artifact_version_id uuid,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (jsonb_typeof(difference_report) = 'object')
        )
    """)
    op.execute("""CREATE UNIQUE INDEX uq_qti_exchange_idempotency
        ON assessment.qti_exchange(actor_id, direction, idempotency_key)""")


def downgrade() -> None:
    op.execute("DROP TABLE assessment.qti_exchange")
```

JSON Schema 要求 `schema_version` 常量 `qti-differences/v1`，`entries` 每项必填六个字段，`severity` 仅允许 `warning/blocking`，并用 `$defs/entry` 与 Python DTO 对齐。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/qti/test_differences.py tests/migrations/test_assessment_qti.py -q`
预期：PASS；随后运行 `alembic upgrade head && alembic downgrade 20261105_110000_assessment_grading && alembic upgrade head`，三次均退出 0。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/qti/contracts.py modules/assessment/qti/differences.py \
  db/migrations/versions/20261105_120000_assessment_qti.py \
  packages/contracts/assessment/qti-difference-report.schema.json \
  tests/unit/assessment/qti/test_differences.py tests/migrations/test_assessment_qti.py
git commit -m "feat: add QTI difference contracts and exchange persistence"
```

### 任务 2：安全 QTI 3 固定卷导入为新草稿

**文件：**
- 创建：`modules/assessment/qti/importer.py`
- 创建：`modules/assessment/qti/service.py`
- 创建：`tests/fixtures/qti3/fixed-choice/imsmanifest.xml`
- 创建：`tests/fixtures/qti3/fixed-choice/test.xml`
- 创建：`tests/fixtures/qti3/fixed-choice/item-1.xml`
- 创建：`tests/fixtures/qti3/fixed-choice/item-2.xml`
- 创建：`tests/fixtures/qti3/fixed-choice.zip`
- 测试：`tests/unit/assessment/qti/test_importer.py`

- [ ] **步骤 1：编写失败的测试**

```python
import io
import zipfile
from uuid import uuid4
import pytest
from modules.assessment.qti.contracts import QtiImportContext
from modules.assessment.qti.importer import QtiPackageError, QtiImporter
from modules.assessment.qti.service import QtiService


def package(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()


def context():
    return QtiImportContext(uuid4(), uuid4(), uuid4(), uuid4(), "import-fixed-1")


def test_import_preserves_order_ignores_external_approval_and_reports_cas():
    source = open("tests/fixtures/qti3/fixed-choice.zip", "rb").read()
    paper, report = QtiImporter().parse(source)
    assert paper.sections[0].slots[0].code == "A1"
    assert paper.sections[0].slots[1].code == "A2"
    assert report.can_submit_for_review is False
    assert {e.kind for e in report.entries} >= {"cas", "external_governance"}


def test_zip_traversal_and_dtd_are_rejected():
    with pytest.raises(QtiPackageError, match="unsafe path"):
        QtiImporter().parse(package({"../escape.xml": b"<x/>"}))
    with pytest.raises(QtiPackageError, match="unsafe XML"):
        QtiImporter().parse(package({"imsmanifest.xml": b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><x>&e;</x>"}))
```

为服务测试使用内存 `PaperService`，其 `import_qti_draft` 返回 `PaperDraftResult(status="draft", review_status="unreviewed")`，并断言外部 `reviewStatus="approved"` 从未作为参数传给治理服务。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/qti/test_importer.py -q`
预期：FAIL，报 `ModuleNotFoundError: modules.assessment.qti.importer`；不是 fixture 解析错误。

- [ ] **步骤 3：编写最少实现代码**

固定 fixture 由四个 XML 文件生成可重复 ZIP，命令为 `python -I -c "from pathlib import Path; from zipfile import ZipFile; root=Path('tests/fixtures/qti3/fixed-choice'); z=ZipFile('tests/fixtures/qti3/fixed-choice.zip','w'); [z.write(p,p.name) for p in sorted(root.iterdir()) if p.suffix=='.xml']; z.close()"`；`item-1.xml` 含外部 `reviewStatus="approved"`，`item-2.xml` 含自定义 CAS responseProcessing，manifest 不含 assessmentResult 以外的成绩包。

`importer.py` 先执行安全解包，再按 manifest 的 test resource 和 item href 解析。输入限制是恶意包防护边界，不是容量成绩：压缩包 20 MiB、解压总量 100 MiB、2,000 文件；支持 `single_choice`、`multiple_choice`、`text_entry`、`extended_text`。遇到 `<selection>` 直接记录 `blocking/extension` 且不生成随机规则。

```python
import hashlib
import io
import posixpath
import zipfile
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from .contracts import DifferenceKind, ImportedPaper, QtiDifferenceReport, Severity
from .differences import DifferenceCollector

class QtiPackageError(ValueError):
    pass

class QtiImporter:
    def _entries(self, source: bytes) -> dict[str, bytes]:
        if len(source) > 20 * 1024 * 1024:
            raise QtiPackageError("package too large")
        result: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(source)) as archive:
                infos = archive.infolist()
                if len(infos) > 2000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024:
                    raise QtiPackageError("expanded package too large")
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/") or ":" in name:
                        raise QtiPackageError("unsafe path")
                    if not info.is_dir():
                        result[name] = archive.read(info)
            return result
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise QtiPackageError("invalid package") from exc

    def _xml(self, body: bytes):
        try:
            return SafeET.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (DefusedXmlException, ValueError) as exc:
            raise QtiPackageError("unsafe XML") from exc

    def parse(self, source: bytes) -> tuple[ImportedPaper, QtiDifferenceReport]:
        files = self._entries(source)
        if "imsmanifest.xml" not in files:
            raise QtiPackageError("imsmanifest.xml missing")
        manifest = self._xml(files["imsmanifest.xml"])
        differences = DifferenceCollector()
        paper = self._parse_fixed_test(files, manifest, differences)
        return paper, differences.finish(schema_version="qti-differences/v1")
```

`_parse_fixed_test` 必须只跟随 manifest 中的 QTI test/item resource，使用 `posixpath.normpath(posixpath.join(base, href))` 定位文件并再次拒绝越界；用 `.findall(".//{*}assessmentSection")` 与 `.findall(".//{*}assessmentItemRef")` 保持文档顺序。每个 item 记录 `source_sha256=hashlib.sha256(body).hexdigest()`；单选读取 `maxChoices=1`，多选读取大于 1，正确响应只作为新草稿答案候选。

以下检测逐条调用 `DifferenceCollector.add`，`source_xml` 使用原始节点序列化字节：
- 非标准 `responseProcessing`：`RESPONSE_PROCESSING/BLOCKING`。
- 名称含 `cas`、`lean` 的扩展：对应 `CAS/BLOCKING`；rubric 为 `RUBRIC/BLOCKING`；未识别数学语义为 `MATH_SEMANTICS/BLOCKING`。
- 其他未知扩展：`EXTENSION/WARNING`。
- 外部 review/approval/publish 属性：`EXTERNAL_GOVERNANCE/WARNING`，解析值绝不写入 `ImportedPaper`。
- `assessmentResult` 资源：`EXTERNAL_RESULTS/BLOCKING`，不导入成绩。

`service.py` 的导入入口固定如下，并在事务返回后再次防御状态提升。

```python
import hashlib
from uuid import UUID
from .contracts import (PaperService, QtiImportContext, QtiImportResult,
                        ReleaseGovernancePort, ReviewGovernancePort)
from .importer import QtiImporter

class QtiGovernanceError(RuntimeError):
    pass

class QtiService:
    def __init__(self, *, paper_service: PaperService,
                 release: ReleaseGovernancePort, review: ReviewGovernancePort,
                 importer: QtiImporter | None = None) -> None:
        self.paper_service = paper_service
        self.release = release
        self.review = review
        self.importer = importer or QtiImporter()

    def import_package(self, package: bytes, *, context: QtiImportContext) -> QtiImportResult:
        self.release.assert_import_target(context=context)
        source_sha256 = hashlib.sha256(package).hexdigest()
        paper, report = self.importer.parse(package)
        draft = self.paper_service.import_qti_draft(
            paper=paper, context=context, report=report, source_sha256=source_sha256)
        if draft.status != "draft" or draft.review_status != "unreviewed":
            raise QtiGovernanceError("QTI import must create an unreviewed draft")
        return QtiImportResult(draft.paper_version_id, draft.qti_exchange_id,
                               draft.status, draft.review_status, report)
```

`PaperService.import_qti_draft` 与 `qti_exchange` 写入必须在同一数据库事务：新建手工蓝图草稿、exercise/answer/rubric 草稿、paper draft 和差异报告；不创建 ReleaseItem、ReviewDecision、assignment 或 grade。存在 blocking 差异时，在 `PaperService.submit_review` 的共享门禁中拒绝该版本，错误码为 `qti_mapping_required`。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/qti/test_importer.py tests/unit/assessment/qti/test_differences.py -q`
预期：PASS；固定槽位顺序为 A1/A2，外部 approved 仍为 `draft/unreviewed`，恶意 ZIP 与 DTD 均被拒绝。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/qti/importer.py modules/assessment/qti/service.py \
  tests/fixtures/qti3 tests/unit/assessment/qti/test_importer.py
git commit -m "feat: import fixed QTI packages as unreviewed drafts"
```

### 任务 3：固定卷导出、Release/Review 适配器与 QTI API

**文件：**
- 创建：`modules/assessment/qti/exporter.py`
- 创建：`modules/assessment/qti/governance.py`
- 修改：`modules/assessment/qti/service.py`
- 创建：`apps/api/routers/qti.py`
- 修改：`apps/api/main.py`
- 修改：`apps/api/deps.py`
- 测试：`tests/unit/assessment/qti/test_exporter.py`
- 测试：`tests/integration/assessment/qti/test_qti_api.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/unit/assessment/qti/test_exporter.py
import io
import json
import zipfile
from decimal import Decimal
from uuid import uuid4
from modules.assessment.qti.contracts import (ImportedChoice, ImportedItem,
    ImportedPaper, ImportedSection, ImportedSlot, PaperExportSnapshot)
from modules.assessment.qti.exporter import QtiExporter


def snapshot():
    item = ImportedItem("q1", "Question 1", "<p>2+2?</p>", "single_choice",
        (ImportedChoice("A", "3"), ImportedChoice("B", "4")), ("B",),
        None, "<rubric>full reasoning required</rubric>", (), "a" * 64)
    paper = ImportedPaper("Fixed", (ImportedSection("A", "Section A", "Answer",
        (ImportedSlot("A1", item, Decimal("5.00")),)),), 1800)
    return PaperExportSnapshot(uuid4(), "b" * 64, uuid4(), paper,
        {"feedback": "after_close", "attempts": 1})


def test_export_keeps_fixed_order_and_sidecar_without_publishing():
    content, output_sha256, report = QtiExporter().export(snapshot(), include_answers=False)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        test_xml = archive.read("assessmentTest.xml").decode()
        sidecar = json.loads(archive.read("aift-manifest.json"))
        item_xml = archive.read("items/q1.xml").decode()
    assert "<selection" not in test_xml
    assert 'shuffle="false"' in test_xml
    assert sidecar["schema_version"] == "aift-paper-sidecar/v1"
    assert sidecar["slots"][0]["code"] == "A1"
    assert sidecar["governance"] == {"review_inherited": False, "release_inherited": False}
    assert "correctResponse" not in item_xml
    assert report.blocking_count == 1  # rubric 未被伪装成标准 QTI
    assert len(output_sha256) == 64
```

```python
# tests/integration/assessment/qti/test_qti_api.py
def test_qti_cannot_bypass_review_release_or_scope(teacher_client, student_client,
        qti3_fixed_zip, qti_import_context, approved_paper_version_id, revoked_paper_version_id):
    imported = teacher_client.post("/api/v1/qti/imports",
        files={"package": ("fixed.zip", qti3_fixed_zip, "application/zip")},
        data=qti_import_context,
        headers={"Idempotency-Key": "api-import-1"})
    assert imported.status_code == 201
    assert imported.json()["status"] == "draft"
    assert imported.json()["review_status"] == "unreviewed"
    assert student_client.post(f"/api/v1/paper-versions/{approved_paper_version_id}/qti-export",
        json={"include_answers": False}, headers={"Idempotency-Key": "student-export"}).status_code == 404
    assert teacher_client.post(f"/api/v1/paper-versions/{revoked_paper_version_id}/qti-export",
        json={"include_answers": False}, headers={"Idempotency-Key": "revoked-export"}).status_code == 410
```

`qti_import_context` fixture 返回 `{"course_id": str(course.id), "offering_id": str(offering.id), "release_context_id": str(release_context.id)}`，三个对象由数据库 fixture 创建且属于同一授权范围；另用未授权课程替换 `course_id`，断言跨课程导入返回 404。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/qti/test_exporter.py tests/integration/assessment/qti/test_qti_api.py -q`
预期：FAIL；单元测试缺 `QtiExporter`，API 两个路径均返回 404。

- [ ] **步骤 3：编写最少实现代码**

`exporter.py` 只输出固定 `assessmentTest/assessmentSection/assessmentItemRef`，不输出 `<selection>`，`ordering shuffle="false"`。旁路 manifest 包含 `paper_version_id`、`manifest_hash`、`release_context_id`、固定策略、槽位来源和差异报告；明确声明不继承治理。CAS/rubric/未知数学语义进入 blocking 差异，不伪造标准元素。`include_answers=True` 才写 `correctResponse`。

```python
import hashlib
import io
import json
import zipfile
from .contracts import (DifferenceKind, PaperExportSnapshot, QtiExportResult,
                        Severity)
from .differences import DifferenceCollector

class QtiExporter:
    def export(self, snapshot: PaperExportSnapshot, *, include_answers: bool):
        differences = DifferenceCollector()
        for section in snapshot.paper.sections:
            for slot in section.slots:
                if slot.item.rubric_xml:
                    differences.add(path=f"/items/{slot.item.identifier}/rubric",
                        qname="qti:rubricBlock", kind=DifferenceKind.RUBRIC,
                        severity=Severity.BLOCKING, detail="rubric requires platform mapping",
                        source_xml=slot.item.rubric_xml.encode())
        report = differences.finish(schema_version="qti-differences/v1")
        sidecar = {"schema_version": "aift-paper-sidecar/v1",
            "paper_version_id": str(snapshot.paper_version_id),
            "manifest_hash": snapshot.manifest_hash,
            "release_context_id": str(snapshot.release_context_id),
            "policy": snapshot.policy,
            "slots": [{"code": slot.code, "item": slot.item.identifier}
                      for section in snapshot.paper.sections for slot in section.slots],
            "governance": {"review_inherited": False, "release_inherited": False},
            "differences": {"blocking": report.blocking_count, "warning": report.warning_count,
                            "report_file": "qti-differences.json"}}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("assessmentTest.xml", self._test_xml(snapshot.paper))
            archive.writestr("aift-manifest.json", json.dumps(sidecar, sort_keys=True))
            archive.writestr("qti-differences.json", json.dumps(
                report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
            for section in snapshot.paper.sections:
                for slot in section.slots:
                    archive.writestr(f"items/{slot.item.identifier}.xml",
                        self._item_xml(slot.item, include_answers=include_answers))
            archive.writestr("imsmanifest.xml", self._manifest_xml(snapshot.paper))
        content = output.getvalue()
        return content, hashlib.sha256(content).hexdigest(), report
```

`_test_xml`、`_item_xml` 和 `_manifest_xml` 使用 `xml.etree.ElementTree` 注册 QTI 3 与 `https://aift.local/ns/qti-extension/v1` 命名空间，序列化前对 identifier 使用 `[A-Za-z_][A-Za-z0-9_.-]*` 全匹配校验，防止路径或 XML 注入。

在 `governance.py` 定义适配器，构造参数为阶段 A—D 共享服务；适配器只调用以下明确端口，QTI 服务不直接查询“当前最新题”。

```python
from typing import Protocol
from uuid import UUID
from .contracts import QtiImportContext

class ReleaseService(Protocol):
    def require_course_permission(self, *, actor_id: UUID, course_id: UUID, permission: str) -> None: ...
    def assert_context_and_license(self, *, release_context_id: UUID, course_id: UUID,
                                   offering_id: UUID | None) -> None: ...
    def assert_paper_release_valid(self, *, paper_version_id: UUID) -> None: ...
    def paper_course_id(self, paper_version_id: UUID) -> UUID: ...

class ReviewService(Protocol):
    def assert_paper_approved(self, *, paper_version_id: UUID) -> None: ...

class ReleaseGovernanceAdapter:
    def __init__(self, service: ReleaseService) -> None:
        self.service = service
    def assert_import_target(self, *, context: QtiImportContext) -> None:
        self.service.require_course_permission(actor_id=context.actor_id,
            course_id=context.course_id, permission="qti.import_draft")
        self.service.assert_context_and_license(release_context_id=context.release_context_id,
            course_id=context.course_id, offering_id=context.offering_id)
    def assert_export_allowed(self, *, paper_version_id: UUID, actor_id: UUID,
                              include_answers: bool) -> None:
        self.service.assert_paper_release_valid(paper_version_id=paper_version_id)
        self.service.require_course_permission(actor_id=actor_id,
            course_id=self.service.paper_course_id(paper_version_id),
            permission="qti.export_answers" if include_answers else "qti.export_student")

class ReviewGovernanceAdapter:
    def __init__(self, service: ReviewService) -> None:
        self.service = service
    def assert_current_paper_approval(self, *, paper_version_id: UUID) -> None:
        self.service.assert_paper_approved(paper_version_id=paper_version_id)
```

`QtiService.export_paper` 必须在读取快照前依次通过 Release 和 Review 门禁，再记录导出审计与 output hash；相同幂等键返回同一 artifact，不重复生成导出事件。

```python
def export_paper(self, *, paper_version_id: UUID, actor_id: UUID, include_answers: bool,
                 idempotency_key: str) -> QtiExportResult:
    self.release.assert_export_allowed(paper_version_id=paper_version_id,
        actor_id=actor_id, include_answers=include_answers)
    self.review.assert_current_paper_approval(paper_version_id=paper_version_id)
    snapshot = self.paper_service.get_qti_export_snapshot(
        paper_version_id=paper_version_id, actor_id=actor_id)
    content, output_sha256, report = QtiExporter().export(snapshot, include_answers=include_answers)
    exchange_id = self.paper_service.record_qti_export(snapshot=snapshot,
        actor_id=actor_id, output_sha256=output_sha256, report=report,
        idempotency_key=idempotency_key)
    return QtiExportResult(content, output_sha256, exchange_id, report)
```

API 固定为 `POST /api/v1/qti/imports`、`GET /api/v1/qti/imports/{exchange_id}/report`、`POST /api/v1/paper-versions/{paper_version_id}/qti-export`。导入使用 `UploadFile`，先读入 20 MiB 限制；两个写端点都要求 `Idempotency-Key`，导出返回 `application/zip`、`X-Content-SHA256` 和 `X-QTI-Exchange-Id`。无权资源统一 404，撤回 410，差异未映射提交审核 422 `qti_mapping_required`。答案导出与普通学生卷导出使用不同权限并分别审计。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/qti/test_exporter.py tests/integration/assessment/qti/test_qti_api.py -q`
预期：PASS；学生导出 404，撤回 410，跨课程导入 404，外部 approved 导入后仍无有效 Review/Release，重复幂等请求只产生一条 `qti_exchange`。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/qti apps/api/routers/qti.py apps/api/main.py apps/api/deps.py \
  tests/unit/assessment/qti/test_exporter.py tests/integration/assessment/qti/test_qti_api.py
git commit -m "feat: govern QTI fixed-paper export and API"
```

### 任务 4：教师 QTI 导入报告与导出交互

**文件：**
- 创建：`apps/web/lib/qti.ts`
- 创建：`apps/web/components/QtiImportReport.tsx`
- 创建：`apps/web/app/teacher/papers/qti/page.tsx`
- 测试：`apps/web/tests/qti/QtiImportReport.test.tsx`
- 测试：`apps/web/tests/qti/page.test.tsx`

- [ ] **步骤 1：编写失败的测试**

```tsx
import { render, screen } from "@testing-library/react";
import { QtiImportReport } from "../../components/QtiImportReport";

const report = {
  schemaVersion: "qti-differences/v1", status: "draft", reviewStatus: "unreviewed",
  blockingCount: 1, warningCount: 1, canSubmitForReview: false,
  entries: [{ path: "/items/q1/responseProcessing", qname: "qti:responseProcessing",
    kind: "response_processing", severity: "blocking",
    detail: "custom response processing cannot be mapped", sourceSha256: "a".repeat(64) }]
};

test("shows draft governance and blocking differences without false success", () => {
  render(<QtiImportReport report={report} />);
  expect(screen.getByRole("status")).toHaveTextContent("已创建新草稿，未继承外部审核、发布或成绩状态");
  expect(screen.getByRole("heading", { name: "阻断项 1" })).toBeVisible();
  expect(screen.getByText("/items/q1/responseProcessing")).toBeVisible();
  expect(screen.queryByRole("button", { name: "提交审核" })).toBeDisabled();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test -- QtiImportReport.test.tsx page.test.tsx`
预期：FAIL，报无法解析 `../../components/QtiImportReport`。

- [ ] **步骤 3：编写最少实现代码**

`apps/web/lib/qti.ts` 导出 `QtiDifferenceEntry`、`QtiImportReportDto`，字段与任务 1 完全对应；API 层只接受 `status === "draft"` 和 `reviewStatus === "unreviewed"` 的导入响应。

```tsx
// apps/web/components/QtiImportReport.tsx
import type { QtiImportReportDto } from "../lib/qti";

export function QtiImportReport({ report }: { report: QtiImportReportDto }) {
  return <section aria-labelledby="qti-report-title">
    <h2 id="qti-report-title">QTI 导入差异报告</h2>
    <p role="status">已创建新草稿，未继承外部审核、发布或成绩状态</p>
    <h3>阻断项 {report.blockingCount}</h3>
    <h3>警告 {report.warningCount}</h3>
    {report.entries.length === 0 ? <p>未发现不可识别扩展或语义差异。</p> :
      <ol>{report.entries.map((entry) => <li key={`${entry.path}-${entry.kind}`}>
        <strong>{entry.severity === "blocking" ? "必须人工映射" : "需要人工确认"}</strong>
        <code>{entry.path}</code><span>{entry.qname}</span><p>{entry.detail}</p>
        <small>来源 SHA-256：{entry.sourceSha256}</small>
      </li>)}</ol>}
    <button type="button" disabled={!report.canSubmitForReview}>提交审核</button>
  </section>;
}
```

页面提供文件选择、课程/班级/Release Context、上传进度和授权导出按钮；上传完成后显示完整报告，不用 toast 代替报告。blocking 时提示“先完成人工映射并重新校验”，warning 也需逐项确认。导出前明确是否包含答案，包含答案时要求二次确认并展示审计提示。任何界面均不出现“外部已审核”“导入即发布”或“随机生成正式卷”。

- [ ] **步骤 4：运行测试验证通过**

运行：`npm --prefix apps/web run test -- QtiImportReport.test.tsx page.test.tsx && npm --prefix apps/web run typecheck`
预期：Vitest 与 TypeScript 均 PASS；阻断状态按钮不可用，报告文字与 API 状态一致。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/lib/qti.ts apps/web/components/QtiImportReport.tsx \
  apps/web/app/teacher/papers/qti/page.tsx apps/web/tests/qti
git commit -m "feat: add accessible teacher QTI import report"
```

### 任务 5：全链路指标、告警与事故降级 runbook

**文件：**
- 创建：`modules/assessment/observability.py`
- 创建：`infra/monitoring/assessment-alerts.yml`
- 修改：`infra/monitoring/prometheus.yml`
- 创建：`docs/operations/assessment-incidents.md`
- 测试：`tests/unit/assessment/test_observability.py`
- 测试：`tests/operations/assessment/test_alert_rules.py`

- [ ] **步骤 1：编写失败的测试**

```python
from prometheus_client import CollectorRegistry
from modules.assessment.observability import AssessmentMetrics


def test_save_and_overdue_metrics_have_low_cardinality_labels():
    registry = CollectorRegistry()
    metrics = AssessmentMetrics(registry)
    metrics.response_saved(outcome="success", mode="formal", seconds=0.12)
    metrics.overdue_unfinalized.set(3)
    assert registry.get_sample_value("assessment_response_save_total",
        {"outcome": "success", "mode": "formal"}) == 1
    assert registry.get_sample_value("assessment_response_save_seconds_bucket",
        {"mode": "formal", "le": "0.25"}) == 1
    assert registry.get_sample_value("assessment_attempt_overdue_unfinalized") == 3


def test_alert_rules_cover_submission_outbox_and_answer_leak():
    text = open("infra/monitoring/assessment-alerts.yml").read()
    assert "assessment_attempt_overdue_unfinalized > 0" in text
    assert "assessment_answer_sensitive_scan_total" in text
    assert "assessment_outbox_evaluation_backlog" in text
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/unit/assessment/test_observability.py tests/operations/assessment/test_alert_rules.py -q`
预期：FAIL，缺 `AssessmentMetrics` 且告警文件不存在。

- [ ] **步骤 3：编写最少实现代码**

指标标签只允许固定枚举和 `render_kind/mode/status/reason`，禁止 paper、student、attempt、email、答案正文成为 label 或日志字段。

```python
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

class AssessmentMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.save = Counter("assessment_response_save_total", "Response saves",
            ("outcome", "mode"), registry=registry)
        self.save_latency = Histogram("assessment_response_save_seconds", "Save latency",
            ("mode",), buckets=(0.1, 0.25, 0.5, 1, 2, 5), registry=registry)
        self.overdue_unfinalized = Gauge("assessment_attempt_overdue_unfinalized",
            "Attempts past effective deadline without finalization", registry=registry)
        self.outbox_backlog = Gauge("assessment_outbox_evaluation_backlog",
            "Pending evaluation outbox events", registry=registry)
        self.sensitive_scan = Counter("assessment_answer_sensitive_scan_total",
            "Sensitive fields detected in student outputs", ("surface",), registry=registry)
    def response_saved(self, *, outcome: str, mode: str, seconds: float) -> None:
        if outcome not in {"success", "failure", "conflict", "duplicate"}:
            raise ValueError("invalid save outcome")
        if mode not in {"formal", "coursework", "practice"}:
            raise ValueError("invalid mode")
        self.save.labels(outcome=outcome, mode=mode).inc()
        self.save_latency.labels(mode=mode).observe(seconds)
```

同一类补齐并接入以下指标，名称固定：`assessment_assembly_candidates`、`assessment_assembly_preflight_failure_total{reason}`、`assessment_assembly_run_total{status}`、`assessment_assembly_duration_seconds`、`assessment_assembly_objective_deviation{objective}`、`assessment_manual_replacement_total`、`assessment_review_return_total`；三种 render 的 `assessment_render_total{render_kind,outcome}`、`assessment_render_pagination_issue_total`；`assessment_reconnect_total{outcome}`、`assessment_response_unsynced`、`assessment_auto_finalize_total{outcome}`；`assessment_cas_fail_total`、`assessment_human_review_queue`、`assessment_grade_revision_total`、`assessment_grade_publish_delay_seconds`、`assessment_appeal_total`；`assessment_authorization_denied_total{surface}`、`assessment_exposure_conflict_total`、`assessment_secure_export_total{render_kind}`、`assessment_revocation_block_total`。

告警表达式如下，数值仅用于 staging 演练验证告警链路，不宣称生产 SLO。

```yaml
groups:
  - name: assessment-acceptance-alerts
    rules:
      - alert: AssessmentAttemptNotFinalized
        expr: assessment_attempt_overdue_unfinalized > 0
        for: 1m
        labels: {severity: critical}
        annotations: {runbook: "docs/operations/assessment-incidents.md#到期未收卷"}
      - alert: AssessmentEvaluationBacklog
        expr: assessment_outbox_evaluation_backlog > 100
        for: 5m
        labels: {severity: warning}
        annotations: {runbook: "docs/operations/assessment-incidents.md#评阅积压"}
      - alert: AssessmentAnswerLeakDetected
        expr: increase(assessment_answer_sensitive_scan_total[5m]) > 0
        labels: {severity: critical}
        annotations: {runbook: "docs/operations/assessment-incidents.md#敏感信息"}
      - alert: AssessmentAutoFinalizeFailed
        expr: increase(assessment_auto_finalize_total{outcome="failure"}[5m]) > 0
        labels: {severity: critical}
        annotations: {runbook: "docs/operations/assessment-incidents.md#到期未收卷"}
```

`assessment-incidents.md` 为每类告警写出值班动作：到期未收卷先冻结新 attempt、查询 response manifest、用共享 finalize 幂等重放；评阅积压明确“提交回执已成功，评阅延迟”并通知教师；自动保存失败显示离线未同步，只允许学生导出本人原始文本；求解器故障切手工组卷且复用原 run，不创建重复 paper；渲染故障不影响保存，答案发布前必须恢复权限检查；敏感信息告警立即关闭相关 Artifact、写入撤回并轮换签名 URL。每节包含精确 PromQL、数据库只读核验 SQL、恢复命令和升级联系人角色，不记录学生答案正文。

- [ ] **步骤 4：运行测试验证通过**

运行：`pytest tests/unit/assessment/test_observability.py tests/operations/assessment/test_alert_rules.py -q && promtool test rules tests/operations/assessment/alert_rules.test.yml && promtool check rules infra/monitoring/assessment-alerts.yml`
预期：全部 PASS；在 staging 分别制造 1 个可控逾期 attempt、101 条合成 outbox、1 次合成扫描事件，三条告警按 `for` 触发，清理后 resolve。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/observability.py infra/monitoring \
  docs/operations/assessment-incidents.md tests/unit/assessment/test_observability.py \
  tests/operations/assessment
git commit -m "feat: add assessment monitoring alerts and incident runbook"
```

### 任务 6：100 并发与 1000 候选/50 槽位设计负载验收

**文件：**
- 创建：`tests/performance/assessment/load_plan.py`
- 创建：`tests/performance/assessment/attempts.k6.js`
- 创建：`tests/performance/assessment/assembly_load.py`
- 创建：`tests/performance/assessment/verify_results.py`
- 创建：`tests/performance/assessment/test_load_plan.py`
- 创建：`tests/performance/assessment/README.md`

- [ ] **步骤 1：编写失败的测试**

```python
from tests.performance.assessment.load_plan import ACCEPTANCE_LOAD, validate_attempt_result


def test_design_load_is_explicit_and_not_a_performance_claim():
    assert ACCEPTANCE_LOAD.students == 100
    assert ACCEPTANCE_LOAD.candidates == 1000
    assert ACCEPTANCE_LOAD.slots == 50
    assert ACCEPTANCE_LOAD.revisions_per_slot == 3
    assert ACCEPTANCE_LOAD.performance_claim is False


def test_concurrent_finalize_requires_one_submission_per_attempt():
    result = {"attempts": 100, "unique_submissions": 100, "duplicate_submissions": 0,
              "lost_accepted_revisions": 0, "invalid_revision_conflicts": 0,
              "unresolved_attempts": 0, "solver_status": "TIMEOUT",
              "solver_has_feasible_solution": True}
    validate_attempt_result(result)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/performance/assessment/test_load_plan.py -q`
预期：FAIL，报 `ModuleNotFoundError: tests.performance.assessment.load_plan`。

- [ ] **步骤 3：编写最少实现代码**

```python
# tests/performance/assessment/load_plan.py
from dataclasses import dataclass

@dataclass(frozen=True)
class LoadPlan:
    students: int = 100
    candidates: int = 1000
    slots: int = 50
    revisions_per_slot: int = 3
    load_profile: str = "design_acceptance"
    performance_claim: bool = False

ACCEPTANCE_LOAD = LoadPlan()

def validate_attempt_result(result: dict[str, int | str | bool]) -> None:
    assert result["attempts"] == 100
    assert result["unique_submissions"] == 100
    assert result["duplicate_submissions"] == 0
    assert result["lost_accepted_revisions"] == 0
    assert result["invalid_revision_conflicts"] == 0
    assert result["unresolved_attempts"] == 0
    assert result["solver_status"] in {"OPTIMAL", "FEASIBLE", "TIMEOUT"}
    if result["solver_status"] == "TIMEOUT":
        assert result["solver_has_feasible_solution"] is True
```

`attempts.k6.js` 用 `constant-vus` 精确启动 100 个虚拟学生，同一 `Date.now()+5000` 屏障后调用开始接口；每槽位发送 3 个 revision，其中 `client_revision_id` 重发一次，再交错过期 `base_revision_no` 与有效请求。每名学生同时触发手动交卷；到期 worker 由 staging 脚本设置为同一秒执行。k6 只以业务正确性 checks 为门禁，不设置虚构 P95。

```javascript
import http from "k6/http";
import { check } from "k6";
import exec from "k6/execution";

export const options = {
  scenarios: { fixed_attempts: { executor: "constant-vus", vus: 100, duration: "3m" } },
  thresholds: { checks: ["rate==1"] }
};

export default function () {
  const token = __ENV[`STUDENT_TOKEN_${exec.vu.idInTest}`];
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const started = http.post(`${__ENV.BASE_URL}/api/v1/assignments/${__ENV.ASSIGNMENT_ID}/attempts`,
    JSON.stringify({}), { headers: { ...headers, "Idempotency-Key": `start-${exec.vu.idInTest}` } });
  check(started, { "attempt started or resumed": (r) => r.status === 201 || r.status === 200 });
  const attempt = started.json();
  for (const slot of attempt.slots) {
    for (let revision = 0; revision < 3; revision += 1) {
      const id = `save-${exec.vu.idInTest}-${slot.slot_id}-${revision}`;
      const body = JSON.stringify({ client_revision_id: id, base_revision_no: revision,
        raw_response: { text: `student-${exec.vu.idInTest}-revision-${revision}` } });
      const saved = http.put(`${__ENV.BASE_URL}/api/v1/attempts/${attempt.attempt_id}/responses/${slot.slot_id}`,
        body, { headers: { ...headers, "Idempotency-Key": id } });
      check(saved, { "save accepted": (r) => [200, 201].includes(r.status) });
      const duplicate = http.put(`${__ENV.BASE_URL}/api/v1/attempts/${attempt.attempt_id}/responses/${slot.slot_id}`,
        body, { headers: { ...headers, "Idempotency-Key": id } });
      check(duplicate, { "duplicate save is idempotent": (r) =>
        [200, 201].includes(r.status) && r.json("revision_no") === revision + 1 });
      const staleId = `stale-${exec.vu.idInTest}-${slot.slot_id}-${revision}`;
      const stale = http.put(`${__ENV.BASE_URL}/api/v1/attempts/${attempt.attempt_id}/responses/${slot.slot_id}`,
        JSON.stringify({ client_revision_id: staleId, base_revision_no: revision,
          raw_response: { text: "stale-tab" } }),
        { headers: { ...headers, "Idempotency-Key": staleId } });
      check(stale, { "stale base returns conflict": (r) => r.status === 409 });
    }
  }
  const submitted = http.post(`${__ENV.BASE_URL}/api/v1/attempts/${attempt.attempt_id}/submit`, "{}",
    { headers: { ...headers, "Idempotency-Key": `submit-${exec.vu.idInTest}` } });
  check(submitted, { "submission receipt returned": (r) => r.status === 200 || r.status === 201 });
}
```

`assembly_load.py` 从已审核的合成 fixture 生成 1,000 个候选、50 个槽位，至少含 20 个互斥组和每槽位 2 个概念覆盖要求；CP-SAT 时间上限由 staging 配置固定为 30 秒，仅是受控测试限时。允许 `OPTIMAL/FEASIBLE/TIMEOUT`，`TIMEOUT` 必须有可行解，`FAILED/INFEASIBLE` 均不通过；独立整卷校验器再次检查总分、覆盖和互斥。脚本输出实际 elapsed、p50/p95/p99、求解 gap 和 worker 版本，不输出“达到生产性能”。

`verify_results.py` 直接查询 staging 只读核验端点：100 个 attempt 必须各对应唯一 submission，重复 revision 数为 0，所有已接收合格 revision 均可追溯，过期 base revision 只返回 409 且不覆盖新值；手动交卷与 worker 竞态不得产生重复预评 outbox。另以长公式、跨页证明、中英混排和图表 fixture 渲染三类卷，检查 PDF 页数、字体与敏感扫描结果。

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/performance/assessment/test_load_plan.py -q
set -a; source infra/.env.staging; set +a
k6 run --summary-export var/acceptance/attempts-k6.json tests/performance/assessment/attempts.k6.js
python -I tests/performance/assessment/assembly_load.py --output var/acceptance/assembly-load.json
python -I tests/performance/assessment/verify_results.py \
  --k6 var/acceptance/attempts-k6.json --assembly var/acceptance/assembly-load.json \
  --output var/acceptance/performance-verdict.json
```

预期：全部退出 0，`performance-verdict.json` 中业务正确性断言全部满足且 `performance_claim=false`。记录实测延时，不设未经目标硬件验证的生产 P95 承诺。

- [ ] **步骤 5：Commit**

```bash
git add tests/performance/assessment
git commit -m "test: add assessment design-load acceptance harness"
```

### 任务 7：全链路安全扫描、越权与防泄露验收

**文件：**
- 修改：`modules/assessment/projections.py`
- 创建：`tests/security/assessment/test_student_leakage.py`
- 创建：`tests/security/assessment/test_authorization.py`
- 创建：`tests/security/assessment/test_content_security.py`
- 创建：`tests/security/assessment/test_rls.py`
- 创建：`tests/security/assessment/test_qti_security.py`
- 创建：`tests/security/assessment/zap-baseline.conf`
- 创建：`tests/security/assessment/run_scans.sh`

- [ ] **步骤 1：编写失败的测试**

```python
import json
import re
import pytest

SENSITIVE = {"answer", "correct_response", "rubric", "answer_test", "solution", "hidden_comment"}

@pytest.mark.parametrize("surface", ["attempt_json", "prefetch_html", "sse_events", "student_pdf", "application_logs"])
def test_student_surfaces_have_no_sensitive_copy(security_fixtures, surface):
    payload = security_fixtures.capture(surface)
    if surface == "student_pdf":
        payload = security_fixtures.pdf_text_metadata_annotations_and_attachments(payload)
    text = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else json.dumps(payload)
    assert not any(re.search(rf'"{key}"\s*:', text, re.I) for key in SENSITIVE)
    assert security_fixtures.sentinel_answer not in text


def test_tampered_ids_and_expired_artifact_are_denied(student_client, security_fixtures):
    for path in security_fixtures.tampered_resource_paths():
        assert student_client.get(path).status_code in {403, 404}
    old_url = security_fixtures.expired_practice_artifact_url()
    assert student_client.get(old_url).status_code in {403, 404, 410}


def test_qti_xss_and_external_entity_do_not_execute(qti3_malicious_zip, teacher_client,
        qti_import_context, security_fixtures):
    response = teacher_client.post("/api/v1/qti/imports",
        files={"package": ("malicious.zip", qti3_malicious_zip, "application/zip")},
        data=qti_import_context, headers={"Idempotency-Key": "malicious-qti-1"})
    assert response.status_code == 422
    assert security_fixtures.read_canary_file() is False
```

`test_rls.py` 另以非 owner、非 superuser 的 `assessment_student` 角色连接数据库，在设置合法 student context 后读取保密 paper、answer render、rubric render、他人 response revision，预期 0 行或权限错误；关闭应用层权限后仍须通过 FORCE RLS 负向测试。

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/security/assessment -q`
预期：FAIL；至少学生 PDF fixture 的 `rubric` 元数据断言、过期 Artifact 断言和 QTI 恶意包断言暴露当前缺口。若失败来自缺少 fixture，先只实现合成 sentinel fixture，再确认功能断言变红。

- [ ] **步骤 3：编写最少实现代码**

学生投影使用独立 Pydantic DTO，不允许 ORM 全对象序列化。

```python
from pydantic import BaseModel, ConfigDict
from uuid import UUID

class StudentSlotDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_id: UUID
    number: str
    prompt_html: str
    public_asset_ids: tuple[UUID, ...]
    input_kind: str
    raw_response: dict | None
    save_state: str
    allowed_feedback: dict | None

class StudentAttemptDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    status: str
    server_deadline_at: str
    slots: tuple[StudentSlotDTO, ...]
```

实现时用题干 sanitizer 白名单过滤 HTML/MathML，MathML 公式保留可读语义；学生 PDF 从 `StudentSlotDTO` 重新构建输入，不复用答案渲染对象。Artifact 每次打开重新计算学生、assignment、曝光、反馈和撤回状态，旧签名 URL 不能作为缓存授权。日志过滤器对 `raw_response` 仅记录哈希，对 sentinel answer、rubric、密钥和签名 query 一律丢弃。

安全脚本必须实际运行 SAST、依赖、镜像、secret 与 DAST，而非只检查文件存在。

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p var/acceptance/security
bandit -r apps/api modules/assessment -f json -o var/acceptance/security/bandit.json
pip-audit -r apps/api/requirements.lock -f json -o var/acceptance/security/pip-audit.json
npm --prefix apps/web audit --audit-level=high --json > var/acceptance/security/npm-audit.json
gitleaks detect --source . --no-git --report-format json --report-path var/acceptance/security/gitleaks.json
trivy image --severity HIGH,CRITICAL --ignore-unfixed --format json \
  --output var/acceptance/security/trivy.json "$ASSESSMENT_IMAGE"
zap-baseline.py -t "$ASSESSMENT_BASE_URL" -c tests/security/assessment/zap-baseline.conf \
  -J var/acceptance/security/zap.json -w var/acceptance/security/zap.md
python -I tests/security/assessment/assert_scan_verdicts.py --directory var/acceptance/security
```

`assert_scan_verdicts.py` 对 HIGH/CRITICAL、secret 和 ZAP High 失败；依赖已存在豁免时必须读取带到期日的 `tests/security/assessment/exceptions.json`，过期豁免视为失败。XSS fixture 覆盖题干、LaTeX、MathML、图片说明、QTI 扩展和附件；恶意附件包含 SVG script、PDF JavaScript、ZIP traversal、XXE 与公式渲染注入。另验证练习答案发布后进入保密正式卷被阻断，求解 worker 收到题干指令时只处理规范化元数据。

- [ ] **步骤 4：运行测试验证通过**

```bash
pytest tests/security/assessment -q
set -a; source infra/.env.staging; set +a
bash tests/security/assessment/run_scans.sh
```

预期：全部退出 0；学生 API/HTML/JS 预取/SSE/PDF/日志无 sentinel 答案或量规，篡改 ID 不越权，RLS 负向测试通过，HIGH/CRITICAL 与 secret 扫描门禁通过。

- [ ] **步骤 5：Commit**

```bash
git add modules/assessment/projections.py tests/security/assessment
git commit -m "test: secure the full assessment delivery chain"
```

### 任务 8：教师与学生全键盘及可访问性验收

**文件：**
- 修改：`apps/web/components/assessment/PaperEditor.tsx`
- 修改：`apps/web/components/assessment/SaveStatusIndicator.tsx`
- 修改：`apps/web/components/QtiImportReport.tsx`
- 创建：`apps/web/tests/accessibility/assessment.spec.ts`
- 创建：`docs/operations/assessment-accessibility.md`

- [ ] **步骤 1：编写失败的测试**

```ts
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("teacher orders slots and student resolves save states without a mouse", async ({ page }) => {
  await page.goto("/teacher/papers/e2e-fixed/edit");
  await page.getByRole("button", { name: "将 A2 下移" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toContainText("A2 已移至第 3 位");
  await page.goto("/student/attempts/e2e-attempt");
  await page.getByRole("textbox", { name: "A1 作答" }).focus();
  await page.keyboard.type("x+1");
  await expect(page.getByRole("status", { name: "保存状态" })).toContainText("已保存至服务器");
  const violations = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(violations.violations).toEqual([]);
});

test("conflicts invalid input and unanswered counts are announced as text", async ({ page }) => {
  await page.goto("/student/attempts/e2e-conflict");
  await expect(page.getByRole("alert")).toContainText("服务器已有更新版本");
  await expect(page.getByRole("button", { name: "载入服务器版本" })).toBeVisible();
  await expect(page.getByRole("button", { name: "复制我的未同步内容" })).toBeVisible();
  await page.goto("/student/attempts/e2e-submit");
  await expect(page.getByRole("status")).toContainText("2 题未答，1 题输入无效");
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm --prefix apps/web run test:a11y -- tests/accessibility/assessment.spec.ts`
预期：FAIL；找不到“将 A2 下移”按钮或保存状态没有可访问名称，并列出 axe violation id。

- [ ] **步骤 3：编写最少实现代码**

`PaperEditor.tsx` 为每个槽位提供“上移/下移/替换/分值”原生按钮和输入框，按钮 disabled 状态反映首尾边界；拖拽只是附加方式。`SaveStatus.tsx` 使用固定 live region，避免每次按键抢焦点。

```tsx
type SaveState = "saving" | "saved" | "offline" | "conflict" | "closed";
const labels: Record<SaveState, string> = {
  saving: "正在保存", saved: "已保存至服务器", offline: "离线，尚有内容未同步",
  conflict: "服务器已有更新版本，请选择如何处理", closed: "作答已关闭"
};
export function SaveStatus({ state }: { state: SaveState }) {
  return <p role={state === "conflict" ? "alert" : "status"}
    aria-label="保存状态" aria-live={state === "conflict" ? "assertive" : "polite"}
    aria-atomic="true">{labels[state]}</p>;
}
```

题干用 `fieldset/legend` 关联题号与说明，错误用 `aria-describedby` 绑定；公式提供 MathML 和可读文本，图表有替代文本。未答、无效输入、正误、blocking/warning 不只靠颜色。打印预览在 Chromium、Firefox、WebKit 及 PDF 中检查长公式、跨页证明、图表、中英混排，无裁切、乱码、孤立题号和空白页。

`assessment-accessibility.md` 写明 NVDA + Firefox 与 VoiceOver + Safari 的人工路径：教师仅用键盘完成替换、调序、调分、整卷审核；学生仅用键盘开始、作答、断线、重连、解决冲突、提交、读取回执；QTI 报告逐项可读。每次执行记录浏览器/阅读器版本、实际观察、签署人和证据哈希。

- [ ] **步骤 4：运行测试验证通过**

运行：`npm --prefix apps/web run test:a11y -- tests/accessibility/assessment.spec.ts && npm --prefix apps/web run lint`
预期：Playwright 三浏览器与 axe 均 PASS；按 runbook 完成一次 NVDA + Firefox 和一次 VoiceOver + Safari 实测，所有状态由文本朗读，焦点顺序与视觉顺序一致。

- [ ] **步骤 5：Commit**

```bash
git add apps/web/components/assessment/PaperEditor.tsx \
  apps/web/components/assessment/SaveStatusIndicator.tsx apps/web/components/QtiImportReport.tsx \
  apps/web/tests/accessibility/assessment.spec.ts docs/operations/assessment-accessibility.md
git commit -m "test: make assessment workflows keyboard and screen-reader accessible"
```

### 任务 9：一门真实课程与真实班级的独立试点验收

**文件：**
- 创建：`docs/operations/assessment-pilot.md`
- 创建：`packages/contracts/assessment/pilot-evidence.schema.json`
- 创建：`tests/acceptance/assessment/collect_pilot_evidence.py`
- 创建：`tests/acceptance/assessment/test_pilot_evidence.py`

- [ ] **步骤 1：编写失败的测试**

```python
import json
import os
from pathlib import Path


def test_real_pilot_evidence_is_complete_and_unassisted():
    evidence_path = Path(os.environ["ASSESSMENT_PILOT_EVIDENCE"])
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert data["pilot_kind"] == "authorized_real_course"
    assert data["teacher"]["completed_without_facilitator_action"] is True
    assert data["teacher"]["flow"] == ["blueprint", "assemble", "review", "publish", "withdraw"]
    assert data["independent_reviewer"]["different_from_teacher"] is True
    assert data["student"]["flow"] == ["start", "disconnect", "resume", "submit", "receipt"]
    assert data["student"]["all_assigned_students_have_one_receipt"] is True
    assert data["qti"]["imported_as_draft"] is True
    assert data["qti"]["external_review_inherited"] is False
    assert data["incidents"]["unexpected_answer_exposure"] == 0
    assert data["incidents"]["lost_submissions"] == 0
    assert len(data["teacher"]["signature_sha256"]) == 64
    assert len(data["independent_reviewer"]["signature_sha256"]) == 64
    assert len(data["course_owner"]["signature_sha256"]) == 64
```

- [ ] **步骤 2：运行测试验证失败**

运行：`ASSESSMENT_PILOT_EVIDENCE=var/acceptance/pilot/summary.json pytest tests/acceptance/assessment/test_pilot_evidence.py -q`
预期：FAIL，报 `FileNotFoundError`；不得为了让测试变绿而手写虚构试点结果。

- [ ] **步骤 3：编写最少实现代码**

`assessment-pilot.md` 固定以下真实执行脚本，并明确 facilitator 只读说明、不代替教师点击：

1. 任课教师从授权题库建立 100 分固定卷，包含至少一道长公式题和一道人工评分题；独立复核教师完成整卷审核，教师布置给一个真实教学班。
2. 教师从另一课程导入含一个已知自定义 responseProcessing 的 QTI 包；系统生成草稿和 blocking 差异，教师不能提交审核，外部 approved 标志无效。完成人工映射后才走本平台正常 Release/Review。
3. 学生开始作答；网络连接在学生保存一个 revision 后中断 30 秒，恢复后确认服务端版本和本地未同步冲突提示；学生解决冲突并交卷，读取含服务端时间和哈希的回执。
4. 到期 worker 对另一名学生的 attempt 与学生手动提交并发执行，只生成一个 submission；CAS fail 转人工，教师确认后发布成绩，练习提交不写正式 grade。
5. 课程负责人撤回一个未开始的新 assignment，核验新 attempt 和新下载立即被阻断，历史 submission/render 仍可限权回放。
6. facilitator 保存不含答案正文的审计计数、回执哈希和签署 PDF。真实试点中任何答案泄露、提交丢失、绕过审核或 facilitator 代操作均为 no-go。

`collect_pilot_evidence.py` 不接受 `--passed` 之类直接宣布成功的参数；它从只读审计导出计算流程序列、回执唯一性、QTI 状态和事故计数，并对三份人工签署文件做 SHA-256。

```python
import argparse
import hashlib
import json
from pathlib import Path

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verified_signature(path: Path, required_declaration: str) -> str:
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if required_declaration not in text:
        raise ValueError(f"signature declaration missing: {required_declaration}")
    return sha256(path)

def require_flow(events: list[dict], field: str, expected: list[str]) -> list[str]:
    position = 0
    for event in events:
        if position < len(expected) and event.get(field) == expected[position]:
            position += 1
    if position != len(expected):
        raise ValueError(f"missing ordered flow: {expected}")
    return expected.copy()

def summarize_audit(audit: dict) -> dict:
    if audit["pilot_kind"] != "authorized_real_course" or audit["facilitator_actions"]:
        raise ValueError("pilot was not independent or not an authorized real course")
    teacher_flow = require_flow(audit["teacher_events"], "action",
        ["blueprint", "assemble", "review", "publish", "withdraw"])
    student_flow = ["start", "disconnect", "resume", "submit", "receipt"]
    for student_id in audit["assigned_student_ids"]:
        events = [e for e in audit["student_events"] if e["student_id"] == student_id]
        require_flow(events, "action", student_flow)
        if len([e for e in events if e["action"] == "receipt"]) != 1:
            raise ValueError("each assigned student must have exactly one receipt")
    if not audit["qti_imports"] or any(item["status"] != "draft" or item["external_review_inherited"]
                                      for item in audit["qti_imports"]):
        raise ValueError("QTI governance mismatch")
    return {
        "pilot_kind": audit["pilot_kind"],
        "teacher": {"completed_without_facilitator_action": True, "flow": teacher_flow},
        "independent_reviewer": {"different_from_teacher": audit["reviewer_id"] != audit["teacher_id"]},
        "student": {"flow": student_flow, "all_assigned_students_have_one_receipt": True},
        "qti": {"imported_as_draft": True, "external_review_inherited": False},
        "incidents": {"unexpected_answer_exposure": audit["incidents"]["unexpected_answer_exposure"],
                      "lost_submissions": audit["incidents"]["lost_submissions"]},
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-export", type=Path, required=True)
    parser.add_argument("--teacher-signature", type=Path, required=True)
    parser.add_argument("--reviewer-signature", type=Path, required=True)
    parser.add_argument("--owner-signature", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit_export.read_text(encoding="utf-8"))
    summary = summarize_audit(audit)  # 输入只含事件类型、主体伪匿名 ID、计数和哈希
    summary["teacher"]["signature_sha256"] = verified_signature(
        args.teacher_signature, "教师独立完成")
    summary["independent_reviewer"]["signature_sha256"] = verified_signature(
        args.reviewer_signature, "复核教师独立")
    summary["course_owner"]["signature_sha256"] = verified_signature(
        args.owner_signature, "试点为真实授权课程")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
```

`summarize_audit` 必须验证签名文件的声明文本包含“教师独立完成”“复核教师独立”“试点为真实授权课程”，并从事件流计算 `different_from_teacher`、一人一回执、断线恢复顺序和 QTI 未继承状态；缺任一事件就抛错，不默认成功。JSON Schema 将 `pilot_kind` 固定为 `authorized_real_course`，五个人工/系统事故字段均必填。

- [ ] **步骤 4：运行测试验证通过**

```bash
python -I tests/acceptance/assessment/collect_pilot_evidence.py \
  --audit-export var/acceptance/pilot/audit-export.json \
  --teacher-signature var/acceptance/pilot/teacher-signature.pdf \
  --reviewer-signature var/acceptance/pilot/reviewer-signature.pdf \
  --owner-signature var/acceptance/pilot/owner-signature.pdf \
  --output var/acceptance/pilot/summary.json
ASSESSMENT_PILOT_EVIDENCE=var/acceptance/pilot/summary.json \
  pytest tests/acceptance/assessment/test_pilot_evidence.py -q
```

预期：PASS；签署和审计来自同一次真实试点，教师独立完成全流程，所有被分配学生各有一个回执，无答案曝光和提交丢失。证据目录不提交 git。

- [ ] **步骤 5：Commit**

```bash
git add docs/operations/assessment-pilot.md packages/contracts/assessment/pilot-evidence.schema.json \
  tests/acceptance/assessment/collect_pilot_evidence.py tests/acceptance/assessment/test_pilot_evidence.py
git commit -m "test: verify the authorized real-course assessment pilot"
```

### 任务 10：备份恢复、发布回滚与整套上线总门禁

**文件：**
- 创建：`scripts/operations/assessment-backup.sh`
- 创建：`scripts/operations/assessment-restore.sh`
- 创建：`scripts/operations/assessment-release.sh`
- 创建：`scripts/operations/assessment-launch-gate.py`
- 创建：`docs/operations/assessment-recovery.md`
- 创建：`docs/operations/assessment-release.md`
- 创建：`tests/operations/assessment/test_recovery_evidence.py`
- 创建：`tests/operations/assessment/test_release_evidence.py`
- 创建：`tests/operations/assessment/verify_restore.py`
- 创建：`tests/operations/assessment/verify_rollback.py`
- 创建：`tests/acceptance/assessment/test_launch_evidence.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/operations/assessment/test_recovery_evidence.py
import json
from pathlib import Path


def test_restore_drill_preserves_fixed_paper_attempt_and_artifact_chain():
    data = json.loads(Path("var/acceptance/recovery/verdict.json").read_text())
    assert data["restored_to_isolated_environment"] is True
    assert data["paper_manifest_mismatches"] == 0
    assert data["response_manifest_mismatches"] == 0
    assert data["missing_artifacts"] == 0
    assert data["submission_count_delta"] == 0
    assert data["outbox_replayed_duplicates"] == 0
    assert data["rls_negative_tests_passed"] is True
```

```python
# tests/operations/assessment/test_release_evidence.py
import json
from pathlib import Path


def test_release_and_rollback_drill_use_same_schema_and_keep_history():
    data = json.loads(Path("var/acceptance/release/verdict.json").read_text())
    assert data["candidate_image_digest"].startswith("sha256:")
    assert data["previous_image_digest"].startswith("sha256:")
    assert data["rollback_completed"] is True
    assert data["database_downgrade_with_live_attempts"] is False
    assert data["pre_release_backup_verified"] is True
    assert data["post_rollback_smoke_passed"] is True
    assert data["historical_attempts_readable"] is True
```

```python
# tests/acceptance/assessment/test_launch_evidence.py
import json
from pathlib import Path

REQUIRED = {f"A{n}" for n in range(1, 36)} | {f"Q{n}" for n in range(1, 9)} | {
    "OBS", "PERF", "SEC", "A11Y", "PILOT", "RECOVERY", "ROLLBACK"}

def test_launch_gate_has_real_passing_evidence_for_every_requirement():
    data = json.loads(Path("var/acceptance/launch/verdict.json").read_text())
    assert set(data["requirements"]) == REQUIRED
    assert all(item["exit_code"] == 0 for item in data["requirements"].values())
    assert data["performance_claim"] is False
    assert data["go"] is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/operations/assessment/test_recovery_evidence.py tests/operations/assessment/test_release_evidence.py tests/acceptance/assessment/test_launch_evidence.py -q`
预期：FAIL，三份由真实演练/命令执行生成的证据文件不存在；不得创建静态成功 JSON 冒充通过。

- [ ] **步骤 3：编写最少实现代码**

**备份与恢复。** `assessment-backup.sh` 在同一维护窗口记录数据库 backup label、对象存储版本清单和当前 Release Context；数据库使用 `pg_dump --format=custom --no-owner --file="$BACKUP_DIR/assessment.dump" "$DATABASE_URL"`，对象使用 `mc mirror --preserve --versions "$MINIO_ALIAS/assessment" "$BACKUP_DIR/objects"`，随后对 dump、对象清单和 runbook 版本生成 SHA-256。生产密钥只从 `infra/.env.staging` 或 secret manager 注入。

恢复只能先到隔离环境，脚本签名固定为 `assessment-restore.sh BACKUP_DIR RESTORE_DATABASE_URL RESTORE_MINIO_ALIAS`。执行 `createdb`/schema 准备、`pg_restore --clean --if-exists --no-owner --dbname="$RESTORE_DATABASE_URL"`、`mc mirror --preserve` 和 `alembic upgrade head` 后，逐项核验 paper manifest、assignment_item、response/submission manifest、grade revision、artifact output hash、outbox 幂等键和 RLS。不得直接覆盖生产库验证备份。

```bash
#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR=$1
RESTORE_DATABASE_URL=$2
RESTORE_MINIO_ALIAS=$3
pg_restore --clean --if-exists --no-owner --dbname="$RESTORE_DATABASE_URL" "$BACKUP_DIR/assessment.dump"
mc mirror --preserve "$BACKUP_DIR/objects" "$RESTORE_MINIO_ALIAS/assessment-restored"
python -I tests/operations/assessment/verify_restore.py \
  --backup "$BACKUP_DIR" --database-url "$RESTORE_DATABASE_URL" \
  --output var/acceptance/recovery/verdict.json
pytest tests/security/assessment/test_rls.py -q
```

`assessment-recovery.md` 写明备份频率由学校保留政策冻结、每日备份完整性检查、每次候选发布前备份、隔离恢复演练、故障角色分工和证据保存位置。演练记录实际 RPO/RTO，但不将一次演练数字宣称为持续保证。

**发布与回滚。** `assessment-release.md` 固定顺序：冻结候选 commit 和镜像 digest → 检查迁移链 `20261105_110000_assessment_grading -> 20261105_120000_assessment_qti` → 验证备份 → 执行 expand 迁移 → 滚动部署 API/worker/web → 运行只读与合成写 smoke → 开放试点范围 → 观察告警 → 签署 go。回滚先关闭新 assignment 发布和 QTI 导入，再切回 `previous_image_digest`；有进行中 attempt 时不执行 destructive downgrade，保持向前兼容 schema，历史 attempt/submission/grade/render 必须可读。仅当候选迁移尚未对生产写入且课程负责人批准时，才按已测试的 down migration 回退。

```bash
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  preflight)
    test -n "$CANDIDATE_IMAGE_DIGEST"
    test -n "$PREVIOUS_IMAGE_DIGEST"
    alembic current
    alembic check
    pytest tests/security/assessment tests/integration/assessment -q
    ;;
  rollback)
    test -n "$PREVIOUS_IMAGE_DIGEST"
    docker compose -f infra/compose.staging.yml up -d \
      --no-deps api worker web
    python -I tests/operations/assessment/verify_rollback.py \
      --output var/acceptance/release/verdict.json
    ;;
  *)
    printf 'usage: %s preflight|rollback\n' "$0" >&2
    exit 2
    ;;
esac
```

部署清单通过 `ASSESSMENT_IMAGE="$PREVIOUS_IMAGE_DIGEST"` 选择回滚镜像；`verify_rollback.py` 必须比较回滚前后历史计数并执行学生安全投影、教师预览和 worker heartbeat smoke，不能只检查容器存活。

两个核验入口固定为 `verify_restore(backup_dir: Path, database_url: str) -> dict` 与 `verify_rollback(before_snapshot: Path, after_snapshot: Path) -> dict`。它们只通过隔离环境只读账号执行 SQL 计数和哈希查询，分别生成 `restored_to_isolated_environment=true` 与 `rollback_completed=true` 的实际检查 verdict；任一计数、哈希、RLS 或 smoke 不一致都退出非零，不能由命令行传入成功标志覆盖。

**总上线门禁。** `assessment-launch-gate.py` 用 `subprocess.run` 逐条执行命令并按真实退出码生成 verdict，命令覆盖阶段 A—D 回归、本计划 QTI、监控、设计负载、安全、可访问性、真实试点证据、恢复和回滚。任何缺文件、非零退出码、额外未知 requirement ID 或 `performance_claim=true` 都为 no-go。

```python
COMMANDS = {
  "A1-A24": ["pytest", "tests/unit/assessment", "tests/integration/assessment", "tests/e2e/assessment", "-q"],
  "Q1-Q8": ["pytest", "tests/unit/assessment/qti", "tests/integration/assessment/qti", "-q"],
  "OBS": ["promtool", "test", "rules", "tests/operations/assessment/alert_rules.test.yml"],
  "PERF": ["python", "-I", "tests/performance/assessment/verify_results.py", "--verify-existing"],
  "SEC": ["pytest", "tests/security/assessment", "-q"],
  "A25-A30": ["python", "-I", "tests/security/assessment/assert_scan_verdicts.py", "--directory", "var/acceptance/security"],
  "A31-A34": ["npm", "--prefix", "apps/web", "run", "test:a11y"],
  "PILOT": ["pytest", "tests/acceptance/assessment/test_pilot_evidence.py", "-q"],
  "RECOVERY": ["pytest", "tests/operations/assessment/test_recovery_evidence.py", "-q"],
  "ROLLBACK": ["pytest", "tests/operations/assessment/test_release_evidence.py", "-q"],
}
```

脚本把组命令展开到 A1—A35、Q1—Q8 的逐项映射；展开表必须使用下一节矩阵，不能仅因一个组命令成功就丢弃单项映射。`go` 只在所有项退出 0、真实试点签署齐全、扫描无未豁免高危、恢复与回滚均为真实演练时设为 true。

- [ ] **步骤 4：运行测试验证通过**

```bash
bash -n scripts/operations/assessment-backup.sh scripts/operations/assessment-restore.sh \
  scripts/operations/assessment-release.sh
set -a; source infra/.env.staging; set +a
bash scripts/operations/assessment-backup.sh
bash scripts/operations/assessment-restore.sh "$BACKUP_DIR" "$RESTORE_DATABASE_URL" "$RESTORE_MINIO_ALIAS"
bash scripts/operations/assessment-release.sh preflight
ASSESSMENT_IMAGE="$PREVIOUS_IMAGE_DIGEST" bash scripts/operations/assessment-release.sh rollback
python -I scripts/operations/assessment-launch-gate.py --execute --output var/acceptance/launch/verdict.json
pytest tests/operations/assessment/test_recovery_evidence.py \
  tests/operations/assessment/test_release_evidence.py \
  tests/acceptance/assessment/test_launch_evidence.py -q
```

预期：全部退出 0，最终 verdict 为 go；恢复后 paper/response/artifact 哈希一致，回滚不破坏进行中作答和历史证据。若任一项失败，发布结论必须是 no-go，并按 `assessment-release.md` 回滚，不允许删除失败证据。

- [ ] **步骤 5：Commit**

```bash
git add scripts/operations docs/operations/assessment-recovery.md \
  docs/operations/assessment-release.md tests/operations/assessment \
  tests/acceptance/assessment/test_launch_evidence.py
git commit -m "test: add assessment recovery rollback and launch gates"
```

## 3. 设计第 18 节与 QTI 验收映射

| 验收项 | 执行证据与负责任务 |
|---|---|
| A1—A9 组卷正确性 | 阶段 A/B 单元、集成、回放和 A/B fixture；任务 10 全量重跑 |
| A10—A15 版本与治理 | 阶段 A/C 不可变、发布、撤回、贡献者复核测试；任务 10 全量重跑 |
| A16—A24 在线作答与评分 | 阶段 C/D 幂等、冲突、到期、策略、人工成绩测试；任务 6 并发验证、任务 10 全量重跑 |
| A25 学生各表面无答案 | 任务 7 的 API/HTML/JS/SSE/PDF/日志 sentinel 测试 |
| A26 篡改资源 ID | 任务 7 的 paper/assignment/attempt/slot/artifact 负向测试 |
| A27 曝光冲突 | 任务 7 的练习答案到保密正式卷阻断测试 |
| A28 Artifact 重新授权 | 任务 7 的过期 URL、旧练习 Artifact、重开页面测试 |
| A29 内容注入与 worker 隔离 | 任务 7 的 XSS、恶意附件和题干指令测试 |
| A30 数据库 FORCE RLS | 任务 7 非 owner/非 superuser 负向测试 |
| A31 教师不用拖拽 | 任务 8 全键盘教师路径 |
| A32 学生键盘与屏幕阅读器 | 任务 8 Playwright、NVDA、VoiceOver |
| A33 状态不只靠颜色 | 任务 8 保存、冲突、未答、无效输入文本断言 |
| A34 浏览器与打印完整 | 任务 6 长公式 fixture 和任务 8 三浏览器/PDF 检查 |
| A35 真实独立试点 | 任务 9 的真实教师、学生、独立复核和签署 |
| Q1 固定 Test/Section/Item 导入 | 任务 2 的 A1/A2 顺序 fixture |
| Q2 导入一律新草稿 | 任务 2/3 的 `draft/unreviewed` 与无治理记录断言 |
| Q3 不识别语义差异报告 | 任务 1—3 的 responseProcessing/CAS/rubric/extension/math 用例 |
| Q4 导出保持槽位和顺序 | 任务 3 无 selection、shuffle=false、sidecar 顺序断言 |
| Q5 不继承外部审核/成绩/发布 | 任务 2/3 外部 approved 与 assessmentResult 测试 |
| Q6 QTI 不绕过 Release/Review | 任务 3 的撤回、未审、跨课程、权限和提交审核门禁 |
| Q7 恶意 QTI 包安全 | 任务 2 ZIP/DTD 与任务 7 XXE/XSS/路径测试 |
| Q8 导出审计与答案分权 | 任务 3 幂等 exchange、output hash、include_answers 权限测试 |

总门禁额外覆盖 OBS、PERF、SEC、A11Y、PILOT、RECOVERY、ROLLBACK。性能证据只证明指定设计负载下的功能正确性和记录当次实测，不证明未测硬件上的吞吐或延时。

## 4. 实施顺序与独立交付

任务 1 → 2 → 3 → 4 构成可独立停止的 QTI 垂直链路；任务 5—8 可分别交付监控、负载、安全与可访问性；任务 9 必须等功能链路可用后在真实课程执行；任务 10 最后执行真实恢复、回滚和总门禁。每个任务只提交本任务列出的文件，失败时保留证据并回到对应任务修复，不在总门禁任务中夹带业务实现。

## 5. 计划自检

- **规格覆盖：** 第 13.2 节 QTI 边界对应任务 1—4；第 17 节指标、告警、降级和设计负载对应任务 5—6；第 18 节 A1—A35 对应上表与任务 10；第 19 节阶段 E 的 QTI、跨课程许可校验和真实数据积累对应任务 3、5、9；第 20 节未来触发器未被提前实现。
- **治理一致：** 导入只产生草稿，导出先通过 Release/Review，QTI 包不成为审核、Release、成绩或曝光真源；所有写 API 有身份、范围与幂等约束。
- **类型一致：** `QtiService.import_package`、`QtiService.export_paper`、`PaperService`、`ReleaseGovernancePort`、`ReviewGovernancePort` 及 DTO 均在任务 1 或对应任务中给出签名，前后使用同一 `QtiImportResult/QtiExportResult`。
- **迁移一致：** 唯一 QTI 迁移为 `20261105_120000_assessment_qti`，`down_revision=20261105_110000_assessment_grading`，包含真实表结构和 upgrade/downgrade 测试，没有空迁移。
- **证据诚实：** 试点、恢复、回滚与总门禁都由真实执行生成证据；所有负载和告警数字均标为设计验收或演练触发条件，不冒充当前性能或生产 SLO。
- **范围闭合：** 不包含 IRT、运行时随机正式卷、在线监考，也不以 QTI 导入导出暗示这些能力已经存在。

