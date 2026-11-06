"""手工固定卷：从候选快照建 draft，并在同一草稿内替换 / 调序 / 调分。

这是任务 4 的唯一写入口——`PaperService.create_manual_draft` 与
`PaperService.edit_draft`（计划书 §0.3 规则 8 点名的两个入口）。它只回答五个问题：

  1. 这条题能不能进这个槽位？（候选必须来自**建卷时固定的** candidate snapshot；
     槽位必须固定 `release_context_id / source_release_id / exercise_version_id /
     exercise_family_id / dependency_manifest_hash`，不允许只有题号）
  2. 现在能不能改？（只有 `draft`；`approved / in_review / withdrawn` 一律拒绝）
  3. 改的是不是我以为的那一版？（`base_version == edit_seq` 乐观锁，过期 409）
  4. 改完的卷子是什么？（每次编辑 `edit_seq + 1`、重算 manifest、清空旧审核哈希）
  5. 旧内容去哪了？（append-only `assessment.paper_edit_event` 变更集；替换/调序/
     调分都留旧值，历史来源不删除）

## 为什么撤销不是"回滚"

规格 §8.2 只允许「撤销本次草稿编辑」。如果编辑覆盖写、没有变更集，撤销只能靠前端
保存一份副本；两个人同时改就会互相覆盖。这里把每次编辑写成一批不可变事件，撤销
将来只需"按 change_set_id 反向应用"（任务 5/前端），而不是改历史行。

## 与计划书的差异（按本仓库技术栈改写）

* 计划书用 SQLAlchemy repository + Alembic；本仓库是原生 SQL + `db.transaction`，
  迁移是 `db/migrations/0030_paper_editing.sql`。paper_* 表是 FORCE RLS，所以每次
  读写都在 `db.transaction(assessment_service=True)` 里。
* 计划书 `EditOperation` 直接携带 `candidate` 对象；这里只接受
  `exercise_version_id`，由服务从固定快照解析成完整来源。**调用方不能伪造
  source_release_id / family_id**——否则"替换来自同一快照"就只是口头约定。
* 计划书 `edit_draft(paper, base_version, snapshot, operations)` 是不落库的纯函数；
  这里保留同名方法但走完整授权/加载/持久化链路，另把纯规则拆成
  `_apply_operations`，便于单测。
* 计划书把 project 的 `paper_version.total_score` 当成槽位和；这里按计划书步骤 3
  的代码保留**蓝图目标分**，调分后的中间状态由任务 5 的整卷校验器报
  `section_score_mismatch / total_score_mismatch`（规格 §16：教师手工改坏总分，
  草稿可保存、提交审核被阻断）。

## 身份口径

`paper_*` 表是 uuid 口径，本仓库课程/账号是 text。与蓝图一样走 `ref_uuid` 映射；
纸卷行的课程范围不自己判断，而是**通过蓝图版本**（`BlueprintService.get_version`）
拿到 `course_ref` 再交给 `CourseScopeAuthorizer`——纸卷的范围永远等于它蓝图的范围，
不存在"纸卷说自己在另一门课"的入口。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ContextManager, Iterator, Literal, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from pydantic import Field

from . import db
from .blueprints import (
    BlueprintVersionNotFoundError,
    BlueprintVersionRow,
    CourseScopeAuthorizer,
    IdempotencyRecord,
    UnknownCourseError,
)
from .candidates import (
    CandidateScope,
    CandidateScopeError,
    CandidateService,
    CandidateSnapshot,
    CandidateSnapshotNotFoundError,
)
from .contracts import (
    BlueprintContent,
    BlueprintSlot,
    CandidateRecord,
    ContractModel,
    PaperSectionView,
    PaperSlotView,
    PaperStatus,
    PaperView,
    Purpose,
    Score,
    SlotSelection,
    canonical_hash,
    validate_score,
)
from .releases import utcnow

MANIFEST_SCHEMA = "paper-manifest/v1"

PAPER_WRITE_ACTION = "paper.write"
PAPER_READ_ACTION = "paper.read"
# 任务 7 的治理动作：提交/复核/撤回各有独立动作名，权限映射仍旧复用课程范围。
PAPER_SUBMIT_ACTION = "paper.submit"
PAPER_REVIEW_ACTION = "paper.review"
PAPER_WITHDRAW_ACTION = "paper.withdraw"
CREATE_SCOPE = "paper_version.create"
EDIT_SCOPE = "paper_version.edit"
MAX_IDEMPOTENCY_KEY = 200

# 手工卷的展示策略固定：不做运行时随机题、不打乱选项（规格 §10.5 display_config）。
FIXED_DISPLAY: dict[str, object] = {"language_pair": "fixed", "option_order": "fixed"}

# 槽位"来源四元组 + 依赖哈希"：写入数据库时缺一项就是"只有题号"。
SOURCE_FIELDS = (
    "exercise_id",
    "exercise_family_id",
    "source_release_id",
    "release_context_id",
    "dependency_manifest_hash",
)
EVIDENCE_SOURCE_FIELDS = tuple(
    name for name in SOURCE_FIELDS
)  # CandidateEvidence 与 PaperSlotView 同名，可直接按名取

EDIT_KINDS = ("replace", "move", "score")


# ---------------------------------------------------------------------------
# 错误（路由层据此映射状态码；规则错误自带原因码）
# ---------------------------------------------------------------------------

class PaperError(Exception):
    """纸卷域错误基类。"""


class PaperNotFoundError(PaperError):
    """纸卷版本不存在（404）。"""


class PaperNotDraftError(PaperError, ValueError):
    """不是草稿（409）。approved 只能克隆新草稿，不能原位改题。"""

    def __init__(self, message: str, current_status: str | None = None) -> None:
        super().__init__(message)
        self.current_status = current_status


class StalePaperVersion(PaperError):
    """`base_version` 与当前 `edit_seq` 不一致（409）。"""

    def __init__(self, message: str, current_version: int | None = None) -> None:
        super().__init__(message)
        self.current_version = current_version


class PaperIdempotencyConflictError(PaperError):
    """同一 Idempotency-Key 被用于不同载荷（409）。"""


class PaperRuleError(PaperError, ValueError):
    """内容不合法（422）；`code` 是稳定的机器可读原因码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 编辑命令与结果行
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EditOperation:
    """一次草稿编辑里的一条操作。

    `replace` 只带 `exercise_version_id`：完整来源由服务从固定快照解析。
    `move` 的 `target_slot_no` 是**本分节内**的 1-based 目标位置（不跨节搬题：
    跨节会改变分节分与蓝图结构，属于重建蓝图而不是编辑纸卷）。
    `score` 走 Decimal 两位小数，禁止二进制浮点。
    """

    kind: str
    slot_id: UUID
    exercise_version_id: UUID | None = None
    target_slot_no: int | None = None
    score: Decimal | None = None
    reason: str | None = None


class PaperVersionRow(PaperView):
    """`assessment.paper_version` 一行 + 分节槽位（= 接口响应体）。

    继承 `PaperView` 而不是另造一份近似结构：manifest 的输入是 `PaperView` 的
    内容字段，响应再附加 `paper_id / version_no / snapshot / 幂等标记` 等元数据。
    manifest **不包含**这些元数据（尤其 created_at/edit_seq），所以同一份内容
    无论是第 1 版还是第 9 次编辑、由谁在什么时候建，哈希都一样。
    """

    paper_id: UUID
    version_no: int = Field(ge=1)
    candidate_snapshot_id: UUID
    base_paper_version_id: UUID | None = None
    # 组卷来源：手工卷两列都为空；约束组卷必须成对（0034 的 ck_paper_assembly_source）。
    # 只靠数据库 CHECK 不够：服务层也要能回答"这份卷是不是求解器给的"。
    assembly_run_id: UUID | None = None
    selected_solution_index: int | None = None
    submitted_manifest_hash: str | None = None
    created_by: UUID
    created_at: datetime
    course_ref: str | None = None
    offering_ref: str | None = None
    created_by_ref: str | None = None
    # 仅"本次请求真的新建了版本"为 True；幂等重放/读取都是 False。
    created: bool = False
    replayed: bool = False


