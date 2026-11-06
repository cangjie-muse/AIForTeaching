"""组卷工作台的聚合读接口（阶段 A 任务 9/10 的前端数据源）。

前端要在一个页面里看到「这门课有哪些卷、各自到哪一步、渲染齐没齐、发布成作业了
没有」。逐个调用现有接口可以拼出来，但每张卡都要先查卷、再查审核、再查渲染、再
查作业——一门课十份卷就是四十次往返，而且中间状态会互相错位（查审核时卷刚被撤回）。

这里只做**一次只读聚合**，不新增任何写能力：

    GET /api/v1/paper-workbench?course_ref=analysis

刻意返回三类计数而不是把整卷内容塞进来：工作台要的是"哪一步没做"，不是题干。
学生投影、答案、量规都不出现在这个响应里——它没有理由携带答案侧数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from .contracts import ContractModel


class WorkbenchPaper(ContractModel):
    """工作台上的一张卷子卡片。"""

    paper_version_id: UUID
    paper_id: UUID
    version_no: int = Field(ge=1)
    paper_kind: str
    status: Literal["draft", "in_review", "approved", "withdrawn"]
    total_score: str
    edit_seq: int = Field(ge=1)
    manifest_hash: str
    review_manifest_hash: str | None = None
    submitted_manifest_hash: str | None = None
    updated_at: datetime
    # 三类渲染是否齐（student / answer / rubric 各自的产物是否存在）
    renders_ready: int = Field(ge=0, le=3)
    # 发布成作业的份数（同一卷可以发给多个班）
    assignments_total: int = Field(ge=0)
    assignments_published: int = Field(ge=0)
    next_action: Literal[
        "fill_slots", "validate", "submit_review", "await_review",
        "render", "publish_assignment", "none", "withdrawn",
    ] = "none"


class WorkbenchView(ContractModel):
    course_ref: str
    papers: tuple[WorkbenchPaper, ...]
    counts: dict[str, int]


NEXT_ACTIONS = {
    "draft": "submit_review",
    "in_review": "await_review",
    "approved": "publish_assignment",
    "withdrawn": "withdrawn",
}


def compute_next_action(paper: WorkbenchPaper) -> str:
    """把状态 + 渲染 + 已发布情况压成一个"下一步该干什么"。

    渲染排在发布之前是硬依赖：`submit-review` 要求三类卷都 ready，工作台若不把
    它显式排前面，教师会先撞 409 `renders_not_ready` 再去猜原因。
    """
    if paper.status == "withdrawn":
        return "withdrawn"
    if paper.status == "draft":
        return "validate" if paper.total_score else "fill_slots"
    if paper.status == "in_review":
        return "await_review"
    if paper.status == "approved":
        if paper.renders_ready < 3:
            return "render"
        if paper.assignments_published == 0:
            return "publish_assignment"
        return "none"
    return "none"


class WorkbenchRepository:
    """只读聚合：一次事务里把卷、渲染数、作业数取齐。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        # 内存实现仅供测试与演示；生产实现见 PostgresWorkbenchRepository。
        self._rows = rows or []

    def papers(self, course_ref: str) -> list[WorkbenchPaper]:
        return [WorkbenchPaper.model_validate(row) for row in self._rows
                if row.get("course_ref") == course_ref]


def build_view(course_ref: str, papers: list[WorkbenchPaper]) -> WorkbenchView:
    counted: dict[str, int] = {}
    for paper in papers:
        counted[paper.status] = counted.get(paper.status, 0) + 1
    enriched = tuple(
        paper.model_copy(update={"next_action": compute_next_action(paper)})
        for paper in papers
    )
    return WorkbenchView(course_ref=course_ref, papers=enriched, counts=counted)


class PostgresWorkbenchRepository:
    """一条 SQL 把卷、渲染数、作业数取齐（同一事务，避免三张卡互相错位）。

    `renders_ready` 只数 paper_render 里存在的 (paper_version_id, render_kind)
    去重行：同一类卷重试多次也只算 1 类，否则重试会把"三类齐了"算成"未齐"。
    """

    def papers(self, course_ref: str) -> list[WorkbenchPaper]:
        from . import db

        rows = db.query(
            """
            WITH course AS (
              SELECT subject AS course_id
                FROM assessment.id_ref
               WHERE kind = 'course' AND ref = %s
            )
            SELECT p.id                AS paper_version_id,
                   p.paper_id          AS paper_id,
                   p.version_no        AS version_no,
                   p.paper_kind        AS paper_kind,
                   p.status            AS status,
                   p.total_score::text AS total_score,
                   p.edit_seq          AS edit_seq,
                   p.manifest_hash     AS manifest_hash,
                   p.review_manifest_hash    AS review_manifest_hash,
                   p.submitted_manifest_hash AS submitted_manifest_hash,
                   p.created_at        AS updated_at,
                   COALESCE(r.kinds, 0)::int AS renders_ready,
                   COALESCE(a.total, 0)::int AS assignments_total,
                   COALESCE(a.published, 0)::int AS assignments_published
              FROM assessment.paper_version p
              JOIN course ON course.course_id = p.course_id
              LEFT JOIN (
                SELECT paper_version_id, count(DISTINCT render_kind) AS kinds
                  FROM assessment.paper_render
                 GROUP BY paper_version_id
              ) r ON r.paper_version_id = p.id
              LEFT JOIN (
                SELECT paper_version_id,
                       count(*) AS total,
                       count(*) FILTER (WHERE status IN ('published','closed')) AS published
                  FROM assessment.assignment
                 GROUP BY paper_version_id
              ) a ON a.paper_version_id = p.id
             ORDER BY p.created_at DESC
            """,
            (course_ref,),
        )
        return [WorkbenchPaper(
            paper_version_id=row["paper_version_id"],
            paper_id=row["paper_id"],
            version_no=row["version_no"],
            paper_kind=row["paper_kind"],
            status=row["status"],
            total_score=row["total_score"],
            edit_seq=row["edit_seq"],
            manifest_hash=row["manifest_hash"],
            review_manifest_hash=row.get("review_manifest_hash"),
            submitted_manifest_hash=row.get("submitted_manifest_hash"),
            updated_at=row["updated_at"],
            renders_ready=row["renders_ready"],
            assignments_total=row["assignments_total"],
            assignments_published=row["assignments_published"],
        ) for row in rows]
