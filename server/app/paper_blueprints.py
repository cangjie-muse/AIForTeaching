"""蓝图版本与候选快照 API。

路由：
    POST /paper-blueprints/{blueprint_id}/versions        创建/追加蓝图版本
    GET  /paper-blueprint-versions/{id}                   读取一个蓝图版本
    POST /paper-blueprint-versions/{id}/candidate-snapshots 生成并保存候选清单（任务 3）
    GET  /paper-blueprint-versions/{id}/candidates        读取最近一次候选清单（任务 3）
    GET  /courses/{course_ref}/paper-blueprints           本课程每份蓝图的最新版本
    POST /courses/{course_ref}/paper-blueprints           按课程号 + 蓝图别名建/追加版本
    GET  /paper-blueprint-versions/{id}/candidate-labels  候选题目的可读标签（只读展示）

为什么还要一组"按课程号"的路由：`blueprint_id` 是 uuid，而前端拿不到 uuid5 的命名
空间。只给 uuid 路由的话，教师建完第一版就再也找不回自己那份蓝图（没有列表入口），
工作台也就无法把"接着上一份继续"串起来。这组路由把 uuid 推导留在服务端，并把
"哪些蓝图版本存在"变成一次有课程范围授权的读取。

为什么摘要有独立的 labels 接口：候选快照只存判定证据（uuid、布尔、原因码），不存
题干——它的哈希是审计凭据，混进展示文案会让同一份快照在不同环境哈希不同。而教师
要按题目内容挑题，所以标签单独走一条只读接口，前端用 `exercise_id` 做连接。

为什么写接口一定要 Idempotency-Key：教师点「保存」时网络超时很常见，浏览器/脚本
重试会带着同一份载荷再来一次。没有幂等键，重试就是第二版；有了它，同键同载荷返回
原版本（200 + `Idempotent-Replay: true`），同键不同载荷 409。幂等记录与版本插入在
**同一个事务**里写，进程崩溃也不会留下"版本在、键不在"的空档。

候选快照的幂等真源是**内容哈希**而不是那把键：同一份候选清单（同蓝图版本、同
Release、同题目集合）得到同一个 `candidate_manifest_hash`，0027 的
`UNIQUE (blueprint_version_id, candidate_manifest_hash)` 保证只落一行，重试返回
原行并带 `created=false`（HTTP 200）。Idempotency-Key 仍然按要求校验——它是接口
契约（§0.3 规则 3），只是不承担去重职责。

为什么追加版本要 base_version_id：草稿编辑是"读—改—写"，两个窗口同时打开时后写的
那个人必须知道自己覆盖了谁。基线过期就是 409，不是静默覆盖。

课程范围：本仓库 `course.id` 是文本（`analysis`），而 0027 的 assessment 表是 uuid。
路由负责用 `ref_uuid` 把文本 id 映射成确定性 uuid，并把原文一并交给服务写进
`assessment.id_ref`；响应里两种 id 都给，前端不必自己算 uuid5。
候选范围（purpose/course/offering/release_context）**一律从蓝图版本派生**，
路由不收这些参数——否则换个 release_context_id 就绕过了整个门禁。

错误码（detail 为 `{code, message}`，便于前端分支；401/403 角色错误沿用 auth 的字符串）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 角色不足（auth.require）/ course_scope_denied
    404 course_not_found / blueprint_version_not_found / candidate_snapshot_not_found
    409 idempotency_conflict / stale_base_version / base_version_required
    422 blueprint_invalid / release_context_not_found / candidate_scope_invalid
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import Actor, require
from .blueprints import (
    BaseVersionRequiredError,
    BlueprintService,
    BlueprintValidationError,
    BlueprintVersionNotFoundError,
    BlueprintVersionRow,
    CourseScopeAuthorizer,
    IdempotencyConflictError,
    PostgresBlueprintRepository,
    SCHEMA_VERSION,
    StaleBaseVersionError,
    UnknownCourseError,
    blueprint_id_for_course,
    ref_uuid,
)
from .candidates import (
    CandidateService,
    CandidateSnapshotNotFoundError,
    PostgresCandidateRepository,
    ReleaseContextNotFoundError,
)
from .contracts import (
    AssemblyMode,
    BlueprintContent,
    BlueprintSection,
    CandidateScope,
    CandidateSnapshot,
    Purpose,
    ReviewPolicy,
    Score,
)
from .releases import exercise_uuid

router = APIRouter(prefix="/api/v1", tags=["paper-blueprints"])

# 同一个依赖对象在两处复用：FastAPI 会缓存同一次请求的结果，鉴权只跑一遍；
# 若各自调用 require("teacher")，会生成两个不同的依赖函数，白白查两次身份。
require_teacher = require("teacher")

MAX_IDEMPOTENCY_KEY = 200


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


class BlueprintVersionIn(BaseModel):
    """创建/追加蓝图版本的请求命令。

    `course_ref / offering_ref` 是本仓库的文本 id；`course_id / offering_id` 由
    路由映射成 uuid5 后构造共享契约 `BlueprintContent`——请求体里不接受 uuid，
    否则调用方得自己实现 uuid5，而命名空间一旦算错就是另一个课程。
    """

    model_config = ConfigDict(extra="forbid")

    course_ref: str = Field(min_length=1, max_length=200)
    offering_ref: str | None = Field(default=None, min_length=1, max_length=200)
    release_context_id: UUID
    purpose: Purpose
    total_score: Score
    duration_seconds: int = Field(gt=0)
    sections: tuple[BlueprintSection, ...] = Field(min_length=1)
    hard_constraints: dict[str, object] = {}
    soft_objectives: dict[str, object] = {}
    review_policy: ReviewPolicy
    assembly_mode: AssemblyMode = "manual"
    base_version_id: UUID | None = None

    def to_content(self) -> BlueprintContent:
        return BlueprintContent(
            schema_version=SCHEMA_VERSION,  # type: ignore[arg-type]
            purpose=self.purpose,
            course_id=ref_uuid("course", self.course_ref),
            offering_id=(
                None if self.offering_ref is None
                else ref_uuid("offering", self.offering_ref)
            ),
            release_context_id=self.release_context_id,
            total_score=self.total_score,
            duration_seconds=self.duration_seconds,
            sections=self.sections,
            hard_constraints=self.hard_constraints,
            soft_objectives=self.soft_objectives,
            review_policy=self.review_policy,
            assembly_mode=self.assembly_mode,
        )


def get_blueprint_service(
    actor: Actor = Depends(require_teacher),
) -> BlueprintService:
    """生产服务：PostgreSQL 仓储 + 按班级名单判定的课程范围授权。"""
    return BlueprintService(
        repository=PostgresBlueprintRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


class CourseBlueprintIn(BaseModel):
    """按课程号建/追加蓝图版本：课程号在路径里，不再出现在请求体。

    `slug` 是蓝图别名（期中/期末/补考），和课程号一起推导出稳定的 `blueprint_id`——
    同别名 = 同一份蓝图的下一版，换别名 = 另一份蓝图。它进 uuid5 的输入，
    所以只允许可读且无歧义的字符（不能带 `/` 或 `:`，否则两个别名可能撞成同一个键）。
    """

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(default="manual", min_length=1, max_length=64,
                      pattern=r"^[A-Za-z0-9._-]+$")
    offering_ref: str | None = Field(default=None, min_length=1, max_length=200)
    release_context_id: UUID
    purpose: Purpose
    total_score: Score
    duration_seconds: int = Field(gt=0)
    sections: tuple[BlueprintSection, ...] = Field(min_length=1)
    hard_constraints: dict[str, object] = {}
    soft_objectives: dict[str, object] = {}
    review_policy: ReviewPolicy
    assembly_mode: AssemblyMode = "manual"
    base_version_id: UUID | None = None

    def to_content(self, course_ref: str) -> BlueprintContent:
        return BlueprintContent(
            schema_version=SCHEMA_VERSION,  # type: ignore[arg-type]
            purpose=self.purpose,
            course_id=ref_uuid("course", course_ref),
            offering_id=(
                None if self.offering_ref is None
                else ref_uuid("offering", self.offering_ref)
            ),
            release_context_id=self.release_context_id,
            total_score=self.total_score,
            duration_seconds=self.duration_seconds,
            sections=self.sections,
            hard_constraints=self.hard_constraints,
            soft_objectives=self.soft_objectives,
            review_policy=self.review_policy,
            assembly_mode=self.assembly_mode,
        )


def _require_idempotency_key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key:
        raise _error(400, "idempotency_key_required",
                     "写接口必须带 Idempotency-Key 头，重试才不会创建第二个版本")
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise _error(400, "idempotency_key_required",
                     f"Idempotency-Key 太长（上限 {MAX_IDEMPOTENCY_KEY} 字符）")
    return key


def _create_version(
    service: BlueprintService,
    actor: Actor,
    *,
    content: BlueprintContent,
    blueprint_id: UUID,
    base_version_id: UUID | None,
    idempotency_key: str,
    course_ref: str,
    offering_ref: str | None,
) -> BlueprintVersionRow:
    """创建/追加一版并统一域错误 → HTTP 码的映射。

    两条写路由（uuid 入口与课程号入口）只在"blueprint_id 从哪来"上不同，其余
    语义必须一模一样——分开写两份 except 迟早会漂成两套状态码。
    """
    try:
        return service.create_version(
            actor_id=ref_uuid("account", actor.id),
            content=content,
            blueprint_id=blueprint_id,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
            course_ref=course_ref,
            offering_ref=offering_ref,
            actor_ref=actor.id,
        )
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", f"课程不存在：{exc}") from exc
    except BaseVersionRequiredError as exc:
        raise _error(409, "base_version_required", str(exc)) from exc
    except StaleBaseVersionError as exc:
        raise _error(409, "stale_base_version", str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise _error(409, "idempotency_conflict", str(exc)) from exc
    except BlueprintValidationError as exc:
        raise _error(422, "blueprint_invalid", str(exc)) from exc


def _finish_create(row: BlueprintVersionRow, response: Response) -> BlueprintVersionRow:
    """201 只给"真的新建了版本"；幂等重放与内容去重都是 200。"""
    if not row.created:
        response.status_code = 200
    if row.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return row


@router.post(
    "/paper-blueprints/{blueprint_id}/versions",
    status_code=201,
    response_model=BlueprintVersionRow,
)
def create_blueprint_version(
    blueprint_id: UUID,
    body: BlueprintVersionIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> BlueprintVersionRow:
    key = _require_idempotency_key(idempotency_key)
    row = _create_version(
        service, actor,
        content=body.to_content(),
        blueprint_id=blueprint_id,
        base_version_id=body.base_version_id,
        idempotency_key=key,
        course_ref=body.course_ref,
        offering_ref=body.offering_ref,
    )
    return _finish_create(row, response)


@router.get(
    "/courses/{course_ref}/paper-blueprints",
    response_model=list[BlueprintVersionRow],
)
def list_course_blueprints(
    course_ref: str,
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> list[BlueprintVersionRow]:
    """本课程每份蓝图的最新版本（新→旧）；没有蓝图就是空列表。

    空列表与 404 分开：新课程没有蓝图是正常状态，教师看到的是"先建一份"，
    而不是"这门课不存在"。课程本身不存在仍然是 404。
    """
    try:
        return service.list_for_course(
            actor_id=ref_uuid("account", actor.id),
            course_ref=course_ref,
            actor_ref=actor.id,
        )
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", f"课程不存在：{exc}") from exc
    except BlueprintValidationError as exc:
        raise _error(422, "blueprint_invalid", str(exc)) from exc


@router.post(
    "/courses/{course_ref}/paper-blueprints",
    status_code=201,
    response_model=BlueprintVersionRow,
)
def create_course_blueprint(
    course_ref: str,
    body: CourseBlueprintIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> BlueprintVersionRow:
    """按"课程号 + 蓝图别名"建第 1 版或追加一版。

    别名的推导管在服务端（`blueprint_id_for_course`）：前端只需要说"这是期末卷"，
    不必也不该知道 uuid5 的命名空间。同一别名第二次提交且内容变了，必须带
    `base_version_id`（当前最新版）——否则 409 base_version_required，
    与 uuid 入口的规则完全一致。
    """
    key = _require_idempotency_key(idempotency_key)
    try:
        blueprint_id = blueprint_id_for_course(course_ref, body.slug)
    except ValueError as exc:
        raise _error(422, "blueprint_invalid", str(exc)) from exc
    row = _create_version(
        service, actor,
        content=body.to_content(course_ref.strip()),
        blueprint_id=blueprint_id,
        base_version_id=body.base_version_id,
        idempotency_key=key,
        course_ref=course_ref.strip(),
        offering_ref=body.offering_ref,
    )
    return _finish_create(row, response)



def get_candidate_service(
    actor: Actor = Depends(require_teacher),
) -> CandidateService:
    """生产候选服务：PostgreSQL 仓储 + 与蓝图同一套课程范围授权。"""
    return CandidateService(
        repository=PostgresCandidateRepository(),
        authorizer=CourseScopeAuthorizer(actor),
    )


def _load_blueprint_version(
    blueprints: BlueprintService, version_id: UUID, actor: Actor
) -> BlueprintVersionRow:
    """读蓝图版本并统一错误映射；候选路由沿用它，避免两处状态码漂移。"""
    try:
        return blueprints.get_version(
            actor_id=ref_uuid("account", actor.id),
            version_id=version_id,
            actor_ref=actor.id,
        )
    except BlueprintVersionNotFoundError as exc:
        raise _error(404, "blueprint_version_not_found",
                     f"蓝图版本不存在：{version_id}") from exc
    except UnknownCourseError as exc:
        raise _error(404, "course_not_found", f"课程不存在：{exc}") from exc
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc


def _candidate_scope(version: BlueprintVersionRow) -> CandidateScope:
    try:
        return CandidateScope.from_version(version)
    except ValueError as exc:
        raise _error(422, "candidate_scope_invalid", str(exc)) from exc


@router.post(
    "/paper-blueprint-versions/{version_id}/candidate-snapshots",
    status_code=201,
    response_model=CandidateSnapshot,
)
def create_candidate_snapshot(
    version_id: UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    service: CandidateService = Depends(get_candidate_service),
) -> CandidateSnapshot:
    """生成候选清单并落库；同内容重试返回原行（200 + created=false）。

    请求体是空的：候选范围完全由蓝图版本决定（见模块 docstring）。这样即使有人
    直接构造请求，也没有"指定别的 Release Context"的入口。
    """
    _require_idempotency_key(idempotency_key)
    version = _load_blueprint_version(blueprints, version_id, actor)
    scoped = _candidate_scope(version)
    try:
        snapshot = service.snapshot(
            actor_id=ref_uuid("account", actor.id), scope=scoped)
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except ReleaseContextNotFoundError as exc:
        # 422 而不是 404：蓝图本身存在，是它引用的 Release 配置错了。
        raise _error(422, "release_context_not_found",
                     f"蓝图引用的 Release Context 不存在：{exc}") from exc
    if not snapshot.created:
        response.status_code = 200
    return snapshot


@router.get(
    "/paper-blueprint-versions/{version_id}/candidates",
    response_model=CandidateSnapshot,
)
def get_candidate_snapshot(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
    service: CandidateService = Depends(get_candidate_service),
) -> CandidateSnapshot:
    """读取最近一次候选清单（含逐条拒绝原因码）；没有快照就是 404。"""
    version = _load_blueprint_version(blueprints, version_id, actor)
    scoped = _candidate_scope(version)
    try:
        return service.latest(actor_id=ref_uuid("account", actor.id), scope=scoped)
    except PermissionError as exc:
        raise _error(403, "course_scope_denied", str(exc)) from exc
    except CandidateSnapshotNotFoundError as exc:
        raise _error(404, "candidate_snapshot_not_found",
                     f"该蓝图版本还没有候选快照：{version_id}") from exc


@router.get(
    "/paper-blueprint-versions/{version_id}",
    response_model=BlueprintVersionRow,
)
def get_blueprint_version(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    service: BlueprintService = Depends(get_blueprint_service),
) -> BlueprintVersionRow:
    return _load_blueprint_version(service, version_id, actor)


class CandidateLabelRow(BaseModel):
    """候选题目的可读标签：只做展示，不参与任何哈希。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: UUID
    exercise_ref: str
    title: str
    review_status: str
    license: str | None = None


