"""打印作业（assignment）：固定投影、发布终检与严重撤回（阶段 A 任务 8）。

## 两条 assignment 链路，边界必须说清楚

* `public.assignment` / `assignment_item`（0001）：**作业提交与批改**链路，
  `server/app/homework.py` 在用，text 主键，配 `submission` / `submission_page`。
* `assessment.assignment` / `assignment_item`（0027）：**打印卷发布**链路，
  uuid 主键，钉死 release / version / family / 分值，item 追加写。

本模块只服务后者。两条链路不共享行、不互相读。将来若要合并，必须先把 0028 的
身份桥（`assessment.id_ref`）铺到作业链路上，否则 `acc-…` 与 uuid 混在一张表里
只会产生第三种口径。

## 为什么发布终检必须在 advisory lock 下

曝光冲突是**跨卷、跨班**的判断：两个教师同时发布同一门课的两份正式卷，各自读到
`assessment.item_exposure_event` 都不含对方尚未写入的 `secure_assigned`，两边都
通过——学生却同时拿到两份含同一道题的保密卷。锁键取
`assignment-publish:{course_ref}`（课程粒度），不是 paper 或 assignment 粒度：
冲突判定的作用域就是课程（§14.2 明确禁止"同一门课自动共享保密题"），锁得更细
等于没锁。

## 固定投影

发布时把 `release_context_id / source_release_id / exercise_version_id /
exercise_instance_id / exercise_family_id / 分值 / 内容哈希` 全部从槽位复制进
`assignment_item`。发布之后教师再改卷（克隆新版本、换题）都不影响已发布作业：
学生手上那张纸的每一题都还能追到"哪一版、哪个 Release"。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterable, Literal, Mapping, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import Field

from .contracts import (
    ContractModel,
    ExposureEvent,
    Score,
    canonical_hash,
)
from .contracts.common import validate_score
from .blueprints import ID_NAMESPACE, ref_uuid
from .db import transaction as db_transaction
from .exposure import ExposureConflict, ExposureService
from .papers import (
    PAPER_READ_ACTION,
    PAPER_WRITE_ACTION,
    PaperVersionRow,
    load_paper_version,
)
from .releases import utcnow

AssignmentStatus = Literal["draft", "scheduled", "published", "closed", "withdrawn"]

PUBLISH_SCOPE = "assignment.publish"
WITHDRAW_SCOPE = "assignment.withdraw"
CREATE_SCOPE = "assignment.create"

MAX_IDEMPOTENCY_KEY = 200
MAX_REASON = 500

# 打印发布的正式卷只有一种策略：一次性、交卷后不再给尝试。阶段 A 不做在线作答，
# 这里固定成一条"行为明确"的策略，而不是留 NULL 让阶段 C 猜。
DEFAULT_POLICY = {
    "policy_kind": "formal",
    "behavior": "deferred",
    "max_attempts": 1,
    "grace_seconds": 0,
    "late_policy": "reject",
    "show_syntax_errors": True,
    "show_math_interpretation": True,
    "show_input_preview": True,
    "correctness_release": "after_close",
    "score_release": "after_close",
    "hint_release": "never",
    "solution_release": "after_close",
    "answer_release": "after_close",
    "allow_item_check": False,
    "max_hint_level": 0,
    "allow_retry": False,
    "practice_penalty": Decimal("0.00"),
    "allow_flag": True,
    "allow_review": False,
    "allow_upload": True,
    "autosave_debounce_ms": 800,
    "autosave_max_interval_ms": 5000,
    "conflict_policy": "per_slot",
}


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class AssignmentError(Exception):
    """打印作业的域错误基类。"""


class AssignmentNotFoundError(AssignmentError, LookupError):
    pass


class AssignmentNotApprovedError(AssignmentError):
    """卷子不是 approved，或审核证据已失效（review_manifest_hash 不匹配）。"""


class AssignmentStateError(AssignmentError):
    """状态机不允许：比如撤回一份还没发布的作业。"""


class AssignmentTimeOrderError(AssignmentError, ValueError):
    """visible_at <= due_at <= hard_close_at 不成立。"""


class AssignmentSourceIncompleteError(AssignmentError, ValueError):
    """槽位缺固定来源：发布出来的作业项必须每一项都能追溯。"""


class AssignmentExposureConflictError(AssignmentError):
    """曝光冲突：这道题的另一份保密卷/已公开答案与本卷冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AssignmentIdempotencyConflictError(AssignmentError):
    """同一 Idempotency-Key 被用在不同的载荷上。"""


class AssignmentInputError(AssignmentError, ValueError):
    pass


# ---------------------------------------------------------------------------
# 行模型
# ---------------------------------------------------------------------------

class AssignmentRow(ContractModel):
    """`assessment.assignment` 的一行（只读快照；写用 model_copy 再 save）。"""

    id: UUID
    course_id: UUID
    course_ref: str
    offering_id: UUID
    offering_ref: str | None = None
    paper_version_id: UUID
    release_context_id: UUID
    attempt_policy_version_id: UUID
    visible_at: datetime
    due_at: datetime
    hard_close_at: datetime
    # 阶段 A 只有打印卷；阶段 C 打开 online/both（0036 已扩 CHECK）。
    distribution_kind: Literal["print", "online", "both"] = "print"
    status: AssignmentStatus = "draft"
    created_by: UUID
    created_at: datetime
    published_at: datetime | None = None
    withdrawn_at: datetime | None = None
    publish_manifest_hash: str | None = None
    review_manifest_hash: str | None = None
    withdrawal_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.status in ("scheduled", "published")


