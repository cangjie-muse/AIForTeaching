"""在线作答的 PostgreSQL 仓储（阶段 C 任务 7 落地前必须先有真实落库能力）。

三条与 0036 对齐的实现要点：

1. **开考用 `INSERT ... ON CONFLICT DO NOTHING` + 回查**：0036 的
   `uq_paper_attempt_one_active` 是部分唯一索引，两个标签页同时点"开始"时只有一个能插进去；
   另一个必须回查拿到同一行，而不是报错。
2. **保存的冲突交给唯一索引**：`(attempt_id, client_revision_id)` 重复时回查原行并返回
   同一修订——这比"先查后插"少一次竞态。
3. **交卷先 `SELECT ... FOR UPDATE` 锁 attempt**：手动交卷与到期 worker 会同时打进来，
   锁保证只有一份快照。

所有语句都在 `db.transaction(assessment_service=True)` 里：这些表都是 FORCE RLS，
不设服务作用域连表 owner 也读不到。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import UUID

from .. import db
from .contracts import AttemptStatus, PolicyTiming
from .repository import AttemptAssignment, AttemptItemSeed, AttemptRow
from .responses import ResponseRow

ASSIGNMENT_COLUMNS = """a.id, a.course_id, a.paper_version_id, a.status,
                        a.distribution_kind, a.visible_at, a.due_at, a.hard_close_at,
                        a.offering_id, a.course_id AS course_ref_uuid"""

POLICY_COLUMNS = """p.max_attempts, p.time_limit_seconds, p.grace_seconds,
                    p.late_policy, p.behavior, p.conflict_policy,
                    p.autosave_debounce_ms, p.autosave_max_interval_ms"""

ATTEMPT_COLUMNS = """id, assignment_id, paper_version_id, student_id, attempt_no,
                     status, started_at, server_deadline_at, worker_due_at"""


def _policy(record: Any) -> PolicyTiming:
    return PolicyTiming(
        time_limit_seconds=record["time_limit_seconds"],
        grace_seconds=record["grace_seconds"] or 0,
        late_policy=record["late_policy"],
        max_attempts=record["max_attempts"],
        behavior=record["behavior"],
        conflict_policy=record["conflict_policy"],
        autosave_debounce_ms=record["autosave_debounce_ms"],
        autosave_max_interval_ms=record["autosave_max_interval_ms"],
    )


def _attempt_row(record: Any) -> AttemptRow:
    return AttemptRow(
        id=record["id"], assignment_id=record["assignment_id"],
        paper_version_id=record["paper_version_id"], student_id=record["student_id"],
        attempt_no=record["attempt_no"], status=record["status"],
        started_at=record["started_at"], server_deadline_at=record["server_deadline_at"],
        worker_due_at=record["worker_due_at"], created=False,
    )


class PostgresOnlineRepository:
    """在线作答的全部端口（attempt / response / submission），一个事务作用域。"""

    @contextmanager
    def unit_of_work(self) -> Iterator["PostgresOnlineRepository"]:
        with db.transaction(assessment_service=True) as cur:
            yield PostgresOnlineRepository(cur)  # type: ignore[arg-type]

    def __init__(self, cur: Any = None) -> None:
        self.cur = cur

    # -- 作业 -----------------------------------------------------------
    def assignment(self, assignment_id: UUID) -> AttemptAssignment | None:
        self.cur.execute(
            f"""SELECT {ASSIGNMENT_COLUMNS}, {POLICY_COLUMNS}, c.ref AS course_ref
                  FROM assessment.assignment a
                  JOIN assessment.attempt_policy_version p
                    ON p.id = a.attempt_policy_version_id
                  LEFT JOIN assessment.id_ref c
                    ON c.subject = a.course_id AND c.kind = 'course'
                 WHERE a.id = %s""",
            (assignment_id,),
        )
        record = self.cur.fetchone()
        if record is None:
            return None
        return AttemptAssignment(
            id=record["id"], course_id=record["course_id"],
            course_ref=record["course_ref"] or str(record["course_id"]),
            offering_id=record["offering_id"],
            paper_version_id=record["paper_version_id"],
            status=record["status"], distribution_kind=record["distribution_kind"],
            visible_at=record["visible_at"], due_at=record["due_at"],
            hard_close_at=record["hard_close_at"], policy=_policy(record),
        )

    def assignment_items(self, assignment_id: UUID) -> tuple[AttemptItemSeed, ...]:
        self.cur.execute(
            """SELECT id, paper_slot_id, ord FROM assessment.assignment_item
                WHERE assignment_id = %s ORDER BY ord""",
            (assignment_id,),
        )
        return tuple(
            AttemptItemSeed(assignment_item_id=record["id"],
                            paper_slot_id=record["paper_slot_id"], ord=record["ord"])
            for record in self.cur.fetchall()
        )

    # -- attempt --------------------------------------------------------
    def active_attempt(self, assignment_id: UUID, student_id: UUID) -> AttemptRow | None:
        self.cur.execute(
            f"""SELECT {ATTEMPT_COLUMNS} FROM assessment.paper_attempt
                 WHERE assignment_id = %s AND student_id = %s
                   AND status IN ('in_progress','finalizing')
                 ORDER BY attempt_no DESC LIMIT 1""",
            (assignment_id, student_id),
        )
        record = self.cur.fetchone()
        return _attempt_row(record) if record else None

    def attempts_for(self, assignment_id: UUID, student_id: UUID) -> tuple[AttemptRow, ...]:
        self.cur.execute(
            f"""SELECT {ATTEMPT_COLUMNS} FROM assessment.paper_attempt
                 WHERE assignment_id = %s AND student_id = %s ORDER BY attempt_no""",
            (assignment_id, student_id),
        )
        return tuple(_attempt_row(record) for record in self.cur.fetchall())

    def get(self, attempt_id: UUID) -> AttemptRow | None:
        self.cur.execute(
            f"SELECT {ATTEMPT_COLUMNS} FROM assessment.paper_attempt WHERE id = %s",
            (attempt_id,),
        )
        record = self.cur.fetchone()
        return _attempt_row(record) if record else None

    def lock_attempt(self, attempt_id: UUID) -> AttemptRow | None:
        """手动交卷与到期 worker 的互斥点：同一 attempt 只有一方能往下走。"""
        self.cur.execute(
            f"""SELECT {ATTEMPT_COLUMNS} FROM assessment.paper_attempt
                 WHERE id = %s FOR UPDATE""",
            (attempt_id,),
        )
        record = self.cur.fetchone()
        return _attempt_row(record) if record else None

    def add(self, row: AttemptRow) -> AttemptRow:
        self.cur.execute(
            """INSERT INTO assessment.paper_attempt
                 (id, assignment_id, paper_version_id, student_id,
                  attempt_policy_version_id, attempt_no, mode, status,
                  started_at, server_deadline_at, worker_due_at)
               SELECT %s, a.id, a.paper_version_id, %s, a.attempt_policy_version_id,
                      %s, 'normal', 'in_progress', %s, %s, %s
                 FROM assessment.assignment a WHERE a.id = %s
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (row.id, row.student_id, row.attempt_no, row.started_at,
             row.server_deadline_at, row.worker_due_at, row.assignment_id),
        )
        inserted = self.cur.fetchone()
        if inserted is not None:
            return row
        # 并发撞上部分唯一索引：回查已经存在的那一行（这就是"重复开始返回原 attempt"）
        existing = self.active_attempt(row.assignment_id, row.student_id)
        if existing is None:  # pragma: no cover - 冲突后必然能查到
            raise RuntimeError("attempt 插入冲突但回查不到进行中的行")
        return existing

    def add_items(self, attempt_id: UUID, items: tuple[AttemptItemSeed, ...]) -> None:
        for seed in items:
            self.cur.execute(
                """INSERT INTO assessment.paper_attempt_item
                     (id, attempt_id, assignment_item_id, paper_slot_id, ord)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (attempt_id, seed.assignment_item_id, seed.paper_slot_id, seed.ord),
            )

    def items_for(self, attempt_id: UUID) -> tuple[AttemptItemSeed, ...]:
        self.cur.execute(
            """SELECT assignment_item_id, paper_slot_id, ord
                 FROM assessment.paper_attempt_item WHERE attempt_id = %s ORDER BY ord""",
            (attempt_id,),
        )
        return tuple(
            AttemptItemSeed(assignment_item_id=record["assignment_item_id"],
                            paper_slot_id=record["paper_slot_id"], ord=record["ord"])
            for record in self.cur.fetchall()
        )

    def find_idempotent(self, scope: str, key: str, actor_id: UUID):
        return None  # attempt 的幂等由部分唯一索引 + active_attempt 承担

    def remember_idempotent(self, *args: Any, **kwargs: Any) -> None:
        return None

    # -- response -------------------------------------------------------
    def add_response(self, row: ResponseRow) -> ResponseRow:
        self.cur.execute(
            """INSERT INTO assessment.response_revision
                 (id, attempt_id, paper_slot_id, revision_no, client_revision_id,
                  raw_response, parse_status, invalid_reasons, server_received_at)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
               ON CONFLICT (attempt_id, client_revision_id) DO NOTHING
               RETURNING id""",
            (row.id, row.attempt_id, row.slot_id, row.revision_no,
             row.client_revision_id, _json(row.raw_response), row.parse_status,
             _json(list(row.invalid_reasons)), row.server_received_at),
        )
        if self.cur.fetchone() is not None:
            return row
        existing = self.by_client_id(row.attempt_id, row.client_revision_id)
        if existing is None:  # pragma: no cover
            raise RuntimeError("响应插入冲突但回查不到原行")
        return existing

    def by_client_id(self, attempt_id: UUID, client_revision_id: str) -> ResponseRow | None:
        self.cur.execute(
            """SELECT id, attempt_id, paper_slot_id, revision_no, client_revision_id,
                      raw_response, parse_status, invalid_reasons, server_received_at
                 FROM assessment.response_revision
                WHERE attempt_id = %s AND client_revision_id = %s""",
            (attempt_id, client_revision_id),
        )
        record = self.cur.fetchone()
        return ResponseRow(
            id=record["id"], attempt_id=record["attempt_id"],
            slot_id=record["paper_slot_id"], revision_no=record["revision_no"],
            client_revision_id=record["client_revision_id"],
            raw_response=record["raw_response"],
            parse_status=record["parse_status"],
            invalid_reasons=tuple(record["invalid_reasons"] or ()),
            server_received_at=record["server_received_at"],
        ) if record else None

    def latest(self, attempt_id: UUID, slot_id: UUID) -> ResponseRow | None:
        self.cur.execute(
            """SELECT id, attempt_id, paper_slot_id, revision_no, client_revision_id,
                      raw_response, parse_status, invalid_reasons, server_received_at
                 FROM assessment.response_revision
                WHERE attempt_id = %s AND paper_slot_id = %s
                ORDER BY revision_no DESC LIMIT 1""",
            (attempt_id, slot_id),
        )
        record = self.cur.fetchone()
        return ResponseRow(
            id=record["id"], attempt_id=record["attempt_id"], slot_id=record["paper_slot_id"],
            revision_no=record["revision_no"],
            client_revision_id=record["client_revision_id"],
            raw_response=record["raw_response"], parse_status=record["parse_status"],
            invalid_reasons=tuple(record["invalid_reasons"] or ()),
            server_received_at=record["server_received_at"],
        ) if record else None

    def revisions_for(self, attempt_id: UUID) -> tuple[ResponseRow, ...]:
        self.cur.execute(
            """SELECT id, attempt_id, paper_slot_id, revision_no, client_revision_id,
                      raw_response, parse_status, invalid_reasons, server_received_at
                 FROM assessment.response_revision WHERE attempt_id = %s""",
            (attempt_id,),
        )
        return tuple(
            ResponseRow(id=record["id"], attempt_id=record["attempt_id"],
                        slot_id=record["paper_slot_id"], revision_no=record["revision_no"],
                        client_revision_id=record["client_revision_id"],
                        raw_response=record["raw_response"],
                        parse_status=record["parse_status"],
                        invalid_reasons=tuple(record["invalid_reasons"] or ()),
                        server_received_at=record["server_received_at"])
            for record in self.cur.fetchall()
        )

    # -- submission -----------------------------------------------------
    def submission_for(self, attempt_id: UUID):
        self.cur.execute(
            """SELECT id, attempt_id, student_id, trigger_kind, submitted_at, item_count,
                      unanswered_count, invalid_count, response_manifest_hash, receipt_hash
                 FROM assessment.submission WHERE attempt_id = %s""",
            (attempt_id,),
        )
        record = self.cur.fetchone()
        if record is None:
            return None
        from .submissions import SubmissionRecord  # 延迟导入避免循环

        return SubmissionRecord(
            id=record["id"], attempt_id=record["attempt_id"],
            student_id=record["student_id"], trigger_kind=record["trigger_kind"],
            submitted_at=record["submitted_at"], item_count=record["item_count"],
            unanswered_count=record["unanswered_count"],
            invalid_count=record["invalid_count"],
            response_manifest_hash=record["response_manifest_hash"],
            receipt_hash=record["receipt_hash"], items=(),
        )

    def save_submission(self, record: Any) -> Any:
        from .submissions import SubmissionRecord

        self.cur.execute(
            """INSERT INTO assessment.submission
                 (id, attempt_id, student_id, trigger_kind, submitted_at, item_count,
                  unanswered_count, invalid_count, response_manifest_hash, receipt_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (attempt_id) DO NOTHING RETURNING id""",
            (record.id, record.attempt_id, record.student_id, record.trigger_kind,
             record.submitted_at, record.item_count, record.unanswered_count,
             record.invalid_count, record.response_manifest_hash, record.receipt_hash),
        )
        if self.cur.fetchone() is not None:
            for item in record.items:
                self.cur.execute(
                    """INSERT INTO assessment.submission_item
                         (id, submission_id, assignment_item_id, paper_slot_id,
                          raw_response, parse_status, evaluation_status, requires_human)
                       VALUES (gen_random_uuid(), %s, %s, %s, %s::jsonb, %s, 'pending', false)
                       ON CONFLICT DO NOTHING""",
                    (record.id, item.assignment_item_id, item.slot_id,
                     _json(item.raw_response), item.parse_status),
                )
            self.cur.execute(
                """UPDATE assessment.paper_attempt
                      SET status = 'submitted', submitted_at = %s, close_reason = %s
                    WHERE id = %s AND status IN ('in_progress','finalizing')""",
                (record.submitted_at, record.trigger_kind, record.attempt_id),
            )
            return record
        existing = self.submission_for(record.attempt_id)
        return existing or record

    def student_slots(self, attempt_id: UUID) -> tuple[Any, ...]:
        """学生重连要的槽位行：题干取自题目正表，响应取"本人最新修订"。

        只 SELECT 必要列：题干、公开资产（display_config 里白名单过的部分）、
        输入方式与本人响应。**不查** answer_md / 量规 / 任何内部字段。
        """
        from .contracts import SlotRow

        # 题干来自 public.exercise：assessment 与 public 之间只有 (uuid, 文本课程号)
        # 的 id_ref 桥接，所以必须先经 id_ref 拿到文本课程号再连题目表。
        # 这一条 SQL 只能在真库上验证（本机没有 PostgreSQL）；集成时若列名有出入，
        # 报错会直接指出（不会静默给出空题干）。
        self.cur.execute(
            """SELECT ai.paper_slot_id, ai.ord,
                      COALESCE(e.prompt_md, '') AS stem,
                      r.raw_response, r.parse_status,
                      COALESCE(r.revision_no, 0) AS revision_no
                 FROM assessment.paper_attempt_item ai
                 JOIN assessment.paper_attempt a ON a.id = ai.attempt_id
                 JOIN assessment.paper_slot slot ON slot.id = ai.paper_slot_id
                 LEFT JOIN assessment.paper_version pv ON pv.id = a.paper_version_id
                 LEFT JOIN assessment.id_ref cr
                        ON cr.subject = pv.course_id AND cr.kind = 'course'
                 LEFT JOIN public.exercise e
                        ON e.course_id = cr.ref AND e.id = slot.exercise_id
                 LEFT JOIN LATERAL (
                      SELECT rr.raw_response, rr.parse_status, rr.revision_no
                        FROM assessment.response_revision rr
                       WHERE rr.attempt_id = ai.attempt_id
                         AND rr.paper_slot_id = ai.paper_slot_id
                       ORDER BY rr.revision_no DESC LIMIT 1
                 ) r ON true
                WHERE ai.attempt_id = %s
                ORDER BY ai.ord""",
            (attempt_id,),
        )
        rows = self.cur.fetchall()
        return tuple(
            SlotRow(
                slot_id=record["paper_slot_id"], slot_no=record["ord"],
                stem=record["stem"], public_assets=(),
                input_kind="text",
                raw_response=record["raw_response"],
                parse_status=record["parse_status"] or "empty",
                revision_no=int(record["revision_no"] or 0),
            )
            for record in rows
        )

    def due_attempts(self, now: datetime) -> tuple[AttemptRow, ...]:
        self.cur.execute(
            f"""SELECT {ATTEMPT_COLUMNS} FROM assessment.paper_attempt
                 WHERE status IN ('in_progress','finalizing') AND worker_due_at <= %s
                 ORDER BY worker_due_at LIMIT 100""",
            (now,),
        )
        return tuple(_attempt_row(record) for record in self.cur.fetchall())


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = ["PostgresOnlineRepository"]
