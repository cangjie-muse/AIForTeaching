"""QTI 导入导出的留痕服务（阶段 E 任务 1—3 的服务层）。

导入导出是**跨系统**动作，因此每一次都必须留下可回答问题的记录：

    "这份卷子什么时候导进来的？少了什么？谁导的？"
    "为什么这次导出被拒绝？拒绝原因是什么？"

关键规则（与设计 §13.2 对齐）：

1. **导入一律 draft**：不继承外部审核结论；`status` 只有 `draft_ready` / `rejected`。
2. **拒绝也要落记录**：不能只返回 422 就把原因丢掉——教师需要看到"缺了哪几项"。
3. **内容寻址**：同一份文件（source_sha256 + 课程 + 导入人）重复导入命中同一行，
   不产生垃圾记录；真改了文件内容才算新导入。
4. **导出不绕过治理**：`export_qti` 的拒绝原因原样落库（`blocked` + reasons），
   不生成产物哈希。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    Difference,
    ExportSlot,
    QtiExportBlocked,
    QtiImportError,
    export_qti,
    import_qti,
)

IMPORT_SCOPE = "qti-import"
EXPORT_SCOPE = "qti-export"


@dataclass(frozen=True)
class ImportRun:
    id: UUID
    course_ref: str
    source_sha256: str
    source_name: str | None
    status: str                 # draft_ready | rejected
    item_count: int
    error_count: int
    warning_count: int
    imported_by: UUID
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class ExportRun:
    id: UUID
    paper_version_id: UUID
    status: str                 # exported | blocked
    reasons: tuple[str, ...]
    item_count: int
    payload_sha256: str | None
    exported_by: UUID
    created_at: datetime
    payload: dict[str, Any] | None = None


class InMemoryQtiRepository:
    def __init__(self) -> None:
        self.imports: list[ImportRun] = []
        self.differences: dict[UUID, list[Difference]] = {}
        self.exports: list[ExportRun] = []

    def find_import(self, *, course_ref: str, source_sha256: str,
                    imported_by: UUID) -> ImportRun | None:
        return next((run for run in self.imports
                     if run.course_ref == course_ref
                     and run.source_sha256 == source_sha256
                     and run.imported_by == imported_by), None)

    def save_import(self, run: ImportRun, differences: Sequence[Difference]) -> None:
        self.imports.append(run)
        self.differences[run.id] = list(differences)

    def save_export(self, run: ExportRun) -> None:
        self.exports.append(run)


class QtiService:
    def __init__(self, *, repository: InMemoryQtiRepository,
                 clock: Callable[[], datetime] = utcnow,
                 id_factory: Callable[[], UUID] = uuid4) -> None:
        self.repository = repository
        self._now = clock
        self._new_id = id_factory

    # -- 导入 -----------------------------------------------------------
    def import_document(self, *, course_ref: str, payload: dict[str, Any],
                        imported_by: UUID,
                        source_name: str | None = None) -> ImportRun:
        if not (course_ref or "").strip():
            raise QtiImportError("qti_import_invalid: 必须指定课程")
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        existing = self.repository.find_import(
            course_ref=course_ref, source_sha256=digest, imported_by=imported_by)
        if existing is not None:
            # 内容寻址：同一份文件重复导入返回同一行，不追加新记录。
            return ImportRun(**{**existing.__dict__, "replayed": True})

        try:
            report = import_qti(payload)
        except QtiImportError as error:
            # 连解析都失败的输入也要留证：记一条 rejected，差异里写原因。
            run = ImportRun(
                id=self._new_id(), course_ref=course_ref, source_sha256=digest,
                source_name=source_name, status="rejected", item_count=0,
                error_count=1, warning_count=0, imported_by=imported_by,
                created_at=self._now())
            self.repository.save_import(run, [Difference(
                code="qti_parse_failed", severity="error", message=str(error))])
            return run

        status = "draft_ready" if report.importable else "rejected"
        run = ImportRun(
            id=self._new_id(), course_ref=course_ref, source_sha256=digest,
            source_name=source_name, status=status, item_count=len(report.items),
            error_count=len(report.errors),
            warning_count=sum(1 for item in report.differences
                              if item.severity == "warning"),
            imported_by=imported_by, created_at=self._now())
        self.repository.save_import(run, report.differences)
        return run

    def differences_of(self, run_id: UUID) -> tuple[Difference, ...]:
        return tuple(self.repository.differences.get(run_id, ()))

    # -- 导出 -----------------------------------------------------------
    def export_paper(self, *, paper_version_id: UUID, slots: Sequence[ExportSlot],
                     paper_status: str, paper_title: str,
                     exported_by: UUID) -> ExportRun:
        try:
            payload = export_qti(slots=slots, paper_status=paper_status,
                                 paper_title=paper_title)
        except QtiExportBlocked as error:
            # 被拒绝也是一条证据：落库 reasons，状态 blocked，没有产物哈希。
            run = ExportRun(
                id=self._new_id(), paper_version_id=paper_version_id,
                status="blocked", reasons=tuple(error.reasons), item_count=0,
                payload_sha256=None, exported_by=exported_by,
                created_at=self._now())
            self.repository.save_export(run)
            return run
        digest = canonical_hash(payload)
        run = ExportRun(
            id=self._new_id(), paper_version_id=paper_version_id, status="exported",
            reasons=(), item_count=len(payload.get("items", [])),
            payload_sha256=digest, exported_by=exported_by, created_at=self._now(),
            payload=payload)
        self.repository.save_export(run)
        return run

    def exports_of(self, paper_version_id: UUID) -> tuple[ExportRun, ...]:
        return tuple(item for item in self.repository.exports
                     if item.paper_version_id == paper_version_id)


__all__ = [
    "EXPORT_SCOPE",
    "ExportRun",
    "IMPORT_SCOPE",
    "ImportRun",
    "InMemoryQtiRepository",
    "QtiService",
]
