"""打印卷渲染入口（阶段 A 任务 10 的前置缺口）。

    POST /api/v1/paper-versions/{paper_version_id}/renders
        { "render_kinds": ["student", "answer", "rubric"] }
      → 202 { "jobs": [{ "render_kind", "job_id" }], "already_ready": [...] }

为什么必须有这个入口：`submit-review` 硬性要求三类卷都已渲染，而任务 6 只做了
worker 与渲染服务，没有任何 HTTP 方式把渲染任务入队。结果就是教师永远撞
`409 renders_not_ready`，整条链路在真实环境走不通。这个接口补的就是这一环。

为什么入队而不是同步渲染：一份卷三类产物要写文件、注册 artifact、写
paper_render，几十秒到几分钟。放在请求里必然超时，然后被迫改成后台线程，再然后
就没有重试、没有进度、不知道卡在哪。`paper-render` 用既有 pipeline_job 的领取、
心跳与重试（0031 已扩 kind 约束）。

幂等：同一份卷同一类产物已经 ready 的不再入队，直接回在 `already_ready` 里。
渲染本身也是内容寻址的——真的重跑一次，输出的 output_hash 相同，文件复用。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import Actor, require
from .blueprints import UnknownCourseError, ref_uuid
from .papers import PaperNotFoundError, PaperScopeAuthorizer, PostgresPaperRepository

router = APIRouter(prefix="/api/v1", tags=["paper-renders"])

require_teacher = require("teacher")

RENDER_KINDS = ("student", "answer", "rubric")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class RenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_kinds: tuple[str, ...] = Field(default=RENDER_KINDS, min_length=1, max_length=3)


@router.post("/paper-versions/{paper_version_id}/renders", status_code=202)
def request_renders(
    paper_version_id: UUID,
    body: RenderIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
) -> dict[str, Any]:
    kinds = tuple(dict.fromkeys(kind.strip() for kind in body.render_kinds))
    unknown = [kind for kind in kinds if kind not in RENDER_KINDS]
    if unknown:
        raise _error(422, "unknown_render_kind", f"未知的渲染类型：{unknown}")
    if idempotency_key is not None and not idempotency_key.strip():
        raise _error(400, "idempotency_key_required", "Idempotency-Key 不能是空白")

    with PostgresPaperRepository().unit_of_work() as uow:
        paper = uow.get(paper_version_id)
    if paper is None:
        raise _error(404, "paper_not_found", f"卷子不存在：{paper_version_id}")
    if paper.status == "withdrawn":
        raise _error(409, "paper_withdrawn", "已撤回的卷子不能导出")

    course_ref = _course_ref(paper.course_id)
    try:
        # 必须用 PaperScopeAuthorizer：它把 paper.write 映射成课程范围判定认识的
        # paper_blueprint.write。直接拿 CourseScopeAuthorizer 传 "paper.write" 会被它
        # 当成未知动作拒掉——所有教师都 403，而桩测试看不出来。
        PaperScopeAuthorizer(actor).require(
            ref_uuid("account", actor.id), "paper.write", course_ref=course_ref)
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc

    ready = _ready_kinds(paper_version_id)
    jobs: list[dict[str, Any]] = []
    for kind in kinds:
        if kind in ready:
            continue
        row = db.query_one(
            """INSERT INTO pipeline_job (kind, payload, requested_by)
               VALUES ('paper-render', %s::jsonb, %s)
               RETURNING id""",
            (json.dumps({
                "paper_version_id": str(paper_version_id),
                "render_kind": kind,
            }, ensure_ascii=False), actor.id),
        )
        jobs.append({"render_kind": kind, "job_id": row["id"]})

    return {
        "paper_version_id": str(paper_version_id),
        "job_ids": [job["job_id"] for job in jobs],
        "jobs": jobs,
        "already_ready": sorted(ready),
        "note": "渲染在 worker 里跑；完成后 submit-review 才允许提交。",
    }


def _ready_kinds(paper_version_id: UUID) -> set[str]:
    rows = db.query(
        "SELECT DISTINCT render_kind FROM assessment.paper_render WHERE paper_version_id = %s",
        (paper_version_id,),
    )
    return {row["render_kind"] for row in rows}


def _course_ref(course_id: UUID) -> str:
    row = db.query_one(
        "SELECT ref FROM assessment.id_ref WHERE subject = %s AND kind = 'course'",
        (course_id,),
    )
    if row is None:
        raise _error(
            409, "course_ref_missing",
            "这门课没有 id_ref 映射，无法确定课程范围（历史数据需先回填）")
    return row["ref"]
