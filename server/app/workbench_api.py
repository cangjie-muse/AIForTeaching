"""组卷工作台 API（阶段 A 任务 9/10 的前端数据源，只读）。

    GET /api/v1/paper-workbench?course_ref=analysis

权限：这门课范围内的教师/管理员。响应只含"哪一步没做"的聚合结论，不含题干、
答案、量规——工作台不需要答案侧数据，携带它就是多一个泄露面。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import Actor, require
from .blueprints import CourseScopeAuthorizer, UnknownCourseError, ref_uuid
from .workbench import (
    PostgresWorkbenchRepository,
    WorkbenchRepository,
    WorkbenchView,
    build_view,
)

router = APIRouter(prefix="/api/v1", tags=["paper-workbench"])

require_teacher = require("teacher")


def get_workbench_repository() -> WorkbenchRepository:
    return PostgresWorkbenchRepository()  # type: ignore[return-value]


def get_workbench_authorizer(actor: Actor = Depends(require_teacher)) -> CourseScopeAuthorizer:
    """授权单独做成依赖：接口测试可以替换它，而不必连数据库。

    生产实现就是 `CourseScopeAuthorizer`——"谁教这门课"的真源是班级名单，
    这里不另造一套权限模型。
    """
    return CourseScopeAuthorizer(actor)


@router.get("/paper-workbench", response_model=WorkbenchView)
def read_workbench(
    course_ref: str = Query(min_length=1, max_length=200),
    actor: Actor = Depends(require_teacher),
    repository: WorkbenchRepository = Depends(get_workbench_repository),
    authorizer: Any = Depends(get_workbench_authorizer),
) -> WorkbenchView:
    try:
        authorizer.require(
            ref_uuid("account", actor.id), "paper_blueprint.read",
            course_ref=course_ref,
        )
    except UnknownCourseError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "course_not_found", "message": str(exc)},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "course_scope_denied", "message": str(exc)},
        ) from exc
    papers = repository.papers(course_ref)
    return build_view(course_ref, papers)


def _unused_actor_id(actor: Actor) -> UUID:
    return ref_uuid("account", actor.id)
