"""求解运行的持久化：`assessment.assembly_run` 的内存与 PostgreSQL 两个实现。

为什么运行行不是"跑完再写一行"：一次求解可能几十秒到几分钟，教师会刷新页面。
如果只在结束时落库，刷新就什么都没有，教师只能再点一次——于是同一份蓝图被求解
三次、产生三个运行，谁也不知道该看哪一个。所以运行先落一行（queued/running），
结果回来再更新同一行，终态之后不再改。

幂等键的作用范围是 `(actor_id, idempotency_key)`：同一个教师用同一把键重试，
返回同一个运行；换个教师用同一把键是另一个运行（不同人的意图不该合并）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .. import db
from .contracts import SOLVED_STATUSES, RunStatus


def _may_transition(current: RunStatus, target: RunStatus) -> bool:
    """状态机：运行中可去任何终态；同一状态可重写（追加诊断）；
    已解出的运行只允许被"反悔"成 FAILED（独立复验不通过）。

    为什么允许 OPTIMAL/FEASIBLE → FAILED：求解器证明的是"满足喂进去的约束"，
    阶段 A 的独立复验可能发现它违反了库里的真实事实（许可失效、Release 撤回）。
    此时把运行标成失败，才是诚实的结论；让一份已经证实有问题的方案继续可被选卷，
    比状态机"漂亮"重要得多。
    """
    if current == target:
        return True
    if current in {RunStatus.QUEUED, RunStatus.RUNNING}:
        return True
    return current in SOLVED_STATUSES and target == RunStatus.FAILED

# 与 0034 的列一一对应；读取时按这些列名取，避免 `SELECT *` 跟着迁移漂移。
RUN_COLUMNS = """id, blueprint_version_id, actor_id, idempotency_key,
                 candidate_manifest, candidate_manifest_hash, candidate_snapshot_id,
                 solver_name, solver_version, solver_config, status, solution_manifest,
                 objective_breakdown, diagnostics, best_bound, optimality_gap_milli,
                 job_id, started_at, completed_at, created_at"""


class AssemblyRunRow(BaseModel):
    """`assessment.assembly_run` 一行。

    `status` 用 `RunStatus` 而不是裸字符串：数据库的 CHECK 与这里的枚举必须一致，
    不然会出现"库里是 feasible、代码只认 solved"这种对不上的终态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    blueprint_version_id: UUID
    actor_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    candidate_manifest: dict[str, Any]
    candidate_manifest_hash: str = Field(min_length=64, max_length=64)
    candidate_snapshot_id: UUID | None = None
    solver_name: str = "cp-sat"
    solver_version: str = ""
    solver_config: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus
    solution_manifest: tuple[dict[str, Any], ...] = ()
    objective_breakdown: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    best_bound: int | None = None
    optimality_gap_milli: int | None = None
    job_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    @property
    def solved(self) -> bool:
        return self.status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE} and bool(
            self.solution_manifest)


class InMemoryAssemblyRunRepository:
    """测试与无库场景用的内存仓储（与 PostgreSQL 语义保持同一套断言）。"""
    def __init__(self, rows: list[AssemblyRunRow] | None = None) -> None:
        self.rows: dict[UUID, AssemblyRunRow] = {row.id: row for row in rows or []}

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryAssemblyRunUnitOfWork"]:
        yield InMemoryAssemblyRunUnitOfWork(self)


class InMemoryAssemblyRunUnitOfWork:
    def __init__(self, repository: InMemoryAssemblyRunRepository) -> None:
        self.repository = repository

    def get(self, run_id: UUID) -> AssemblyRunRow | None:
        return self.repository.rows.get(run_id)

    def find_idempotent(self, actor_id: UUID, key: str) -> AssemblyRunRow | None:
        for row in self.repository.rows.values():
            if row.actor_id == actor_id and row.idempotency_key == key:
                return row
        return None

    def add(self, row: AssemblyRunRow) -> AssemblyRunRow:
        existing = self.find_idempotent(row.actor_id, row.idempotency_key)
        if existing is not None:
            # 与 0034 的唯一约束同义：同一 (actor, key) 只能有一行。
            if existing.blueprint_version_id != row.blueprint_version_id:
                raise ValueError("idempotency key reused for another blueprint")
            return existing
        self.repository.rows[row.id] = row
        return row

    def update(self, row: AssemblyRunRow) -> AssemblyRunRow:
        stored = self.repository.rows.get(row.id)
        if stored is None:
            raise KeyError(f"assembly run not found: {row.id}")
        if not _may_transition(stored.status, row.status):
            raise ValueError(
                f"assembly run {row.id} 已是终态 {stored.status}，不能改成 {row.status}")
        self.repository.rows[row.id] = row
        return row

    def record_diagnostics(self, run_id: UUID, diagnostics: dict[str, Any]) -> AssemblyRunRow:
        """只写诊断（审计注记），不动状态与方案：终态之后的校验结论走这条路。"""
        stored = self.repository.rows.get(run_id)
        if stored is None:
            raise KeyError(f"assembly run not found: {run_id}")
        updated = stored.model_copy(update={"diagnostics": dict(diagnostics)})
        self.repository.rows[run_id] = updated
        return updated

    def latest_for_blueprint(self, blueprint_version_id: UUID, limit: int = 20,
                             ) -> list[AssemblyRunRow]:
        # 同一时刻创建的两个运行（测试里很常见）不能靠 uuid 排序：uuid 顺序是随机的，
        # "最新"会随机变化。内存仓储按插入顺序做次序，与 PostgreSQL 的 created_at
        # 精度语义对齐。
        indexed = [
            (index, row) for index, row in enumerate(self.repository.rows.values())
            if row.blueprint_version_id == blueprint_version_id
        ]
        indexed.sort(key=lambda item: (item[1].created_at, item[0]), reverse=True)
        return [row for _, row in indexed[:limit]]


