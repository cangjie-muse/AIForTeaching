"""待复核列表接口（阶段 D 补充：教师复核台缺的那一块）。

设计取舍：列表**只给"需要人看"的提交**（按提交时间倒序），并且每条只带教师
真正需要的最小信息——提交 id、学生、交卷时间、题数、需要人工确认的项数。
不带学生答案正文（教师点进去才拉），避免列表接口变相成为"批量看答案"的入口。
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import Actor, require
from . import grading_api

router = APIRouter(prefix="/api/v1", tags=["grading"])
require_teacher = require("teacher")


class PendingSubmission(BaseModel):
    submission_id: UUID
    student_id: UUID
    submitted_at: str
    item_count: int
    requires_human_count: int
    attempt_no: int = 1


class PendingList(BaseModel):
    items: list[PendingSubmission]
    total: int


pending: Callable[[str, int], list[dict[str, Any]]] | None = None


def wire_pending(factory: Callable[[str, int], list[dict[str, Any]]]) -> None:
    """注入"待复核列表"数据源（真库时是 SQL 查询，测试时是桩）。"""
    global pending
    pending = factory


@router.get("/grading/submissions", response_model=PendingList)
def list_submissions(
    status: str = Query(default="pending", pattern="^(pending|all)$"),
    limit: int = Query(default=50, ge=1, le=200),
    actor: Actor = Depends(require_teacher),
) -> PendingList:
    if pending is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "grading_not_configured",
                    "message": "待复核列表未装配：需要注入数据源"})
    rows = pending(status, limit)
    items = [PendingSubmission(**row) for row in rows]
    return PendingList(items=items, total=len(items))


__all__ = ["router", "wire_pending", "PendingSubmission", "PendingList"]
