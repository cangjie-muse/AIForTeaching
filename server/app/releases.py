"""固定 Release Context 与内容寻址题目版本（不建 exercise_version 表）。

## 为什么用内容寻址版本

本仓库没有 Release / exercise_version / exercise_instance 体系：`exercise` 是
`(course_id text, id text)` 单行，题干/答案原地 UPDATE（`exercise_revision` 只留
prompt/answer/difficulty 的历史，不记录许可与审核状态），也没有 license 表。
计划书假定的 `release_context / release_item / exercise_version` 一个都不存在。

版本 id 用内容寻址：

    content_hash        = sha256(canonical_json({prompt, answer}))
    exercise_version_id = uuid5(固定命名空间, "exercise_version:{course}:{exercise}:{content_hash}")

于是任何一次编辑都会换哈希 → 换版本 id，不需要版本表；同一个内容在任何环境
（开发库/测试库/生产）都得到同一个 id，候选快照的哈希因此可跨环境复现。

**已知限制（必须写进交付说明）**：

* 快照只存 uuid + 内容哈希 + 快照时刻的 review_status/license，**不存正文**，
  所以"直接读出旧版本正文"做不到。
* 有一条**尽力而为**的补历史路径：遍历 `exercise_revision`（触发器记录
  prompt/answer/difficulty/node_ids 的历史）逐版本重算哈希，命中 version id 就能
  找回当时的 prompt/answer。但它不记录 review_status 与 license，所以**当时的资格
  结论无法复原**；题目行被 DELETE 时 revision 级联删除，旧正文也就没了。
* 因此也无法做"依赖失效传播"：改了答案不会自动把引用旧版本的卷标成失效，
  只能靠重新冻结 + 候选校验时的 `content_drift` 发现。

## 为什么仍然需要一个最小的 release_context / release_item

"属于固定 Release Context"是硬门槛。没有成员关系表，这个门槛只能由调用方传一个
布尔值——那正是任务书禁止的"假装 Release 体系已存在"。所以 0029 只新增两张表：
release_context（含 base 链）+ release_item（固定 version id 与内容哈希），
不建 exercise_version / exercise_instance 表。它是"固定这一版"的事实记录，
不是第二套内容真源：题目正文仍然只在 `exercise` 里。

## 冻结（freeze）是什么

把当前 `exercise` 的内容算出版本 id 并写进一个 Release Context，此后的候选资格
都以"Release 固定的那个版本"为准：题目再被原地编辑，Release 就会判成
`content_drift`（需重新冻结），而不是悄悄用新内容去印审核过的卷。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Iterable, Iterator, Literal, Protocol, Sequence
from uuid import UUID, uuid4, uuid5

from pydantic import Field

from . import db
from .blueprints import ID_NAMESPACE
from .contracts import ContractModel, Purpose, canonical_hash

CONTENT_SCHEMA = "exercise-content/v1"
RELEASE_SCHEMA = "release-context/v1"

ReleaseStatus = Literal["open", "frozen", "retired"]
ReleaseItemState = Literal["included", "excluded", "withdrawn"]

# review_status（本仓库唯一审核真源）→ 候选资格口径的终态。
# 显式写成常量而不是散落在服务里的 in (...)：漏一个状态就等于放过一批未审的题。
APPROVED_REVIEW_STATUSES = frozenset({"teacher-reviewed", "published"})
REVOKED_REVIEW_STATUSES = frozenset({"retired"})
PENDING_REVIEW_STATUSES = frozenset({"draft", "machine-checked"})

# license 是自由文本（DB 里没有枚举）。这里给一个**显式拒绝集合**而不是白名单：
# 出版社名称之类的自由文本不该被判成"无效"，但"unknown/unlicensed"这类占位符必须拦。
# 已知限制：真正的许可合规判断仍要人来做，这里只挡明显没许可的。
INVALID_LICENSES = frozenset({
    "unknown", "unlicensed", "none", "n/a", "na", "denied",
    "all-rights-reserved", "proprietary", "待定", "未知", "无",
})

SUPPORTED_LANGUAGES = frozenset({"zh", "en"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 内容寻址
# ---------------------------------------------------------------------------

def content_hash(prompt_md: str, answer_md: str | None) -> str:
    """题干 + 答案的规范哈希。

    只哈希正文两列：难度、知识点、来源属于"元数据"，改它们不该换版本 id——否则
    挂一个知识点就让已批准的卷全部失效。解析（solution）在本仓库没有列，等有了
    再进哈希，届时所有版本 id 会整体换代（一次性、可接受）。
    """
    return canonical_hash({
        "schema": CONTENT_SCHEMA,
        "prompt": prompt_md or "",
        "answer": answer_md or "",
    })


def _key(course_ref: str, exercise_ref: str) -> str:
    return f"{course_ref}:{exercise_ref}"


def exercise_uuid(course_ref: str, exercise_ref: str) -> UUID:
    """稳定题目身份（等价于"同族题"）：只由课程号 + 题号决定。"""
    return uuid5(ID_NAMESPACE, f"exercise:{_key(course_ref, exercise_ref)}")


def family_id_for(course_ref: str, exercise_ref: str) -> UUID:
    """同族题 id：本仓库没有 exercise_variant 分组（全部为 0 行），

    因此"同族"= 同一道稳定题目（`exercise.id` 相同）。曝光冲突、去重都按它算：
    改一个答案不该让一道题"换个族"从而绕过曝光门禁。
    """
    return uuid5(ID_NAMESPACE, f"exercise_family:{_key(course_ref, exercise_ref)}")


def version_id_for(course_ref: str, exercise_ref: str, digest: str) -> UUID:
    """内容寻址版本 id：同内容同 id，改一个字就换 id。"""
    return uuid5(ID_NAMESPACE, f"exercise_version:{_key(course_ref, exercise_ref)}:{digest}")


def concept_uuid(course_ref: str, node_ref: str) -> UUID:
    """知识点 id 映射（`knowledge_node.id` 是 text）。"""
    return uuid5(ID_NAMESPACE, f"concept:{course_ref}:{node_ref}")


# ---------------------------------------------------------------------------
# 行模型
# ---------------------------------------------------------------------------

class ExerciseSnapshot(ContractModel):
    """冻结时读到的一行 `exercise`（只含候选资格与版本需要的列）。"""

    course_ref: str = Field(min_length=1, max_length=200)
    exercise_ref: str = Field(min_length=1, max_length=200)
    prompt_md: str
    answer_md: str | None = None
    review_status: str = Field(min_length=1, max_length=40)
    license: str | None = Field(default=None, max_length=200)
    language: str = Field(default="zh", max_length=20)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    content_version: int = Field(default=1, ge=1)
    node_refs: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return content_hash(self.prompt_md, self.answer_md)

    @property
    def version_id(self) -> UUID:
        return version_id_for(self.course_ref, self.exercise_ref, self.digest)

    @property
    def family_id(self) -> UUID:
        return family_id_for(self.course_ref, self.exercise_ref)

    @property
    def exercise_id(self) -> UUID:
        return exercise_uuid(self.course_ref, self.exercise_ref)

    @property
    def answer_present(self) -> bool:
        return bool((self.answer_md or "").strip())


class ReleaseItem(ContractModel):
    """`assessment.release_item` 一行：某个 Release 固定了题目的哪一版。"""

    id: UUID
    release_context_id: UUID
    source_release_id: UUID
    exercise_id: UUID
    exercise_version_id: UUID
    content_hash: str = Field(min_length=64, max_length=64)
    exercise_family_id: UUID
    course_ref: str = Field(min_length=1, max_length=200)
    exercise_ref: str = Field(min_length=1, max_length=200)
    state: ReleaseItemState = "included"
    decided_by: UUID
    decided_at: datetime


class ReleaseContext(ContractModel):
    """`assessment.release_context` 一行：一个固定下来的取题范围。"""

    id: UUID
    course_id: UUID
    course_ref: str = Field(min_length=1, max_length=200)
    offering_id: UUID | None = None
    offering_ref: str | None = Field(default=None, max_length=200)
    purpose: Purpose
    status: ReleaseStatus = "frozen"
    base_release_id: UUID | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    item_count: int = Field(ge=0)
    created_by: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class ReleaseError(Exception):
    """Release 域错误基类。"""


class UnknownReleaseError(ReleaseError):
    """base_release_id 指向不存在的 Release。"""


class ReleaseScopeError(ReleaseError, ValueError):
    """base Release 或题目不属于本课程。"""


class UnknownExerciseError(ReleaseError, ValueError):
    """要冻结的题号在课程里不存在。"""


class DuplicateReleaseItemError(ReleaseError, ValueError):
    """同一次冻结里出现重复题号（会让 (context, exercise) 唯一键直接炸）。"""


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class ReleaseRepository(Protocol):
    def unit_of_work(self) -> ContextManager["ReleaseUnitOfWork"]: ...


class ReleaseUnitOfWork(Protocol):
    def load_exercises(self, course_ref: str,
                       exercise_refs: Sequence[str]) -> tuple[ExerciseSnapshot, ...]: ...

    def get_context(self, release_context_id: UUID) -> ReleaseContext | None: ...

    def list_items(self, release_context_id: UUID) -> tuple[ReleaseItem, ...]: ...

    def save(self, context: ReleaseContext,
             items: tuple[ReleaseItem, ...]) -> ReleaseContext: ...


# ---------------------------------------------------------------------------
# 内存实现（单元测试 / 无库环境）
# ---------------------------------------------------------------------------

class InMemoryReleaseRepository:
    def __init__(self, exercises: Iterable[ExerciseSnapshot] = ()) -> None:
        self.exercises: dict[tuple[str, str], ExerciseSnapshot] = {
            (item.course_ref, item.exercise_ref): item for item in exercises
        }
        self.contexts: dict[UUID, ReleaseContext] = {}
        self.items: dict[UUID, tuple[ReleaseItem, ...]] = {}

    def upsert_exercise(self, snapshot: ExerciseSnapshot) -> None:
        self.exercises[(snapshot.course_ref, snapshot.exercise_ref)] = snapshot

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryReleaseRepository"]:
        yield self

    def load_exercises(self, course_ref: str,
                       exercise_refs: Sequence[str]) -> tuple[ExerciseSnapshot, ...]:
        found = [
            self.exercises[(course_ref, ref)]
            for ref in exercise_refs
            if (course_ref, ref) in self.exercises
        ]
        return tuple(found)

    def get_context(self, release_context_id: UUID) -> ReleaseContext | None:
        return self.contexts.get(release_context_id)

    def list_items(self, release_context_id: UUID) -> tuple[ReleaseItem, ...]:
        return self.items.get(release_context_id, ())

    def save(self, context: ReleaseContext,
             items: tuple[ReleaseItem, ...]) -> ReleaseContext:
        stored = self.contexts.get(context.id)
        if stored is not None:
            return stored
        self.contexts[context.id] = context
        self.items[context.id] = items
        return context


class PostgresReleaseRepository:
    """`assessment.release_context / release_item` 的 PostgreSQL 仓储。

    所有语句都在 `db.transaction(assessment_service=True)` 里：release 表与
    paper_* 同属 assessment 命名空间，写路径统一带 RLS 作用域，避免以后回头补。
    """

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresReleaseUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresReleaseUnitOfWork(cur)


_CONTEXT_COLUMNS = """id, course_id, course_ref, offering_id, offering_ref, purpose,
                      status, base_release_id, content_hash, item_count,
                      created_by, created_at"""

_ITEM_COLUMNS = """id, release_context_id, source_release_id, exercise_id,
                   exercise_version_id, content_hash, exercise_family_id,
                   course_ref, exercise_ref, state, decided_by, decided_at"""


def _context_from_db(row: Any, *, item_count: int | None = None) -> ReleaseContext:
    """行 → 模型。`item_count` 是派生值（不是列），由调用方按场景给。"""
    return ReleaseContext(
        id=row["id"],
        course_id=row["course_id"],
        course_ref=row["course_ref"],
        offering_id=row.get("offering_id"),
        offering_ref=row.get("offering_ref"),
        purpose=row["purpose"],
        status=row["status"],
        base_release_id=row.get("base_release_id"),
        content_hash=row["content_hash"],
        item_count=(
            int(row.get("item_count") or 0) if item_count is None else item_count
        ),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _item_from_db(row: Any) -> ReleaseItem:
    return ReleaseItem(
        id=row["id"],
        release_context_id=row["release_context_id"],
        source_release_id=row["source_release_id"],
        exercise_id=row["exercise_id"],
        exercise_version_id=row["exercise_version_id"],
        content_hash=row["content_hash"],
        exercise_family_id=row["exercise_family_id"],
        course_ref=row["course_ref"],
        exercise_ref=row["exercise_ref"],
        state=row["state"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
    )


class _PostgresReleaseUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def load_exercises(self, course_ref: str,
                       exercise_refs: Sequence[str]) -> tuple[ExerciseSnapshot, ...]:
        self.cur.execute(
            """SELECT e.id AS exercise_ref,
                      e.prompt_md, e.answer_md, e.review_status, e.license,
                      e.language, e.difficulty, e.content_version,
                      ARRAY(
                        SELECT en.node_id FROM exercise_node en
                         WHERE en.course_id = e.course_id AND en.exercise_id = e.id
                      ) AS node_refs
                 FROM exercise e
                WHERE e.course_id = %s AND e.id = ANY(%s)""",
            (course_ref, list(exercise_refs)),
        )
        return tuple(
            ExerciseSnapshot(
                course_ref=course_ref,
                exercise_ref=row["exercise_ref"],
                prompt_md=row["prompt_md"] or "",
                answer_md=row.get("answer_md"),
                review_status=row["review_status"],
                license=row.get("license"),
                language=row.get("language") or "zh",
                difficulty=row.get("difficulty"),
                content_version=row.get("content_version") or 1,
                node_refs=tuple(row.get("node_refs") or ()),
            )
            for row in self.cur.fetchall()
        )

    def get_context(self, release_context_id: UUID) -> ReleaseContext | None:
        # item_count 不落列（避免与 release_item 双写不一致），读取时按 included 现算。
        self.cur.execute(
            f"""SELECT {_CONTEXT_COLUMNS},
                       (
                         SELECT count(*)::int FROM assessment.release_item ri
                          WHERE ri.release_context_id = rc.id AND ri.state = 'included'
                       ) AS item_count
                  FROM assessment.release_context rc
                 WHERE rc.id = %s""",
            (release_context_id,),
        )
        row = self.cur.fetchone()
        return _context_from_db(row) if row else None

    def list_items(self, release_context_id: UUID) -> tuple[ReleaseItem, ...]:
        self.cur.execute(
            f"""SELECT {_ITEM_COLUMNS} FROM assessment.release_item
                 WHERE release_context_id = %s
                 ORDER BY exercise_ref""",
            (release_context_id,),
        )
        return tuple(_item_from_db(row) for row in self.cur.fetchall())

    def save(self, context: ReleaseContext,
             items: tuple[ReleaseItem, ...]) -> ReleaseContext:
        # 同一 id 重复冻结：Release 是不可变事实，返回已存在的那一条，不覆盖。
        self.cur.execute(
            f"""INSERT INTO assessment.release_context
                  (id, course_id, course_ref, offering_id, offering_ref, purpose,
                   status, base_release_id, content_hash, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING {_CONTEXT_COLUMNS}""",
            (
                context.id, context.course_id, context.course_ref,
                context.offering_id, context.offering_ref, context.purpose,
                context.status, context.base_release_id, context.content_hash,
                context.created_by,
            ),
        )
        inserted = self.cur.fetchone()
        if not inserted:
            existing = self.get_context(context.id)
            if existing is None:  # pragma: no cover —— 冲突与查询不可能同时落空
                raise ReleaseError("release_context 冲突但读不到原行")
            return existing
        for item in items:
            self.cur.execute(
                """INSERT INTO assessment.release_item
                      (id, release_context_id, source_release_id, exercise_id,
                       exercise_version_id, content_hash, exercise_family_id,
                       course_ref, exercise_ref, state, decided_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (release_context_id, exercise_id) DO NOTHING""",
                (
                    item.id, item.release_context_id, item.source_release_id,
                    item.exercise_id, item.exercise_version_id, item.content_hash,
                    item.exercise_family_id, item.course_ref, item.exercise_ref,
                    item.state, item.decided_by,
                ),
            )
        return _context_from_db(inserted, item_count=len(items))


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class ReleaseService:
    """把当前题目内容冻结进一个 Release Context（唯一的 Release 写入口）。"""

    def __init__(
        self,
        repository: ReleaseRepository | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self._new_id = id_factory
        self._now = clock

    def freeze(
        self,
        *,
        actor_id: UUID,
        course_ref: str,
        purpose: Purpose,
        exercise_refs: Sequence[str],
        offering_ref: str | None = None,
        base_release_id: UUID | None = None,
        status: ReleaseStatus = "frozen",
        label: str | None = None,
        release_context_id: UUID | None = None,
        source_release_id: UUID | None = None,
    ) -> ReleaseContext:
        if self.repository is None:
            raise ReleaseError("缺少 Release 仓储：拒绝无持久化的冻结")
        refs = tuple(dict.fromkeys(str(ref).strip() for ref in exercise_refs))
        if not refs or any(not ref for ref in refs):
            raise ReleaseScopeError("冻结必须给出至少一个非空题号")
        if len(refs) != len(tuple(exercise_refs)):
            raise DuplicateReleaseItemError("同一次冻结里出现重复题号")

        context_id = release_context_id or self._new_id()
        now = self._now()

        with self.repository.unit_of_work() as uow:
            exercises = uow.load_exercises(course_ref, refs)
            missing = sorted(set(refs) - {item.exercise_ref for item in exercises})
            if missing:
                raise UnknownExerciseError(
                    f"课程 {course_ref} 里没有这些题号：{'、'.join(missing)}"
                )
            if base_release_id is not None:
                base = uow.get_context(base_release_id)
                if base is None:
                    raise UnknownReleaseError(str(base_release_id))
                if base.course_ref != course_ref:
                    raise ReleaseScopeError(
                        f"base Release 属于课程 {base.course_ref}，不能作为 {course_ref} 的基础"
                    )
            ordered = sorted(exercises, key=lambda item: item.exercise_ref)
            items = tuple(
                ReleaseItem(
                    id=self._new_id(),
                    release_context_id=context_id,
                    # 本仓库没有上游 Release 链：来源就是本 Release 自己固定的这一版
                    source_release_id=source_release_id or context_id,
                    exercise_id=item.exercise_id,
                    exercise_version_id=item.version_id,
                    content_hash=item.digest,
                    exercise_family_id=item.family_id,
                    course_ref=item.course_ref,
                    exercise_ref=item.exercise_ref,
                    state="included",
                    decided_by=actor_id,
                    decided_at=now,
                )
                for item in ordered
            )
            context = ReleaseContext(
                id=context_id,
                course_id=uuid5(ID_NAMESPACE, f"course:{course_ref}"),
                course_ref=course_ref,
                offering_id=(
                    None if offering_ref is None
                    else uuid5(ID_NAMESPACE, f"offering:{offering_ref}")
                ),
                offering_ref=offering_ref,
                purpose=purpose,
                status=status,
                base_release_id=base_release_id,
                content_hash=canonical_hash({
                    "schema": RELEASE_SCHEMA,
                    "course_ref": course_ref,
                    "offering_ref": offering_ref,
                    "purpose": purpose,
                    "status": status,
                    "base_release_id": base_release_id,
                    "label": label,
                    "items": [
                        {
                            "exercise_ref": item.exercise_ref,
                            "exercise_version_id": item.exercise_version_id,
                            "content_hash": item.content_hash,
                            "state": item.state,
                        }
                        for item in items
                    ],
                }),
                item_count=len(items),
                created_by=actor_id,
                created_at=now,
            )
            return uow.save(context, items)

    def freeze_idempotent(
        self,
        *,
        actor_id: UUID,
        course_ref: str,
        purpose: Purpose,
        exercise_refs: Sequence[str],
        offering_ref: str | None = None,
        base_release_id: UUID | None = None,
        status: ReleaseStatus = "frozen",
        label: str | None = None,
    ) -> tuple[ReleaseContext, bool]:
        """内容寻址的冻结：同一请求（同一批内容）永远得到同一个 Release id。

        重试安全不靠 Idempotency-Key，而是靠 id 由内容决定：

            release_context_id = uuid5(ns, "release:{指纹}")

        指纹包含课程/班级/用途/状态/base/label 与每条 (题号, 内容哈希)。内容变了
        id 就跟着变——那本来就该是一条新 Release，不是重放。`save` 对同 id 用
        `ON CONFLICT (id) DO NOTHING` 并返回已有行，所以并发重复请求只会有一条
        落库，另一个请求拿到同一行（created=False）。

        为什么这里不强求 Idempotency-Key：冻结是"把当前内容固定下来"，它的输入
        完整决定了输出，本来就没有"同一请求产生两种结果"的可能。给一个纯函数式
        操作套幂等键只会变成走过场。
        """
        if self.repository is None:
            raise ReleaseError("缺少 Release 仓储：拒绝无持久化的冻结")
        refs = tuple(dict.fromkeys(str(ref).strip() for ref in exercise_refs))
        if not refs or any(not ref for ref in refs):
            raise ReleaseScopeError("冻结必须给出至少一个非空题号")

        with self.repository.unit_of_work() as uow:
            exercises = uow.load_exercises(course_ref, refs)
            missing = sorted(set(refs) - {item.exercise_ref for item in exercises})
            if missing:
                raise UnknownExerciseError(
                    f"课程 {course_ref} 里没有这些题号：{'、'.join(missing)}"
                )
            fingerprint = canonical_hash({
                "schema": "release-freeze/v1",
                "course_ref": course_ref,
                "offering_ref": offering_ref,
                "purpose": purpose,
                "status": status,
                "base_release_id": base_release_id,
                "label": label,
                "items": [
                    {"exercise_ref": item.exercise_ref, "digest": item.digest}
                    for item in sorted(exercises, key=lambda entry: entry.exercise_ref)
                ],
            })
            context_id = uuid5(ID_NAMESPACE, f"release:{fingerprint}")
            existing = uow.get_context(context_id)

        context = self.freeze(
            actor_id=actor_id, course_ref=course_ref, purpose=purpose,
            exercise_refs=refs, offering_ref=offering_ref,
            base_release_id=base_release_id, status=status, label=label,
            release_context_id=context_id,
        )
        return context, existing is None

    def get(self, release_context_id: UUID) -> ReleaseContext:
        if self.repository is None:
            raise ReleaseError("缺少 Release 仓储")
        with self.repository.unit_of_work() as uow:
            context = uow.get_context(release_context_id)
        if context is None:
            raise UnknownReleaseError(str(release_context_id))
        return context

    def items(self, release_context_id: UUID) -> tuple[ReleaseItem, ...]:
        if self.repository is None:
            raise ReleaseError("缺少 Release 仓储")
        with self.repository.unit_of_work() as uow:
            return uow.list_items(release_context_id)


__all__ = [
    "APPROVED_REVIEW_STATUSES",
    "CONTENT_SCHEMA",
    "DuplicateReleaseItemError",
    "ExerciseSnapshot",
    "INVALID_LICENSES",
    "InMemoryReleaseRepository",
    "PENDING_REVIEW_STATUSES",
    "PostgresReleaseRepository",
    "RELEASE_SCHEMA",
    "REVOKED_REVIEW_STATUSES",
    "ReleaseContext",
    "ReleaseError",
    "ReleaseItem",
    "ReleaseItemState",
    "ReleaseScopeError",
    "ReleaseService",
    "ReleaseStatus",
    "SUPPORTED_LANGUAGES",
    "UnknownExerciseError",
    "UnknownReleaseError",
    "concept_uuid",
    "content_hash",
    "exercise_uuid",
    "family_id_for",
    "utcnow",
    "version_id_for",
]
