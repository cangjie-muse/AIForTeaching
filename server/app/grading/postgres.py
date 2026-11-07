"""评分域的 PostgreSQL 仓储（阶段 D 任务 8 的落库面）。

0037 已把表建好；这里只做"服务层端口 → SQL"的映射，不重复业务规则。

与内存实现（revisions/publish）的差异只有一处：**写入顺序**。数据库里
`grade_revision` 有 append-only 触发器（只允许发布字段变），所以：

  * 新增版本 = INSERT 一条新行；改分绝不 UPDATE 旧行；
  * 发布 = UPDATE 状态/发布人/发布时间（这是触发器唯一放行的更新）；
  * 撤回同上：不是 UPDATE 成 withdrawn 就算完，而是**新增一条 withdrawn 行**，
    旧行保持 published（历史事实）。学生"当前成绩"由 `current_published_revision_id` 决定。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator
from uuid import UUID

from .. import db
from .contracts import GradeRevision
from .revisions import DecisionRecord
from .publish import AppealRecord, PublishResult
from .contracts import Appeal, AppealStatus


class PostgresGradingRepository:
    @contextmanager
    def unit_of_work(self) -> Iterator["PostgresGradingRepository"]:
        with db.transaction(assessment_service=True) as cur:
            yield PostgresGradingRepository(cur)  # type: ignore[arg-type]

    def __init__(self, cur: Any = None) -> None:
        self.cur = cur

    # -- grade & revisions ----------------------------------------------
    def ensure_grade(self, *, submission_id: UUID, student_id: UUID) -> UUID:
        self.cur.execute(
            "SELECT id FROM assessment.grade WHERE submission_id = %s", (submission_id,))
        row = self.cur.fetchone()
        if row:
            return row["id"]
        self.cur.execute(
            """INSERT INTO assessment.grade (id, submission_id, status)
               VALUES (gen_random_uuid(), %s, 'draft') RETURNING id""",
            (submission_id,))
        return self.cur.fetchone()["id"]

    def revisions_for(self, submission_id: UUID) -> tuple[GradeRevision, ...]:
        self.cur.execute(
            """SELECT id, submission_id, revision_no, previous_revision_id, total_score,
                      status, change_reason, created_at, published_by, published_at
                 FROM assessment.grade_revision
                WHERE submission_id = %s ORDER BY revision_no""",
            (submission_id,))
        return tuple(
            GradeRevision(
                id=row["id"], submission_id=row["submission_id"],
                revision_no=row["revision_no"],
                total_score=Decimal(row["total_score"]),
                status=row["status"], previous_revision_id=row["previous_revision_id"],
                reason=row["change_reason"], created_at=row["created_at"],
                published_by=row["published_by"], published_at=row["published_at"],
            )
            for row in self.cur.fetchall())

    def save_revision(self, revision: GradeRevision) -> GradeRevision:
        self.cur.execute(
            """UPDATE assessment.grade_revision
                  SET status = %s, published_by = %s, published_at = %s
                WHERE id = %s""",
            (revision.status, revision.published_by, revision.published_at, revision.id))
        return revision

    def insert_revision(self, revision: GradeRevision, *, grade_id: UUID,
                        created_by: UUID) -> None:
        self.cur.execute(
            """INSERT INTO assessment.grade_revision
                 (id, grade_id, submission_id, revision_no, previous_revision_id,
                  total_score, status, change_reason, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s)
               ON CONFLICT (grade_id, revision_no) DO NOTHING""",
            (revision.id, grade_id, revision.submission_id, revision.revision_no,
             revision.previous_revision_id, revision.total_score, revision.reason,
             created_by, revision.created_at))

    def decisions_for_current(self, submission_id: UUID) -> tuple[DecisionRecord, ...]:
        self.cur.execute(
            """SELECT d.id, d.submission_item_id, d.grade_revision_id, d.decision_seq,
                      d.score, d.rationale, d.supersedes_id, d.decided_by, d.decided_at
                 FROM assessment.item_grade_decision d
                WHERE d.grade_revision_id = (
                      SELECT id FROM assessment.grade_revision
                       WHERE submission_id = %s
                       ORDER BY revision_no DESC LIMIT 1)
                ORDER BY d.decision_seq""",
            (submission_id,))
        return tuple(
            DecisionRecord(
                id=row["id"], submission_item_id=row["submission_item_id"],
                grade_revision_id=row["grade_revision_id"], sequence=row["decision_seq"],
                score=Decimal(row["score"]) if row["score"] is not None else Decimal("0"),
                decided_by=row["decided_by"], decided_at=row["decided_at"],
                rationale=row["rationale"], supersedes_id=row["supersedes_id"],
            )
            for row in self.cur.fetchall())

    def last_decision(self, submission_item_id: UUID) -> DecisionRecord | None:
        self.cur.execute(
            """SELECT id, submission_item_id, grade_revision_id, decision_seq, score,
                      rationale, supersedes_id, decided_by, decided_at
                 FROM assessment.item_grade_decision
                WHERE submission_item_id = %s ORDER BY decision_seq DESC LIMIT 1""",
            (submission_item_id,))
        row = self.cur.fetchone()
        if row is None:
            return None
        return DecisionRecord(
            id=row["id"], submission_item_id=row["submission_item_id"],
            grade_revision_id=row["grade_revision_id"], sequence=row["decision_seq"],
            score=Decimal(row["score"]) if row["score"] is not None else Decimal("0"),
            decided_by=row["decided_by"], decided_at=row["decided_at"],
            rationale=row["rationale"], supersedes_id=row["supersedes_id"],
        )

    def save_decision(self, record: DecisionRecord) -> None:
        self.cur.execute(
            """INSERT INTO assessment.item_grade_decision
                 (id, submission_item_id, grade_revision_id, decision_seq, score,
                  rationale, decided_by, decided_at, supersedes_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (submission_item_id, decision_seq) DO NOTHING""",
            (record.id, record.submission_item_id, record.grade_revision_id,
             record.sequence, record.score, record.rationale, record.decided_by,
             record.decided_at, record.supersedes_id))

    def current_published(self, submission_id: UUID) -> UUID | None:
        self.cur.execute(
            """SELECT g.current_published_revision_id AS id
                 FROM assessment.grade g WHERE g.submission_id = %s""",
            (submission_id,))
        row = self.cur.fetchone()
        return row["id"] if row else None

    def set_current_published(self, *, submission_id: UUID, revision_id: UUID | None) -> None:
        self.cur.execute(
            """UPDATE assessment.grade SET current_published_revision_id = %s
                WHERE submission_id = %s""",
            (revision_id, submission_id))

    # -- 幂等 -------------------------------------------------------------
    def find_idempotent(self, scope: str, key: str, actor: UUID):
        self.cur.execute(
            """SELECT fingerprint, payload FROM assessment.idempotency_record
                WHERE scope = %s AND idempotency_key = %s AND actor_id = %s""",
            (scope, key, actor))
        row = self.cur.fetchone()
        if row is None:
            return None
        return (row["fingerprint"], _publish_result_from(row["payload"]))

    def remember_idempotent(self, scope: str, key: str, actor: UUID,
                            fingerprint: str, result: PublishResult) -> None:
        import json

        self.cur.execute(
            """INSERT INTO assessment.idempotency_record
                 (scope, idempotency_key, actor_id, fingerprint, payload)
               VALUES (%s, %s, %s, %s, %s::jsonb)
               ON CONFLICT DO NOTHING""",
            (scope, key, actor, fingerprint,
             json.dumps({"revision_id": str(result.revision.id),
                         "revision_no": result.revision.revision_no,
                         "status": result.revision.status}, ensure_ascii=False)))

    # -- 申诉 -------------------------------------------------------------
    def save_appeal(self, record: AppealRecord) -> None:
        appeal = record.appeal
        self.cur.execute(
            """INSERT INTO assessment.grade_appeal
                 (id, submission_id, student_id, grade_revision_id, reason, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (appeal.id, appeal.submission_id, appeal.student_id,
             appeal.grade_revision_id, appeal.reason, appeal.status.value
             if hasattr(appeal.status, "value") else appeal.status, appeal.created_at))

    def update_appeal(self, record: AppealRecord) -> None:
        appeal = record.appeal
        self.cur.execute(
            """UPDATE assessment.grade_appeal
                  SET status = %s, resolved_at = %s, resolved_by = %s
                WHERE id = %s""",
            (appeal.status.value if hasattr(appeal.status, "value") else appeal.status,
             appeal.decided_at, appeal.decided_by, appeal.id))

    def appeals_for(self, submission_id: UUID) -> tuple[AppealRecord, ...]:
        self.cur.execute(
            """SELECT id, submission_id, student_id, grade_revision_id, reason,
                      status, created_at, resolved_at, resolved_by
                 FROM assessment.grade_appeal WHERE submission_id = %s
                 ORDER BY created_at""",
            (submission_id,))
        return tuple(
            AppealRecord(Appeal(
                id=row["id"], submission_id=row["submission_id"],
                student_id=row["student_id"], grade_revision_id=row["grade_revision_id"],
                reason=row["reason"], status=AppealStatus(row["status"]),
                created_at=row["created_at"], decided_at=row["resolved_at"],
                decided_by=row["resolved_by"]))
            for row in self.cur.fetchall())


def _publish_result_from(payload: dict[str, Any]) -> PublishResult:
    """把幂等记录还原成"重放结果"。

    这里不重建完整版本行：调用方只用 id/状态/版本号做回应。缺的字段用占位值，
    并在 `replayed=True` 上体现"这是重放，不是新发布"。
    """
    from .contracts import GradeRevision as _Revision

    return PublishResult(
        revision=_Revision(
            id=UUID(str(payload["revision_id"])),
            submission_id=UUID(str(payload.get("submission_id") or payload["revision_id"])),
            revision_no=int(payload["revision_no"]),
            total_score=Decimal(str(payload.get("total_score") or "0")),
            status=payload["status"], created_at=None),
        replayed=True, fingerprint="")


__all__ = ["PostgresGradingRepository"]