class AssignmentItemRow(ContractModel):
    """`assessment.assignment_item` 的一行：发布时的固定投影。"""

    id: UUID
    assignment_id: UUID
    paper_slot_id: UUID
    ord: int = Field(ge=1)
    release_context_id: UUID
    source_release_id: UUID
    exercise_version_id: UUID
    exercise_instance_id: UUID | None = None
    exercise_family_id: UUID
    score: Score
    content_hash: str = Field(min_length=64, max_length=64)


class AssignmentView(ContractModel):
    """接口返回：作业头 + 固定投影 + 是否重放。"""

    assignment: AssignmentRow
    items: tuple[AssignmentItemRow, ...] = ()
    created: bool = False
    replayed: bool = False


class PublishCheck(ContractModel):
    """发布终检的逐项结论：给前端的不是"失败"，而是"哪几项没过"。"""

    code: str
    passed: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class AssignmentUnitOfWork(Protocol):
    def lock_course(self, course_ref: str) -> None: ...
    def course_exists(self, course_ref: str) -> bool: ...
    def load_paper(self, paper_version_id: UUID) -> PaperVersionRow | None: ...
    def ensure_attempt_policy(self, *, course_ref: str, paper_version_id: UUID) -> UUID: ...
    def add(self, row: AssignmentRow) -> AssignmentRow: ...
    def save(self, row: AssignmentRow) -> AssignmentRow: ...
    def get(self, assignment_id: UUID) -> AssignmentRow | None: ...
    def list_for_paper(self, paper_version_id: UUID) -> tuple[AssignmentRow, ...]: ...
    def items(self, assignment_id: UUID) -> tuple[AssignmentItemRow, ...]: ...
    def save_items(self, rows: Iterable[AssignmentItemRow]) -> None: ...
    def exposure_events(self, family_ids: Iterable[UUID]) -> tuple[ExposureEvent, ...]: ...
    def record_exposure(self, events: Iterable[ExposureEvent]) -> None: ...
    def find_idempotent(self, scope: str, key: str, actor_id: UUID) -> Any | None: ...
    def save_idempotent(self, scope: str, key: str, actor_id: UUID,
                        request_hash: str, response: Mapping[str, Any]) -> None: ...
    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None: ...
    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None: ...


class AssignmentRepository(Protocol):
    def unit_of_work(self) -> ContextManager[AssignmentUnitOfWork]: ...


# ---------------------------------------------------------------------------
# 内存实现（测试与演示用；与 PostgreSQL 实现共享全部规则）
# ---------------------------------------------------------------------------

class InMemoryAssignmentRepository:
    def __init__(self, *, papers: Iterable[PaperVersionRow] = (),
                 courses: Iterable[str] = (),
                 events: Iterable[ExposureEvent] = ()) -> None:
        self.papers = {paper.id: paper for paper in papers}
        self.courses = set(courses)
        self.events: list[ExposureEvent] = list(events)
        self.rows: dict[UUID, AssignmentRow] = {}
        self.items: dict[UUID, list[AssignmentItemRow]] = {}
        self.idempotency: dict[tuple[str, str, UUID], dict[str, Any]] = {}
        self.refs: dict[tuple[str, UUID], str] = {}
        self.policies: dict[UUID, UUID] = {}
        self.locks: list[str] = []

    def add_paper(self, paper: PaperVersionRow) -> None:
        self.papers[paper.id] = paper

    def unit_of_work(self) -> AssignmentUnitOfWork:
        return _InMemoryAssignmentUnitOfWork(self)


