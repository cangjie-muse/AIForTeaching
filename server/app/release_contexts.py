"""Release Context 冻结 API（阶段 A 任务 8 的入口补齐）。

     POST /api/v1/release-contexts     把当前题目内容冻结成一个 Release
     GET  /api/v1/release-contexts/{id}

为什么这个模块到任务 8 才出现：任务 3 只做了候选资格与曝光门禁，`ReleaseService.freeze`
一直停在服务层，没有任何 HTTP 入口。后果是候选接口在真实环境永远 422
`release_context_not_found`——链路在数据上"正确"，却根本走不到。发布打印作业必须
先有 Release，所以入口必须在这里补上。

为什么不要求 Idempotency-Key：冻结是**内容寻址**的纯操作（见
`ReleaseService.freeze_idempotent`）。同一批题、同一用途、同一个 base，算出的
release_context_id 必然相同；内容变了 id 就变，那本来就该是一条新 Release。给一个
由输入唯一决定输出的操作套幂等键，只会让调用方误以为"带不带键行为不同"。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, require
from .blueprints import (
    CourseScopeAuthorizer,
    UnknownCourseError,
    ref_uuid,
)
from .contracts import Purpose
from .releases import (
    DuplicateReleaseItemError,
    PostgresReleaseRepository,
    ReleaseContext,
    ReleaseError,
    ReleaseScopeError,
    ReleaseService,
    UnknownExerciseError,
    UnknownReleaseError,
)

router = APIRouter(prefix="/api/v1", tags=["release-contexts"])

require_teacher = require("teacher")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class ReleaseFreezeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_ref: str = Field(min_length=1, max_length=200)
    purpose: Purpose
    exercise_refs: tuple[str, ...] = Field(min_length=1, max_length=5000)
    offering_ref: str | None = Field(default=None, min_length=1, max_length=200)
    base_release_id: UUID | None = None
    label: str | None = Field(default=None, max_length=200)
    # 只允许冻结或建开放集：retired 是退役动作，不该跟创建混在一个入口。
    status: str = Field(default="frozen", pattern="^(frozen|open)$")


class ReleaseFreezeOut(BaseModel):
    release_context: ReleaseContext
    created: bool


def _service() -> ReleaseService:
    return ReleaseService(repository=PostgresReleaseRepository())


def _actor_uuid(actor: Actor) -> UUID:
    return ref_uuid("account", actor.id)


@router.post("/release-contexts", response_model=ReleaseFreezeOut, status_code=201)
def freeze_release(
    body: ReleaseFreezeIn,
    response: Response,
    actor: Actor = Depends(require_teacher),
    service: ReleaseService = Depends(_service),
) -> ReleaseFreezeOut:
    actor_id = _actor_uuid(actor)
    try:
        CourseScopeAuthorizer(actor).require(
            actor_id, "paper_blueprint.write",
            course_ref=body.course_ref, offering_ref=body.offering_ref,
        )
        context, created = service.freeze_idempotent(
            actor_id=actor_id,
            course_ref=body.course_ref,
            purpose=body.purpose,
            exercise_refs=body.exercise_refs,
            offering_ref=body.offering_ref,
            base_release_id=body.base_release_id,
            status=body.status,  # type: ignore[arg-type]
            label=body.label,
        )
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except UnknownExerciseError as exc:
        raise _error(422, "unknown_exercise", str(exc)) from exc
    except DuplicateReleaseItemError as exc:
        raise _error(409, "duplicate_exercise", str(exc)) from exc
    except UnknownReleaseError as exc:
        raise _error(404, "base_release_not_found", str(exc)) from exc
    except ReleaseScopeError as exc:
        raise _error(422, "release_scope_invalid", str(exc)) from exc
    except ReleaseError as exc:
        raise _error(422, "release_invalid", str(exc)) from exc
    if not created:
        # 内容寻址命中已有 Release：这不是"新建失败"，而是同一请求的重放。
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return ReleaseFreezeOut(release_context=context, created=created)


@router.get("/release-contexts/{release_context_id}", response_model=ReleaseContext)
def get_release(
    release_context_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: ReleaseService = Depends(_service),
) -> ReleaseContext:
    try:
        context = service.get(release_context_id)
        CourseScopeAuthorizer(actor).require(
            _actor_uuid(actor), "paper_blueprint.read",
            course_ref=context.course_ref, offering_ref=context.offering_ref,
        )
    except UnknownReleaseError as exc:
        raise _error(404, "release_not_found", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", str(exc)) from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    return context
