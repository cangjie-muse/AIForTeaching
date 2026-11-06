"""版本化手工蓝图：内容校验、不可变版本、内容哈希、幂等与课程范围授权。

这一层回答四个问题，且只回答这四个：

  1. 这份蓝图能不能成为一个版本？（分值两位小数、总分等于槽位之和、分节/槽位编码
     唯一、正式卷必须有班级与双审策略、阶段 A 只允许手工组卷）
  2. 版本号是多少、基线是谁？（`base_version_id` 乐观锁：追加版本必须指向当前最新，
     过期或缺失都是冲突，绝不覆盖别人的修改）
  3. 这段内容是不是已经存过？（同一蓝图内 `content_hash` 相同 → 复用已有版本，
     对应 0027 的 `UNIQUE (content_hash, blueprint_id)`；重试不会长出新版本）
  4. 这个请求是不是重试？（`Idempotency-Key` + 请求哈希；同键同载荷返回原版本，
     同键不同载荷 409；键按用户隔离，否则会读到别人的蓝图）

身份口径：0027 的 assessment 表把 `course_id / created_by` 定义成 uuid，而本仓库
`course.id`（`analysis`）与 `app_account.id`（`acc-…`）都是 text。二者不可能直接
相等，所以这里用固定的 uuid5 命名空间做确定性映射（`ref_uuid`），并把映射记进
`assessment.id_ref`（0028）供读取时还原文本 id。为什么不把库列改成 text：0027 已
提交、且 uuid 列是后续候选/纸卷/曝光的共同口径；改动面远大于一张桥接表。

持久化：`PostgresBlueprintRepository` 的每次读写都在
`db.transaction(assessment_service=True)` 里——paper_* 表是 FORCE RLS，不在同一
事务里 `SET LOCAL app.assessment_service = 'on'` 就一行都写不进去。版本号分配额外
拿 `pg_advisory_xact_lock`，否则两个并发请求会各自算出同一个 version_no。
本机没有可连的 PostgreSQL，因此内存实现承担单元测试；SQL 由静态断言覆盖。
"""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from . import db
from .contracts import (
    AssemblyMode,
    BlueprintContent,
    Purpose,
    ReviewPolicy,
    Score,
    canonical_hash,
    validate_score,
)

SCHEMA_VERSION = "paper-blueprint/v1"

# 固定命名空间。改它等于让所有既有蓝图的 course_id 失效，绝不能在发布后调整。
ID_NAMESPACE = UUID("8f14e45f-ceea-567f-a1b2-3c4d5e6f7a80")
REF_KINDS = ("course", "offering", "account", "release_context", "paper-blueprint")

WRITE_ACTION = "paper_blueprint.write"
READ_ACTION = "paper_blueprint.read"
IDEMPOTENCY_SCOPE = "paper_blueprint.version.create"


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class BlueprintError(Exception):
    """蓝图域错误的基类；路由层据此映射状态码。"""


class BlueprintValidationError(BlueprintError, ValueError):
    """内容不合法（422）。同时是 ValueError，便于纯服务层调用方按老习惯捕获。"""


class StaleBaseVersionError(BlueprintError):
    """base_version 与当前最新版本不一致（409）。"""


class BaseVersionRequiredError(StaleBaseVersionError):
    """已有历史版本却没带 base_version（409）——不带基线就等于盲目覆盖。"""


class IdempotencyConflictError(BlueprintError):
    """同一幂等键被用于不同载荷（409）。"""


class BlueprintVersionNotFoundError(BlueprintError):
    """版本不存在（404）。"""


class UnknownCourseError(BlueprintError):
    """课程不存在（404）；与"没权限"分开，避免把笔误说成权限问题。"""


def ref_uuid(kind: str, ref: str) -> UUID:
    """文本 id → assessment 空间的确定性 uuid。

    同一门课在开发库、测试库、生产库得到同一个 uuid，蓝图内容哈希因此可跨环境复现。
    """
    if kind not in REF_KINDS:
        raise ValueError(f"unknown ref kind: {kind!r}")
    cleaned = (ref or "").strip()
    if not cleaned:
        raise ValueError("ref 不能为空")
    if len(cleaned) > 200:
        raise ValueError("ref 太长（上限 200 字符）")
    return uuid5(ID_NAMESPACE, f"{kind}:{cleaned}")