class _InMemoryAssignmentUnitOfWork:
    """内存事务：没有真正的回滚，`__exit__` 只用来对齐 `with` 协议。

    内存实现故意不做"失败回滚"：它的用途是验证规则（哪些组合该拒绝），
    而原子性由 PostgreSQL 的事务与 scripts/verify-migrations.cjs 的真引擎断言保证。
    在内存里模拟回滚会给出一种不存在的安全感。
    """

    def __init__(self, repo: InMemoryAssignmentRepository) -> None:
        self.repo = repo

    def __enter__(self) -> "_InMemoryAssignmentUnitOfWork":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def lock_course(self, course_ref: str) -> None:
        self.repo.locks.append(course_ref)

    def course_exists(self, course_ref: str) -> bool:
        return course_ref in self.repo.courses

    def load_paper(self, paper_version_id: UUID) -> PaperVersionRow | None:
        return self.repo.papers.get(paper_version_id)

    def ensure_attempt_policy(self, *, course_ref: str, paper_version_id: UUID) -> UUID:
        key = uuid5(ID_NAMESPACE, f"attempt_policy:{course_ref}:{paper_version_id}")
        self.repo.policies.setdefault(key, key)
        return key

    def add(self, row: AssignmentRow) -> AssignmentRow:
        self.repo.rows[row.id] = row
        self.repo.items.setdefault(row.id, [])
        return row

    def save(self, row: AssignmentRow) -> AssignmentRow:
        self.repo.rows[row.id] = row
        return row

    def get(self, assignment_id: UUID) -> AssignmentRow | None:
        return self.repo.rows.get(assignment_id)

    def list_for_paper(self, paper_version_id: UUID) -> tuple[AssignmentRow, ...]:
        # 新的在前：教师打开卷子时最关心"我刚建的那份发给哪个班了"。
        rows = [row for row in self.repo.rows.values()
                if row.paper_version_id == paper_version_id]
        return tuple(sorted(rows, key=lambda row: row.created_at, reverse=True))

    def items(self, assignment_id: UUID) -> tuple[AssignmentItemRow, ...]:
        return tuple(self.repo.items.get(assignment_id, ()))

    def save_items(self, rows: Iterable[AssignmentItemRow]) -> None:
        for row in rows:
            bucket = self.repo.items.setdefault(row.assignment_id, [])
            bucket.append(row)

    def exposure_events(self, family_ids: Iterable[UUID]) -> tuple[ExposureEvent, ...]:
        wanted = set(family_ids)
        return tuple(event for event in self.repo.events
                     if event.exercise_family_id in wanted)

    def record_exposure(self, events: Iterable[ExposureEvent]) -> None:
        self.repo.events.extend(events)

    def find_idempotent(self, scope: str, key: str, actor_id: UUID) -> Any | None:
        return self.repo.idempotency.get((scope, key, actor_id))

    def save_idempotent(self, scope: str, key: str, actor_id: UUID,
                        request_hash: str, response: Mapping[str, Any]) -> None:
        self.repo.idempotency[(scope, key, actor_id)] = {
            "request_hash": request_hash, "response": dict(response),
        }

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None:
        self.repo.refs[(kind, subject)] = ref

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        return self.repo.refs.get((kind, subject))


# ---------------------------------------------------------------------------
# PostgreSQL 实现
# ---------------------------------------------------------------------------

_ASSIGNMENT_COLUMNS = """id, course_id, offering_id, paper_version_id,
    release_context_id, attempt_policy_version_id, visible_at, due_at,
    hard_close_at, distribution_kind, status, created_by, created_at,
    published_at, withdrawn_at, publish_manifest_hash, review_manifest_hash,
    withdrawal_reason"""

_ITEM_COLUMNS = """id, assignment_id, paper_slot_id, ord, release_context_id,
    source_release_id, exercise_version_id, exercise_instance_id,
    exercise_family_id, score, content_hash"""


class PostgresAssignmentRepository:
    """assessment.assignment / assignment_item 的 PostgreSQL 仓储。

    写路径统一 `db.transaction(assessment_service=True)`：paper_* 是 FORCE RLS，
    没有 SET LOCAL 连表 owner 都读不到。assignment 表本身没有开 RLS（0027 只对
    paper_* 开了），但读卷面仍然需要作用域。
    """

    def unit_of_work(self) -> ContextManager[AssignmentUnitOfWork]:
        return _postgres_uow()


