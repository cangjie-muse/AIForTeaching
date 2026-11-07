"""QTI 导入导出 HTTP 契约（阶段 E 任务 3）。

    教师：POST /qti/imports          上传一份 QTI（JSON）→ 得到 draft_ready / rejected
          GET  /qti/imports/{id}     查看这次导入的差异清单
          POST /qti/exports          导出固定卷 → 成功给产物，拒绝给 reasons

安全与语义：

  * 导入**永远不会**返回 "approved"：`status` 只有 draft_ready / rejected；
  * 拒绝也落记录，且响应里带全部差异（教师要知道少了哪几项）；
  * 导出被拒时返回 409 + reasons，而不是空成功；
  * 未装配服务 → 503（与评分服务一致，不假装可用）。
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .qti.contracts import ExportSlot
from .qti.service import ImportRun, QtiService

router = APIRouter(prefix="/api/v1", tags=["qti"])
require_teacher = require("teacher")


class Services:
    def __init__(self, *, qti: QtiService, export_slots: Callable[[UUID], list[ExportSlot]],
                 paper_status: Callable[[UUID], str],
                 paper_title: Callable[[UUID], str]) -> None:
        self.qti = qti
        self.export_slots = export_slots
        self.paper_status = paper_status
        self.paper_title = paper_title


_services: Callable[[], Services] | None = None


def wire(factory: Callable[[], Services]) -> None:
    global _services
    _services = factory


def wire_dev_fallback() -> bool:
    """开发环境用"空卷"装配，只为让接口可点通；导出会被正确拒绝（空卷未批准）。"""
    from .qti.service import InMemoryQtiRepository

    repository = InMemoryQtiRepository()
    service = QtiService(repository=repository)
    wire(lambda: Services(qti=service, export_slots=lambda paper: [],
                          paper_status=lambda paper: "draft",
                          paper_title=lambda paper: "未命名卷"))
    return True


def get_services() -> Services:
    if _services is None:
        raise HTTPException(503, detail={"code": "qti_not_configured",
                                        "message": "QTI 服务未装配"})
    return _services()


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_ref: str = Field(min_length=1, max_length=200)
    source_name: str | None = Field(default=None, max_length=255)
    document: dict[str, Any]


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_version_id: UUID


def _run_out(run: ImportRun) -> dict[str, Any]:
    return {"run_id": str(run.id), "status": run.status,
            "item_count": run.item_count, "error_count": run.error_count,
            "warning_count": run.warning_count, "replayed": run.replayed}


@router.post("/qti/imports", status_code=201)
def import_qti_document(body: ImportIn, actor: Actor = Depends(require_teacher),
                        services: Services = Depends(get_services)) -> dict[str, Any]:
    run = services.qti.import_document(
        course_ref=body.course_ref, payload=body.document,
        imported_by=getattr(actor, "user_uuid", None) or UUID(int=1),
        source_name=body.source_name)
    return {**_run_out(run),
            "differences": [item.model_dump(mode="json")
                            for item in services.qti.differences_of(run.id)]}


@router.get("/qti/imports/{run_id}")
def get_import(run_id: UUID, actor: Actor = Depends(require_teacher),
               services: Services = Depends(get_services)) -> dict[str, Any]:
    run = next((item for item in services.qti.repository.imports if item.id == run_id), None)
    if run is None:
        raise HTTPException(404, detail={"code": "qti_import_not_found",
                                        "message": f"没有这次导入：{run_id}"})
    return {**_run_out(run),
            "differences": [item.model_dump(mode="json")
                            for item in services.qti.differences_of(run.id)]}


@router.post("/qti/exports")
def export_paper(body: ExportIn, actor: Actor = Depends(require_teacher),
                 services: Services = Depends(get_services)) -> dict[str, Any]:
    run = services.qti.export_paper(
        paper_version_id=body.paper_version_id,
        slots=services.export_slots(body.paper_version_id),
        paper_status=services.paper_status(body.paper_version_id),
        paper_title=services.paper_title(body.paper_version_id),
        exported_by=getattr(actor, "user_uuid", None) or UUID(int=1))
    if run.status == "blocked":
        raise HTTPException(409, detail={
            "code": "qti_export_blocked", "message": "导出被拒绝",
            "reasons": list(run.reasons)})
    return {"run_id": str(run.id), "status": run.status, "item_count": run.item_count,
            "payload_sha256": run.payload_sha256, "payload": run.payload}


__all__ = ["router", "Services", "wire", "wire_dev_fallback", "get_services"]