class PostgresAssemblyRunRepository:
    """`assessment.assembly_run` 的 PostgreSQL 仓储（表启用了 FORCE RLS）。"""

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresAssemblyRunUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresAssemblyRunUnitOfWork(cur)


class _PostgresAssemblyRunUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def get(self, run_id: UUID) -> AssemblyRunRow | None:
        self.cur.execute(
            f"SELECT {RUN_COLUMNS} FROM assessment.assembly_run WHERE id = %s", (run_id,))
        record = self.cur.fetchone()
        return self._row(record) if record else None

    def find_idempotent(self, actor_id: UUID, key: str) -> AssemblyRunRow | None:
        self.cur.execute(
            f"""SELECT {RUN_COLUMNS} FROM assessment.assembly_run
                 WHERE actor_id = %s AND idempotency_key = %s""",
            (actor_id, key),
        )
        record = self.cur.fetchone()
        return self._row(record) if record else None

    def add(self, row: AssemblyRunRow) -> AssemblyRunRow:
        self.cur.execute(
            """INSERT INTO assessment.assembly_run (
                 id, blueprint_version_id, actor_id, idempotency_key,
                 candidate_manifest, candidate_manifest_hash, candidate_snapshot_id,
                 solver_name, solver_version, solver_config, status,
                 solution_manifest, objective_breakdown, diagnostics,
                 best_bound, optimality_gap_milli, job_id, started_at, completed_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s,
                       %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
               ON CONFLICT (actor_id, idempotency_key) DO NOTHING
               RETURNING id""",
            (
                row.id, row.blueprint_version_id, row.actor_id, row.idempotency_key,
                _json(row.candidate_manifest), row.candidate_manifest_hash,
                row.candidate_snapshot_id, row.solver_name, row.solver_version,
                _json(row.solver_config), str(row.status),
                _json(list(row.solution_manifest)), _json(row.objective_breakdown),
                _json(row.diagnostics), row.best_bound, row.optimality_gap_milli,
                row.job_id, row.started_at, row.completed_at,
            ),
        )
        inserted = self.cur.fetchone()
        if inserted is not None:
            return row
        # 并发插入撞上唯一键：返回已存在的那一行（与 ON CONFLICT DO NOTHING 配套）。
        existing = self.find_idempotent(row.actor_id, row.idempotency_key)
        if existing is None:  # pragma: no cover - 唯一键冲突后必然能查到
            raise RuntimeError("assembly run insert conflicted but no row found")
        return existing

    def update(self, row: AssemblyRunRow) -> AssemblyRunRow:
        self.cur.execute(
            """UPDATE assessment.assembly_run
                  SET status = %s,
                      solution_manifest = %s::jsonb,
                      objective_breakdown = %s::jsonb,
                      diagnostics = %s::jsonb,
                      best_bound = %s,
                      optimality_gap_milli = %s,
                      job_id = %s,
                      started_at = %s,
                      completed_at = %s
                WHERE id = %s
                  AND (status IN ('queued', 'running')
                       OR status = %s
                       OR (status IN ('optimal', 'feasible') AND %s = 'failed'))
              RETURNING id""",
            (
                str(row.status), _json(list(row.solution_manifest)),
                _json(row.objective_breakdown), _json(row.diagnostics),
                row.best_bound, row.optimality_gap_milli, row.job_id,
                row.started_at, row.completed_at, row.id,
                str(row.status), str(row.status),
            ),
        )
        if self.cur.fetchone() is None:
            # 只剩两种可能：行不存在，或状态机不允许这次改写。
            raise ValueError(
                f"assembly run {row.id} 不存在或不允许从当前状态改成 {row.status}")
        return row

    def record_diagnostics(self, run_id: UUID, diagnostics: dict[str, Any]) -> None:
        """只写诊断：终态之后的审计注记（例如独立复验结论）不能动状态。"""
        self.cur.execute(
            "UPDATE assessment.assembly_run SET diagnostics = %s::jsonb WHERE id = %s",
            (_json(diagnostics), run_id),
        )

    def latest_for_blueprint(self, blueprint_version_id: UUID, limit: int = 20,
                             ) -> list[AssemblyRunRow]:
        self.cur.execute(
            f"""SELECT {RUN_COLUMNS} FROM assessment.assembly_run
                 WHERE blueprint_version_id = %s
                 ORDER BY created_at DESC, id
                 LIMIT %s""",
            (blueprint_version_id, limit),
        )
        return [self._row(record) for record in self.cur.fetchall()]

    @staticmethod
    def _row(record: Any) -> AssemblyRunRow:
        return AssemblyRunRow(
            id=record[0], blueprint_version_id=record[1], actor_id=record[2],
            idempotency_key=record[3], candidate_manifest=record[4],
            candidate_manifest_hash=record[5], candidate_snapshot_id=record[6],
            solver_name=record[7], solver_version=record[8], solver_config=record[9],
            status=record[10], solution_manifest=tuple(record[11] or ()),
            objective_breakdown=record[12] or {}, diagnostics=record[13] or {},
            best_bound=record[14], optimality_gap_milli=record[15], job_id=record[16],
            started_at=record[17], completed_at=record[18], created_at=record[19],
        )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def new_run_id() -> UUID:
    return uuid4()


__all__ = [
    "AssemblyRunRow",
    "InMemoryAssemblyRunRepository",
    "InMemoryAssemblyRunUnitOfWork",
    "PostgresAssemblyRunRepository",
    "RUN_COLUMNS",
    "new_run_id",
]