class _PostgresAssignmentUnitOfWork:
    def __init__(self, cur: Any, refs: "_PostgresAssignmentRepository") -> None:
        self.cur = cur
        self.refs = refs

    # -- 读 ---------------------------------------------------------------
    def lock_course(self, course_ref: str) -> None:
        # 事务级 advisory lock：同课程发布串行化。hashtext 是 32 位，碰撞只会让
        # 两门不相干的课偶尔互相排队，不会让同一门课绕过锁——安全性优先于吞吐。
        self.cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"assignment-publish:{course_ref}",),
        )

    def course_exists(self, course_ref: str) -> bool:
        self.cur.execute("SELECT 1 AS ok FROM course WHERE id = %s", (course_ref,))
        return self.cur.fetchone() is not None

    def load_paper(self, paper_version_id: UUID) -> PaperVersionRow | None:
        return load_paper_version(self.cur, paper_version_id)

    def ensure_attempt_policy(self, *, course_ref: str, paper_version_id: UUID) -> UUID:
        policy_id = uuid5(ID_NAMESPACE, f"attempt_policy:{course_ref}:{paper_version_id}")
        version_id = uuid5(ID_NAMESPACE, f"attempt_policy_version:{policy_id}")
        content = dict(DEFAULT_POLICY)
        content_hash = canonical_hash({
            "schema": "attempt-policy/v1",
            "policy_kind": content["policy_kind"],
            "behavior": content["behavior"],
            "max_attempts": content["max_attempts"],
            "late_policy": content["late_policy"],
        })
        content["content_hash"] = content_hash
        self.cur.execute(
            """INSERT INTO assessment.attempt_policy_version (
                   id, policy_id, version_no, policy_kind, content_hash,
                   max_attempts, behavior, time_limit_seconds, grace_seconds,
                   late_policy, show_syntax_errors, show_math_interpretation,
                   show_input_preview, correctness_release, score_release,
                   hint_release, solution_release, answer_release,
                   allow_item_check, max_hint_level, allow_retry, practice_penalty,
                   allow_flag, allow_review, allow_upload, autosave_debounce_ms,
                   autosave_max_interval_ms, conflict_policy)
               VALUES (%s, %s, 1, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (version_id, policy_id, content["policy_kind"], content_hash,
             content["max_attempts"], content["behavior"], content["grace_seconds"],
             content["late_policy"], content["show_syntax_errors"],
             content["show_math_interpretation"], content["show_input_preview"],
             content["correctness_release"], content["score_release"],
             content["hint_release"], content["solution_release"],
             content["answer_release"], content["allow_item_check"],
             content["max_hint_level"], content["allow_retry"],
             content["practice_penalty"], content["allow_flag"],
             content["allow_review"], content["allow_upload"],
             content["autosave_debounce_ms"], content["autosave_max_interval_ms"],
             content["conflict_policy"]),
        )
        return version_id

    def get(self, assignment_id: UUID) -> AssignmentRow | None:
        self.cur.execute(
            f"""SELECT {_ASSIGNMENT_COLUMNS}
                  FROM assessment.assignment
                 WHERE id = %s""",
            (assignment_id,),
        )
        row = self.cur.fetchone()
        return self._row(row) if row else None

    def list_for_paper(self, paper_version_id: UUID) -> tuple[AssignmentRow, ...]:
        """同一份卷可以发给多个班；发布按钮要的是"这份卷现在有哪些作业"。

        按 created_at 倒序：一份卷常常先发一个班做试点，再扩到其它班，
        最上面那条才是教师此刻要操作的对象。
        """
        self.cur.execute(
            f"""SELECT {_ASSIGNMENT_COLUMNS}
                  FROM assessment.assignment
                 WHERE paper_version_id = %s
                 ORDER BY created_at DESC, id""",
            (paper_version_id,),
        )
        return tuple(self._row(row) for row in self.cur.fetchall())

    def items(self, assignment_id: UUID) -> tuple[AssignmentItemRow, ...]:
        self.cur.execute(
            f"""SELECT {_ITEM_COLUMNS}
                  FROM assessment.assignment_item
                 WHERE assignment_id = %s
                 ORDER BY ord""",
            (assignment_id,),
        )
        return tuple(self._item(row) for row in self.cur.fetchall())

    def exposure_events(self, family_ids: Iterable[UUID]) -> tuple[ExposureEvent, ...]:
        wanted = list(dict.fromkeys(family_ids))
        if not wanted:
            return ()
        self.cur.execute(
            """SELECT exercise_version_id, exercise_instance_id,
                      exercise_family_id, scope_offering_id, event_kind,
                      visible_from, occurred_at
                 FROM assessment.item_exposure_event
                WHERE exercise_family_id = ANY(%s)""",
            (wanted,),
        )
        return tuple(ExposureEvent(
            exercise_version_id=row["exercise_version_id"],
            exercise_instance_id=row.get("exercise_instance_id"),
            exercise_family_id=row["exercise_family_id"],
            scope_offering_id=row.get("scope_offering_id"),
            event_kind=row["event_kind"],
            visible_from=row["visible_from"],
            occurred_at=row["occurred_at"],
        ) for row in self.cur.fetchall())

    # -- 写 ---------------------------------------------------------------
    def add(self, row: AssignmentRow) -> AssignmentRow:
        self.cur.execute(
            """INSERT INTO assessment.assignment (
                   id, course_id, offering_id, paper_version_id,
                   release_context_id, attempt_policy_version_id, visible_at,
                   due_at, hard_close_at, distribution_kind, status, created_by,
                   created_at, published_at, withdrawn_at, publish_manifest_hash,
                   review_manifest_hash, withdrawal_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s)""",
            (row.id, row.course_id, row.offering_id, row.paper_version_id,
             row.release_context_id, row.attempt_policy_version_id,
             row.visible_at, row.due_at, row.hard_close_at,
             row.distribution_kind, row.status, row.created_by, row.created_at,
             row.published_at, row.withdrawn_at, row.publish_manifest_hash,
             row.review_manifest_hash, row.withdrawal_reason),
        )
        return row

    def save(self, row: AssignmentRow) -> AssignmentRow:
        self.cur.execute(
            """UPDATE assessment.assignment
                  SET status = %s, published_at = %s, withdrawn_at = %s,
                      publish_manifest_hash = %s, review_manifest_hash = %s,
                      withdrawal_reason = %s
                WHERE id = %s""",
            (row.status, row.published_at, row.withdrawn_at,
             row.publish_manifest_hash, row.review_manifest_hash,
             row.withdrawal_reason, row.id),
        )
        return row

    def save_items(self, rows: Iterable[AssignmentItemRow]) -> None:
        for row in rows:
            self.cur.execute(
                """INSERT INTO assessment.assignment_item (
                       id, assignment_id, paper_slot_id, ord, release_context_id,
                       source_release_id, exercise_version_id,
                       exercise_instance_id, exercise_family_id, score,
                       content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (assignment_id, paper_slot_id) DO NOTHING""",
                (row.id, row.assignment_id, row.paper_slot_id, row.ord,
                 row.release_context_id, row.source_release_id,
                 row.exercise_version_id, row.exercise_instance_id,
                 row.exercise_family_id, row.score, row.content_hash),
            )

    def record_exposure(self, events: Iterable[ExposureEvent]) -> None:
        for event in events:
            self.cur.execute(
                """INSERT INTO assessment.item_exposure_event (
                       id, exercise_version_id, exercise_instance_id,
                       exercise_family_id, paper_version_id, assignment_id,
                       scope_offering_id, event_kind, visible_from,
                       metadata_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (uuid4(), event.exercise_version_id, event.exercise_instance_id,
                 event.exercise_family_id, None, getattr(event, "assignment_id", None),
                 event.scope_offering_id, event.event_kind, event.visible_from,
                 canonical_hash({
                     "kind": event.event_kind,
                     "family": event.exercise_family_id,
                     "offering": event.scope_offering_id,
                     "visible_from": event.visible_from,
                 })),
            )

    def find_idempotent(self, scope: str, key: str, actor_id: UUID) -> Any | None:
        self.cur.execute(
            """SELECT request_hash, response
                 FROM assessment.idempotency_record
                WHERE scope = %s AND idempotency_key = %s AND actor_id = %s""",
            (scope, key, actor_id),
        )
        return self.cur.fetchone()

    def save_idempotent(self, scope: str, key: str, actor_id: UUID,
                        request_hash: str, response: Mapping[str, Any]) -> None:
        self.cur.execute(
            """INSERT INTO assessment.idempotency_record (
                   id, scope, idempotency_key, actor_id, request_hash, response,
                   created_at)
               VALUES (%s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (scope, idempotency_key, actor_id) DO NOTHING""",
            (uuid4(), scope, key, actor_id, request_hash, _json(response)),
        )

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None:
        self.cur.execute(
            """INSERT INTO assessment.id_ref (subject, kind, ref)
               VALUES (%s, %s, %s)
               ON CONFLICT (kind, ref) DO NOTHING""",
            (subject, kind, ref),
        )

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        self.cur.execute(
            "SELECT ref FROM assessment.id_ref WHERE subject = %s AND kind = %s",
            (subject, kind),
        )
        row = self.cur.fetchone()
        return row["ref"] if row else None

    # -- 行映射 -----------------------------------------------------------
    def _row(self, record: Mapping[str, Any]) -> AssignmentRow:
        course_ref = self.refs.lookup(self.cur, "course", record["course_id"]) or ""
        offering_ref = self.refs.lookup(self.cur, "offering", record["offering_id"])
        return AssignmentRow(
            id=record["id"],
            course_id=record["course_id"],
            course_ref=course_ref,
            offering_id=record["offering_id"],
            offering_ref=offering_ref,
            paper_version_id=record["paper_version_id"],
            release_context_id=record["release_context_id"],
            attempt_policy_version_id=record["attempt_policy_version_id"],
            visible_at=record["visible_at"],
            due_at=record["due_at"],
            hard_close_at=record["hard_close_at"],
            distribution_kind=record["distribution_kind"],
            status=record["status"],
            created_by=record["created_by"],
            created_at=record["created_at"],
            published_at=record.get("published_at"),
            withdrawn_at=record.get("withdrawn_at"),
            publish_manifest_hash=record.get("publish_manifest_hash"),
            review_manifest_hash=record.get("review_manifest_hash"),
            withdrawal_reason=record.get("withdrawal_reason"),
        )

    @staticmethod
    def _item(record: Mapping[str, Any]) -> AssignmentItemRow:
        return AssignmentItemRow(
            id=record["id"],
            assignment_id=record["assignment_id"],
            paper_slot_id=record["paper_slot_id"],
            ord=record["ord"],
            release_context_id=record["release_context_id"],
            source_release_id=record["source_release_id"],
            exercise_version_id=record["exercise_version_id"],
            exercise_instance_id=record.get("exercise_instance_id"),
            exercise_family_id=record["exercise_family_id"],
            score=record["score"],
            content_hash=record["content_hash"],
        )


class _PostgresAssignmentRepository:
    """把 id_ref 查询与事务边界放在仓储外面，避免 UoW 自己再开连接。"""

    @staticmethod
    def lookup(cur: Any, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        cur.execute(
            "SELECT ref FROM assessment.id_ref WHERE subject = %s AND kind = %s",
            (subject, kind),
        )
        row = cur.fetchone()
        return row["ref"] if row else None


@contextmanager
def _postgres_uow() -> Any:
    with db_transaction(assessment_service=True) as cur:
        yield _PostgresAssignmentUnitOfWork(cur, _PostgresAssignmentRepository())


# ---------------------------------------------------------------------------
# 曝光目标：ExposureService 只读 exercise_version_id / exercise_family_id
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ExposureTarget:
    exercise_version_id: UUID
    exercise_family_id: UUID


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class AssignmentService:
    """打印作业的唯一写入口（create / publish / withdraw）。"""

    def __init__(
        self,
        repository: AssignmentRepository | None = None,
        authorizer: Any | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self._new_id = id_factory
        self._now = clock

    # -- 建草稿 -----------------------------------------------------------
    def create_draft(
        self,
        *,
        actor_id: UUID,
        paper_version_id: UUID,
        offering_ref: str,
        visible_at: datetime,
        due_at: datetime,
        hard_close_at: datetime,
        idempotency_key: str,
        actor_ref: str | None = None,
        distribution_kind: str = "print",
    ) -> AssignmentView:
        if self.repository is None:
            raise AssignmentError("缺少作业仓储：拒绝无持久化的发布")
        if distribution_kind not in ("print", "online", "both"):
            raise AssignmentInputError(
                f"不支持的分发方式：{distribution_kind}（只允许 print/online/both）")
        key = _clean_key(idempotency_key)
        times = _clean_times(visible_at, due_at, hard_close_at)

        fingerprint = canonical_hash({
            "scope": CREATE_SCOPE,
            "paper_version_id": paper_version_id,
            "offering_ref": offering_ref,
            "visible_at": times[0],
            "due_at": times[1],
            "hard_close_at": times[2],
            "distribution_kind": distribution_kind,
        })

        with self.repository.unit_of_work() as uow:
            replayed = _replay(uow, CREATE_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return AssignmentView.model_validate(replayed).model_copy(
                    update={"replayed": True, "created": False})

            paper = uow.load_paper(paper_version_id)
            if paper is None:
                raise AssignmentNotFoundError(f"卷子不存在：{paper_version_id}")
            course_ref = uow.lookup_ref("course", paper.course_id)
            if not course_ref:
                raise AssignmentInputError(
                    "卷子的课程没有 id_ref 映射：无法确定课程范围（历史数据需先回填）")
            offering_id = ref_uuid("offering", offering_ref)
            self._require(actor_id, course_ref, offering_ref, course_id=paper.course_id)
            _require_approved(paper)

            policy_id = uow.ensure_attempt_policy(
                course_ref=course_ref, paper_version_id=paper_version_id)
            row = AssignmentRow(
                id=self._new_id(),
                course_id=paper.course_id,
                course_ref=course_ref,
                offering_id=offering_id,
                offering_ref=offering_ref,
                paper_version_id=paper_version_id,
                release_context_id=paper.release_context_id,
                attempt_policy_version_id=policy_id,
                visible_at=times[0],
                due_at=times[1],
                hard_close_at=times[2],
                status="draft",
                distribution_kind=distribution_kind,
                created_by=actor_id,
                created_at=self._now(),
                review_manifest_hash=paper.review_manifest_hash,
            )
            uow.add(row)
            uow.remember_ref("course", course_ref, paper.course_id)
            uow.remember_ref("offering", offering_ref, offering_id)
            view = AssignmentView(assignment=row, items=(), created=True)
            uow.save_idempotent(CREATE_SCOPE, key, actor_id, fingerprint,
                                view.model_dump(mode="json"))
            return view

    # -- 发布 -------------------------------------------------------------
    def publish(
        self,
        *,
        actor_id: UUID,
        assignment_id: UUID,
        idempotency_key: str,
    ) -> AssignmentView:
        if self.repository is None:
            raise AssignmentError("缺少作业仓储：拒绝无持久化的发布")
        key = _clean_key(idempotency_key)
        fingerprint = canonical_hash({
            "scope": PUBLISH_SCOPE, "assignment_id": assignment_id,
        })

        with self.repository.unit_of_work() as uow:
            replayed = _replay(uow, PUBLISH_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return AssignmentView.model_validate(replayed).model_copy(
                    update={"replayed": True, "created": False})

            current = uow.get(assignment_id)
            if current is None:
                raise AssignmentNotFoundError(f"作业不存在：{assignment_id}")
            if current.status not in ("draft", "scheduled"):
                raise AssignmentStateError(
                    f"作业状态是 {current.status}，只有 draft/scheduled 能发布")
            self._require(actor_id, current.course_ref, current.offering_ref,
                          course_id=current.course_id)

            # 锁在授权之后、读卷面之前：锁内读到的曝光事件才是"发布时点"的事实。
            uow.lock_course(current.course_ref)

            paper = uow.load_paper(current.paper_version_id)
            if paper is None:
                raise AssignmentNotFoundError(f"卷子不存在：{current.paper_version_id}")
            checks = self._precheck(current, paper, uow, self._now())

            items = tuple(self._project(current, paper))
            uow.save_items(items)
            events = self._exposure_events(current, paper, items)
            if events:
                uow.record_exposure(events)

            published = current.model_copy(update={
                "status": "published",
                "published_at": checks.published_at,
                "publish_manifest_hash": paper.manifest_hash,
                "review_manifest_hash": paper.review_manifest_hash,
            })
            uow.save(published)
            view = AssignmentView(assignment=published, items=items, created=True)
            uow.save_idempotent(PUBLISH_SCOPE, key, actor_id, fingerprint,
                                view.model_dump(mode="json"))
            return view

    # -- 严重撤回 ---------------------------------------------------------
    def withdraw(
        self,
        *,
        actor_id: UUID,
        assignment_id: UUID,
        reason: str,
        idempotency_key: str,
    ) -> AssignmentView:
        if self.repository is None:
            raise AssignmentError("缺少作业仓储：拒绝无持久化的撤回")
        key = _clean_key(idempotency_key)
        cleaned_reason = _clean_reason(reason)
        fingerprint = canonical_hash({
            "scope": WITHDRAW_SCOPE, "assignment_id": assignment_id,
            "reason": cleaned_reason,
        })

        with self.repository.unit_of_work() as uow:
            replayed = _replay(uow, WITHDRAW_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return AssignmentView.model_validate(replayed).model_copy(
                    update={"replayed": True, "created": False})

            current = uow.get(assignment_id)
            if current is None:
                raise AssignmentNotFoundError(f"作业不存在：{assignment_id}")
            if current.status == "withdrawn":
                raise AssignmentStateError("作业已经撤回过，不能重复撤回")
            if current.status not in ("scheduled", "published", "closed"):
                raise AssignmentStateError(
                    f"作业状态是 {current.status}，只有已发布/已关闭能撤回")
            self._require(actor_id, current.course_ref, current.offering_ref,
                          course_id=current.course_id)

            uow.lock_course(current.course_ref)
            items = uow.items(assignment_id)
            now = self._now()
            # 严重撤回：把这些题（同族）标记为 withdrawn。曝光规则里 withdrawn
            # 是无条件拦截项——撤回意味着"这题不能再进任何卷"，不是"换个用途还能用"。
            uow.record_exposure(tuple(
                ExposureEvent(
                    exercise_version_id=item.exercise_version_id,
                    exercise_instance_id=item.exercise_instance_id,
                    exercise_family_id=item.exercise_family_id,
                    scope_offering_id=current.offering_id,
                    event_kind="withdrawn",
                    visible_from=now,
                    occurred_at=now,
                )
                for item in items
            ))
            withdrawn = current.model_copy(update={
                "status": "withdrawn",
                "withdrawn_at": now,
                "withdrawal_reason": cleaned_reason,
            })
            uow.save(withdrawn)
            view = AssignmentView(assignment=withdrawn, items=items, created=False)
            uow.save_idempotent(WITHDRAW_SCOPE, key, actor_id, fingerprint,
                                view.model_dump(mode="json"))
            return view

    # -- 读 ---------------------------------------------------------------
    def get(self, *, actor_id: UUID, assignment_id: UUID) -> AssignmentView:
        if self.repository is None:
            raise AssignmentError("缺少作业仓储")
        with self.repository.unit_of_work() as uow:
            row = uow.get(assignment_id)
            if row is None:
                raise AssignmentNotFoundError(f"作业不存在：{assignment_id}")
            self._require(actor_id, row.course_ref, row.offering_ref,
                          course_id=row.course_id, read=True)
            return AssignmentView(assignment=row, items=uow.items(assignment_id))

    def list_for_paper(self, *, actor_id: UUID,
                       paper_version_id: UUID) -> tuple[AssignmentRow, ...]:
        """这份卷已经登记过哪些作业（草稿/已发布/已撤回都在里面）。

        授权从**卷子的课程**派生，而不是从某个作业行派生：只拿得到 assignment_id
        的人没法用别的课程范围绕开判定；卷子不存在就先 404，不泄露"它有没有作业"。
        只读，所以用 read 动作——助教能看发布进度，但不能因此获得写权限。
        """
        if self.repository is None:
            raise AssignmentError("缺少作业仓储")
        with self.repository.unit_of_work() as uow:
            paper = uow.load_paper(paper_version_id)
            if paper is None:
                raise AssignmentNotFoundError(f"卷子不存在：{paper_version_id}")
            course_ref = uow.lookup_ref("course", paper.course_id)
            if not course_ref:
                raise AssignmentInputError(
                    "卷子的课程没有 id_ref 映射：无法确定课程范围（历史数据需先回填）")
            self._require(actor_id, course_ref, None, course_id=paper.course_id,
                          read=True)
            return uow.list_for_paper(paper_version_id)

    # -- 内部 -------------------------------------------------------------
    def _require(self, actor_id: UUID, course_ref: str, offering_ref: str | None,
                 *, course_id: UUID | None = None, read: bool = False) -> None:
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的写入")
        # 用 paper.* 的动作名而不是自定义名字：发布作业的权限就是"这门课的教师"，
        # 复用同一套动作映射，多一个动作名就多一处会漂移的权限模型。
        action = PAPER_READ_ACTION if read else PAPER_WRITE_ACTION
        self.authorizer.require(
            actor_id, action,
            course_id=course_id, offering_id=None,
            course_ref=course_ref, offering_ref=offering_ref,
        )

    def _precheck(self, current: AssignmentRow, paper: PaperVersionRow,
                  uow: AssignmentUnitOfWork, now: datetime) -> Any:
        """发布终检：任何一项不过就抛，绝不"先发布再说"。"""
        _require_approved(paper)
        if current.review_manifest_hash != paper.review_manifest_hash:
            raise AssignmentNotApprovedError(
                "审核证据已失效：作业登记时的 review_manifest_hash 与卷子当前值不一致")
        if paper.submitted_manifest_hash != paper.manifest_hash:
            raise AssignmentNotApprovedError(
                "卷面 manifest 与提交审核时冻结的值不一致，审核证据不能代表当前卷面")
        _clean_times(current.visible_at, current.due_at, current.hard_close_at)

        slots = [slot for section in paper.sections for slot in section.slots]
        if not slots:
            raise AssignmentSourceIncompleteError("卷面没有任何槽位，不能发布")
        for slot in slots:
            missing = _missing_sources(slot)
            if missing:
                raise AssignmentSourceIncompleteError(
                    f"槽位 {slot.code} 缺固定来源：{'、'.join(missing)}")
            if slot.release_context_id != current.release_context_id:
                raise AssignmentSourceIncompleteError(
                    f"槽位 {slot.code} 的 Release 与作业不一致")

        families = [slot.exercise_family_id for slot in slots]
        events = uow.exposure_events(families)
        targets = [_ExposureTarget(slot.exercise_version_id, slot.exercise_family_id)
                   for slot in slots]
        try:
            ExposureService.assert_no_conflict(
                paper.paper_kind, current.offering_id, targets, events, now)
        except ExposureConflict as error:
            raise AssignmentExposureConflictError(error.code, str(error)) from error

        return _Precheck(published_at=now)

    def _project(self, current: AssignmentRow,
                 paper: PaperVersionRow) -> list[AssignmentItemRow]:
        items: list[AssignmentItemRow] = []
        ord_ = 0
        for section in paper.sections:
            for slot in section.slots:
                ord_ += 1
                items.append(AssignmentItemRow(
                    id=self._new_id(),
                    assignment_id=current.id,
                    paper_slot_id=slot.id,
                    ord=ord_,
                    release_context_id=slot.release_context_id,
                    source_release_id=slot.source_release_id,
                    exercise_version_id=slot.exercise_version_id,
                    exercise_instance_id=slot.exercise_instance_id,
                    exercise_family_id=slot.exercise_family_id,
                    score=validate_score(Decimal(slot.score)),
                    content_hash=slot.content_hash,
                ))
        return items

    @staticmethod
    def _exposure_events(current: AssignmentRow, paper: PaperVersionRow,
                         items: Iterable[AssignmentItemRow]) -> tuple[ExposureEvent, ...]:
        """正式卷发布即写 secure_assigned：让**下一份**卷在预检时就能看到这次占用。

        练习/作业卷不写：它们不要求保密，写进去反而会让正式卷误判"这题已经被
        保密占用过"。visible_from 用 visible_at，而不是 now：预检关心的正是
        "开考那一刻会不会冲突"。
        """
        if paper.paper_kind != "formal":
            return ()
        return tuple(
            ExposureEvent(
                exercise_version_id=item.exercise_version_id,
                exercise_instance_id=item.exercise_instance_id,
                exercise_family_id=item.exercise_family_id,
                scope_offering_id=current.offering_id,
                event_kind="secure_assigned",
                visible_from=current.visible_at,
                occurred_at=current.visible_at,
            )
            for item in items
        )


@dataclass(frozen=True)
class _Precheck:
    published_at: datetime


# ---------------------------------------------------------------------------
# 纯规则
# ---------------------------------------------------------------------------

def _require_approved(paper: PaperVersionRow) -> None:
    if paper.status != "approved":
        raise AssignmentNotApprovedError(
            f"卷子状态是 {paper.status}：只有 approved 的卷子能发布成作业")
    if not paper.review_manifest_hash:
        raise AssignmentNotApprovedError("卷子缺少 review_manifest_hash：审核证据不完整")


def _missing_sources(slot: Any) -> tuple[str, ...]:
    missing: list[str] = []
    for name in ("release_context_id", "source_release_id", "exercise_version_id",
                 "exercise_family_id"):
        if getattr(slot, name, None) is None:
            missing.append(name)
    if not getattr(slot, "content_hash", ""):
        missing.append("content_hash")
    return tuple(missing)


def _clean_times(visible_at: datetime, due_at: datetime,
                 hard_close_at: datetime) -> tuple[datetime, datetime, datetime]:
    times = [_utc(value) for value in (visible_at, due_at, hard_close_at)]
    if not (times[0] <= times[1] <= times[2]):
        raise AssignmentTimeOrderError(
            "时间顺序必须是 visible_at <= due_at <= hard_close_at："
            f"收到 {times[0].isoformat()} / {times[1].isoformat()} / {times[2].isoformat()}")
    return times[0], times[1], times[2]


def _clean_key(value: str | None) -> str:
    if value is None or not str(value).strip():
        raise AssignmentInputError("写接口必须带 Idempotency-Key")
    cleaned = str(value).strip()
    if len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise AssignmentInputError(f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY} 字符")
    return cleaned


def _clean_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if not cleaned:
        raise AssignmentInputError("撤回必须给出理由")
    if len(cleaned) > MAX_REASON:
        raise AssignmentInputError(f"撤回理由不能超过 {MAX_REASON} 字符")
    return cleaned


def _json(value: Mapping[str, Any]) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _replay(uow: AssignmentUnitOfWork, scope: str, key: str, actor_id: UUID,
            fingerprint: str) -> dict[str, Any] | None:
    record = uow.find_idempotent(scope, key, actor_id)
    if record is None:
        return None
    stored_hash = record["request_hash"] if isinstance(record, Mapping) else record.request_hash
    stored_response = record["response"] if isinstance(record, Mapping) else record.response
    if stored_hash != fingerprint:
        raise AssignmentIdempotencyConflictError(
            "同一 Idempotency-Key 被用于不同的请求载荷")
    if isinstance(stored_response, str):
        import json
        return json.loads(stored_response)
    return dict(stored_response)


__all__ = [
    "AssignmentError",
    "AssignmentExposureConflictError",
    "AssignmentIdempotencyConflictError",
    "AssignmentInputError",
    "AssignmentItemRow",
    "AssignmentNotApprovedError",
    "AssignmentNotFoundError",
    "AssignmentRepository",
    "AssignmentRow",
    "AssignmentService",
    "AssignmentSourceIncompleteError",
    "AssignmentStateError",
    "AssignmentTimeOrderError",
    "AssignmentView",
    "DEFAULT_POLICY",
    "InMemoryAssignmentRepository",
    "PostgresAssignmentRepository",
]
