"""独立整卷校验器（任务 5）：只从已落库事实重算，给出可哈希的确定性结论。

## 为什么必须是"独立"的

计划书要求同一个校验器同时服务三处：草稿自检、提交审核前的门禁、任务 7 的双审证据。
如果它复用 `PaperService.create_manual_draft` / `edit_draft` 在内存里的中间对象，就只是
"把建卷算法自己的答案再抄一遍"——有人绕过服务直接改库（改分值、改来源、改
`manifest_hash`）时，校验器会拿着旧对象说"一切正常"。

所以本模块的输入只有四类**持久化事实**：

  1. `assessment.paper_version / paper_section / paper_slot` 的当前行（含库里的
     `manifest_hash` 与各层 `content_hash`）；
  2. `assessment.paper_blueprint_version.blueprint_ast`（蓝图版本内容）；
  3. `assessment.candidate_snapshot.candidate_manifest`（建卷时固定的候选清单）；
  4. 此刻的 Release Context 与 `assessment.item_exposure_event`（经
     `CandidateService.preview` 只读重算，不写候选快照）。

校验器不调用 `PaperService`，不读建卷时的返回值，也不缓存上一次结论。

## 确定性（`report_hash`）

`ValidationResult.issues` 用固定键排序：先 error 后 warning，再按分节在卷面上的顺序、
槽位号、原因码、消息。`report_hash = sha256(canonical_json(report_payload(result)))`，
payload 只含"结论内容"：schema、paper/blueprint/snapshot/release 身份、库里的 manifest
哈希、重算出的 manifest 哈希、valid、计数与有序 issues。**不含任何时间字段**：同一份
卷子在任何时刻校验，只要结论不变，哈希就不变；曝光事件到可见时刻后结论变了，哈希
自然改变。`paper_status` 也不参与哈希——draft→approved 不是内容变化，不应让双审证据
对不上，但它仍出现在响应里供界面使用。

## 只读

仓储端口只有 `load`；PostgreSQL 实现只有 SELECT，全部跑在
`db.transaction(assessment_service=True)` 里（paper_* 是 FORCE RLS）。本模块不写
`governance.quality_check`（本仓库没有该 schema），也不需要新表：报告由响应返回，
需要留证时把 `report_hash` 写进既有的审核/作业证据即可。

## 严重级别

`severity="error"` 让 `valid=False`，阻断提交审核/发布；`severity="warning"` 不阻断。
warning 只用于"数据未知"或"允许教师调整"的偏离（例如缺预计时长、槽位分与蓝图目标
不同），绝不把硬约束降级成提示（规格 §6.3、§8.1）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol, Sequence
from uuid import UUID

from . import db
from .blueprints import BlueprintVersionNotFoundError
from .candidates import (
    CandidatePreview,
    CandidateScope,
    CandidateService,
    CandidateSnapshot,
    CandidateSnapshotNotFoundError,
    ReleaseScopeInfo,
    manifest_hash,
    release_scope_reasons,
    snapshot_from_manifest,
)
from .contracts import (
    BlueprintContent,
    CandidateRecord,
    PaperSectionView,
    PaperSlotView,
    PaperView,
    RejectedCandidate,
    ValidationIssue,
    ValidationResult,
    canonical_hash,
)
from .exposure import ExposureConflict, ExposureService
from .papers import (
    PAPER_READ_ACTION,
    PaperNotFoundError,
    paper_manifest,
    section_content_hash,
    slot_content_hash,
)
from .releases import utcnow

VALIDATION_SCHEMA = "paper-validation-report/v1"

# 已填槽位必须齐全的六项固定来源。`exercise_instance_id` 不在此列：静态题本来就没有
# 实例（0027 的 CHECK 也允许 instance 为空）；参数化题由候选的 `parameterized` 资格
# 门禁负责，校验器不把"静态题没有实例"误报成来源缺失。
REQUIRED_SOURCE_FIELDS = (
    "exercise_id",
    "exercise_version_id",
    "exercise_family_id",
    "source_release_id",
    "release_context_id",
    "dependency_manifest_hash",
)
# 空槽位允许保留 release_context_id 占位（建卷路径就是这么写的），但只要出现下面
# 任何一项，就说明它不是"显式缺口"而是"只有题号的半填槽位"。
_PARTIAL_SLOT_FIELDS = (
    "exercise_id",
    "exercise_family_id",
    "source_release_id",
    "dependency_manifest_hash",
)
_IDENTITY_FIELDS = (
    "exercise_id",
    "exercise_instance_id",
    "exercise_family_id",
    "source_release_id",
    "release_context_id",
    "dependency_manifest_hash",
)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """校验域错误基类。"""


class ValidationInputError(ValidationError, ValueError):
    """校验输入本身不成立（缺课程文本号、快照/蓝图配置错误）。路由映射 422。"""


class PaperValidationBlockedError(ValidationError):
    """提交审核/发布门禁：卷子有 error 级 issue。携带完整报告，不只是一个字符串。"""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__(
            f"整卷校验未通过：{result.error_count} 项硬约束错误、"
            f"{result.warning_count} 项提示"
        )
        self.result = result


# ---------------------------------------------------------------------------
# 输入与仓储端口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationSubject:
    """一次校验的持久化输入（纸卷行 + 蓝图内容 + 固定候选快照 + 文本范围号）。

    `blueprint_content_hash` 是库里存储的值，校验器会用 `canonical_hash(blueprint)`
    重新算一遍再比较：蓝图 AST 被直接改库时，这里能发现。
    """

    paper: PaperView
    blueprint: BlueprintContent
    blueprint_content_hash: str
    snapshot: CandidateSnapshot
    course_ref: str | None = None
    offering_ref: str | None = None


class PaperValidationUnitOfWork(Protocol):
    def load(self, paper_version_id: UUID) -> ValidationSubject | None: ...


class PaperValidationRepository(Protocol):
    def unit_of_work(self) -> ContextManager[PaperValidationUnitOfWork]: ...


class InMemoryPaperValidationRepository:
    """单元测试/无库环境的只读仓储；`subject` 可替换以模拟"直接改库"。"""

    def __init__(self, subject: ValidationSubject | None = None) -> None:
        self.subject = subject

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryPaperValidationRepository"]:
        yield self

    def load(self, paper_version_id: UUID) -> ValidationSubject | None:
        if self.subject is not None and self.subject.paper.id == paper_version_id:
            return self.subject
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


class PostgresPaperValidationRepository:
    """只读 PostgreSQL 仓储：每条 SELECT 都在 assessment 服务作用域事务里。"""

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresValidationUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresValidationUnitOfWork(cur)


class _PostgresValidationUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def load(self, paper_version_id: UUID) -> ValidationSubject | None:
        self.cur.execute(
            """SELECT id, blueprint_version_id, candidate_snapshot_id,
                      course_id, offering_id, release_context_id, paper_kind,
                      total_score, duration_seconds, status, edit_seq,
                      manifest_hash, review_manifest_hash
                 FROM assessment.paper_version
                WHERE id = %s""",
            (paper_version_id,),
        )
        row = self.cur.fetchone()
        if row is None:
            return None
        sections = self._sections(paper_version_id)
        paper = PaperView(
            id=row["id"],
            blueprint_version_id=row["blueprint_version_id"],
            release_context_id=row["release_context_id"],
            course_id=row["course_id"],
            offering_id=row.get("offering_id"),
            purpose=row["paper_kind"],
            paper_kind=row["paper_kind"],
            status=row["status"],
            total_score=row["total_score"],
            duration_seconds=row["duration_seconds"],
            edit_seq=row["edit_seq"],
            manifest_hash=row["manifest_hash"],
            review_manifest_hash=row.get("review_manifest_hash"),
            sections=sections,
        )
        blueprint, blueprint_hash = self._blueprint(row["blueprint_version_id"])
        snapshot = self._snapshot(row["candidate_snapshot_id"])
        course_ref, offering_ref = self._refs(
            row["course_id"], row.get("offering_id"))
        return ValidationSubject(
            paper=paper,
            blueprint=blueprint,
            blueprint_content_hash=blueprint_hash,
            snapshot=snapshot,
            course_ref=course_ref,
            offering_ref=offering_ref,
        )

    def _sections(self, paper_version_id: UUID) -> tuple[PaperSectionView, ...]:
        self.cur.execute(
            """SELECT s.id AS section_id, s.code AS section_code, s.title,
                      s.instruction, s.sort_order, s.section_score,
                      s.content_hash AS section_content_hash,
                      sl.id AS slot_id, sl.code AS slot_code, sl.slot_no,
                      sl.exercise_id, sl.exercise_version_id,
                      sl.exercise_instance_id, sl.exercise_family_id,
                      sl.source_release_id, sl.release_context_id, sl.score,
                      sl.estimated_seconds, sl.display_config,
                      sl.dependency_manifest_hash,
                      sl.content_hash AS slot_content_hash
                 FROM assessment.paper_section s
                 LEFT JOIN assessment.paper_slot sl ON sl.section_id = s.id
                WHERE s.paper_version_id = %s
                ORDER BY s.sort_order, s.code, sl.slot_no""",
            (paper_version_id,),
        )
        sections: dict[UUID, dict[str, Any]] = {}
        for record in self.cur.fetchall():
            section = sections.get(record["section_id"])
            if section is None:
                section = {
                    "code": record["section_code"],
                    "title": record["title"],
                    "instruction": record["instruction"],
                    "sort_order": record["sort_order"],
                    "section_score": record["section_score"],
                    "content_hash": record["section_content_hash"],
                    "slots": [],
                }
                sections[record["section_id"]] = section
            if record["slot_id"] is None:
                continue
            section["slots"].append(PaperSlotView(
                id=record["slot_id"],
                section_code=record["section_code"],
                code=record["slot_code"],
                slot_no=record["slot_no"],
                exercise_id=record.get("exercise_id"),
                exercise_version_id=record.get("exercise_version_id"),
                exercise_instance_id=record.get("exercise_instance_id"),
                exercise_family_id=record.get("exercise_family_id"),
                source_release_id=record.get("source_release_id"),
                release_context_id=record.get("release_context_id"),
                score=record["score"],
                estimated_seconds=record.get("estimated_seconds"),
                display_config=_as_dict(record.get("display_config")),
                dependency_manifest_hash=record.get("dependency_manifest_hash"),
                content_hash=record["slot_content_hash"],
            ))
        return tuple(
            PaperSectionView(
                code=section["code"],
                title=section["title"],
                instruction=section["instruction"],
                sort_order=section["sort_order"],
                section_score=section["section_score"],
                slots=tuple(section["slots"]),
                content_hash=section["content_hash"],
            )
            for section in sections.values()
        )

    def _blueprint(self, blueprint_version_id: UUID) -> tuple[BlueprintContent, str]:
        self.cur.execute(
            """SELECT blueprint_ast, content_hash
                 FROM assessment.paper_blueprint_version
                WHERE id = %s""",
            (blueprint_version_id,),
        )
        row = self.cur.fetchone()
        if row is None:
            raise BlueprintVersionNotFoundError(str(blueprint_version_id))
        return BlueprintContent.model_validate(_as_dict(row["blueprint_ast"])), row["content_hash"]

    def _snapshot(self, snapshot_id: UUID) -> CandidateSnapshot:
        self.cur.execute(
            """SELECT id, candidate_manifest, candidate_manifest_hash, created_at
                 FROM assessment.candidate_snapshot
                WHERE id = %s""",
            (snapshot_id,),
        )
        row = self.cur.fetchone()
        if row is None:
            raise CandidateSnapshotNotFoundError(str(snapshot_id))
        return snapshot_from_manifest(
            _as_dict(row["candidate_manifest"]),
            id=row["id"],
            manifest_hash=row["candidate_manifest_hash"],
            created_at=row["created_at"],
        )

    def _refs(self, course_id: UUID,
              offering_id: UUID | None) -> tuple[str | None, str | None]:
        self.cur.execute(
            """SELECT subject, kind, ref
                 FROM assessment.id_ref
                WHERE (subject = %s AND kind = 'course')
                   OR (subject = %s AND kind = 'offering')""",
            (course_id, offering_id),
        )
        refs = {(record["subject"], record["kind"]): record["ref"]
                for record in self.cur.fetchall()}
        return (
            refs.get((course_id, "course")),
            refs.get((offering_id, "offering")) if offering_id is not None else None,
        )


# ---------------------------------------------------------------------------
# 规范化辅助
# ---------------------------------------------------------------------------

def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _delta(expected: Decimal, actual: Decimal) -> str:
    """缺口 = 目标 - 实际；正数表示少了几分，负数表示多了。Decimal 精确相减。"""
    return f"{expected - actual:.2f}"


def report_payload(result: ValidationResult) -> dict[str, Any]:
    """`report_hash` 的规范化输入；不含时间与 paper_status（见模块 docstring）。"""
    return {
        "schema": result.schema_version,
        "paper_version_id": result.paper_version_id,
        "blueprint_version_id": result.blueprint_version_id,
        "candidate_snapshot_id": result.candidate_snapshot_id,
        "release_context_id": result.release_context_id,
        "manifest_hash": result.manifest_hash,
        "recomputed_manifest_hash": result.recomputed_manifest_hash,
        "valid": result.valid,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "issues": [issue.model_dump(mode="json") for issue in result.issues],
    }


# ---------------------------------------------------------------------------
# 纯校验器
# ---------------------------------------------------------------------------

class PaperValidationService:
    """`validate` / `assert_valid` 是整卷校验的唯一入口（计划书 §0.3 规则 8）。"""

    def __init__(
        self,
        *,
        repository: PaperValidationRepository | None = None,
        candidates: CandidateService | None = None,
        authorizer: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self.candidates = candidates
        self.authorizer = authorizer
        self._now = clock

    # -- 应用服务 -----------------------------------------------------------
    def validate(self, *, actor_id: UUID, paper_version_id: UUID) -> ValidationResult:
        """重新读库、重新判候选、重新算 manifest，返回确定性报告。"""
        if self.repository is None:
            raise ValidationError("缺少校验仓储：拒绝无持久化事实的校验")
        with self.repository.unit_of_work() as uow:
            subject = uow.load(paper_version_id)
        if subject is None:
            raise PaperNotFoundError(str(paper_version_id))
        self._authorize(actor_id, subject)
        if self.candidates is None:
            raise ValidationError("缺少候选服务：无法获得当前资格与曝光事实")
        if not subject.course_ref:
            raise ValidationInputError("蓝图版本缺少 course_ref，无法做课程范围校验")
        scope = CandidateScope(
            blueprint_version_id=subject.paper.blueprint_version_id,
            purpose=subject.paper.purpose,
            course_id=subject.paper.course_id,
            course_ref=subject.course_ref,
            offering_id=subject.paper.offering_id,
            offering_ref=subject.offering_ref,
            release_context_id=subject.paper.release_context_id,
        )
        preview = self.candidates.preview(actor_id=actor_id, scope=scope)
        return self.evaluate(subject, preview=preview, now=self._now())

    def assert_valid(self, *, actor_id: UUID, paper_version_id: UUID) -> ValidationResult:
        """提交审核/发布门禁：零 error 才放行；否则抛出携带完整报告的异常。"""
        result = self.validate(actor_id=actor_id, paper_version_id=paper_version_id)
        if not result.valid:
            raise PaperValidationBlockedError(result)
        return result

    def _authorize(self, actor_id: UUID, subject: ValidationSubject) -> None:
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的整卷校验")
        if not subject.course_ref:
            raise ValidationInputError("蓝图版本缺少 course_ref，无法做课程范围校验")
        self.authorizer.require(
            actor_id, PAPER_READ_ACTION,
            course_id=subject.paper.course_id,
            course_ref=subject.course_ref,
            offering_id=subject.paper.offering_id,
            offering_ref=subject.offering_ref,
        )

    # -- 纯校验（无 IO；单元测试直接调它） ---------------------------------
    @staticmethod
    def evaluate(
        subject: ValidationSubject,
        *,
        preview: CandidatePreview,
        now: datetime,
    ) -> ValidationResult:
        paper = subject.paper
        blueprint = subject.blueprint
        snapshot = subject.snapshot
        issues: list[ValidationIssue] = []

        def add(code: str, message: str, *, severity: str = "error",
                slot: PaperSlotView | None = None, section_code: str | None = None,
                detail: Mapping[str, str | int | list[str] | None] | None = None
                ) -> None:
            issues.append(ValidationIssue(
                code=code,
                message=message,
                severity=severity,  # type: ignore[arg-type]
                slot_id=None if slot is None else slot.id,
                slot_no=None if slot is None else slot.slot_no,
                section_code=(
                    section_code if section_code is not None
                    else (None if slot is None else slot.section_code)
                ),
                detail=dict(detail or {}),
            ))

        # 0) 身份：卷、蓝图、快照必须指向同一件事。
        _check_identity(subject, add)

        # 1) 从槽位原始字段重算每一层内容哈希与分节分。
        recomputed_sections, section_sums = _recomputed_sections(paper, add)
        recomputed_paper = paper.model_copy(update={"sections": recomputed_sections})
        recomputed_manifest = paper_manifest(recomputed_paper)

        # 2) 分值：分节、全卷、蓝图目标，全部 Decimal 精确比较。
        _check_score_totals(paper, blueprint, recomputed_sections, section_sums, add)

        # 3) 结构与覆盖：缺槽、空节、题型/概念/目标/必选/排除。
        current_by_version, rejected_by_version = _current_maps(preview)
        _check_structure_and_coverage(
            paper, blueprint, snapshot, current_by_version, rejected_by_version,
            preview.release, add,
        )

        # 4) manifest 防篡改：重算值必须等于库里的值。
        if paper.manifest_hash != recomputed_manifest:
            add(
                "manifest_hash_mismatch",
                f"库里的 manifest_hash 与重算不一致：库 {paper.manifest_hash[:12]}…，"
                f"重算 {recomputed_manifest[:12]}…（有人绕过服务直接改库？）",
                detail={
                    "stored": paper.manifest_hash,
                    "recomputed": recomputed_manifest,
                },
            )

        # 5) 当前 Release 范围（正式卷必须冻结、范围必须一致）。
        _check_release(subject, preview, add)

        # 6) 时长：缺失不能按 0 参与；只有全部已知时才判断上限。
        _check_duration(blueprint, paper, paper.sections, add)

        # 7) 硬约束分组：共享材料 / 相互泄题。
        _check_constraint_groups(blueprint, paper, add)

        # 8) 曝光冲突：复用 ExposureService，绝不另写一套。
        _check_exposure(
            paper, preview, snapshot, current_by_version, now, add)

        # 9) 软目标不参与硬约束，也不许混成绿色通过。
        if blueprint.soft_objectives:
            add(
                "soft_objectives_not_evaluated",
                "软目标不参与硬约束判定；本次 valid 只代表硬约束通过。"
                f"未评估：{','.join(sorted(str(key) for key in blueprint.soft_objectives))}",
                severity="warning",
                detail={"objectives": ",".join(
                    sorted(str(key) for key in blueprint.soft_objectives))},
            )

        ordered = _order_issues(issues, paper)
        error_count = sum(1 for item in ordered if item.severity == "error")
        result = ValidationResult(
            schema_version=VALIDATION_SCHEMA,
            paper_version_id=paper.id,
            paper_status=paper.status,
            blueprint_version_id=paper.blueprint_version_id,
            candidate_snapshot_id=snapshot.id,
            release_context_id=paper.release_context_id,
            manifest_hash=paper.manifest_hash,
            recomputed_manifest_hash=recomputed_manifest,
            valid=error_count == 0,
            error_count=error_count,
            warning_count=len(ordered) - error_count,
            issues=ordered,
            report_hash="0" * 64,
        )
        return result.model_copy(
            update={"report_hash": canonical_hash(report_payload(result))}
        )


# ---------------------------------------------------------------------------
# 各检查段
# ---------------------------------------------------------------------------

def _check_identity(subject: ValidationSubject, add: Any) -> None:
    paper = subject.paper
    blueprint = subject.blueprint
    snapshot = subject.snapshot
    if subject.blueprint_content_hash != canonical_hash(blueprint):
        add(
            "blueprint_content_hash_mismatch",
            "蓝图版本内容哈希与 blueprint_ast 重算不一致：蓝图内容被直接改库",
            detail={
                "stored": subject.blueprint_content_hash,
                "recomputed": canonical_hash(blueprint),
            },
        )
    if paper.release_context_id != blueprint.release_context_id:
        add(
            "blueprint_release_context_mismatch",
            "纸卷的 Release Context 与蓝图版本不一致",
            detail={
                "paper": str(paper.release_context_id),
                "blueprint": str(blueprint.release_context_id),
            },
        )
    if paper.purpose != blueprint.purpose:
        add(
            "blueprint_purpose_mismatch",
            f"纸卷用途 {paper.purpose} 与蓝图用途 {blueprint.purpose} 不一致",
            detail={"paper": paper.purpose, "blueprint": blueprint.purpose},
        )
    scope_fields = [
        name for name, left, right in (
            ("course_id", paper.course_id, blueprint.course_id),
            ("offering_id", paper.offering_id, blueprint.offering_id),
        )
        if left != right
    ]
    if scope_fields:
        add(
            "blueprint_scope_mismatch",
            f"纸卷的课程/班级范围与蓝图不一致：{','.join(scope_fields)}",
            detail={"fields": ",".join(scope_fields)},
        )

    snapshot_fields = [
        name for name, left, right in (
            ("blueprint_version_id", snapshot.blueprint_version_id, paper.blueprint_version_id),
            ("release_context_id", snapshot.release_context_id, paper.release_context_id),
            ("purpose", snapshot.purpose, paper.purpose),
            ("course_id", snapshot.course_id, paper.course_id),
            ("offering_id", snapshot.offering_id, paper.offering_id),
        )
        if left != right
    ]
    if snapshot_fields:
        add(
            "candidate_snapshot_scope_mismatch",
            f"固定的候选快照不属于本卷范围：{','.join(snapshot_fields)}",
            detail={"fields": ",".join(snapshot_fields)},
        )
    # 内存夹具可能没有 manifest 原文；DB 行一定有。没有原文就不假装校验过。
    if snapshot.manifest and snapshot.candidate_manifest_hash != manifest_hash(snapshot.manifest):
        add(
            "candidate_snapshot_hash_mismatch",
            "候选快照清单哈希与 candidate_manifest 重算不一致",
            detail={
                "stored": snapshot.candidate_manifest_hash,
                "recomputed": manifest_hash(snapshot.manifest),
            },
        )


def _recomputed_sections(
    paper: PaperView, add: Any,
) -> tuple[tuple[PaperSectionView, ...], dict[str, Decimal]]:
    """从槽位字段重算 content_hash / section_score / section content_hash。

    这一步是"防绕过服务直接改库"的核心：manifest 里每一层哈希都要能由原始字段
    独立算回来，否则攻击者只要同时改字段和旧哈希就能骗过审核。
    """
    sections: list[PaperSectionView] = []
    section_sums: dict[str, Decimal] = {}
    for section in paper.sections:
        slots: list[PaperSlotView] = []
        for slot in section.slots:
            recomputed = slot_content_hash(slot)
            if slot.content_hash != recomputed:
                add(
                    "slot_content_hash_mismatch",
                    f"槽位 {slot.code} 的 content_hash 与槽位字段重算不一致",
                    slot=slot,
                    detail={
                        "stored": slot.content_hash or "",
                        "recomputed": recomputed,
                    },
                )
            slots.append(slot.model_copy(update={"content_hash": recomputed}))
        section_total = sum((item.score for item in slots), Decimal("0.00"))
        section_sums[section.code] = section_total
        rebuilt = section.model_copy(update={
            "slots": tuple(slots),
            "section_score": section_total,
        })
        recomputed_section_hash = section_content_hash(rebuilt)
        if section.content_hash != recomputed_section_hash:
            add(
                "section_content_hash_mismatch",
                f"分节 {section.code} 的 content_hash 与分节字段重算不一致",
                section_code=section.code,
                detail={
                    "stored": section.content_hash or "",
                    "recomputed": recomputed_section_hash,
                },
            )
        sections.append(rebuilt.model_copy(
            update={"content_hash": recomputed_section_hash}))
    return tuple(sections), section_sums


def _check_score_totals(
    paper: PaperView,
    blueprint: BlueprintContent,
    recomputed_sections: Sequence[PaperSectionView],
    section_sums: Mapping[str, Decimal],
    add: Any,
) -> None:
    blueprint_sections = {section.code: section for section in blueprint.sections}
    for section in paper.sections:
        actual = section_sums[section.code]
        if section.section_score != actual:
            add(
                "section_score_mismatch",
                f"分节 {section.code} 的 section_score={_fmt(section.section_score)}，"
                f"但槽位合计={_fmt(actual)}（精确比较，无容差）",
                section_code=section.code,
                detail={
                    "expected": _fmt(actual),
                    "actual": _fmt(section.section_score),
                    "delta": _delta(actual, section.section_score),
                },
            )
        target_section = blueprint_sections.get(section.code)
        if target_section is not None:
            target = sum(
                (slot.score for slot in target_section.slots), Decimal("0.00"))
            if actual != target:
                add(
                    "blueprint_section_score_mismatch",
                    f"分节 {section.code} 蓝图目标 {_fmt(target)}，"
                    f"实际 {_fmt(actual)}，差 {_delta(target, actual)} 分",
                    section_code=section.code,
                    detail={
                        "expected": _fmt(target),
                        "actual": _fmt(actual),
                        "delta": _delta(target, actual),
                    },
                )
        if not section.slots:
            add(
                "empty_section",
                f"分节 {section.code} 没有任何槽位",
                section_code=section.code,
            )

    total = sum(
        (slot.score for section in recomputed_sections for slot in section.slots),
        Decimal("0.00"),
    )
    if total != paper.total_score:
        add(
            "total_score_mismatch",
            f"槽位合计 {_fmt(total)}，卷面总分 {_fmt(paper.total_score)}，"
            f"差 {_delta(paper.total_score, total)} 分（Decimal 精确比较，无容差）",
            detail={
                "expected": _fmt(paper.total_score),
                "actual": _fmt(total),
                "delta": _delta(paper.total_score, total),
            },
        )
    if paper.total_score != blueprint.total_score:
        add(
            "blueprint_total_score_mismatch",
            f"卷面总分 {_fmt(paper.total_score)} 与蓝图目标分 "
            f"{_fmt(blueprint.total_score)} 不一致",
            detail={
                "expected": _fmt(blueprint.total_score),
                "actual": _fmt(paper.total_score),
                "delta": _delta(blueprint.total_score, paper.total_score),
            },
        )


def _check_structure_and_coverage(
    paper: PaperView,
    blueprint: BlueprintContent,
    snapshot: CandidateSnapshot,
    current_by_version: Mapping[UUID, CandidateRecord],
    rejected_by_version: Mapping[UUID, RejectedCandidate],
    release: ReleaseScopeInfo | None,
    add: Any,
) -> None:
    blueprint_sections = {section.code: section for section in blueprint.sections}
    blueprint_slots = {
        slot.code: (section.code, slot)
        for section in blueprint.sections for slot in section.slots
    }
    paper_sections = {section.code: section for section in paper.sections}
    paper_slots = {slot.code: slot for section in paper.sections for slot in section.slots}
    all_slots = [slot for section in paper.sections for slot in section.slots]
    _check_duplicates(all_slots, add)

    for code in blueprint_sections:
        if code not in paper_sections:
            target = sum((slot.score for slot in blueprint_sections[code].slots),
                         Decimal("0.00"))
            add(
                "section_missing",
                f"蓝图分节 {code} 在卷面上不存在，缺 {_fmt(target)} 分",
                section_code=code,
                detail={"missing_score": _fmt(target)},
            )
    for code in paper_sections:
        if code not in blueprint_sections:
            add(
                "section_not_in_blueprint",
                f"卷面分节 {code} 不在蓝图版本里",
                section_code=code,
            )
    for code, (section_code, spec) in blueprint_slots.items():
        if code not in paper_slots:
            add(
                "blueprint_slot_missing",
                f"蓝图槽位 {code} 在卷面上不存在，缺 {_fmt(spec.score)} 分",
                section_code=section_code,
                detail={"missing_score": _fmt(spec.score)},
            )

    snapshot_by_version = {
        record.evidence.exercise_version_id: record for record in snapshot.accepted
    }
    selected: set[UUID] = set()
    for slot in all_slots:
        spec_entry = blueprint_slots.get(slot.code)
        spec = spec_entry[1] if spec_entry else None
        if spec_entry is None:
            add("slot_not_in_blueprint", f"卷面槽位 {slot.code} 不在蓝图版本里", slot=slot)
        elif slot.score != spec.score:
            # 分节内重新分配分值是教师权限（§8.2），所以这里只是偏离提示；
            # 分节总额与全卷总额仍然是硬约束（见 _check_score_totals）。
            add(
                "blueprint_slot_score_mismatch",
                f"槽位 {slot.code} 蓝图目标 {_fmt(spec.score)}，"
                f"实际 {_fmt(slot.score)}，差 {_delta(spec.score, slot.score)} 分",
                severity="warning",
                slot=slot,
                detail={
                    "expected": _fmt(spec.score),
                    "actual": _fmt(slot.score),
                    "delta": _delta(spec.score, slot.score),
                },
            )

        if slot.exercise_version_id is None:
            add(
                "slot_unfilled",
                f"槽位 {slot.code} 还没有选题"
                + (f"（缺 {_fmt(spec.score)} 分）" if spec else ""),
                slot=slot,
                detail={"missing_score": _fmt(spec.score if spec else slot.score)},
            )
            stray = [
                name for name in _PARTIAL_SLOT_FIELDS
                if getattr(slot, name, None) not in (None, "")
            ]
            if stray:
                add(
                    "slot_partially_filled",
                    f"空槽位 {slot.code} 残留了来源字段：{','.join(stray)}",
                    slot=slot,
                    detail={"fields": ",".join(stray)},
                )
            continue

        selected.add(slot.exercise_version_id)
        missing = [
            name for name in REQUIRED_SOURCE_FIELDS
            if getattr(slot, name, None) in (None, "")
        ]
        if missing:
            add(
                "source_incomplete",
                f"槽位 {slot.code} 缺少固定来源字段：{','.join(missing)}",
                slot=slot,
                detail={"missing": ",".join(missing)},
            )
        if slot.release_context_id not in (None, paper.release_context_id):
            add(
                "release_context_mismatch",
                f"槽位 {slot.code} 的 Release Context 与整卷不一致",
                slot=slot,
                detail={
                    "slot": str(slot.release_context_id),
                    "paper": str(paper.release_context_id),
                },
            )

        pinned = snapshot_by_version.get(slot.exercise_version_id)
        if pinned is None:
            add(
                "candidate_not_in_snapshot",
                "槽位题目版本不在建卷时固定的候选快照里（替换必须来自同一快照）",
                slot=slot,
                detail={"exercise_version_id": str(slot.exercise_version_id)},
            )
        else:
            differing = [
                name for name in _IDENTITY_FIELDS
                if getattr(slot, name, None) != getattr(pinned.evidence, name, None)
            ]
            if differing:
                add(
                    "candidate_snapshot_evidence_mismatch",
                    f"槽位 {slot.code} 的来源与快照证据不一致：{','.join(differing)}",
                    slot=slot,
                    detail={"fields": ",".join(differing)},
                )

        # —— 当前是否还合格（快照合格 != 现在合格）——
        if release is not None:
            if slot.exercise_version_id in current_by_version:
                current = current_by_version[slot.exercise_version_id]
                if slot.dependency_manifest_hash not in (
                    None, current.evidence.dependency_manifest_hash
                ):
                    add(
                        "dependency_manifest_mismatch",
                        "槽位固定的依赖清单哈希与当前题目证据不一致："
                        "快照后题目/审核依赖可能已变，整卷审核需重新固定",
                        slot=slot,
                        detail={
                            "stored": slot.dependency_manifest_hash or "",
                            "current": current.evidence.dependency_manifest_hash,
                        },
                    )
            elif slot.exercise_version_id in rejected_by_version:
                rejected = rejected_by_version[slot.exercise_version_id]
                add(
                    "candidate_ineligible",
                    f"槽位 {slot.code} 的题目当前不合格：{','.join(rejected.reasons)}",
                    slot=slot,
                    detail={"reasons": ",".join(rejected.reasons)},
                )
            else:
                add(
                    "candidate_not_in_release",
                    f"槽位 {slot.code} 的题目已不在当前 Release 的候选记录里",
                    slot=slot,
                )

        evidence = pinned.evidence if pinned is not None else (
            current_by_version[slot.exercise_version_id].evidence
            if slot.exercise_version_id in current_by_version else None
        )
        if spec is None or evidence is None:
            continue
        if spec.exercise_kinds:
            kind = evidence.exercise_kind
            if not kind or kind == "unknown":
                add(
                    "exercise_kind_unknown",
                    f"槽位 {slot.code} 的题型未知，不能断言满足蓝图题型要求"
                    "（规格 §6.5：缺题型不能进入正式候选）",
                    slot=slot,
                    detail={"required": ",".join(spec.exercise_kinds)},
                )
            elif kind not in spec.exercise_kinds:
                add(
                    "exercise_kind_mismatch",
                    f"槽位 {slot.code} 题型 {kind} 不在蓝图允许的 "
                    f"{','.join(spec.exercise_kinds)} 里",
                    slot=slot,
                    detail={"actual": kind, "required": ",".join(spec.exercise_kinds)},
                )
        missing_concepts = sorted(
            {str(item) for item in spec.required_concept_ids}
            - {str(item) for item in evidence.concept_ids}
        )
        if missing_concepts:
            add(
                "concept_coverage_missing",
                f"槽位 {slot.code} 缺少蓝图要求的概念：{','.join(missing_concepts)}",
                slot=slot,
                detail={"missing": ",".join(missing_concepts)},
            )
        missing_outcomes = sorted(
            {str(item) for item in spec.learning_outcome_ids}
            - {str(item) for item in evidence.learning_outcome_ids}
        )
        if missing_outcomes:
            add(
                "outcome_coverage_missing",
                f"槽位 {slot.code} 缺少蓝图要求的学习目标：{','.join(missing_outcomes)}",
                slot=slot,
                detail={"missing": ",".join(missing_outcomes)},
            )
        if slot.exercise_version_id in set(spec.excluded_exercise_version_ids):
            add(
                "excluded_exercise",
                f"槽位 {slot.code} 使用了蓝图明确排除的题目版本",
                slot=slot,
                detail={"exercise_version_id": str(slot.exercise_version_id)},
            )
        if spec.mandatory_exercise_version_ids and (
            slot.exercise_version_id not in set(spec.mandatory_exercise_version_ids)
        ):
            add(
                "slot_not_mandatory",
                f"槽位 {slot.code} 没有使用蓝图指定的必选题目版本",
                slot=slot,
            )

    # 必选题必须在全卷出现（同一版本只报一次）。
    mandatory: dict[UUID, tuple[str, str]] = {}
    for section in blueprint.sections:
        for spec in section.slots:
            for version_id in spec.mandatory_exercise_version_ids:
                mandatory.setdefault(version_id, (section.code, spec.code))
    for version_id in sorted(mandatory, key=str):
        if version_id in selected:
            continue
        section_code, slot_code = mandatory[version_id]
        add(
            "mandatory_exercise_missing",
            f"蓝图必选题版本 {version_id} 没有出现在卷面（槽位 {slot_code}）",
            section_code=section_code,
            detail={
                "missing_version": str(version_id),
                "slot_code": slot_code,
            },
        )


def _check_duplicates(slots: Sequence[PaperSlotView], add: Any) -> None:
    """同一 exercise 不得重复；同一 family 不得重复（A/B 卷不在阶段 A）。"""
    by_exercise: dict[UUID, list[PaperSlotView]] = {}
    by_family: dict[UUID, list[PaperSlotView]] = {}
    for slot in slots:
        if slot.exercise_id is not None:
            by_exercise.setdefault(slot.exercise_id, []).append(slot)
        if slot.exercise_family_id is not None:
            by_family.setdefault(slot.exercise_family_id, []).append(slot)
    for exercise_id in sorted(by_exercise, key=str):
        group = by_exercise[exercise_id]
        if len(group) < 2:
            continue
        add(
            "duplicate_exercise",
            "同一道题（exercise_id）在卷内出现 "
            f"{len(group)} 次（槽位 {','.join(str(item.slot_no) for item in group)}）",
            slot=group[1],
            detail={
                "occurrences": ",".join(str(item.slot_no) for item in group),
                "exercise_id": str(exercise_id),
            },
        )
    for family_id in sorted(by_family, key=str):
        group = by_family[family_id]
        if len(group) < 2:
            continue
        add(
            "exercise_family_conflict",
            "同一题族在卷内出现 "
            f"{len(group)} 次（槽位 {','.join(str(item.slot_no) for item in group)}）；"
            "阶段 A 没有 A/B 卷锚题例外",
            slot=group[1],
            detail={
                "occurrences": ",".join(str(item.slot_no) for item in group),
                "exercise_family_id": str(family_id),
            },
        )


def _current_maps(
    preview: CandidatePreview,
) -> tuple[dict[UUID, CandidateRecord], dict[UUID, RejectedCandidate]]:
    accepted = {
        record.evidence.exercise_version_id: record for record in preview.accepted
    }
    rejected = {item.exercise_version_id: item for item in preview.rejected}
    return accepted, rejected


def _check_release(subject: ValidationSubject, preview: CandidatePreview,
                   add: Any) -> None:
    if preview.release is None:
        add(
            "release_not_found",
            f"Release Context {subject.paper.release_context_id} 不存在，"
            "无法确认候选仍属于固定发布",
            detail={"release_context_id": str(subject.paper.release_context_id)},
        )
        return
    if not subject.course_ref:  # pragma: no cover —— validate() 已先挡
        add(
            "release_scope_unverifiable",
            "蓝图版本缺少 course_ref，无法核对 Release 范围",
        )
        return
    scope = CandidateScope(
        blueprint_version_id=subject.paper.blueprint_version_id,
        purpose=subject.paper.purpose,
        course_id=subject.paper.course_id,
        course_ref=subject.course_ref,
        offering_id=subject.paper.offering_id,
        offering_ref=subject.offering_ref,
        release_context_id=subject.paper.release_context_id,
    )
    reasons = release_scope_reasons(scope, preview.release)
    if reasons:
        add(
            "release_scope_mismatch",
            f"Release Context 不满足本卷要求：{','.join(reasons)}",
            detail={"reasons": ",".join(reasons)},
        )


def _check_duration(blueprint: BlueprintContent, paper: PaperView,
                    sections: Sequence[PaperSectionView], add: Any) -> None:
    constraints = blueprint.hard_constraints or {}
    maximum_raw = constraints.get("duration_max_seconds")
    required = bool(constraints.get("duration_required")) or maximum_raw is not None
    slots = [slot for section in sections for slot in section.slots]
    missing = [slot for slot in slots if slot.estimated_seconds is None]
    if missing:
        detail = {"missing_slots": ",".join(str(slot.slot_no) for slot in missing)}
        if required:
            add(
                "estimated_seconds_missing",
                "启用时长约束，但有槽位缺少预计时长；缺失不能按 0 参与计算",
                detail=detail,
            )
        else:
            add(
                "estimated_seconds_unknown",
                "有槽位缺少预计时长，总时长按未知处理（不虚构为 0）",
                severity="warning",
                detail=detail,
            )
        # 缺失时不虚构总时长结论：既不报超时，也不声称满足上限。
        return
    maximum = paper.duration_seconds if maximum_raw is None else maximum_raw
    try:
        maximum_seconds = int(str(maximum))
    except (TypeError, ValueError):
        add(
            "invalid_hard_constraint",
            f"duration_max_seconds 不是整数秒：{maximum!r}",
            detail={"value": str(maximum)},
        )
        return
    actual = sum(slot.estimated_seconds or 0 for slot in slots)
    if required and actual > maximum_seconds:
        add(
            "duration_exceeded",
            f"预计总时长 {actual} 秒，超过上限 {maximum_seconds} 秒",
            detail={"actual": str(actual), "maximum": str(maximum_seconds)},
        )


def _check_constraint_groups(blueprint: BlueprintContent, paper: PaperView,
                             add: Any) -> None:
    constraints = blueprint.hard_constraints or {}
    selected = {
        slot.exercise_version_id: slot
        for section in paper.sections for slot in section.slots
        if slot.exercise_version_id is not None
    }
    for key, code in (
        ("conflict_groups", "shared_material_conflict"),
        ("leak_groups", "mutual_answer_leak"),
    ):
        groups = constraints.get(key) or []
        if not isinstance(groups, (list, tuple)):
            add(
                "invalid_hard_constraint",
                f"{key} 必须是分组列表",
                detail={"value": str(groups)},
            )
            continue
        for group in groups:
            try:
                parsed = {UUID(str(item)) for item in group}
            except (TypeError, ValueError):
                add(
                    "invalid_hard_constraint",
                    f"{key} 里的版本 id 不是合法 UUID：{group!r}",
                    detail={"value": str(group)},
                )
                continue
            hits = parsed & set(selected)
            if len(hits) > 1:
                first = selected[sorted(hits, key=str)[0]]
                add(
                    code,
                    f"同一冲突组有 {len(hits)} 道题同时进卷："
                    + ",".join(str(item) for item in sorted(hits, key=str)),
                    slot=first,
                    detail={
                        "versions": ",".join(str(item) for item in sorted(hits, key=str)),
                        "slots": ",".join(str(selected[item].slot_no)
                                          for item in sorted(hits, key=str)),
                    },
                )


def _check_exposure(paper: PaperView, preview: CandidatePreview,
                    snapshot: CandidateSnapshot,
                    current_by_version: Mapping[UUID, CandidateRecord],
                    now: datetime, add: Any) -> None:
    """以"卷面固定证据"而不是"当前 accepted"做曝光检查。

    候选服务会把单个候选的曝光冲突放进 `rejected`；如果这里只看 `accepted`，
    某个槽位就会只剩 `candidate_ineligible`，而看不到专门的 `exposure_conflict`。
    快照证据足够判断 family/version 冲突，且它才是卷子真正固定的题；当前证据只
    作为快照缺项的兜底。
    """
    if preview.release is None:
        return
    pinned = {
        record.evidence.exercise_version_id: record.evidence
        for record in snapshot.accepted
    }
    selected: dict[UUID, Any] = {}
    for section in paper.sections:
        for slot in section.slots:
            version_id = slot.exercise_version_id
            if version_id is None:
                continue
            evidence = pinned.get(version_id)
            if evidence is None and version_id in current_by_version:
                evidence = current_by_version[version_id].evidence
            if evidence is not None:
                selected[version_id] = evidence
    if not selected:
        return
    try:
        ExposureService.assert_no_conflict(
            paper.purpose, paper.offering_id,
            tuple(selected[version_id] for version_id in sorted(selected, key=str)),
            preview.exposure_events, now,
        )
    except ExposureConflict as exc:
        add(
            "exposure_conflict",
            f"{exc.code}: {exc}",
            detail={"reason": exc.code},
        )


def _order_issues(issues: Sequence[ValidationIssue],
                  paper: PaperView) -> tuple[ValidationIssue, ...]:
    """固定顺序：error 先、再按分节卷面顺序、槽位号、原因码、消息。"""
    section_rank = {section.code: index for index, section in enumerate(paper.sections)}

    def key(item: ValidationIssue) -> tuple[int, int, int, str, str]:
        return (
            0 if item.severity == "error" else 1,
            section_rank.get(item.section_code or "", -1),
            item.slot_no or 0,
            item.code,
            item.message,
        )

    return tuple(sorted(issues, key=key))


__all__ = [
    "InMemoryPaperValidationRepository",
    "PaperValidationBlockedError",
    "PaperValidationRepository",
    "PaperValidationService",
    "PaperValidationUnitOfWork",
    "PostgresPaperValidationRepository",
    "REQUIRED_SOURCE_FIELDS",
    "VALIDATION_SCHEMA",
    "ValidationError",
    "ValidationInputError",
    "ValidationSubject",
    "report_payload",
]
