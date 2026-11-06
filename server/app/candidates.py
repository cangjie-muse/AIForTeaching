"""候选资格判定与候选快照（`CandidateService.snapshot`）。

一句话：**只有「属于固定 Release Context + 审核有效 + 许可有效 + 未撤回 + 有答案 +
可渲染 + 无曝光冲突」的题才进 accepted**，其余全部落进 rejected 并带原因码。

## 判定为什么写在服务层而不是 SQL 里

SQL 负责"把事实取回来"（Release 链、当前内容、审核/许可、依赖、曝光事件），
判定写成纯函数 `candidate_reasons`。这样同一份规则能被三处复用而不会漂移：
候选预检（本模块）、整卷校验（任务 4）、发布终检（任务 5）——`ExposureService`
已经按这个方式共享，资格判定也一样。

## 与计划书的差异

* 计划书里 `latest_decisions` 的键是"依赖类型 + 版本 ID"（题干/答案/量规各一条
  review_decision_item）。本仓库**没有** governance 审核表，唯一审核真源是
  `exercise.review_status`（题干与答案共用一条状态；没有量规列）。因此 decision
  键固定为 `exercise`，值为 `review_status` 的终态映射；`dependency_manifest_hash`
  覆盖知识点、内容哈希与决策序号，承担"依赖清单"的角色。
* 计划书要求"参数化题必须有已审实例"。本仓库没有 `exercise_instance` 表，
  source 恒置 `parameterized=False`；契约层的这道门仍然保留（见 reserved 原因码）。
* 计划书要求"云端处理许可"。没有"是否需要云端"的标记，无法判定，故不设门；
  已在交付说明里列为未实现项。

## 不变式

1. `accepted` 里的每条都通过了**全部**规则（没有任何 tolerated 警告项）；
2. `rejected` 里的每条至少有一个原因码，且原因码都在 `REASON_MEANINGS` 里；
3. `candidate_manifest_hash` 只由内容决定：不含时间、不含快照 id，
   重复生成同输入得到同一哈希（对应 0027 的 UNIQUE(blueprint_version_id, hash)）。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from pydantic import Field

from . import db
from .contracts import (
    CandidateEvidence,
    CandidateReason,
    CandidateRecord,
    CandidateScope,
    CandidateSnapshot,
    ContractModel,
    ExposureEvent,
    RejectedCandidate,
    canonical_hash,
)
from .contracts.candidate import REASON_MEANINGS
from .exposure import ExposureConflict, ExposureService
from .releases import (
    APPROVED_REVIEW_STATUSES,
    INVALID_LICENSES,
    REVOKED_REVIEW_STATUSES,
    SUPPORTED_LANGUAGES,
    ReleaseStatus,
    concept_uuid,
    content_hash,
    exercise_uuid,
    family_id_for,
    utcnow,
)

SNAPSHOT_SCHEMA = "paper-candidate-snapshot/v1"
SNAPSHOT_ACTION = "paper_blueprint.write"
READ_ACTION = "paper_blueprint.read"

# 蓝图/发布动作里"是否需要保密"的用途集合。只有 formal 要求题目从未公开过答案。
SECRET_PURPOSES = frozenset({"formal"})


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class CandidateError(Exception):
    """候选域错误基类。"""


class ReleaseContextNotFoundError(CandidateError):
    """蓝图指向的 Release Context 不存在（fail closed：不是"候选为空"）。"""


class CandidateSnapshotNotFoundError(CandidateError):
    """该蓝图版本还没有候选快照。"""


class CandidateScopeError(CandidateError, ValueError):
    """候选范围缺失/非法（例如蓝图没有文本课程号）。"""


# ---------------------------------------------------------------------------
# 作用域与结果行
# ---------------------------------------------------------------------------

class ReleaseScopeInfo(ContractModel):
    """候选快照需要的 Release 元数据（不读 release_item 正文）。"""

    release_context_id: UUID
    course_id: UUID
    course_ref: str = Field(min_length=1, max_length=200)
    offering_id: UUID | None = None
    offering_ref: str | None = Field(default=None, max_length=200)
    status: ReleaseStatus
    item_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 纯判定
# ---------------------------------------------------------------------------

def decision_for_review_status(review_status: str) -> str:
    """`review_status` → 候选口径的终态值（approved / revoked / pending）。"""
    if review_status in APPROVED_REVIEW_STATUSES:
        return "approved"
    if review_status in REVOKED_REVIEW_STATUSES:
        return "revoked"
    return "pending"


def license_is_valid(license: str | None) -> bool:
    """显式拒绝集合之外都算"有许可"；空值单独由 `license_missing` 报告。"""
    text = (license or "").strip().lower()
    return bool(text) and text not in INVALID_LICENSES


def candidate_reasons(record: CandidateRecord) -> tuple[CandidateReason, ...]:
    """一条候选的全部拒绝原因（纯函数，无 IO）。

    规则顺序只影响原因码排列，不影响结论：**任何一条命中即不合格**。
    """
    evidence = record.evidence
    reasons: list[CandidateReason] = []

    def add(code: CandidateReason) -> None:
        if code not in reasons:
            reasons.append(code)

    # 1) 固定 Release Context：条目状态比布尔更可解释，所以先按状态给最具体的码。
    state = record.release_state
    if state == "excluded":
        add("outside_release_context")
    elif state == "missing":
        add("exercise_missing")
    elif state == "drifted":
        add("content_drift")
    elif state == "withdrawn":
        add("release_withdrawn")
    if not evidence.in_context:
        add("outside_release_context")

    # 2) 审核终态。decisions 为空 = 没有任何人工决策记录，按"未审"处理（fail closed）。
    decisions = tuple(evidence.latest_decisions.values())
    if not evidence.decision_seqs:
        add("review_not_approved")
    elif any(value == "revoked" for value in decisions):
        add("revoked")
    elif any(value != "approved" for value in decisions):
        add("review_not_approved")
    if evidence.revoked:
        add("revoked")

    # 3) 内容完整性与许可。缺答案与"许可缺失/无效"分开报：教师改的地方不一样。
    if not record.answer_present:
        add("answer_missing")
    if not (record.license or "").strip():
        add("license_missing")
    elif not evidence.license_valid or not license_is_valid(record.license):
        add("license_invalid")

    # 4) 渲染与语言。
    if not evidence.language_pair_valid:
        add("language_pair_invalid")
    if not evidence.render_ready:
        add("render_not_ready")

    # 5) 保留位维度（本仓库暂无数据真源，但契约层必须留门）。
    if evidence.parameterized and (
        evidence.exercise_instance_id is None or not evidence.instance_approved
    ):
        add("missing_approved_instance")
    if not evidence.dependencies_complete:
        add("dependencies_incomplete")
    if evidence.hidden_evaluation:
        add("hidden_evaluation")
    if evidence.private_draft:
        add("private_draft")
    if not evidence.purpose_allowed:
        add("purpose_not_allowed")

    return tuple(reasons)


def release_scope_reasons(scope: CandidateScope,
                          release: ReleaseScopeInfo) -> tuple[CandidateReason, ...]:
    """整卷共性的前置拒绝原因（Release 范围与冻结状态）。

    为什么放在每条候选上而不是直接抛错：教师看到的是"这些题为什么都不能用"，
    需要一条能与题目对上的解释；直接 500/空列表会让界面只能显示"候选不足"。
    """
    reasons: list[CandidateReason] = []
    if release.course_id != scope.course_id or release.course_ref != scope.course_ref:
        reasons.append("scope_mismatch")
    # release 的 offering 为 None 表示"整门课共享"；否则必须与本卷班级一致。
    if release.offering_ref not in {None, scope.offering_ref}:
        reasons.append("scope_mismatch")
    if scope.purpose in SECRET_PURPOSES and release.status != "frozen":
        reasons.append("release_not_frozen")
    return tuple(reasons)


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class CandidateRepository(Protocol):
    def unit_of_work(self) -> ContextManager["CandidateUnitOfWork"]: ...


class CandidateUnitOfWork(Protocol):
    def load_release(self, scope: CandidateScope) -> ReleaseScopeInfo | None: ...

    def load_records(self, scope: CandidateScope) -> tuple[CandidateRecord, ...]: ...

    def load_exposure(self, family_ids: Sequence[UUID]) -> tuple[ExposureEvent, ...]: ...

    def add(self, snapshot: CandidateSnapshot,
            manifest: Mapping[str, Any]) -> CandidateSnapshot: ...

    def latest(self, blueprint_version_id: UUID) -> CandidateSnapshot | None: ...

    def get(self, snapshot_id: UUID) -> CandidateSnapshot | None: ...


# ---------------------------------------------------------------------------
# 内存实现（单元测试 / 无库环境）
# ---------------------------------------------------------------------------

class InMemoryCandidateRepository:
    def __init__(
        self,
        *,
        release: ReleaseScopeInfo | None = None,
        records: Sequence[CandidateRecord] = (),
        events: Sequence[ExposureEvent] = (),
    ) -> None:
        self.releases: dict[UUID, ReleaseScopeInfo] = {}
        if release is not None:
            self.releases[release.release_context_id] = release
        self.records: tuple[CandidateRecord, ...] = tuple(records)
        self.events: tuple[ExposureEvent, ...] = tuple(events)
        self.snapshots: dict[tuple[UUID, str], CandidateSnapshot] = {}

    # -- 测试夹具 ---------------------------------------------------------
    def seed_release(self, info: ReleaseScopeInfo,
                     *, context_id: UUID | None = None) -> ReleaseScopeInfo:
        stored = info if context_id is None else info.model_copy(
            update={"release_context_id": context_id}
        )
        self.releases[stored.release_context_id] = stored
        return stored

    def seed_record(self, record: CandidateRecord) -> None:
        self.records = (*self.records, record)

    # -- 端口 -------------------------------------------------------------
    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryCandidateRepository"]:
        yield self

    def load_release(self, scope: CandidateScope) -> ReleaseScopeInfo | None:
        return self.releases.get(scope.release_context_id)

    def load_records(self, scope: CandidateScope) -> tuple[CandidateRecord, ...]:
        return tuple(
            record for record in self.records
            if record.evidence.release_context_id == scope.release_context_id
        )

    def load_exposure(self, family_ids: Sequence[UUID]) -> tuple[ExposureEvent, ...]:
        wanted = set(family_ids)
        return tuple(event for event in self.events if event.exercise_family_id in wanted)

    def add(self, snapshot: CandidateSnapshot,
            manifest: Mapping[str, Any]) -> CandidateSnapshot:
        key = (snapshot.blueprint_version_id, snapshot.candidate_manifest_hash)
        existing = self.snapshots.get(key)
        if existing is not None:
            return existing.model_copy(update={"created": False})
        self.snapshots[key] = snapshot
        return snapshot

    def latest(self, blueprint_version_id: UUID) -> CandidateSnapshot | None:
        candidates = [
            snapshot for (version, _), snapshot in self.snapshots.items()
            if version == blueprint_version_id
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda item: (item.created_at, str(item.id)))
        # 读取路径不是"刚刚创建"，created 一律 False，避免 GET 也声称 201 语义。
        return newest.model_copy(update={"created": False})

    def get(self, snapshot_id: UUID) -> CandidateSnapshot | None:
        for snapshot in self.snapshots.values():
            if snapshot.id == snapshot_id:
                # 读取路径：created 一律 False（不是本次请求新建的）。
                return snapshot.model_copy(update={"created": False})
        return None


# ---------------------------------------------------------------------------
# PostgreSQL 实现
# ---------------------------------------------------------------------------

def _record_from_db(row: Mapping[str, Any], scope: CandidateScope) -> CandidateRecord:
    """把一行联表结果变成候选记录。

    Release 固定的版本（`exercise_version_id` / `released_content_hash`）与
    "当前内容"是两件事：前者是审核过的身份，后者用来判断是否被原地编辑。
    审核/许可/语言/答案取**当前值**——它们回答的是"现在还能不能用"。
    """
    course_ref = str(row["course_ref"])
    exercise_ref = str(row["exercise_ref"])
    released_hash = str(row["released_content_hash"])
    released_version = row["exercise_version_id"]

    current_available = row.get("joined_exercise_ref") is not None
    if not current_available:
        release_state = "missing"
        prompt = ""
        answer: str | None = None
        review_status = "missing"
        license_text: str | None = None
        language = ""
        difficulty = None
        content_version = None
        node_refs: tuple[str, ...] = ()
    else:
        prompt = row.get("prompt_md") or ""
        answer = row.get("answer_md")
        review_status = row.get("review_status") or "draft"
        license_text = row.get("license")
        language = row.get("language") or ""
        difficulty = row.get("difficulty")
        content_version = row.get("content_version")
        node_refs = tuple(row.get("node_refs") or ())
        current_hash = content_hash(prompt, answer)
        item_state = row.get("state") or "included"
        if item_state == "withdrawn":
            release_state = "withdrawn"
        elif item_state == "excluded":
            release_state = "excluded"
        elif current_hash != released_hash:
            release_state = "drifted"
        else:
            release_state = "included"

    decision = decision_for_review_status(review_status)
    concept_ids = tuple(concept_uuid(course_ref, ref) for ref in node_refs)
    decision_seqs: dict[str, int] = (
        {"exercise": int(content_version)} if content_version else {}
    )
    latest_decisions = {"exercise": decision}
    dependency_manifest_hash = canonical_hash({
        "exercise_version_id": released_version,
        "content_hash": released_hash,
        "concept_ids": concept_ids,
        "decision_seqs": decision_seqs,
    })
    evidence = CandidateEvidence(
        release_context_id=scope.release_context_id,
        source_release_id=row["source_release_id"],
        exercise_id=row.get("exercise_id") or exercise_uuid(course_ref, exercise_ref),
        exercise_version_id=released_version,
        exercise_instance_id=None,
        exercise_family_id=row.get("exercise_family_id") or family_id_for(course_ref, exercise_ref),
        exercise_kind="unknown",  # 本仓库 exercise 没有题型列（已知限制）
        concept_ids=concept_ids,
        learning_outcome_ids=(),
        difficulty_label=None if difficulty is None else f"difficulty-{int(difficulty)}",
        estimated_seconds=None,
        decision_seqs=decision_seqs,
        latest_decisions=latest_decisions,
        dependency_manifest_hash=dependency_manifest_hash,
        content_hash=released_hash,
        in_context=release_state == "included",
        license_valid=license_is_valid(license_text),
        revoked=decision == "revoked",
        hidden_evaluation=False,
        private_draft=False,
        parameterized=False,
        instance_approved=True,
        dependencies_complete=True,
        render_ready=bool(prompt.strip()),
        language_pair_valid=(language or "").strip().lower() in SUPPORTED_LANGUAGES,
        purpose_allowed=True,
        cloud_allowed=True,
    )
    return CandidateRecord(
        evidence=evidence,
        release_state=release_state,  # type: ignore[arg-type]
        review_status=review_status,
        license=license_text,
        answer_present=bool((answer or "").strip()),
    )


class PostgresCandidateRepository:
    """候选读取与快照写入的 PostgreSQL 仓储。

    每次调用都是一个 `db.transaction(assessment_service=True)`：release_* 与
    candidate_snapshot 都在 assessment 命名空间，且 paper_* 是 FORCE RLS，
    统一带作用域可以让同一段代码在以后被整卷/发布流程复用时不必返工。
    """

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresCandidateUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresCandidateUnitOfWork(cur)


class _PostgresCandidateUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def load_release(self, scope: CandidateScope) -> ReleaseScopeInfo | None:
        self.cur.execute(
            """SELECT rc.id, rc.course_id, rc.course_ref, rc.offering_id,
                      rc.offering_ref, rc.status,
                      (
                        SELECT count(*)::int FROM assessment.release_item ri
                         WHERE ri.release_context_id = rc.id AND ri.state = 'included'
                      ) AS item_count
                 FROM assessment.release_context rc
                WHERE rc.id = %s""",
            (scope.release_context_id,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        return ReleaseScopeInfo(
            release_context_id=row["id"],
            course_id=row["course_id"],
            course_ref=row["course_ref"],
            offering_id=row.get("offering_id"),
            offering_ref=row.get("offering_ref"),
            status=row["status"],
            item_count=row["item_count"],
        )

    def load_records(self, scope: CandidateScope) -> tuple[CandidateRecord, ...]:
        # 两件事同时做：
        #   1) 沿 base_release_id 解析"有效 base 链"（规格 §6.2 允许基础发布 + 小修订）；
        #   2) 同一题在链上出现多次时取最近一层（precedence=1），旧层不得盖住新层。
        # depth < 32 是环保护：误配的 base 链不会让查询永远跑不完。
        self.cur.execute(
            """WITH RECURSIVE chain AS (
                   SELECT rc.id, rc.base_release_id, 0 AS depth
                     FROM assessment.release_context rc
                    WHERE rc.id = %s
                   UNION ALL
                   SELECT parent.id, parent.base_release_id, chain.depth + 1
                     FROM assessment.release_context parent
                     JOIN chain ON parent.id = chain.base_release_id
                    WHERE chain.depth < 32
               ),
               ranked AS (
                   SELECT ri.*,
                          ROW_NUMBER() OVER (
                            PARTITION BY ri.exercise_ref ORDER BY chain.depth
                          ) AS precedence
                     FROM assessment.release_item ri
                     JOIN chain ON chain.id = ri.release_context_id
               )
               SELECT ri.course_ref, ri.exercise_ref, ri.source_release_id,
                      ri.exercise_id, ri.exercise_version_id,
                      ri.content_hash AS released_content_hash,
                      ri.exercise_family_id, ri.state,
                      e.id AS joined_exercise_ref, e.prompt_md, e.answer_md,
                      e.review_status, e.license, e.language, e.difficulty,
                      e.content_version,
                      ARRAY(
                        SELECT en.node_id FROM exercise_node en
                         WHERE en.course_id = ri.course_ref
                           AND en.exercise_id = ri.exercise_ref
                      ) AS node_refs
                 FROM ranked ri
                 LEFT JOIN exercise e
                        ON e.course_id = ri.course_ref AND e.id = ri.exercise_ref
                WHERE ri.precedence = 1
                ORDER BY ri.exercise_ref""",
            (scope.release_context_id,),
        )
        return tuple(_record_from_db(row, scope) for row in self.cur.fetchall())

    def load_exposure(self, family_ids: Sequence[UUID]) -> tuple[ExposureEvent, ...]:
        if not family_ids:
            return ()
        self.cur.execute(
            """SELECT exercise_version_id, exercise_instance_id, exercise_family_id,
                      scope_offering_id, event_kind, visible_from, occurred_at
                 FROM assessment.item_exposure_event
                WHERE exercise_family_id = ANY(%s)
                ORDER BY occurred_at DESC""",
            (list(family_ids),),
        )
        return tuple(
            ExposureEvent(
                exercise_version_id=row["exercise_version_id"],
                exercise_instance_id=row.get("exercise_instance_id"),
                exercise_family_id=row["exercise_family_id"],
                scope_offering_id=row.get("scope_offering_id"),
                event_kind=row["event_kind"],
                visible_from=row["visible_from"],
                occurred_at=row["occurred_at"],
            )
            for row in self.cur.fetchall()
        )

    def add(self, snapshot: CandidateSnapshot,
            manifest: Mapping[str, Any]) -> CandidateSnapshot:
        self.cur.execute(
            """INSERT INTO assessment.candidate_snapshot
                 (id, blueprint_version_id, candidate_manifest,
                  candidate_manifest_hash, created_by)
               VALUES (%s, %s, %s::jsonb, %s, %s::uuid)
               ON CONFLICT (blueprint_version_id, candidate_manifest_hash) DO NOTHING
               RETURNING id, created_at""",
            (
                snapshot.id,
                snapshot.blueprint_version_id,
                _json(manifest),
                snapshot.candidate_manifest_hash,
                # manifest 里的 created_by 是审计字段（字符串），不参与哈希
                snapshot.manifest.get("created_by"),
            ),
        )
        inserted = self.cur.fetchone()
        if inserted:
            return snapshot.model_copy(update={
                "id": inserted["id"],
                "created_at": inserted["created_at"],
                "created": True,
            })
        # 内容键命中：同一份候选清单已经存过，返回原行而不是报 500。
        self.cur.execute(
            """SELECT id, candidate_manifest, candidate_manifest_hash, created_at
                 FROM assessment.candidate_snapshot
                WHERE blueprint_version_id = %s AND candidate_manifest_hash = %s""",
            (snapshot.blueprint_version_id, snapshot.candidate_manifest_hash),
        )
        existing = self.cur.fetchone()
        if existing is None:  # pragma: no cover —— 唯一键与查询条件不可能同时落空
            raise CandidateError("候选快照内容去重失败：唯一键命中但读不到原行")
        return _snapshot_from_manifest(
            _as_dict(existing["candidate_manifest"]),
            id=existing["id"],
            manifest_hash=existing["candidate_manifest_hash"],
            created_at=existing["created_at"],
        )

    def latest(self, blueprint_version_id: UUID) -> CandidateSnapshot | None:
        self.cur.execute(
            """SELECT id, candidate_manifest, candidate_manifest_hash, created_at
                 FROM assessment.candidate_snapshot
                WHERE blueprint_version_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1""",
            (blueprint_version_id,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        return _snapshot_from_manifest(
            _as_dict(row["candidate_manifest"]),
            id=row["id"],
            manifest_hash=row["candidate_manifest_hash"],
            created_at=row["created_at"],
        )

    def get(self, snapshot_id: UUID) -> CandidateSnapshot | None:
        """按 id 读取一份已存候选清单（纸卷建卷时**固定**用它，而不是取最新）。"""
        self.cur.execute(
            """SELECT id, candidate_manifest, candidate_manifest_hash, created_at
                 FROM assessment.candidate_snapshot
                WHERE id = %s""",
            (snapshot_id,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        return _snapshot_from_manifest(
            _as_dict(row["candidate_manifest"]),
            id=row["id"],
            manifest_hash=row["candidate_manifest_hash"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# manifest 与序列化
# ---------------------------------------------------------------------------

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _as_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _snapshot_from_manifest(
    manifest: Mapping[str, Any],
    *,
    id: UUID,
    manifest_hash: str,
    created_at: datetime,
) -> CandidateSnapshot:
    scope = manifest.get("scope") or {}
    return CandidateSnapshot(
        id=id,
        blueprint_version_id=scope["blueprint_version_id"],
        release_context_id=scope["release_context_id"],
        purpose=scope["purpose"],
        course_id=scope["course_id"],
        offering_id=scope.get("offering_id"),
        accepted=tuple(
            CandidateRecord.model_validate(item)
            for item in (manifest.get("accepted") or ())
        ),
        rejected=tuple(
            RejectedCandidate.model_validate(item)
            for item in (manifest.get("rejected") or ())
        ),
        candidate_manifest_hash=manifest_hash,
        created_at=created_at,
        created=False,
        manifest=dict(manifest),
    )


def snapshot_from_manifest(
    manifest: Mapping[str, Any],
    *,
    id: UUID,
    manifest_hash: str,
    created_at: datetime,
) -> CandidateSnapshot:
    """公开入口：把 `candidate_manifest` jsonb 还原成快照视图。

    纸卷服务（server/app/papers.py）按快照 id 读取，需要与候选服务同一套解析规则；
    这里只暴露一个转发函数，避免把私有实现 import 到另一个模块后各自演化。
    """
    return _snapshot_from_manifest(
        manifest, id=id, manifest_hash=manifest_hash, created_at=created_at
    )


def build_manifest(
    *,
    scope: CandidateScope,
    release: ReleaseScopeInfo,
    accepted: Sequence[CandidateRecord],
    rejected: Sequence[RejectedCandidate],
    events: Sequence[ExposureEvent],
    as_of: datetime,
) -> dict[str, Any]:
    """规范清单：只由内容决定（没有时间戳、没有快照 id）。

    `created_by` 单独放在 `created_by` 键里但不参与哈希——它不影响"哪些题合格"，
    参与哈希会让同内容换个操作者就换哈希，幂等键就废了。
    """
    visible_events = sorted(
        (event for event in events if event.visible_from <= as_of),
        key=lambda event: (
            str(event.exercise_family_id),
            event.event_kind,
            event.visible_from.isoformat(),
            event.occurred_at.isoformat(),
        ),
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        # 一律走 mode="json"：UUID/Decimal 在库里是原生类型，进 manifest 必须是字符串，
        # 否则 jsonb 写入会炸，而且跨环境哈希对不上。
        "scope": scope.model_dump(mode="json"),
        "release": {
            "status": release.status,
            "item_count": release.item_count,
        },
        "accepted": [
            record.model_dump(mode="json")
            for record in sorted(accepted, key=lambda item: str(item.evidence.exercise_version_id))
        ],
        "rejected": [
            item.model_dump(mode="json")
            for item in sorted(rejected, key=lambda item: str(item.exercise_version_id))
        ],
        "exposure_events": [event.model_dump(mode="json") for event in visible_events],
    }


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """清单哈希：`created_by` 不参与（见 `build_manifest`）。"""
    payload = {key: value for key, value in manifest.items() if key != "created_by"}
    return canonical_hash(payload)


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

class CandidateService:
    """`CandidateService.snapshot` 是候选清单的唯一入口（计划书 §0.3 规则 8）。"""

    def __init__(
        self,
        repository: CandidateRepository | None = None,
        authorizer: Any | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self._new_id = id_factory
        self._now = clock

    # -- 授权 ---------------------------------------------------------------
    def _authorise(self, actor_id: UUID, action: str, scope: CandidateScope) -> None:
        if self.authorizer is None:
            # 与 BlueprintService 同一口径：没有授权端口就拒绝，绝不"默认放行"。
            raise PermissionError("缺少授权端口：拒绝无授权的候选快照操作")
        self.authorizer.require(
            actor_id, action,
            course_id=scope.course_id,
            course_ref=scope.course_ref,
            offering_id=scope.offering_id,
            offering_ref=scope.offering_ref,
        )
        if action == SNAPSHOT_ACTION and not self.authorizer.is_human(actor_id):
            raise PermissionError("候选快照只能由人类用户生成")

    # -- 生成 ---------------------------------------------------------------
    def snapshot(self, *, actor_id: UUID, scope: CandidateScope) -> CandidateSnapshot:
        self._authorise(actor_id, SNAPSHOT_ACTION, scope)
        as_of = self._now()
        with self._unit_of_work() as uow:
            release = uow.load_release(scope)
            if release is None:
                # fail closed：蓝图指向一个不存在的 Release，是配置错误而不是"候选为空"。
                raise ReleaseContextNotFoundError(str(scope.release_context_id))
            records = uow.load_records(scope)
            events = uow.load_exposure(
                tuple({record.evidence.exercise_family_id for record in records})
            )
            common = release_scope_reasons(scope, release)
            accepted: list[CandidateRecord] = []
            rejected: list[RejectedCandidate] = []
            for record in records:
                reasons: list[CandidateReason] = list(common)
                for code in candidate_reasons(record):
                    if code not in reasons:
                        reasons.append(code)
                if not reasons:
                    try:
                        ExposureService.assert_no_conflict(
                            scope.purpose, scope.offering_id,
                            (record.evidence,), events, as_of,
                        )
                    except ExposureConflict:
                        reasons.append("exposure_conflict")
                if reasons:
                    rejected.append(RejectedCandidate(
                        exercise_version_id=record.evidence.exercise_version_id,
                        exercise_id=record.evidence.exercise_id,
                        exercise_family_id=record.evidence.exercise_family_id,
                        review_status=record.review_status,
                        reasons=tuple(reasons),
                    ))
                else:
                    accepted.append(record)

            manifest = build_manifest(
                scope=scope, release=release,
                accepted=accepted, rejected=rejected,
                events=events, as_of=as_of,
            )
            digest = manifest_hash(manifest)
            view = CandidateSnapshot(
                id=self._new_id(),
                blueprint_version_id=scope.blueprint_version_id,
                release_context_id=scope.release_context_id,
                purpose=scope.purpose,
                course_id=scope.course_id,
                offering_id=scope.offering_id,
                accepted=tuple(accepted),
                rejected=tuple(rejected),
                candidate_manifest_hash=digest,
                created_at=as_of,
                created=True,
                manifest={**manifest, "created_by": str(actor_id)},
            )
            return uow.add(view, view.manifest)

    # -- 读取 ---------------------------------------------------------------
    def latest(self, *, actor_id: UUID, scope: CandidateScope) -> CandidateSnapshot:
        self._authorise(actor_id, READ_ACTION, scope)
        with self._unit_of_work() as uow:
            stored = uow.latest(scope.blueprint_version_id)
        if stored is None:
            raise CandidateSnapshotNotFoundError(str(scope.blueprint_version_id))
        return stored

    def get_snapshot(self, *, actor_id: UUID, scope: CandidateScope,
                     snapshot_id: UUID) -> CandidateSnapshot:
        """按 id 读取快照，并核对它确实属于这个蓝图版本/Release/范围。

        纸卷（paper_version.candidate_snapshot_id）必须固定到**建卷那一刻**的候选清单：
        取"最新快照"会让一次重新冻结 Release 悄悄改变可替换的题目集合。所以读的是
        指定 id，并且四元组（蓝图版本、Release、用途、课程/班级）必须与当前范围一致；
        任何一项不符都是 `CandidateScopeError`（路由映射 422 `snapshot_mismatch`）。
        """
        self._authorise(actor_id, READ_ACTION, scope)
        with self._unit_of_work() as uow:
            stored = uow.get(snapshot_id)
        if stored is None:
            raise CandidateSnapshotNotFoundError(str(snapshot_id))
        if (
            stored.blueprint_version_id != scope.blueprint_version_id
            or stored.release_context_id != scope.release_context_id
            or stored.purpose != scope.purpose
            or stored.course_id != scope.course_id
            or stored.offering_id != scope.offering_id
        ):
            raise CandidateScopeError(
                f"candidate snapshot {snapshot_id} 不属于蓝图版本 "
                f"{scope.blueprint_version_id} 的当前范围"
            )
        return stored

    # -- 内部 ---------------------------------------------------------------
    def _unit_of_work(self) -> ContextManager[CandidateUnitOfWork]:
        if self.repository is None:
            raise CandidateError("缺少候选仓储：拒绝无持久化的快照生成")
        return self.repository.unit_of_work()


__all__ = [
    "CandidateError",
    "CandidateRepository",
    "CandidateScopeError",
    "CandidateService",
    "CandidateSnapshotNotFoundError",
    "CandidateUnitOfWork",
    "InMemoryCandidateRepository",
    "PostgresCandidateRepository",
    "REASON_MEANINGS",
    "ReleaseContextNotFoundError",
    "ReleaseScopeInfo",
    "SNAPSHOT_ACTION",
    "SNAPSHOT_SCHEMA",
    "build_manifest",
    "candidate_reasons",
    "decision_for_review_status",
    "license_is_valid",
    "manifest_hash",
    "release_scope_reasons",
    "snapshot_from_manifest",
]