class PaperEditEvent(ContractModel):
    """`assessment.paper_edit_event` 一行：一次草稿编辑的不可变证据。

    为什么 replace 要同时存旧/新四元组：半年后要回答"这道题是什么时候换掉的、
    换掉的是哪一版、当时的分值是多少"。只存 new 或只存 diff 都答不全。
    """

    id: UUID
    paper_version_id: UUID
    change_set_id: UUID
    edit_seq: int = Field(ge=1)
    ord: int = Field(ge=1)
    kind: Literal["replace", "move", "score"]
    slot_id: UUID
    reason: str | None = None
    previous_exercise_id: UUID | None = None
    previous_exercise_version_id: UUID | None = None
    previous_exercise_instance_id: UUID | None = None
    previous_exercise_family_id: UUID | None = None
    previous_source_release_id: UUID | None = None
    previous_release_context_id: UUID | None = None
    new_exercise_id: UUID | None = None
    new_exercise_version_id: UUID | None = None
    new_exercise_instance_id: UUID | None = None
    new_exercise_family_id: UUID | None = None
    new_source_release_id: UUID | None = None
    new_release_context_id: UUID | None = None
    previous_slot_no: int | None = None
    new_slot_no: int | None = None
    previous_score: Score | None = None
    new_score: Score | None = None
    manifest_hash: str = Field(min_length=64, max_length=64)
    actor_id: UUID
    occurred_at: datetime


# ---------------------------------------------------------------------------
# 纯函数：内容哈希、manifest、来源完整性
# ---------------------------------------------------------------------------

def slot_content_hash(slot: PaperSlotView) -> str:
    """槽位内容哈希：题目身份 + 来源 + 分值/时长 + 依赖，**不含槽位 id**。

    不含 id 是刻意的：克隆一份已批准卷时槽位 id 必须换（PK 唯一），但"内容有没有
    变"不该因为换 id 就说变了；否则审核过的 manifest 在克隆/复制时全部失效。
    """
    return canonical_hash({
        "section_code": slot.section_code,
        "code": slot.code,
        "slot_no": slot.slot_no,
        "exercise_id": slot.exercise_id,
        "exercise_version_id": slot.exercise_version_id,
        "exercise_instance_id": slot.exercise_instance_id,
        "exercise_family_id": slot.exercise_family_id,
        "source_release_id": slot.source_release_id,
        "release_context_id": slot.release_context_id,
        "score": slot.score,
        "estimated_seconds": slot.estimated_seconds,
        "display_config": slot.display_config,
        "dependency_manifest_hash": slot.dependency_manifest_hash,
    })


def section_content_hash(section: PaperSectionView) -> str:
    return canonical_hash({
        "code": section.code,
        "title": section.title,
        "instruction": section.instruction,
        "sort_order": section.sort_order,
        "section_score": section.section_score,
        "slots": [slot.content_hash for slot in section.slots],
    })


def slot_source_missing(slot: PaperSlotView) -> tuple[str, ...]:
    """已填槽位缺少哪些固定来源字段；空槽位（全 None）返回 ()。

    空槽位是 draft 的合法缺口（§16 "某槽位无候选先显示缺口"），由任务 5 的整卷
    校验器在提交审核时拦。**半填**的槽位永远非法：那正是"只有题号"的形态。
    """
    if slot.exercise_version_id is None:
        return ()
    return tuple(
        name for name in SOURCE_FIELDS if getattr(slot, name) in (None, "")
    )


def evidence_source_missing(evidence: Any) -> tuple[str, ...]:
    return tuple(
        name for name in EVIDENCE_SOURCE_FIELDS
        if getattr(evidence, name, None) in (None, "")
    )


def manifest_payload(paper: PaperView) -> dict[str, Any]:
    """整卷 manifest 的规范结构：只含内容，不含身份/时间/编辑序号。"""
    return {
        "schema": MANIFEST_SCHEMA,
        "blueprint_version_id": paper.blueprint_version_id,
        "release_context_id": paper.release_context_id,
        "purpose": paper.purpose,
        "paper_kind": paper.paper_kind,
        "total_score": paper.total_score,
        "duration_seconds": paper.duration_seconds,
        "sections": [
            {
                "code": section.code,
                "title": section.title,
                "instruction": section.instruction,
                "sort_order": section.sort_order,
                "section_score": section.section_score,
                "content_hash": section.content_hash,
                "slots": [
                    {
                        "section_code": slot.section_code,
                        "code": slot.code,
                        "slot_no": slot.slot_no,
                        "exercise_id": slot.exercise_id,
                        "exercise_version_id": slot.exercise_version_id,
                        "exercise_instance_id": slot.exercise_instance_id,
                        "exercise_family_id": slot.exercise_family_id,
                        "source_release_id": slot.source_release_id,
                        "release_context_id": slot.release_context_id,
                        "score": slot.score,
                        "estimated_seconds": slot.estimated_seconds,
                        "display_config": slot.display_config,
                        "dependency_manifest_hash": slot.dependency_manifest_hash,
                        "content_hash": slot.content_hash,
                    }
                    for slot in section.slots
                ],
            }
            for section in paper.sections
        ],
    }


def paper_manifest(paper: PaperView) -> str:
    """整卷内容哈希（64 位小写 hex）。审核失效判断只认它。"""
    return canonical_hash(manifest_payload(paper))


def _clean_ids(value: tuple[UUID, ...] | list[UUID]) -> set[UUID]:
    return {item for item in value}


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class PaperUnitOfWork(Protocol):
    """一个事务内的纸卷读写集合。"""

    def lock_paper(self, paper_id: UUID) -> None: ...

    def latest_for_paper(self, paper_id: UUID) -> PaperVersionRow | None: ...

    def add(self, row: PaperVersionRow) -> PaperVersionRow: ...

    def get(self, version_id: UUID) -> PaperVersionRow | None: ...

    def save_edit(self, row: PaperVersionRow, base_version: int,
                  events: Sequence[PaperEditEvent]) -> PaperVersionRow: ...

    def list_edit_events(self, version_id: UUID) -> tuple[PaperEditEvent, ...]: ...

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None: ...

    def save_idempotent(self, record: IdempotencyRecord) -> None: ...

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None: ...

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None: ...


class PaperRepository(Protocol):
    def unit_of_work(self) -> ContextManager[PaperUnitOfWork]: ...


# ---------------------------------------------------------------------------
# 内存实现（单元测试 / 无库环境）
# ---------------------------------------------------------------------------

class InMemoryPaperRepository:
    """语义与 0027/0030 的唯一键、触发器一致的内存仓储。"""

    def __init__(self) -> None:
        self.versions: dict[UUID, PaperVersionRow] = {}
        self.idempotency: dict[tuple[str, str, UUID], IdempotencyRecord] = {}
        self.events: list[PaperEditEvent] = []
        self.refs: dict[tuple[str, UUID], str] = {}

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryPaperRepository"]:
        yield self

    # -- 端口 -------------------------------------------------------------
    def lock_paper(self, paper_id: UUID) -> None:
        return None

    def latest_for_paper(self, paper_id: UUID) -> PaperVersionRow | None:
        rows = [row for row in self.versions.values() if row.paper_id == paper_id]
        return max(rows, key=lambda row: row.version_no) if rows else None

    def add(self, row: PaperVersionRow) -> PaperVersionRow:
        # 与 UNIQUE (paper_id, version_no) 一致；任务 4 每个 paper 身份只建第 1 版，
        # 已存在同名身份说明调用方把"纠错"写成了"覆盖"。
        if self.latest_for_paper(row.paper_id) is not None:
            raise PaperRuleError(
                "paper_identity_conflict",
                "该 paper_id 已有版本；纠错/克隆请用 base_paper_version_id 建新 paper 身份",
            )
        self.versions[row.id] = row
        return row

    def get(self, version_id: UUID) -> PaperVersionRow | None:
        return self.versions.get(version_id)

    def save_edit(self, row: PaperVersionRow, base_version: int,
                  events: Sequence[PaperEditEvent]) -> PaperVersionRow:
        current = self.versions.get(row.id)
        if current is None:
            raise PaperNotFoundError(str(row.id))
        if current.status != "draft":
            raise PaperNotDraftError(
                f"only draft can be edited（当前状态 {current.status}）",
                current_status=current.status,
            )
        if current.edit_seq != base_version:
            raise StalePaperVersion(
                f"stale_version: base_version={base_version}，当前 edit_seq={current.edit_seq}",
                current_version=current.edit_seq,
            )
        self.versions[row.id] = row
        self.events.extend(events)
        return row

    def list_edit_events(self, version_id: UUID) -> tuple[PaperEditEvent, ...]:
        return tuple(
            sorted(
                (event for event in self.events if event.paper_version_id == version_id),
                key=lambda event: (event.edit_seq, event.ord),
            )
        )

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None:
        return self.idempotency.get((scope, idempotency_key, actor_id))

    def save_idempotent(self, record: IdempotencyRecord) -> None:
        key = (record.scope, record.idempotency_key, record.actor_id)
        if key in self.idempotency:
            raise PaperIdempotencyConflictError(
                f"幂等键已被使用：{record.idempotency_key}"
            )
        self.idempotency[key] = record

    def remember_ref(self, kind: str, ref: str, subject: UUID) -> None:
        self.refs.setdefault((kind, subject), ref)

    def lookup_ref(self, kind: str, subject: UUID | None) -> str | None:
        if subject is None:
            return None
        return self.refs.get((kind, subject))