class CandidateLabelList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CandidateLabelRow]
    count: int


def label_title(prompt_md: str | None, limit: int = 80) -> str:
    """题干 → 一行标签：取第一行非空文本并截断。

    为什么不直接把题干整段塞进下拉框：换行会让 `<option>` 撑成多行，教师反而
    找不到题目；完整题干在编辑器与题目详情页里看，这里只负责"能不能认出来"。
    """
    for line in (prompt_md or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
    return "（无题干）"


@router.get(
    "/paper-blueprint-versions/{version_id}/candidate-labels",
    response_model=CandidateLabelList,
)
def list_candidate_labels(
    version_id: UUID,
    actor: Actor = Depends(require_teacher),
    blueprints: BlueprintService = Depends(get_blueprint_service),
) -> CandidateLabelList:
    """本课程题目的可读标签，键是 `exercise_id`（与候选快照里的 uuid 同源）。

    为什么键要现场算：`exercise_uuid` 是 uuid5(course_ref, exercise_ref)，库里
    没有反向映射列。这里按课程扫一遍题目再算 uuid，与候选快照构建用的是同一个
    函数，所以两边一定对得上；比给 `assessment.id_ref` 加一类"题"记录更省事，
    也不会让 id_ref 表变成业务数据的第二真源。

    授权来自蓝图版本的读取（`_load_blueprint_version` 已经做了课程范围判定），
    所以本接口不会把"别的课有哪些题"漏给不在范围内的教师。
    """
    version = _load_blueprint_version(blueprints, version_id, actor)
    course_ref = (version.course_ref or "").strip()
    if not course_ref:
        raise _error(404, "course_not_found",
                     f"蓝图版本没有课程号映射，无法列出题目：{version_id}")
    rows = db.query(
        """SELECT id, prompt_md, review_status, license
             FROM exercise
            WHERE course_id = %s
            ORDER BY id""",
        (course_ref,),
    )
    items = [
        CandidateLabelRow(
            exercise_id=exercise_uuid(course_ref, str(row["id"])),
            exercise_ref=str(row["id"]),
            title=label_title(row.get("prompt_md")),
            review_status=str(row.get("review_status") or "draft"),
            license=row.get("license"),
        )
        for row in rows
    ]
    return CandidateLabelList(items=items, count=len(items))