def blueprint_id_for_course(course_ref: str, slug: str) -> UUID:
    """一门课里"手工蓝图"的稳定身份：课程号 + 蓝图别名。

    为什么由服务端算而不是让调用方给 uuid：前端拿不到 uuid5 的命名空间，算错一次
    就是另一门课的蓝图；而"同一门课的同一个别名 = 同一份蓝图"正好是教师的心智模型
    （期中卷、期末卷各一个别名），追加版本时也就不必先知道 uuid。
    """
    cleaned_course = (course_ref or "").strip()
    cleaned_slug = (slug or "").strip()
    if not cleaned_course or not cleaned_slug:
        raise ValueError("course_ref 与 slug 都不能为空")
    return ref_uuid("paper-blueprint", f"{cleaned_course}/{cleaned_slug}")


# ---------------------------------------------------------------------------
# 行模型（仓储返回值 = 接口响应体，不再另设 DTO）
# ---------------------------------------------------------------------------

class BlueprintVersionRow(BaseModel):
    """`assessment.paper_blueprint_version` 的一行（含蓝图文本身）。

    `course_ref / offering_ref / created_by_ref` 不属于 0027 的列，来自 0028 的
    `assessment.id_ref` 桥接表——响应里同时给 uuid 和本仓库的文本 id，前端不必
    自己算 uuid5，排查问题时也能直接对上 `course.id`。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    blueprint_id: UUID
    version_no: int = Field(ge=1)
    course_id: UUID
    offering_id: UUID | None = None
    release_context_id: UUID
    purpose: Purpose
    assembly_mode: AssemblyMode
    total_score: Score = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    content: BlueprintContent
    base_version_id: UUID | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    change_set_id: UUID
    created_by: UUID
    created_at: datetime
    course_ref: str | None = None
    offering_ref: str | None = None
    created_by_ref: str | None = None
    # created=True 只出现在"本次请求真的插入了新版本"的响应里；幂等重放、内容去重、
    # 以及 GET 读取都是 False。路由据此把 201 改成 200。
    created: bool = False
    # 仅标记"同一 Idempotency-Key 的重复请求"，用于 Idempotent-Replay 响应头
    replayed: bool = False


class IdempotencyRecord(BaseModel):
    """`assessment.idempotency_record` 的一行：键 + 请求哈希 + 当时的响应快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    scope: str
    idempotency_key: str
    actor_id: UUID
    request_hash: str = Field(min_length=64, max_length=64)
    response: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class BlueprintUnitOfWork(Protocol):
    """一个事务内的读写集合；SQL 实现绑定同一个 cursor，内存实现就是仓储自己。"""

    def lock_blueprint(self, blueprint_id: UUID) -> None: ...

    def latest(self, blueprint_id: UUID) -> BlueprintVersionRow | None: ...

    def latest_for_course(self, course_id: UUID) -> list[BlueprintVersionRow]:
        """本课程每份蓝图的最新版本（一个蓝图一行），按创建时间倒序。"""
        ...

    def find_by_content_hash(self, blueprint_id: UUID,
                             content_hash: str) -> BlueprintVersionRow | None: ...

    def add(self, row: BlueprintVersionRow) -> BlueprintVersionRow: ...

    def get(self, version_id: UUID) -> BlueprintVersionRow | None: ...

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None: ...

    def save_idempotent(self, record: IdempotencyRecord) -> None: ...

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None: ...

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None: ...


class BlueprintRepository(Protocol):
    def unit_of_work(self) -> ContextManager[BlueprintUnitOfWork]: ...