# ---------------------------------------------------------------------------
# PostgreSQL 实现
# ---------------------------------------------------------------------------

_VERSION_COLUMNS = """id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id,
                      base_paper_version_id, course_id, offering_id, release_context_id,
                      paper_kind, total_score, duration_seconds, status, edit_seq,
                      manifest_hash, submitted_manifest_hash, review_manifest_hash,
                      created_by, created_at, assembly_run_id, selected_solution_index"""

_SECTION_COLUMNS = """id, paper_version_id, code, title, instruction, sort_order,
                      section_score, display_config, content_hash"""

_SLOT_COLUMNS = """id, section_id, code, slot_no, exercise_id, exercise_version_id,
                   exercise_instance_id, exercise_family_id, source_release_id,
                   release_context_id, score, estimated_seconds, display_config,
                   dependency_manifest_hash, content_hash"""

_EDIT_EVENT_COLUMNS = """id, paper_version_id, change_set_id, edit_seq, ord,
                         operation_kind, slot_id, reason,
                         previous_exercise_id, previous_exercise_version_id,
                         previous_exercise_instance_id, previous_exercise_family_id,
                         previous_source_release_id, previous_release_context_id,
                         new_exercise_id, new_exercise_version_id,
                         new_exercise_instance_id, new_exercise_family_id,
                         new_source_release_id, new_release_context_id,
                         previous_slot_no, new_slot_no, previous_score, new_score,
                         manifest_hash, actor_id, occurred_at"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


class PostgresPaperRepository:
    """assessment.paper_version / section / slot / edit_event 的 PostgreSQL 仓储。

    paper_* 是 `FORCE ROW LEVEL SECURITY`：不在同一事务里
    `SET LOCAL app.assessment_service = 'on'`，连表 owner 都读不到一行。
    事务作用域由 `db.transaction(assessment_service=True)` 统一设置。
    """

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresPaperUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresPaperUnitOfWork(cur)


class _PostgresPaperUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    # -- 读 ---------------------------------------------------------------
    def lock_paper(self, paper_id: UUID) -> None:
        # 与蓝图版本同一手法：把同一 paper 身份的 version_no 分配串行化。
        self.cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(paper_id),))

    def latest_for_paper(self, paper_id: UUID) -> PaperVersionRow | None:
        self.cur.execute(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM assessment.paper_version
                 WHERE paper_id = %s
                 ORDER BY version_no DESC
                 LIMIT 1""",
            (paper_id,),
        )
        record = self.cur.fetchone()
        return self._row(record) if record else None

    def get(self, version_id: UUID) -> PaperVersionRow | None:
        self.cur.execute(
            f"""SELECT {_VERSION_COLUMNS}
                  FROM assessment.paper_version
                 WHERE id = %s""",
            (version_id,),
        )
        record = self.cur.fetchone()
        return self._row(record) if record else None

    def list_edit_events(self, version_id: UUID) -> tuple[PaperEditEvent, ...]:
        self.cur.execute(
            f"""SELECT {_EDIT_EVENT_COLUMNS}
                  FROM assessment.paper_edit_event
                 WHERE paper_version_id = %s
                 ORDER BY edit_seq, ord""",
            (version_id,),
        )
        return tuple(_event_from_db(record) for record in self.cur.fetchall())

    def _row(self, record: Mapping[str, Any]) -> PaperVersionRow:
        """把卷头行补上分节与槽位，拼成完整视图。"""
        version_id = record["id"]
        self.cur.execute(
            f"""SELECT {_SECTION_COLUMNS}
                  FROM assessment.paper_section
                 WHERE paper_version_id = %s
                 ORDER BY sort_order""",
            (version_id,),
        )
        section_rows = self.cur.fetchall()
        section_ids = [row["id"] for row in section_rows]
        slots_by_section: dict[UUID, list[PaperSlotView]] = {sid: [] for sid in section_ids}
        if section_ids:
            self.cur.execute(
                f"""SELECT {_SLOT_COLUMNS}
                      FROM assessment.paper_slot
                     WHERE section_id = ANY(%s)
                     ORDER BY slot_no""",
                (section_ids,),
            )
            section_code = {row["id"]: row["code"] for row in section_rows}
            for slot in self.cur.fetchall():
                slots_by_section[slot["section_id"]].append(PaperSlotView(
                    id=slot["id"],
                    section_code=section_code[slot["section_id"]],
                    code=slot["code"],
                    slot_no=slot["slot_no"],
                    exercise_id=slot.get("exercise_id"),
                    exercise_version_id=slot.get("exercise_version_id"),
                    exercise_instance_id=slot.get("exercise_instance_id"),
                    exercise_family_id=slot.get("exercise_family_id"),
                    source_release_id=slot.get("source_release_id"),
                    release_context_id=slot.get("release_context_id"),
                    score=slot["score"],
                    estimated_seconds=slot.get("estimated_seconds"),
                    display_config=_as_dict(slot.get("display_config")),
                    dependency_manifest_hash=slot.get("dependency_manifest_hash"),
                    content_hash=slot["content_hash"],
                ))
        sections = tuple(
            PaperSectionView(
                code=row["code"],
                title=row["title"],
                instruction=row["instruction"],
                sort_order=row["sort_order"],
                section_score=row["section_score"],
                slots=tuple(slots_by_section[row["id"]]),
                content_hash=row["content_hash"],
            )
            for row in section_rows
        )
        return PaperVersionRow(
            id=record["id"],
            paper_id=record["paper_id"],
            version_no=record["version_no"],
            blueprint_version_id=record["blueprint_version_id"],
            candidate_snapshot_id=record["candidate_snapshot_id"],
            base_paper_version_id=record.get("base_paper_version_id"),
            course_id=record["course_id"],
            offering_id=record.get("offering_id"),
            release_context_id=record["release_context_id"],
            purpose=record["paper_kind"],
            paper_kind=record["paper_kind"],
            status=record["status"],
            total_score=record["total_score"],
            duration_seconds=record["duration_seconds"],
            edit_seq=record["edit_seq"],
            manifest_hash=record["manifest_hash"],
            submitted_manifest_hash=record.get("submitted_manifest_hash"),
            review_manifest_hash=record.get("review_manifest_hash"),
            sections=sections,
            created_by=record["created_by"],
            created_at=record["created_at"],
            assembly_run_id=record.get("assembly_run_id"),
            selected_solution_index=record.get("selected_solution_index"),
        )

    # -- 写 ---------------------------------------------------------------
    def add(self, row: PaperVersionRow) -> PaperVersionRow:
        if self.latest_for_paper(row.paper_id) is not None:
            raise PaperRuleError(
                "paper_identity_conflict",
                "该 paper_id 已有版本；纠错/克隆请用 base_paper_version_id 建新 paper 身份",
            )
        self.cur.execute(
            """INSERT INTO assessment.paper_version
                 (id, paper_id, version_no, blueprint_version_id, candidate_snapshot_id,
                  base_paper_version_id, assembly_run_id, selected_solution_index,
                  course_id, offering_id, release_context_id, paper_kind,
                  total_score, duration_seconds, status, edit_seq, manifest_hash,
                  submitted_manifest_hash, review_manifest_hash, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s)""",
            (
                row.id, row.paper_id, row.version_no, row.blueprint_version_id,
                row.candidate_snapshot_id, row.base_paper_version_id,
                row.assembly_run_id, row.selected_solution_index,
                row.course_id, row.offering_id, row.release_context_id,
                row.paper_kind or row.purpose, row.total_score, row.duration_seconds,
                row.status, row.edit_seq, row.manifest_hash, row.created_by,
            ),
        )
        self._write_children(row)
        return row

    def save_edit(self, row: PaperVersionRow, base_version: int,
                  events: Sequence[PaperEditEvent]) -> PaperVersionRow:
        # 单条 UPDATE 同时承担乐观锁与"只有 draft 可写"：
        #   * 影响行数 0 就说明有人先改了（edit_seq 变了）或卷子已不是 draft；
        #   * 不在应用层先读后写，避免两个窗口都读到 1 再各自写 2。
        self.cur.execute(
            """UPDATE assessment.paper_version
                  SET edit_seq = %s,
                      manifest_hash = %s,
                      submitted_manifest_hash = NULL,
                      review_manifest_hash = NULL,
                      total_score = %s,
                      duration_seconds = %s
                WHERE id = %s AND edit_seq = %s AND status = 'draft'""",
            (
                row.edit_seq, row.manifest_hash, row.total_score,
                row.duration_seconds, row.id, base_version,
            ),
        )
        if self.cur.rowcount != 1:
            self.cur.execute(
                "SELECT status, edit_seq FROM assessment.paper_version WHERE id = %s",
                (row.id,),
            )
            current = self.cur.fetchone()
            if current is None:
                raise PaperNotFoundError(str(row.id))
            if current["status"] != "draft":
                raise PaperNotDraftError(
                    f"only draft can be edited（当前状态 {current['status']}）",
                    current_status=current["status"],
                )
            raise StalePaperVersion(
                f"stale_version: base_version={base_version}，"
                f"当前 edit_seq={current['edit_seq']}",
                current_version=current["edit_seq"],
            )

        self._write_children(row)
        # 编辑让仍开放的审核轮次失效：task 5 的整卷双审按 manifest 开轮，
        # manifest 变了就不能再返回 approved（0027 的 UNIQUE 也挡同 manifest 重审）。
        self.cur.execute(
            """UPDATE assessment.paper_review_round
                  SET status = 'invalidated', closed_at = now()
                WHERE paper_version_id = %s AND status = 'open'""",
            (row.id,),
        )
        for event in events:
            self.cur.execute(
                """INSERT INTO assessment.paper_edit_event
                     (id, paper_version_id, change_set_id, edit_seq, ord,
                      operation_kind, slot_id, reason,
                      previous_exercise_id, previous_exercise_version_id,
                      previous_exercise_instance_id, previous_exercise_family_id,
                      previous_source_release_id, previous_release_context_id,
                      new_exercise_id, new_exercise_version_id,
                      new_exercise_instance_id, new_exercise_family_id,
                      new_source_release_id, new_release_context_id,
                      previous_slot_no, new_slot_no, previous_score, new_score,
                      manifest_hash, actor_id, occurred_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event.id, event.paper_version_id, event.change_set_id,
                    event.edit_seq, event.ord, event.kind, event.slot_id, event.reason,
                    event.previous_exercise_id, event.previous_exercise_version_id,
                    event.previous_exercise_instance_id,
                    event.previous_exercise_family_id,
                    event.previous_source_release_id, event.previous_release_context_id,
                    event.new_exercise_id, event.new_exercise_version_id,
                    event.new_exercise_instance_id, event.new_exercise_family_id,
                    event.new_source_release_id, event.new_release_context_id,
                    event.previous_slot_no, event.new_slot_no,
                    event.previous_score, event.new_score,
                    event.manifest_hash, event.actor_id, event.occurred_at,
                ),
            )
        return row

    def _write_children(self, row: PaperVersionRow) -> None:
        """写分节与槽位。编辑时槽位行**原地更新**，不删除（历史由变更集保留）。"""
        # 先按 code 查出现有分节；新建时为空，由数据库生成分节 id。
        self.cur.execute(
            """SELECT s.id, s.code FROM assessment.paper_section s
                WHERE s.paper_version_id = %s""",
            (row.id,),
        )
        existing_sections = {record["code"]: record["id"] for record in self.cur.fetchall()}
        for section_view in row.sections:
            section_id = existing_sections.get(section_view.code)
            if section_id is None:
                section_id = uuid4()
                self.cur.execute(
                    """INSERT INTO assessment.paper_section
                         (id, paper_version_id, code, title, instruction, sort_order,
                          section_score, display_config, content_hash)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s)""",
                    (
                        section_id, row.id, section_view.code, section_view.title,
                        section_view.instruction, section_view.sort_order,
                        section_view.section_score, section_view.content_hash,
                    ),
                )
            else:
                self.cur.execute(
                    """UPDATE assessment.paper_section
                          SET title = %s, instruction = %s, sort_order = %s,
                              section_score = %s, content_hash = %s
                        WHERE id = %s""",
                    (
                        section_view.title, section_view.instruction,
                        section_view.sort_order, section_view.section_score,
                        section_view.content_hash, section_id,
                    ),
                )
            self._write_slots(section_id, section_view)

    def _write_slots(self, section_id: UUID, section_view: PaperSectionView) -> None:
        self.cur.execute(
            """SELECT id, code FROM assessment.paper_slot WHERE section_id = %s""",
            (section_id,),
        )
        existing = {record["code"]: record["id"] for record in self.cur.fetchall()}
        # 调序时目标 slot_no 可能已被另一行占用；先整体挪到负数，再写最终值，
        # 避免 UNIQUE (section_id, slot_no) 在逐行更新中途冲突。
        if existing:
            self.cur.execute(
                "UPDATE assessment.paper_slot SET slot_no = -slot_no WHERE section_id = %s",
                (section_id,),
            )
        for slot_view in section_view.slots:
            # 新建/克隆时用服务生成的槽位 id——响应里的 id 必须就是库里的 id，
            # 否则客户端拿返回值去做 PATCH 会得到 slot_not_found。
            slot_id = existing.get(slot_view.code) or slot_view.id
            values = (
                slot_view.code, slot_view.slot_no, slot_view.exercise_id,
                slot_view.exercise_version_id, slot_view.exercise_instance_id,
                slot_view.exercise_family_id, slot_view.source_release_id,
                slot_view.release_context_id, slot_view.score,
                slot_view.estimated_seconds,
                _json(slot_view.display_config), slot_view.dependency_manifest_hash,
                slot_view.content_hash,
            )
            if slot_view.code in existing:
                self.cur.execute(
                    """UPDATE assessment.paper_slot
                          SET code = %s, slot_no = %s, exercise_id = %s,
                              exercise_version_id = %s, exercise_instance_id = %s,
                              exercise_family_id = %s, source_release_id = %s,
                              release_context_id = %s, score = %s,
                              estimated_seconds = %s, display_config = %s::jsonb,
                              dependency_manifest_hash = %s, content_hash = %s
                        WHERE id = %s""",
                    (*values, slot_id),
                )
            else:
                self.cur.execute(
                    """INSERT INTO assessment.paper_slot
                         (id, section_id, code, slot_no, exercise_id,
                          exercise_version_id, exercise_instance_id, exercise_family_id,
                          source_release_id, release_context_id, score,
                          estimated_seconds, display_config, dependency_manifest_hash,
                          content_hash)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s::jsonb, %s, %s)""",
                    (slot_id, section_id, *values),
                )

    # -- 幂等与 id_ref ------------------------------------------------------
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
        return IdempotencyRecord(
            id=record["id"],
            scope=record["scope"],
            idempotency_key=record["idempotency_key"],
            actor_id=record["actor_id"],
            request_hash=record["request_hash"],
            response=_as_dict(record["response"]),
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
                record.actor_id, record.request_hash, _json(record.response),
            ),
        )
        if self.cur.rowcount == 0:
            raise PaperIdempotencyConflictError(
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


def _event_from_db(record: Mapping[str, Any]) -> PaperEditEvent:
    return PaperEditEvent(
        id=record["id"],
        paper_version_id=record["paper_version_id"],
        change_set_id=record["change_set_id"],
        edit_seq=record["edit_seq"],
        ord=record["ord"],
        kind=record["operation_kind"],
        slot_id=record["slot_id"],
        reason=record.get("reason"),
        previous_exercise_id=record.get("previous_exercise_id"),
        previous_exercise_version_id=record.get("previous_exercise_version_id"),
        previous_exercise_instance_id=record.get("previous_exercise_instance_id"),
        previous_exercise_family_id=record.get("previous_exercise_family_id"),
        previous_source_release_id=record.get("previous_source_release_id"),
        previous_release_context_id=record.get("previous_release_context_id"),
        new_exercise_id=record.get("new_exercise_id"),
        new_exercise_version_id=record.get("new_exercise_version_id"),
        new_exercise_instance_id=record.get("new_exercise_instance_id"),
        new_exercise_family_id=record.get("new_exercise_family_id"),
        new_source_release_id=record.get("new_source_release_id"),
        new_release_context_id=record.get("new_release_context_id"),
        previous_slot_no=record.get("previous_slot_no"),
        new_slot_no=record.get("new_slot_no"),
        previous_score=record.get("previous_score"),
        new_score=record.get("new_score"),
        manifest_hash=record["manifest_hash"],
        actor_id=record["actor_id"],
        occurred_at=record["occurred_at"],
    )


# ---------------------------------------------------------------------------
# 授权端口：把 paper.* 动作映射到既有的课程范围判定
# ---------------------------------------------------------------------------

class PaperScopeAuthorizer:
    """纸卷权限 = 该课程范围内的教师/管理员。

    动作名（`paper.write` / `paper.read`，以及任务 7 的 `paper.submit` /
    `paper.review` / `paper.withdraw`）与蓝图分开，是为了任务 5+ 能独立收紧
    （例如复核人只看不写）；但**范围判定**直接复用 `CourseScopeAuthorizer`，
    不复制第二套"谁教这门课"的实现——两套实现迟早会漂移成两个权限模型。
    """

    _ACTIONS = {
        PAPER_WRITE_ACTION: "paper_blueprint.write",
        PAPER_READ_ACTION: "paper_blueprint.read",
        # 提交/撤回是写动作；复核只需要读范围——复核人不必是这份卷子的作者，
        # 但必须是这门课里有班级的教师（或管理员）。
        PAPER_SUBMIT_ACTION: "paper_blueprint.write",
        PAPER_REVIEW_ACTION: "paper_blueprint.read",
        PAPER_WITHDRAW_ACTION: "paper_blueprint.write",
    }

    def __init__(self, actor: Any) -> None:
        self.actor = actor
        self._inner = CourseScopeAuthorizer(actor)

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        mapped = self._ACTIONS.get(action)
        if mapped is None:
            raise PermissionError(action)
        self._inner.require(actor_id, mapped, **scope)

    def is_human(self, actor_id: UUID) -> bool:
        return self._inner.is_human(actor_id)


def load_paper_version(cur: Any, version_id: UUID) -> PaperVersionRow | None:
    """在**调用方的事务**里读一份完整卷面（卷头 + 分节 + 槽位）。

    为什么要有这个函数：任务 8 的发布终检必须在同一事务、同一 advisory lock 下
    读卷面，不能另开一个 `db.transaction()` —— 那会拿到第二条连接，既读不到锁内
    的写入，也躲不开 paper_* 的 FORCE RLS。这里复用同一套列常量与行映射，不复制
    第二份"卷面长什么样"的 SQL；少一份就少一处会漂移的字段清单。
    """
    return _PostgresPaperUnitOfWork(cur).get(version_id)


# ---------------------------------------------------------------------------
# 纯编辑规则（无 IO，便于单测）
# ---------------------------------------------------------------------------

def _require_score(value: Decimal | None) -> Decimal:
    """分值必须是 Decimal、有限、非负、最多两位小数（计划书 §0.3 规则 1）。"""
    if not isinstance(value, Decimal):
        raise PaperRuleError("score_precision", f"score 必须是 Decimal，收到 {value!r}")
    try:
        validate_score(value)
    except ValueError as exc:
        raise PaperRuleError("score_precision", str(exc)) from exc
    if value < 0:
        raise PaperRuleError("score_precision", f"score 不能为负：{value}")
    return value


def _check_slot_choice(spec: BlueprintSlot, candidate: CandidateRecord,
                       release_context_id: UUID) -> None:
    """一条候选能不能进这个槽位——入卷前的全部槽位级硬校验。"""
    evidence = candidate.evidence
    missing = evidence_source_missing(evidence)
    if missing:
        # 候选自身缺来源（正常快照不会发生）：宁可拒绝，也不写"只有题号"的槽位
        raise PaperRuleError(
            "source_incomplete",
            f"候选缺少固定来源字段：{'、'.join(missing)}",
        )
    if evidence.release_context_id != release_context_id:
        raise PaperRuleError(
            "release_context_mismatch",
            "候选来自另一个 Release Context，不能进入本卷",
        )
    if evidence.exercise_version_id in _clean_ids(spec.excluded_exercise_version_ids):
        raise PaperRuleError(
            "candidate_excluded",
            f"蓝图槽位 {spec.code} 明确排除了该题目版本",
        )
    if spec.mandatory_exercise_version_ids and (
        evidence.exercise_version_id not in _clean_ids(spec.mandatory_exercise_version_ids)
    ):
        raise PaperRuleError(
            "candidate_not_mandatory",
            f"蓝图槽位 {spec.code} 指定了必选题目版本",
        )


def _slot_from_candidate(slot: PaperSlotView, candidate: CandidateRecord) -> PaperSlotView:
    evidence = candidate.evidence
    updated = slot.model_copy(update={
        "exercise_id": evidence.exercise_id,
        "exercise_version_id": evidence.exercise_version_id,
        "exercise_instance_id": evidence.exercise_instance_id,
        "exercise_family_id": evidence.exercise_family_id,
        "source_release_id": evidence.source_release_id,
        "release_context_id": evidence.release_context_id,
        "estimated_seconds": (
            evidence.estimated_seconds
            if evidence.estimated_seconds is not None
            else slot.estimated_seconds
        ),
        "dependency_manifest_hash": evidence.dependency_manifest_hash,
    })
    return updated.model_copy(update={"content_hash": slot_content_hash(updated)})


def _finalise_sections(sections: Sequence[PaperSectionView]) -> tuple[PaperSectionView, ...]:
    """重算每个槽位/分节的内容哈希与分节分（顺序与分值都可能被改过）。"""
    finalised: list[PaperSectionView] = []
    for section in sections:
        slots = tuple(
            slot.model_copy(update={"content_hash": slot_content_hash(slot)})
            for slot in section.slots
        )
        section_score = sum((slot.score for slot in slots), Decimal("0.00"))
        rebuilt = section.model_copy(update={"slots": slots, "section_score": section_score})
        finalised.append(rebuilt.model_copy(
            update={"content_hash": section_content_hash(rebuilt)}))
    return tuple(finalised)


def _apply_operations(paper: PaperVersionRow, blueprint: BlueprintContent,
                      snapshot: CandidateSnapshot, operations: Sequence[EditOperation],
                      ) -> PaperVersionRow:
    """在内存里应用一批编辑操作，返回 edit_seq+1 的新行（不落库）。

    全部校验先于任何持久化：任一操作失败整批不生效（SQL 仓储在同一事务里写）。
    """
    if not operations:
        raise PaperRuleError("no_operations", "一次编辑至少要有一条操作")
    candidates = {record.evidence.exercise_version_id: record for record in snapshot.accepted}
    specs = {
        slot_spec.code: slot_spec
        for section_spec in blueprint.sections
        for slot_spec in section_spec.slots
    }
    sections: list[PaperSectionView] = list(paper.sections)
    for operation in operations:
        if operation.kind not in EDIT_KINDS:
            raise PaperRuleError(
                "unknown_operation",
                f"未知编辑操作：{operation.kind!r}（只允许 replace/move/score）",
            )
        located = None
        for section_index, section in enumerate(sections):
            for slot_index, slot in enumerate(section.slots):
                if slot.id == operation.slot_id:
                    located = (section_index, slot_index)
                    break
            if located:
                break
        if located is None:
            raise PaperRuleError(
                "slot_not_found",
                f"槽位 {operation.slot_id} 不在本卷",
            )
        section_index, slot_index = located
        section = sections[section_index]
        slots = list(section.slots)
        slot = slots[slot_index]

        if operation.kind == "replace":
            if operation.exercise_version_id is None:
                raise PaperRuleError(
                    "missing_exercise_version_id",
                    "replace 必须给 exercise_version_id（完整来源由固定快照解析）",
                )
            candidate = candidates.get(operation.exercise_version_id)
            if candidate is None:
                # 关键门禁：替换只能来自建卷时固定的候选快照，不能取"当前最新版本"
                raise PaperRuleError(
                    "candidate_not_in_snapshot",
                    "replacement is not in the pinned candidate snapshot："
                    "该题目版本不在建卷时的候选清单里",
                )
            spec = specs.get(slot.code)
            if spec is None:  # pragma: no cover —— 槽位 code 必然来自蓝图
                raise PaperRuleError("unknown_slot_code", f"蓝图没有槽位 {slot.code}")
            _check_slot_choice(spec, candidate, paper.release_context_id)
            slots[slot_index] = _slot_from_candidate(slot, candidate)

        elif operation.kind == "move":
            target = operation.target_slot_no
            if not isinstance(target, int) or isinstance(target, bool) or not (
                1 <= target <= len(slots)
            ):
                raise PaperRuleError(
                    "invalid_slot_order",
                    f"target_slot_no 必须在 1..{len(slots)} 之间，收到 {target!r}",
                )
            moved = slots.pop(slot_index)
            slots.insert(target - 1, moved)
            slots = [
                slot_item.model_copy(update={"slot_no": number})
                for number, slot_item in enumerate(slots, 1)
            ]

        else:  # score
            score = _require_score(operation.score)
            slots[slot_index] = slot.model_copy(update={"score": score})

        sections[section_index] = section.model_copy(update={"slots": tuple(slots)})

    finalised = _finalise_sections(sections)
    edited = paper.model_copy(update={
        "sections": finalised,
        "edit_seq": paper.edit_seq + 1,
        "review_manifest_hash": None,
        "submitted_manifest_hash": None,
        "replayed": False,
        "created": False,
    })
    return edited.model_copy(update={"manifest_hash": paper_manifest(edited)})


def build_edit_events(before: PaperVersionRow, after: PaperVersionRow,
                      operations: Sequence[EditOperation], *,
                      change_set_id: UUID, actor_id: UUID,
                      occurred_at: datetime) -> tuple[PaperEditEvent, ...]:
    """把一批操作写成不可变事件：旧来源与新来源同时保留。"""
    before_slots = {slot.id: slot for section in before.sections for slot in section.slots}
    after_slots = {slot.id: slot for section in after.sections for slot in section.slots}
    events: list[PaperEditEvent] = []
    for index, operation in enumerate(operations, 1):
        previous = before_slots.get(operation.slot_id)
        new = after_slots.get(operation.slot_id)
        if previous is None or new is None:  # pragma: no cover —— 应用操作后必然存在
            raise PaperRuleError("slot_not_found", f"槽位 {operation.slot_id} 不在本卷")
        events.append(PaperEditEvent(
            id=uuid4(),
            paper_version_id=after.id,
            change_set_id=change_set_id,
            edit_seq=after.edit_seq,
            ord=index,
            kind=operation.kind,  # type: ignore[arg-type]
            slot_id=operation.slot_id,
            reason=operation.reason,
            previous_exercise_id=previous.exercise_id,
            previous_exercise_version_id=previous.exercise_version_id,
            previous_exercise_instance_id=previous.exercise_instance_id,
            previous_exercise_family_id=previous.exercise_family_id,
            previous_source_release_id=previous.source_release_id,
            previous_release_context_id=previous.release_context_id,
            new_exercise_id=new.exercise_id,
            new_exercise_version_id=new.exercise_version_id,
            new_exercise_instance_id=new.exercise_instance_id,
            new_exercise_family_id=new.exercise_family_id,
            new_source_release_id=new.source_release_id,
            new_release_context_id=new.release_context_id,
            previous_slot_no=previous.slot_no,
            new_slot_no=new.slot_no,
            previous_score=previous.score,
            new_score=new.score,
            manifest_hash=after.manifest_hash,
            actor_id=actor_id,
            occurred_at=occurred_at,
        ))
    return tuple(events)


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

class PaperService:
    """`create_manual_draft` 与 `edit_draft` 是纸卷写入的唯一入口。

    依赖三个端口：
      * `repository` —— paper_* 表（本模块）；
      * `blueprints` —— 蓝图版本（课程范围、槽位规格）；
      * `candidates` —— 固定候选快照（可替换题目集合）。

    为什么要通过 `blueprints` 而不是直接读纸卷行上的 course_id 做授权：纸卷行的
    课程范围必须与它的蓝图版本一致，而"谁教这门课"的真源只有一个（班级名单）。
    从蓝图拿 `course_ref` 再授权，纸卷就不可能声称自己在另一门课。
    """

    def __init__(
        self,
        *,
        repository: PaperRepository | None = None,
        authorizer: Any | None = None,
        blueprints: Any | None = None,
        candidates: CandidateService | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self.blueprints = blueprints
        self.candidates = candidates
        self._new_id = id_factory
        self._now = clock

    # -- 建卷 ---------------------------------------------------------------
    def create_manual_draft(
        self,
        *,
        actor_id: UUID,
        actor_ref: str | None = None,
        blueprint_version_id: UUID | None = None,
        candidate_snapshot_id: UUID | None = None,
        selections: Sequence[SlotSelection] = (),
        paper_id: UUID | None = None,
        base_paper_version_id: UUID | None = None,
        idempotency_key: str | None = None,
        assembly_run_id: UUID | None = None,
        selected_solution_index: int | None = None,
    ) -> PaperVersionRow:
        """从蓝图 + 固定候选快照建立 draft；或把已批准卷克隆成新 paper 身份的 draft。

        `assembly_run_id / selected_solution_index` 是阶段 B 的组卷来源：两者要么
        同时给（约束组卷），要么同时为空（手工卷）。只给一个是错的——那会让卷子
        声称"来自某次运行"却说不出是第几个方案，回放就断了（0034 也这么约束）。
        """
        self._require_ports()
        key = _clean_key(idempotency_key)
        if (assembly_run_id is None) != (selected_solution_index is None):
            raise PaperRuleError(
                "invalid_assembly_source",
                "组卷来源必须成对给出：assembly_run_id 与 selected_solution_index 要么都有、要么都空",
            )
        if assembly_run_id is not None and (blueprint_version_id is None or not selections):
            raise PaperRuleError(
                "invalid_assembly_source",
                "约束组卷必须同时给出蓝图版本与选题（求解方案不能事后补）",
            )
        if base_paper_version_id is not None:
            if blueprint_version_id is not None or candidate_snapshot_id is not None or selections:
                raise PaperRuleError(
                    "invalid_create_mode",
                    "克隆只能用 base_paper_version_id，不能再带蓝图/快照/选题",
                )
            return self._clone(
                actor_id=actor_id, actor_ref=actor_ref,
                base_paper_version_id=base_paper_version_id,
                paper_id=paper_id, idempotency_key=key,
            )
        if blueprint_version_id is None or candidate_snapshot_id is None:
            raise PaperRuleError(
                "invalid_create_mode",
                "建卷必须同时给 blueprint_version_id 与 candidate_snapshot_id"
                "（候选范围不能由调用方自选）",
            )
        version = self._load_blueprint(actor_id, actor_ref, blueprint_version_id)
        self._authorize_write(actor_id, actor_ref, version)
        self._check_assembly_source(version, assembly_run_id, selected_solution_index)
        fingerprint = canonical_hash({
            "mode": "draft",
            "blueprint_version_id": blueprint_version_id,
            "candidate_snapshot_id": candidate_snapshot_id,
            "selections": tuple(selections),
            "paper_id": paper_id,
            "assembly_run_id": assembly_run_id,
            "selected_solution_index": selected_solution_index,
        })
        with self._unit_of_work() as uow:
            replay = self._replay(uow, CREATE_SCOPE, key, actor_id, fingerprint)
            if replay is not None:
                return replay
            snapshot = self._snapshot_for(actor_id, version, candidate_snapshot_id)
            row = self._build_draft(
                actor_id=actor_id, version=version, snapshot=snapshot,
                selections=tuple(selections), paper_id=paper_id,
                actor_ref=actor_ref,
                assembly_run_id=assembly_run_id,
                selected_solution_index=selected_solution_index,
            )
            uow.lock_paper(row.paper_id)
            stored = uow.add(row)
            self._remember_refs(uow, version, actor_id, actor_ref)
            self._save_replay(uow, CREATE_SCOPE, key, actor_id, fingerprint, stored)
            return stored

    def _build_draft(self, *, actor_id: UUID, version: BlueprintVersionRow,
                     snapshot: CandidateSnapshot, selections: tuple[SlotSelection, ...],
                     paper_id: UUID | None, actor_ref: str | None,
                     assembly_run_id: UUID | None = None,
                     selected_solution_index: int | None = None) -> PaperVersionRow:
        blueprint = version.content
        accepted = {
            record.evidence.exercise_version_id: record
            for record in snapshot.accepted
        }
        # 先判槽位编码本身：'蓝图没有这个槽位'比'这个版本的题不在快照里'更接近
        # 调用方的笔误，先报前者能少一次无意义的排查。
        selected: dict[str, UUID] = {}
        for selection in selections:
            if selection.slot_code in selected:
                raise PaperRuleError(
                    "duplicate_slot_selection",
                    f"槽位 {selection.slot_code} 被选择了不止一次",
                )
            selected[selection.slot_code] = selection.exercise_version_id
        known_codes = {
            slot_spec.code
            for section_spec in blueprint.sections
            for slot_spec in section_spec.slots
        }
        unknown = sorted(set(selected) - known_codes)
        if unknown:
            raise PaperRuleError(
                "unknown_slot_code",
                f"蓝图没有这些槽位：{'、'.join(unknown)}",
            )
        chosen: dict[str, CandidateRecord] = {}
        for slot_code, version_id in selected.items():
            candidate = accepted.get(version_id)
            if candidate is None:
                raise PaperRuleError(
                    "candidate_not_in_snapshot",
                    "selection is not in the pinned candidate snapshot："
                    f"{version_id}",
                )
            chosen[slot_code] = candidate

        sections: list[PaperSectionView] = []
        for order, section_spec in enumerate(blueprint.sections, 1):
            slots: list[PaperSlotView] = []
            for number, slot_spec in enumerate(section_spec.slots, 1):
                candidate = chosen.get(slot_spec.code)
                if candidate is not None:
                    _check_slot_choice(slot_spec, candidate, version.release_context_id)
                    evidence = candidate.evidence
                    slot = PaperSlotView(
                        id=self._new_id(),
                        section_code=section_spec.code,
                        code=slot_spec.code,
                        slot_no=number,
                        exercise_id=evidence.exercise_id,
                        exercise_version_id=evidence.exercise_version_id,
                        exercise_instance_id=evidence.exercise_instance_id,
                        exercise_family_id=evidence.exercise_family_id,
                        source_release_id=evidence.source_release_id,
                        release_context_id=evidence.release_context_id,
                        score=slot_spec.score,
                        estimated_seconds=(
                            evidence.estimated_seconds
                            if evidence.estimated_seconds is not None
                            else slot_spec.estimated_seconds
                        ),
                        display_config=dict(FIXED_DISPLAY),
                        dependency_manifest_hash=evidence.dependency_manifest_hash,
                        content_hash=None,
                    )
                else:
                    # 空槽位：所有来源字段同时为空，是**显式缺口**而不是"只有题号"。
                    # 任务 5 的整卷校验器会在提交审核时要求填满或明确删除。
                    slot = PaperSlotView(
                        id=self._new_id(),
                        section_code=section_spec.code,
                        code=slot_spec.code,
                        slot_no=number,
                        score=slot_spec.score,
                        estimated_seconds=slot_spec.estimated_seconds,
                        display_config=dict(FIXED_DISPLAY),
                        release_context_id=version.release_context_id,
                        content_hash=None,
                    )
                slot = slot.model_copy(update={"content_hash": slot_content_hash(slot)})
                slots.append(slot)
            section_score = sum((slot.score for slot in slots), Decimal("0.00"))
            section_view = PaperSectionView(
                code=section_spec.code,
                title=section_spec.title,
                instruction=section_spec.instruction,
                sort_order=order,
                section_score=section_score,
                slots=tuple(slots),
                content_hash=None,
            )
            sections.append(section_view.model_copy(
                update={"content_hash": section_content_hash(section_view)}))

        view = PaperView(
            id=self._new_id(),
            blueprint_version_id=version.id,
            release_context_id=version.release_context_id,
            course_id=version.course_id,
            offering_id=version.offering_id,
            purpose=version.purpose,
            paper_kind=version.purpose,
            status="draft",
            total_score=version.total_score,
            duration_seconds=version.duration_seconds,
            edit_seq=1,
            manifest_hash="",
            review_manifest_hash=None,
            sections=tuple(sections),
        )
        row = PaperVersionRow(
            id=view.id,
            paper_id=paper_id or self._new_id(),
            version_no=1,
            blueprint_version_id=view.blueprint_version_id,
            candidate_snapshot_id=snapshot.id,
            base_paper_version_id=None,
            assembly_run_id=assembly_run_id,
            selected_solution_index=selected_solution_index,
            course_id=view.course_id,
            offering_id=view.offering_id,
            release_context_id=view.release_context_id,
            purpose=view.purpose,
            paper_kind=view.paper_kind,
            status=view.status,
            total_score=view.total_score,
            duration_seconds=view.duration_seconds,
            edit_seq=1,
            manifest_hash="",
            review_manifest_hash=None,
            sections=view.sections,
            submitted_manifest_hash=None,
            created_by=actor_id,
            created_at=self._now(),
            course_ref=version.course_ref,
            offering_ref=version.offering_ref,
            created_by_ref=actor_ref,
            created=True,
        )
        return row.model_copy(update={"manifest_hash": paper_manifest(view)})

    def _clone(self, *, actor_id: UUID, actor_ref: str | None,
               base_paper_version_id: UUID, paper_id: UUID | None,
               idempotency_key: str | None) -> PaperVersionRow:
        """已批准/已撤回卷只能克隆成**新 paper 身份**的新草稿。

        为什么不复用 base 的 paper_id：0027 的 `UNIQUE (manifest_hash, paper_id)`
        规定同一 paper 身份不能存两份内容相同的版本——而克隆的初始内容必然相同。
        编号语义因此是"新身份的第 1 版 + base_paper_version_id 指回旧身份"，
        既保留纠错链，又不制造"同一身份两个同内容版本"的悖论。
        """
        with self._unit_of_work() as uow:
            base = uow.get(base_paper_version_id)
        if base is None:
            raise PaperNotFoundError(str(base_paper_version_id))
        version = self._load_blueprint(actor_id, actor_ref, base.blueprint_version_id)
        self._authorize_write(actor_id, actor_ref, version)
        if base.status not in {"approved", "withdrawn"}:
            raise PaperRuleError(
                "base_not_approved",
                f"只能克隆 approved/withdrawn 卷；当前状态 {base.status}",
            )
        if paper_id is not None and paper_id == base.paper_id:
            raise PaperRuleError(
                "paper_identity_conflict",
                "克隆必须使用新的 paper_id：同一 paper 身份不能有两个同内容版本",
            )
        fingerprint = canonical_hash({
            "mode": "clone",
            "base_paper_version_id": base_paper_version_id,
            "paper_id": paper_id,
        })
        with self._unit_of_work() as uow:
            replay = self._replay(uow, CREATE_SCOPE, idempotency_key, actor_id, fingerprint)
            if replay is not None:
                return replay
            sections = tuple(
                _clone_section(section, self._new_id) for section in base.sections
            )
            view = PaperView(
                id=self._new_id(),
                blueprint_version_id=base.blueprint_version_id,
                release_context_id=base.release_context_id,
                course_id=base.course_id,
                offering_id=base.offering_id,
                purpose=base.purpose,
                paper_kind=base.paper_kind or base.purpose,
                status="draft",
                total_score=base.total_score,
                duration_seconds=base.duration_seconds,
                edit_seq=1,
                manifest_hash="",
                review_manifest_hash=None,
                sections=sections,
            )
            row = PaperVersionRow(
                id=view.id,
                paper_id=paper_id or self._new_id(),
                version_no=1,
                blueprint_version_id=view.blueprint_version_id,
                candidate_snapshot_id=base.candidate_snapshot_id,
                base_paper_version_id=base.id,
                course_id=view.course_id,
                offering_id=view.offering_id,
                release_context_id=view.release_context_id,
                purpose=view.purpose,
                paper_kind=view.paper_kind,
                status=view.status,
                total_score=view.total_score,
                duration_seconds=view.duration_seconds,
                edit_seq=1,
                manifest_hash="",
                review_manifest_hash=None,
                sections=view.sections,
                submitted_manifest_hash=None,
                created_by=actor_id,
                created_at=self._now(),
                course_ref=version.course_ref,
                offering_ref=version.offering_ref,
                created_by_ref=actor_ref,
                created=True,
            )
            uow.lock_paper(row.paper_id)
            stored = uow.add(row.model_copy(
                update={"manifest_hash": paper_manifest(view)}))
            self._remember_refs(uow, version, actor_id, actor_ref)
            self._save_replay(
                uow, CREATE_SCOPE, idempotency_key, actor_id, fingerprint, stored)
            return stored

    # -- 编辑 ---------------------------------------------------------------
    def edit_draft(
        self,
        *,
        actor_id: UUID,
        actor_ref: str | None = None,
        paper_version_id: UUID,
        base_version: int,
        operations: Sequence[EditOperation],
        idempotency_key: str | None = None,
    ) -> PaperVersionRow:
        """在同一个 draft 内替换/调序/调分；`base_version` 必须是当前 `edit_seq`。"""
        self._require_ports()
        key = _clean_key(idempotency_key)
        with self._unit_of_work() as uow:
            paper = uow.get(paper_version_id)
        if paper is None:
            raise PaperNotFoundError(str(paper_version_id))
        version = self._load_blueprint(actor_id, actor_ref, paper.blueprint_version_id)
        self._authorize_write(actor_id, actor_ref, version)
        fingerprint = canonical_hash({
            "paper_version_id": paper_version_id,
            "base_version": base_version,
            "operations": tuple(operations),
        })
        with self._unit_of_work() as uow:
            # 幂等重放必须先于 base_version 判断：网络重试带的是**原请求**的
            # base_version，而第一次已经把它推进了一版。先判 stale 会把"重试"
            # 误报成"别人改过"，客户端于是永远拿不到原本成功的结果。
            replay = self._replay(uow, EDIT_SCOPE, key, actor_id, fingerprint)
            if replay is not None:
                return replay
            if paper.status != "draft":
                raise PaperNotDraftError(
                    f"only draft can be edited（当前状态 {paper.status}）",
                    current_status=paper.status,
                )
            if paper.edit_seq != base_version:
                raise StalePaperVersion(
                    f"stale_version: base_version={base_version}，"
                    f"当前 edit_seq={paper.edit_seq}",
                    current_version=paper.edit_seq,
                )
            snapshot = self._snapshot_for(
                actor_id, version, paper.candidate_snapshot_id)
            edited = _apply_operations(
                paper, version.content, snapshot, tuple(operations))
            occurred_at = self._now()
            events = build_edit_events(
                paper, edited, tuple(operations),
                change_set_id=self._new_id(), actor_id=actor_id,
                occurred_at=occurred_at,
            )
            stored = uow.save_edit(edited, base_version, events)
            self._save_replay(uow, EDIT_SCOPE, key, actor_id, fingerprint, stored)
            return stored.model_copy(update={
                "course_ref": version.course_ref,
                "offering_ref": version.offering_ref,
                "created_by_ref": uow.lookup_ref("account", paper.created_by),
            })

    # -- 读取 ---------------------------------------------------------------
    def get_version(self, *, actor_id: UUID, actor_ref: str | None = None,
                    paper_version_id: UUID) -> PaperVersionRow:
        with self._unit_of_work() as uow:
            row = uow.get(paper_version_id)
            if row is None:
                raise PaperNotFoundError(str(paper_version_id))
            version = self._load_blueprint(
                actor_id, actor_ref, row.blueprint_version_id)
            self._authorize(actor_id, actor_ref, version, PAPER_READ_ACTION)
            created_by_ref = uow.lookup_ref("account", row.created_by)
        return row.model_copy(update={
            "course_ref": version.course_ref,
            "offering_ref": version.offering_ref,
            "created_by_ref": created_by_ref,
        })

    def edit_events(self, *, actor_id: UUID, actor_ref: str | None = None,
                    paper_version_id: UUID) -> tuple[PaperEditEvent, ...]:
        with self._unit_of_work() as uow:
            row = uow.get(paper_version_id)
            if row is None:
                raise PaperNotFoundError(str(paper_version_id))
            version = self._load_blueprint(
                actor_id, actor_ref, row.blueprint_version_id)
            self._authorize(actor_id, actor_ref, version, PAPER_READ_ACTION)
            return uow.list_edit_events(paper_version_id)

    # -- 内部 ---------------------------------------------------------------
    def _require_ports(self) -> None:
        if self.repository is None:
            raise PaperError("缺少纸卷仓储：拒绝无持久化的建卷/编辑")
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的纸卷写入")
        if self.blueprints is None or self.candidates is None:
            raise PaperError("缺少蓝图/候选服务：纸卷必须固定在蓝图版本与候选快照上")

    def _unit_of_work(self) -> ContextManager[PaperUnitOfWork]:
        if self.repository is None:  # pragma: no cover —— _require_ports 已挡
            raise PaperError("缺少纸卷仓储")
        return self.repository.unit_of_work()

    def _load_blueprint(self, actor_id: UUID, actor_ref: str | None,
                        version_id: UUID) -> BlueprintVersionRow:
        # get_version 自己会做课程范围的 READ 授权；纸卷的写权限在 _authorize 里再判。
        return self.blueprints.get_version(
            actor_id=actor_id, version_id=version_id, actor_ref=actor_ref)

    def _authorize(self, actor_id: UUID, actor_ref: str | None,
                   version: BlueprintVersionRow, action: str) -> None:
        course_ref = version.course_ref
        if not course_ref:
            # fail closed：没有文本课程号就无法判断"谁教这门课"
            raise PermissionError("蓝图版本缺少 course_ref，无法做课程范围授权")
        self.authorizer.require(
            actor_id, action,
            course_id=version.course_id,
            course_ref=course_ref,
            offering_id=version.offering_id,
            offering_ref=version.offering_ref,
            actor_ref=actor_ref,
        )

    def _authorize_write(self, actor_id: UUID, actor_ref: str | None,
                         version: BlueprintVersionRow) -> None:
        self._authorize(actor_id, actor_ref, version, PAPER_WRITE_ACTION)
        if not self.authorizer.is_human(actor_id):  # type: ignore[union-attr]
            raise PermissionError("纸卷只能由人类用户创建/编辑")

    @staticmethod
    def _check_assembly_source(version: BlueprintVersionRow, assembly_run_id: UUID | None,
                               selected_solution_index: int | None) -> None:
        """蓝图模式与建卷来源必须匹配，两个方向都拦。

        为什么不能只靠"取值为空就当手工卷"：教师拿一份约束组卷蓝图手工挑题，
        得到的卷子看起来完全正常，但它没有求解记录，而蓝图上写着"这份卷应当由
        求解器组装"。反过来，手工蓝图配上 assembly_run_id 则是伪造来源。
        """
        mode = getattr(version, "assembly_mode", "manual")
        if assembly_run_id is None and mode != "manual":
            raise PaperRuleError(
                "assembly_only_blueprint",
                "这份蓝图是约束组卷（assembly_mode=constraint_assisted）："
                "必须从一次求解运行选方案建卷，不能手工挑题",
            )
        if assembly_run_id is not None and mode == "manual":
            raise PaperRuleError(
                "manual_blueprint_with_assembly_source",
                "手工蓝图不能声称来自求解运行：组卷来源只能由约束组卷蓝图产生",
            )
        if assembly_run_id is not None and selected_solution_index is not None:
            if not 0 <= selected_solution_index <= 2:
                raise PaperRuleError(
                    "solution_index_out_of_range",
                    f"方案序号必须在 0..2 之间，收到 {selected_solution_index}",
                )

    def _snapshot_for(self, actor_id: UUID, version: BlueprintVersionRow,
                      snapshot_id: UUID) -> CandidateSnapshot:
        try:
            scope = CandidateScope.from_version(version)
        except ValueError as exc:
            raise CandidateScopeError(str(exc)) from exc
        return self.candidates.get_snapshot(  # type: ignore[union-attr]
            actor_id=actor_id, scope=scope, snapshot_id=snapshot_id)

    def _replay(self, uow: PaperUnitOfWork, scope: str, key: str | None,
                actor_id: UUID, fingerprint: str) -> PaperVersionRow | None:
        """幂等重放：同键同载荷返回原响应；同键不同载荷 409。

        必须在"当前 edit_seq/身份"判断之前：重试不该因为第一次已经推进了一版而 409。
        """
        if key is None:
            return None
        record = uow.find_idempotent(scope, key, actor_id)
        if record is None:
            return None
        if record.request_hash != fingerprint:
            raise PaperIdempotencyConflictError(
                "同一 Idempotency-Key 被用于不同的纸卷载荷"
            )
        stored = PaperVersionRow.model_validate(record.response)
        return stored.model_copy(update={"created": False, "replayed": True})

    def _save_replay(self, uow: PaperUnitOfWork, scope: str, key: str | None,
                     actor_id: UUID, fingerprint: str,
                     row: PaperVersionRow) -> None:
        if key is None:
            return
        uow.save_idempotent(IdempotencyRecord(
            id=self._new_id(),
            scope=scope,
            idempotency_key=key,
            actor_id=actor_id,
            request_hash=fingerprint,
            response=row.model_dump(mode="json"),
            created_at=self._now(),
        ))

    def _remember_refs(self, uow: PaperUnitOfWork, version: BlueprintVersionRow,
                       actor_id: UUID, actor_ref: str | None) -> None:
        if version.course_ref:
            uow.remember_ref("course", version.course_ref, version.course_id)
        if version.offering_ref and version.offering_id:
            uow.remember_ref("offering", version.offering_ref, version.offering_id)
        if actor_ref:
            uow.remember_ref("account", actor_ref, actor_id)


def _clone_section(section: PaperSectionView, new_id: Callable[[], UUID]
                   ) -> PaperSectionView:
    """克隆分节：槽位 id 必须换（PK 唯一），内容哈希不换（内容没变）。"""
    slots = tuple(slot.model_copy(update={"id": new_id()}) for slot in section.slots)
    cloned = section.model_copy(update={"slots": slots})
    return cloned.model_copy(update={"content_hash": section_content_hash(cloned)})


def _clean_key(key: str | None) -> str | None:
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned or len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise PaperRuleError(
            "idempotency_key_required",
            f"Idempotency-Key 不能为空且不超过 {MAX_IDEMPOTENCY_KEY} 字符",
        )
    return cleaned


__all__ = [
    "CREATE_SCOPE",
    "EDIT_KINDS",
    "EDIT_SCOPE",
    "EditOperation",
    "InMemoryPaperRepository",
    "MANIFEST_SCHEMA",
    "PAPER_READ_ACTION",
    "PAPER_REVIEW_ACTION",
    "PAPER_SUBMIT_ACTION",
    "PAPER_WITHDRAW_ACTION",
    "PAPER_WRITE_ACTION",
    "PaperEditEvent",
    "PaperError",
    "PaperIdempotencyConflictError",
    "PaperNotDraftError",
    "PaperNotFoundError",
    "PaperRepository",
    "PaperRuleError",
    "PaperScopeAuthorizer",
    "PaperService",
    "PaperUnitOfWork",
    "PaperVersionRow",
    "PostgresPaperRepository",
    "SOURCE_FIELDS",
    "StalePaperVersion",
    "build_edit_events",
    "evidence_source_missing",
    "manifest_payload",
    "paper_manifest",
    "section_content_hash",
    "slot_content_hash",
    "slot_source_missing",
]
