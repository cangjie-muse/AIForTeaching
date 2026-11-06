"""打印卷渲染入口与三类产物的读取/下载（阶段 A 任务 10 的前置缺口）。

    POST /api/v1/paper-versions/{paper_version_id}/renders
        { "render_kinds": ["student", "answer", "rubric"] }
      → 202 { "jobs": [{ "render_kind", "job_id" }], "already_ready": [...] }
    GET  /api/v1/paper-versions/{id}/renders              三类产物清单（最近一版卷面）
    GET  /api/v1/paper-renders/{render_id}/download       按卷种分别授权的下载
    GET  /api/v1/paper-renders/{render_id}/preview        按卷种分别授权的浏览器内预览

为什么必须有入队入口：`submit-review` 硬性要求三类卷都已渲染，而任务 6 只做了
worker 与渲染服务，没有任何 HTTP 方式把渲染任务入队。结果就是教师永远撞
`409 renders_not_ready`，整条链路在真实环境走不通。这个接口补的就是这一环。

为什么入队而不是同步渲染：一份卷三类产物要写文件、注册 artifact、写
paper_render，几十秒到几分钟。放在请求里必然超时，然后被迫改成后台线程，再然后
就没有重试、没有进度、不知道卡在哪。`paper-render` 用既有 pipeline_job 的领取、
心跳与重试（0031 已扩 kind 约束）。

幂等：同一份卷同一类产物已经 ready 的不再入队，直接回在 `already_ready` 里。
渲染本身也是内容寻址的——真的重跑一次，输出的 output_hash 相同，文件复用。

下载为什么强制附件 + nosniff：产物是**可打印 HTML**（本仓库没有 PDF 生成依赖）。
若让浏览器就地渲染，同源就多了一个能执行脚本的文档入口。所以一律
`Content-Disposition: attachment`、`X-Content-Type-Options: nosniff`、
`Content-Security-Policy: sandbox`——教师需要的是下载后用浏览器打印，不是把它
当成应用的一部分执行。

预览为什么是另一条路由：复核人需要"看到的就是要打印的那一版"，所以要有 inline
的入口；但 inline 的风险靠 `CSP: sandbox` 管住（脚本不执行），安全头与下载分开写，
就不会有人为了省事把下载也改成 inline。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import Actor, require
from .blueprints import UnknownCourseError, ref_uuid
from .papers import PaperNotFoundError, PaperScopeAuthorizer, PostgresPaperRepository
from .rendering import (
    MIME_TYPE,
    PDF_IMPLEMENTED,
    PDF_NOTICE,
    RENDER_KINDS,
    LocalObjectStore,
    PaperRenderAuthorizer,
    PaperRenderIncompleteError,
    PaperRenderInputError,
    PaperRenderIntegrityError,
    PaperRenderNotFoundError,
    PaperRenderService,
    PaperRenderWithdrawnError,
    PostgresPaperRenderRepository,
)

router = APIRouter(prefix="/api/v1", tags=["paper-renders"])

require_teacher = require("teacher")


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


# ---------------------------------------------------------------------------
# 三类产物的读取与下载
# ---------------------------------------------------------------------------

def get_render_service(actor: Actor = Depends(require_teacher)):
    """按**请求者本人**装配渲染服务：授权用 PaperRenderAuthorizer，不是 worker 的。

    worker 用 ServiceRenderAuthorizer（动作闭集放行三类 render），那是给后台任务
    的。HTTP 路径若误用它，任何登录教师都能下载任何课程的卷子——这里必须带上
    actor，让课程范围判定真正跑起来。
    """
    return PaperRenderService(
        repository=PostgresPaperRenderRepository(),
        authorizer=PaperRenderAuthorizer(actor),
        object_store=LocalObjectStore(),
    )


def _render_error(exc: Exception) -> HTTPException:
    """渲染域错误 → 稳定 HTTP 码。410 表示"曾经有、现在不能再用"（已撤回）。"""
    if isinstance(exc, PaperRenderWithdrawnError):
        return _error(410, "paper_withdrawn", "卷子已撤回：历史版本只读，不能继续渲染或下载")
    if isinstance(exc, PaperRenderNotFoundError):
        return _error(404, "render_not_found", str(exc))
    if isinstance(exc, PaperRenderIntegrityError):
        return _error(500, "render_integrity_error", str(exc))
    if isinstance(exc, PaperRenderIncompleteError):
        return _error(409, "paper_incomplete", str(exc))
    if isinstance(exc, PaperRenderInputError):
        return _error(422, "render_input_invalid", str(exc))
    return _error(500, "render_failed", str(exc))


@router.get("/paper-versions/{paper_version_id}/renders")
def list_paper_renders(
    paper_version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: Any = Depends(get_render_service),
) -> dict[str, Any]:
    """这份卷**当前卷面**已产出的三类产物清单（供下载链接用）。

    只列 `paper_manifest_hash` 等于当前 manifest 的 ready 行：卷面改过之后，旧
    manifest 的产物仍然留在库里（历史证据），但它不是"这一版的可打印稿"。把它们
    混进下载列表，教师会打印出一份和屏幕上不一样的卷子。

    没有权限的卷种不出现（不是一个 403 挡住整个列表）：只被授权看学生卷的助教
    仍然应该能下载学生卷。
    """
    try:
        subject, records = service.list_current_renders(
            ref_uuid("account", actor.id), paper_version_id)
    except PermissionError as exc:
        raise _error(403, "render_scope_denied", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 域错误统一映射，不外泄栈
        raise _render_error(exc) from exc

    by_kind = {record.render_kind: record for record in records}
    return {
        "paper_version_id": str(paper_version_id),
        "manifest_hash": subject.manifest_hash,
        "status": subject.status,
        "media_type": MIME_TYPE,
        "pdf_implemented": PDF_IMPLEMENTED,
        "notice": PDF_NOTICE,
        "renders": [
            {
                "render_id": str(by_kind[kind].id),
                "render_kind": kind,
                "output_hash": by_kind[kind].output_hash,
                "created_at": getattr(by_kind[kind], "created_at", None),
                "download_path": f"/api/v1/paper-renders/{by_kind[kind].id}/download",
                "preview_path": f"/api/v1/paper-renders/{by_kind[kind].id}/preview",
            }
            for kind in RENDER_KINDS if kind in by_kind
        ],
        "missing_kinds": [kind for kind in RENDER_KINDS if kind not in by_kind],
    }


def _attachment_name(kind: str, subject: Any) -> str:
    """下载文件名：ASCII 兜底 + RFC 5987 中文名。

    中文课程名直接塞进 Content-Disposition 的 filename= 会被部分客户端截断成乱码，
    所以给两份：`filename` 只用 ASCII，`filename*` 放 UTF-8 原名。
    """
    title = str(getattr(subject, "course_title", "") or "paper")
    short = str(getattr(subject, "version_short_code", "") or "")
    chinese = f"{title}{('-' + short) if short else ''}-{kind}.html"
    safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "-"
                   for ch in chinese)
    safe = "-".join(part for part in safe.split("-") if part) or f"paper-{kind}.html"
    if not safe.endswith(".html"):
        safe += ".html"
    return f'{safe}; filename*=UTF-8\'\'{quote(chinese)}'


@router.get("/paper-renders/{render_id}/download")
def download_paper_render(
    render_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: Any = Depends(get_render_service),
) -> Response:
    """下载一类产物：三卷种分别授权，且必须是当前卷面的产物。

    服务层会重新走一遍 `paper.render.<kind>` 权限并核对落盘字节的
    `output_hash`——手工拼一个 render_id 换不到别人的答案卷，改过文件也下不出来。
    """
    body, record, subject = _render_bytes(service, actor, render_id)
    headers = {
        "Content-Disposition": f"attachment; filename={_attachment_name(record.render_kind, subject)}",
        # 同源 HTML 一旦被内联渲染就成了可执行文档；强制下载 + nosniff + sandbox
        # 三层一起，才不依赖浏览器对 Content-Disposition 的实现差异。
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "no-store",
    }
    return Response(content=body, media_type=MIME_TYPE, headers=headers)


@router.get("/paper-renders/{render_id}/preview")
def preview_paper_render(
    render_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: Any = Depends(get_render_service),
) -> Response:
    """在浏览器里直接看这一类产物（教师复核"打印出来到底长什么样"）。

    为什么单独一条路由而不是给 download 加 `?inline=1`：两条路的安全头必须不同
    （attachment 对 inline），写在同一个处理函数里迟早有人手一抖把下载也改成
    inline——那等于把"可打印 HTML"变成一个同源可执行文档入口。这里仍然是
    `CSP: sandbox` + `nosniff` + `no-store`，脚本不会执行、也不会被缓存。
    授权与哈希校验完全复用 download（`_render_bytes`），预览拿不到的东西下载也拿不到。
    """
    body, record, subject = _render_bytes(service, actor, render_id)
    headers = {
        "Content-Disposition": f"inline; filename={_attachment_name(record.render_kind, subject)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "no-store",
    }
    return Response(content=body, media_type=MIME_TYPE, headers=headers)


def _render_bytes(service: Any, actor: Actor, render_id: UUID) -> tuple[bytes, Any, Any]:
    """取产物字节并统一错误映射；下载与预览共用（两处状态码不会漂）。"""
    try:
        return service.download(ref_uuid("account", actor.id), render_id)
    except PermissionError as exc:
        # 不区分"不存在"与"没权限"会不会更好？这里刻意区分：不存在的渲染就是 404，
        # 存在但越权才是 403。保密资源的存在性本身不敏感（uuid 不可枚举）。
        raise _error(403, "render_scope_denied", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _render_error(exc) from exc


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