class InMemoryBlueprintRepository:
    """内存仓储：语义与 0027/0028 的唯一键一致，供单元测试与无库环境使用。"""

    def __init__(self) -> None:
        self.versions: dict[UUID, BlueprintVersionRow] = {}
        self.by_content: dict[tuple[UUID, str], BlueprintVersionRow] = {}
        self.idempotency: dict[tuple[str, str, UUID], IdempotencyRecord] = {}
        self.refs: dict[tuple[str, UUID], str] = {}

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryBlueprintRepository"]:
        yield self

    def lock_blueprint(self, blueprint_id: UUID) -> None:
        return None

    def latest(self, blueprint_id: UUID) -> BlueprintVersionRow | None:
        rows = [row for row in self.versions.values() if row.blueprint_id == blueprint_id]
        return max(rows, key=lambda row: row.version_no) if rows else None

    def latest_for_course(self, course_id: UUID) -> list[BlueprintVersionRow]:
        """每份蓝图只留最新一版；排序与 SQL 实现一致（created_at 倒序）。

        为什么是"每份蓝图最新一版"而不是全部版本：教师是在选"用哪份蓝图继续"，
        历史版本对他的这步操作没有意义，却会把列表撑长。
        """
        newest: dict[UUID, BlueprintVersionRow] = {}
        for row in self.versions.values():
            if row.course_id != course_id:
                continue
            current = newest.get(row.blueprint_id)
            if current is None or row.version_no > current.version_no:
                newest[row.blueprint_id] = row
        return sorted(newest.values(), key=lambda row: (row.created_at, str(row.id)),
                      reverse=True)

    def find_by_content_hash(self, blueprint_id: UUID,
                             content_hash: str) -> BlueprintVersionRow | None:
        return self.by_content.get((blueprint_id, content_hash))

    def add(self, row: BlueprintVersionRow) -> BlueprintVersionRow:
        existing = self.by_content.get((row.blueprint_id, row.content_hash))
        if existing is not None:
            return existing
        for other in self.versions.values():
            if other.blueprint_id == row.blueprint_id and other.version_no == row.version_no:
                raise BlueprintError(
                    f"version_no 冲突：{row.blueprint_id}/{row.version_no}"
                )
        self.versions[row.id] = row
        self.by_content[(row.blueprint_id, row.content_hash)] = row
        return row

    def get(self, version_id: UUID) -> BlueprintVersionRow | None:
        return self.versions.get(version_id)

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None:
        return self.idempotency.get((scope, idempotency_key, actor_id))

    def save_idempotent(self, record: IdempotencyRecord) -> None:
        key = (record.scope, record.idempotency_key, record.actor_id)
        if key in self.idempotency:
            raise IdempotencyConflictError(
                f"幂等键已被使用：{record.idempotency_key}"
            )
        self.idempotency[key] = record

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None:
        self.refs.setdefault((kind, subject), ref)

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        return self.refs.get((kind, subject))


_VERSION_COLUMNS = """id, blueprint_id, version_no, course_id, offering_id, release_context_id,
                      purpose, assembly_mode, total_score, duration_seconds, blueprint_ast,
                      base_version_id, content_hash, change_set_id, created_by, created_at"""


def _row_from_db(record: Mapping[str, Any]) -> BlueprintVersionRow:
    """jsonb / numeric 由 psycopg 反序列化成 dict / Decimal，这里只做模型重建。"""
    ast = record.get("blueprint_ast") or {}
    if isinstance(ast, str):  # 防御性：某些驱动把 jsonb 当文本返回
        ast = json.loads(ast)
    return BlueprintVersionRow(
        id=record["id"],
        blueprint_id=record["blueprint_id"],
        version_no=record["version_no"],
        course_id=record["course_id"],
        offering_id=record.get("offering_id"),
        release_context_id=record["release_context_id"],
        purpose=record["purpose"],
        assembly_mode=record["assembly_mode"],
        total_score=record["total_score"],
        duration_seconds=record["duration_seconds"],
        content=BlueprintContent.model_validate(ast),
        base_version_id=record.get("base_version_id"),
        content_hash=record["content_hash"],
        change_set_id=record["change_set_id"],
        created_by=record["created_by"],
        created_at=record["created_at"],
    )


class _PostgresUnitOfWork:
    """所有语句共用 `db.transaction(assessment_service=True)` 的 cursor。"""

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def lock_blueprint(self, blueprint_id: UUID) -> None:
        # 事务级 advisory lock：并发追加版本时把 version_no 分配串行化；
        # 事务结束自动释放，不需要额外解锁路径。
        self.cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(blueprint_id),))

    def _one(self, sql: str, params: tuple) -> BlueprintVersionRow | None:
        self.cur.execute(sql, params)
        record = self.cur.fetchone()
        return _row_from_db(record) if record else None

    def latest(self, blueprint_id: UUID) -> BlueprintVersionRow | None:
        return self._one(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM assessment.paper_blueprint_version
                 WHERE blueprint_id = %s
                 ORDER BY version_no DESC
                 LIMIT 1""",
            (blueprint_id,),
        )

    def find_by_content_hash(self, blueprint_id: UUID,
                             content_hash: str) -> BlueprintVersionRow | None:
        return self._one(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM assessment.paper_blueprint_version
                 WHERE blueprint_id = %s AND content_hash = %s""",
            (blueprint_id, content_hash),
        )

    def add(self, row: BlueprintVersionRow) -> BlueprintVersionRow:
        self.cur.execute(
            f"""INSERT INTO assessment.paper_blueprint_version
                  (id, blueprint_id, version_no, course_id, offering_id,
                   release_context_id, purpose, assembly_mode, total_score,
                   duration_seconds, blueprint_ast, base_version_id, content_hash,
                   change_set_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (content_hash, blueprint_id) DO NOTHING
                RETURNING {_VERSION_COLUMNS}""",
            (
                row.id, row.blueprint_id, row.version_no,
                row.course_id, row.offering_id,
                row.release_context_id, row.purpose, row.assembly_mode,
                row.total_score, row.duration_seconds,
                json.dumps(row.content.model_dump(mode="json"), ensure_ascii=False),
                row.base_version_id,
                row.content_hash, row.change_set_id, row.created_by,
            ),
        )
        inserted = self.cur.fetchone()
        if inserted:
            return _row_from_db(inserted)
        # 唯一键命中：并发/重试已经写入同一内容，复用那一行而不是报 500。
        existing = self.find_by_content_hash(row.blueprint_id, row.content_hash)
        if existing is None:  # pragma: no cover —— 唯一键与查询条件不可能同时落空
            raise BlueprintError("内容去重失败：唯一键命中但读不到原行")
        return existing

    def get(self, version_id: UUID) -> BlueprintVersionRow | None:
        return self._one(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM assessment.paper_blueprint_version
                 WHERE id = %s""",
            (version_id,),
        )

    def latest_for_course(self, course_id: UUID) -> list[BlueprintVersionRow]:
        # DISTINCT ON 是 PostgreSQL 的"每组取一行"：组内按 version_no 倒序，
        # 取到的就是每份蓝图的最新版；外层再按创建时间倒序给教师最近的在最上面。
        # 不用窗口函数是为了让计划能直接走 (course_id, blueprint_id, version_no)
        # 上的索引，而不是先排序整张表再过滤。
        self.cur.execute(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM (
                        SELECT DISTINCT ON (blueprint_id) {_VERSION_COLUMNS}
                          FROM assessment.paper_blueprint_version
                         WHERE course_id = %s
                         ORDER BY blueprint_id, version_no DESC
                       ) latest
                 ORDER BY created_at DESC, id""",
            (course_id,),
        )
        return [_row_from_db(record) for record in self.cur.fetchall()]

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None:
        self.cur.execute(
            """SELECT id, scope, idempotency_key, actor_id, request_hash,
                      response, created_at
                 FROM assessment.idempotency_record
                WHERE scope = %s AND idempotency_key = %s AND actor_id = %s""",
            (scope, idempotency_key, actor_id),
        )
        record = self.cur.fetchone()
        if not record:
            return None
        payload = record["response"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return IdempotencyRecord(
            id=record["id"],
            scope=record["scope"],
            idempotency_key=record["idempotency_key"],
            actor_id=record["actor_id"],
            request_hash=record["request_hash"],
            response=payload,
            created_at=record["created_at"],
        )

    def save_idempotent(self, record: IdempotencyRecord) -> None:
        self.cur.execute(
            """INSERT INTO assessment.idempotency_record
                 (id, scope, idempotency_key, actor_id, request_hash, response)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (scope, idempotency_key, actor_id) DO NOTHING""",
            (
                record.id, record.scope, record.idempotency_key,
                record.actor_id, record.request_hash,
                json.dumps(record.response, ensure_ascii=False),
            ),
        )
        if self.cur.rowcount == 0:
            raise IdempotencyConflictError(
                f"幂等键已被并发请求使用：{record.idempotency_key}"
            )

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None:
        self.cur.execute(
            """INSERT INTO assessment.id_ref (subject, kind, ref)
               VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (subject, kind, ref),
        )

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        self.cur.execute(
            "SELECT ref FROM assessment.id_ref WHERE subject = %s AND kind = %s",
            (subject, kind),
        )
        record = self.cur.fetchone()
        return record["ref"] if record else None


class PostgresBlueprintRepository:
    """assessment 表的 PostgreSQL 仓储。

    每个工作单元都是一个带 RLS 作用域的事务：paper_* 表 FORCE RLS，缺了
    `SET LOCAL app.assessment_service = 'on'` 连表 owner 都写不进去。
    """

    @contextmanager
    def unit_of_work(self) -> Iterator[_PostgresUnitOfWork]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresUnitOfWork(cur)


# ---------------------------------------------------------------------------
# 授权端口的生产实现
# ---------------------------------------------------------------------------

class CourseScopeAuthorizer:
    """教师必须在这门课有班级；管理员放行；课程不存在报 404 而不是 403。

    为什么查 `teaching_class`：本仓库没有独立的课程授权表，"谁教这门课"的既有
    真源就是班级名单（`teacher_id + course_id`）。管理员由 `auth.Actor.has` 放行。
    """

    def __init__(self, actor: Any) -> None:
        self.actor = actor

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if action not in {WRITE_ACTION, READ_ACTION}:
            raise PermissionError(action)
        if self.actor.anonymous:
            raise PermissionError("请先登录")
        course_ref = scope.get("course_ref")
        if not course_ref:
            raise PermissionError("蓝图缺少课程范围（course_ref）")
        if db.query_one("SELECT 1 AS ok FROM course WHERE id = %s", (course_ref,)) is None:
            raise UnknownCourseError(str(course_ref))
        if self.actor.has("admin"):
            return
        if not self.actor.has("teacher"):
            raise PermissionError("需要教师或管理员角色")
        in_scope = db.query_one(
            """SELECT 1 AS ok FROM teaching_class
                WHERE course_id = %s AND teacher_id = %s
                LIMIT 1""",
            (course_ref, self.actor.id),
        )
        if in_scope is None:
            raise PermissionError(f"当前账号在课程 {course_ref} 没有班级")
        offering_ref = scope.get("offering_ref")
        if offering_ref:
            matches = db.query_one(
                """SELECT 1 AS ok FROM teaching_class
                    WHERE id = %s AND course_id = %s AND teacher_id = %s""",
                (offering_ref, course_ref, self.actor.id),
            )
            if matches is None:
                raise PermissionError(f"班级 {offering_ref} 不在课程 {course_ref} 的授权范围内")

    def is_human(self, actor_id: UUID) -> bool:
        # 生产里 actor 只可能来自会话/统一认证，服务账号不经过 HTTP 路由。
        return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

class BlueprintService:
    """`BlueprintService.create_version` 是蓝图写入的唯一入口（计划书 §0.3 规则 8）。"""

    def __init__(
        self,
        repository: BlueprintRepository | None = None,
        authorizer: Any | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self._new_id = id_factory
        self._now = clock

    # -- 校验 ---------------------------------------------------------------

    @staticmethod
    def validate_content(actual: Decimal, expected: Decimal) -> None:
        """分值必须两位小数，且总分等于槽位之和。"""
        try:
            validate_score(actual)
        except ValueError as exc:  # 契约层的错误文案已经是 "two decimal places"
            raise BlueprintValidationError(str(exc)) from exc
        if actual != expected:
            raise BlueprintValidationError(
                f"total_score must equal slot score sum（{actual} != {expected}）"
            )

    @classmethod
    def validate_blueprint(cls, content: BlueprintContent) -> None:
        sections = content.sections
        section_codes = [section.code for section in sections]
        slot_codes = [slot.code for section in sections for slot in section.slots]
        if not sections or not slot_codes or len(set(section_codes)) != len(section_codes):
            raise BlueprintValidationError("sections must be non-empty and uniquely coded")
        if len(set(slot_codes)) != len(slot_codes):
            raise BlueprintValidationError("slot codes must be globally unique")
        for section in sections:
            codes = [slot.code for slot in section.slots]
            if len(set(codes)) != len(codes):
                raise BlueprintValidationError("slot codes must be unique within section")
        total = sum(
            (slot.score for section in sections for slot in section.slots),
            Decimal("0"),
        )
        cls.validate_content(total, content.total_score)
        if content.assembly_mode != "manual":
            raise BlueprintValidationError(
                "阶段 A 只支持手工组卷（assembly_mode=manual）；约束组卷的 assembly_run 不在本阶段"
            )
        if content.purpose == "formal" and (
            content.offering_id is None or content.review_policy != "formal_dual_review"
        ):
            raise BlueprintValidationError(
                "formal paper requires offering and formal_dual_review"
            )

    @staticmethod
    def _check_refs(content: BlueprintContent, course_ref: str | None,
                    offering_ref: str | None, actor_id: UUID,
                    actor_ref: str | None) -> None:
        """文本 ref 与 uuid 必须来自同一映射，否则读取时还原不回来。"""
        if course_ref is not None and content.course_id != ref_uuid("course", course_ref):
            raise BlueprintValidationError("course_ref 与 content.course_id 不一致")
        if offering_ref is not None:
            expected = ref_uuid("offering", offering_ref)
            if content.offering_id != expected:
                raise BlueprintValidationError("offering_ref 与 content.offering_id 不一致")
        if actor_ref is not None and actor_id != ref_uuid("account", actor_ref):
            raise BlueprintValidationError("actor_ref 与 actor_id 不一致")

    @staticmethod
    def request_hash(content: BlueprintContent, base_version_id: UUID | None,
                     course_ref: str | None, offering_ref: str | None) -> str:
        """幂等键对应的请求指纹：载荷有任何变化就不是同一次重试。"""
        return canonical_hash({
            "content": content,
            "base_version_id": base_version_id,
            "course_ref": course_ref,
            "offering_ref": offering_ref,
        })

    # -- 写入 ---------------------------------------------------------------

    def create_version(
        self,
        *,
        actor_id: UUID,
        content: BlueprintContent,
        blueprint_id: UUID | None,
        base_version_id: UUID | None,
        idempotency_key: str | None = None,
        course_ref: str | None = None,
        offering_ref: str | None = None,
        actor_ref: str | None = None,
    ) -> BlueprintVersionRow:
        """创建第 1 版或追加一版；内容相同则复用已有版本。"""
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的写入")
        # 先授权再校验：不能让没权限的人用 422/409 的差异探测内容规则。
        self.authorizer.require(
            actor_id, WRITE_ACTION,
            course_id=content.course_id, offering_id=content.offering_id,
            course_ref=course_ref, offering_ref=offering_ref, actor_ref=actor_ref,
        )
        if not self.authorizer.is_human(actor_id):
            raise PermissionError("蓝图版本只能由人类用户创建")
        self.validate_blueprint(content)
        self._check_refs(content, course_ref, offering_ref, actor_id, actor_ref)
        if idempotency_key is not None:
            cleaned = idempotency_key.strip()
            if not cleaned or len(cleaned) > 200:
                raise BlueprintValidationError(
                    "Idempotency-Key 不能为空且不超过 200 字符"
                )
            idempotency_key = cleaned

        stable_blueprint_id = blueprint_id or self._new_id()
        content_hash = canonical_hash(content)
        fingerprint = self.request_hash(content, base_version_id, course_ref, offering_ref)

        with self._unit_of_work() as uow:
            previous = None
            if uow is not None:
                uow.lock_blueprint(stable_blueprint_id)
                previous = uow.latest(stable_blueprint_id)

            # 1) 幂等重放：必须在基线判断之前——重试不该因为"已经推进了一版"而 409。
            if idempotency_key is not None and uow is not None:
                record = uow.find_idempotent(IDEMPOTENCY_SCOPE, idempotency_key, actor_id)
                if record is not None:
                    if record.request_hash != fingerprint:
                        raise IdempotencyConflictError(
                            "同一 Idempotency-Key 被用于不同的蓝图内容"
                        )
                    stored = BlueprintVersionRow.model_validate(record.response)
                    return stored.model_copy(
                        update={"created": False, "replayed": True}
                    )

            # 2) 内容去重：同蓝图内同哈希复用已有版本（对应唯一键），重试不新增版本。
            if uow is not None:
                existing = uow.find_by_content_hash(stable_blueprint_id, content_hash)
                if existing is not None:
                    return existing.model_copy(update={"created": False})

            # 3) 乐观锁：追加必须指向当前最新版本。
            if previous is None:
                if base_version_id is not None:
                    raise StaleBaseVersionError("蓝图还没有历史版本，base_version_id 必须为空")
                version_no = 1
            else:
                if base_version_id is None:
                    raise BaseVersionRequiredError(
                        "追加版本必须带 base_version_id（当前最新版本）"
                    )
                if base_version_id != previous.id:
                    raise StaleBaseVersionError(
                        f"base_version_id 不是当前最新版本：最新为 {previous.id}"
                    )
                version_no = previous.version_no + 1

            row = BlueprintVersionRow(
                id=self._new_id(),
                blueprint_id=stable_blueprint_id,
                version_no=version_no,
                course_id=content.course_id,
                offering_id=content.offering_id,
                release_context_id=content.release_context_id,
                purpose=content.purpose,
                assembly_mode=content.assembly_mode,
                total_score=content.total_score,
                duration_seconds=content.duration_seconds,
                content=content,
                base_version_id=base_version_id,
                content_hash=content_hash,
                change_set_id=self._new_id(),
                created_by=actor_id,
                created_at=self._now(),
                course_ref=course_ref,
                offering_ref=offering_ref,
                created_by_ref=actor_ref,
                created=True,
            )
            if uow is None:  # 无仓储的纯校验模式（计划书任务 2 的单测入口）
                return row

            stored = uow.add(row)
            if stored.id != row.id:
                # 并发写入同一内容：复用已存在的那一版
                return stored.model_copy(update={"created": False})
            if course_ref:
                uow.remember_ref("course", course_ref, content.course_id)
            if offering_ref:
                uow.remember_ref("offering", offering_ref, content.offering_id)
            if actor_ref:
                uow.remember_ref("account", actor_ref, actor_id)
            if idempotency_key is not None:
                uow.save_idempotent(IdempotencyRecord(
                    id=self._new_id(),
                    scope=IDEMPOTENCY_SCOPE,
                    idempotency_key=idempotency_key,
                    actor_id=actor_id,
                    request_hash=fingerprint,
                    response=row.model_dump(mode="json"),
                    created_at=self._now(),
                ))
            return row

    # -- 读取 ---------------------------------------------------------------

    def list_for_course(self, *, actor_id: UUID, course_ref: str,
                        actor_ref: str | None = None) -> list[BlueprintVersionRow]:
        """本课程每份蓝图的最新版本（新→旧），供教师"接着上一份继续"。

        授权用文本课程号而不是 uuid：课程范围的真源是 `teaching_class.course_id`
        （text），只有它才能回答"这位教师教不教这门课"。授权在查询之前完成，
        没权限的人拿不到"这门课有几份蓝图"这种信息。
        """
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的读取")
        cleaned = (course_ref or "").strip()
        if not cleaned:
            raise BlueprintValidationError("course_ref 不能为空")
        course_id = ref_uuid("course", cleaned)
        self.authorizer.require(
            actor_id, READ_ACTION,
            course_id=course_id, course_ref=cleaned, actor_ref=actor_ref,
        )
        with self._unit_of_work() as uow:
            if uow is None:
                return []
            rows = uow.latest_for_course(course_id)
            refs: dict[UUID, str | None] = {}
            for row in rows:
                if row.offering_id is not None and row.offering_id not in refs:
                    refs[row.offering_id] = (
                        row.offering_ref or uow.lookup_ref("offering", row.offering_id)
                    )
        return [
            row.model_copy(update={
                "course_ref": cleaned,
                "offering_ref": refs.get(row.offering_id) if row.offering_id else None,
            })
            for row in rows
        ]

    def get_version(self, *, actor_id: UUID, version_id: UUID,
                    actor_ref: str | None = None) -> BlueprintVersionRow:
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的读取")
        with self._unit_of_work() as uow:
            if uow is None:
                raise BlueprintVersionNotFoundError(str(version_id))
            row = uow.get(version_id)
            if row is None:
                raise BlueprintVersionNotFoundError(str(version_id))
            course_ref = row.course_ref or uow.lookup_ref("course", row.course_id)
            offering_ref = row.offering_ref or uow.lookup_ref("offering", row.offering_id)
            created_by_ref = row.created_by_ref or uow.lookup_ref("account", row.created_by)
        row = row.model_copy(update={
            "course_ref": course_ref,
            "offering_ref": offering_ref,
            "created_by_ref": created_by_ref,
        })
        self.authorizer.require(
            actor_id, READ_ACTION,
            course_id=row.course_id, offering_id=row.offering_id,
            course_ref=course_ref, offering_ref=offering_ref, actor_ref=actor_ref,
        )
        return row

    # -- 内部 ---------------------------------------------------------------

    def _unit_of_work(self) -> ContextManager[BlueprintUnitOfWork | None]:
        if self.repository is None:
            return nullcontext(None)
        return self.repository.unit_of_work()
