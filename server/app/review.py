"""审核工作台 API。

设计见 docs/审核工作台设计.md。要点：
  1. 队列不是人工建的，是从数据异常自动生成的（build_queue）。
  2. 每条决定都写 audit_log；不写审计的接口不允许存在。
  3. 修订历史由数据库触发器负责，API 不需要（也不应该）自己维护。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, current_actor

router = APIRouter(prefix="/api/v1/review", tags=["review"])


class IssueIn(BaseModel):
    category: str
    severity: str = "major"
    detail: str | None = None


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(claim|approve|reject|publish|retire|request-changes)$")
    note: str | None = None
    issues: list[IssueIn] = Field(default_factory=list)


class ExercisePatch(BaseModel):
    prompt_md: str | None = None
    answer_md: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    source_page: int | None = None
    node_ids: list[str] | None = None
    change_note: str | None = None
    human_verified: bool = False


@router.get("/summary")
def summary(course_id: str | None = None) -> dict:
    """审核大盘：按可信等级、审核状态、错误分类三个维度统计。"""
    tier_sql = """
        SELECT course_id, tier, count(*)::int AS n
        FROM v_exercise_verification
        {where}
        GROUP BY course_id, tier
        ORDER BY course_id, tier
    """
    state_sql = """
        SELECT state, count(*)::int AS n FROM review_task {where} GROUP BY state
    """
    if course_id:
        tiers = db.query(tier_sql.format(where="WHERE course_id = %s"), (course_id,))
        states = db.query(state_sql.format(where="WHERE course_id = %s"), (course_id,))
    else:
        tiers = db.query(tier_sql.format(where=""))
        states = db.query(state_sql.format(where=""))
    cats = db.query(
        """
        SELECT i.category, count(*)::int AS n
        FROM review_issue i
        JOIN review_task t ON t.id = i.task_id
        WHERE i.resolved_at IS NULL
        GROUP BY i.category ORDER BY n DESC
        """
    )
    return {"tiers": tiers, "task_states": states, "open_issues": cats}


@router.post("/queue/build")
def build_queue(course_id: str | None = None, limit: int = 2000) -> dict:
    """从数据异常自动生成待审任务与错误清单。

    只处理"机器能判定"的异常：缺挂载、溯源缺失、页码不精确、带待核标记。
    内容与答案的正确性永远不自动判定——那是教师的事。
    """
    params: tuple = (course_id,) if course_id else ()
    course_filter = "AND e.course_id = %s" if course_id else ""

    created = db.query(
        f"""
        INSERT INTO review_task (course_id, target, target_id, priority)
        SELECT e.course_id, 'exercise', e.id,
               CASE
                 WHEN e.source_raw LIKE '%%待核%%' THEN 1
                 WHEN NOT EXISTS (SELECT 1 FROM exercise_node n
                                   WHERE n.course_id = e.course_id AND n.exercise_id = e.id) THEN 2
                 WHEN e.provenance = 'unknown' THEN 2
                 WHEN e.source_page IS NULL THEN 3
                 ELSE 4
               END
        FROM exercise e
        WHERE e.review_status IN ('draft', 'machine-checked') {course_filter}
        ORDER BY e.course_id, e.id
        LIMIT %s
        ON CONFLICT (course_id, target, target_id) DO NOTHING
        RETURNING id
        """,
        params + (limit,),
    )

    # 错误清单：只在任务还没有任何未解决 issue 时生成，避免重复跑时堆积
    issues = db.query(
        f"""
        WITH fresh AS (
          SELECT t.id, t.course_id, t.target_id
          FROM review_task t
          WHERE t.state = 'pending' {course_filter.replace('e.course_id', 't.course_id')}
            AND NOT EXISTS (SELECT 1 FROM review_issue i WHERE i.task_id = t.id)
        )
        INSERT INTO review_issue (task_id, category, severity, detail, raised_by)
        SELECT f.id, cat.category, cat.severity, cat.detail, 'import-audit'
        FROM fresh f
        JOIN exercise e ON e.course_id = f.course_id AND e.id = f.target_id
        CROSS JOIN LATERAL (
          SELECT 'missing-node'::review_category AS category, 'major' AS severity, '题目未挂任何知识点' AS detail
          WHERE NOT EXISTS (SELECT 1 FROM exercise_node n
                             WHERE n.course_id = e.course_id AND n.exercise_id = e.id)
          UNION ALL
          SELECT 'source-missing'::review_category, 'major', '溯源无法判定'
          WHERE e.provenance = 'unknown'
          UNION ALL
          SELECT 'page-imprecise'::review_category, 'minor', '无精确书页，仅章节范围或缺失'
          WHERE e.source_page IS NULL
          UNION ALL
          SELECT 'content-error'::review_category, 'minor', '原文含"待核"标记，需人工确认'
          WHERE e.source_raw LIKE '%%待核%%'
        ) AS cat
        RETURNING id
        """,
        params,
    )
    return {"tasks_created": len(created), "issues_created": len(issues)}


@router.get("/queue")
def queue(
    course_id: str | None = None,
    state: str = "pending",
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    rows = db.query(
        """
        SELECT t.id AS task_id, t.course_id, t.target_id AS exercise_id,
               t.state, t.priority, t.created_at,
               e.prompt_md, e.difficulty, e.provenance, e.source_page,
               v.tier,
               coalesce(array_agg(DISTINCT n.node_id) FILTER (WHERE n.node_id IS NOT NULL), '{}') AS node_ids,
               coalesce(json_agg(DISTINCT jsonb_build_object(
                 'category', i.category, 'severity', i.severity, 'detail', i.detail))
                 FILTER (WHERE i.id IS NOT NULL), '[]') AS issues
        FROM review_task t
        JOIN exercise e ON e.course_id = t.course_id AND e.id = t.target_id
        LEFT JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
        LEFT JOIN exercise_node n ON n.course_id = e.course_id AND n.exercise_id = e.id
        LEFT JOIN review_issue i ON i.task_id = t.id AND i.resolved_at IS NULL
        WHERE t.state = %s
          AND (%s::text IS NULL OR t.course_id = %s)
          AND (%s::text IS NULL OR EXISTS (
                SELECT 1 FROM review_issue x WHERE x.task_id = t.id AND x.category = %s::review_category))
        GROUP BY t.id, e.course_id, e.id, v.tier
        ORDER BY t.priority, t.created_at
        LIMIT %s OFFSET %s
        """,
        (state, course_id, course_id, category, category, limit, offset),
    )
    return {"items": rows, "count": len(rows)}


@router.post("/tasks/{task_id}/decision")
def decide(task_id: int, body: DecisionIn, actor: Actor = Depends(current_actor)) -> dict:
    x_actor = actor.id
    task = db.query_one("SELECT * FROM review_task WHERE id = %s", (task_id,))
    if not task:
        raise HTTPException(404, f"任务不存在：{task_id}")

    mapping = {
        "claim": "in-review",
        "approve": "approved",
        "reject": "rejected",
        "publish": "published",
        "retire": "rejected",
        "request-changes": "changes-requested",
    }
    new_state = mapping[body.action]

    with db.cursor() as cur:
        cur.execute("SELECT set_config('app.actor', %s, true)", (x_actor,))
        cur.execute(
            """
            UPDATE review_task
               SET state = %s::review_state,
                   claimed_at = CASE WHEN %s = 'in-review' THEN now() ELSE claimed_at END,
                   decided_at = CASE WHEN %s IN ('approved','rejected','published') THEN now() ELSE decided_at END,
                   assignee = coalesce(assignee, %s)
             WHERE id = %s
            """,
            (new_state, new_state, new_state, x_actor, task_id),
        )
        for issue in body.issues:
            cur.execute(
                """
                INSERT INTO review_issue (task_id, category, severity, detail, raised_by)
                VALUES (%s, %s::review_category, %s, %s, %s)
                """,
                (task_id, issue.category, issue.severity, issue.detail, x_actor),
            )
        if body.action == "publish":
            cur.execute(
                """
                UPDATE exercise
                   SET review_status = 'published',
                       human_verified_at = coalesce(human_verified_at, now()),
                       human_verified_by = coalesce(human_verified_by, %s)
                 WHERE course_id = %s AND id = %s
                """,
                (x_actor, task["course_id"], task["target_id"]),
            )
            cur.execute(
                """
                INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                VALUES (%s, 'publish-exercise', 'exercise', %s,
                        jsonb_build_object('note', %s::text))
                """,
                (x_actor, f"{task['course_id']}/{task['target_id']}", body.note),
            )
    return {"task_id": task_id, "state": new_state}


@router.patch("/exercises/{course_id}/{exercise_id}")
def patch_exercise(
    course_id: str,
    exercise_id: str,
    body: ExercisePatch,
    actor: Actor = Depends(current_actor),
) -> dict:
    """改题干/答案/难度/页码/挂载。修订历史由触发器自动落盘。"""
    x_actor = actor.id
    exists = db.query_one(
        "SELECT 1 AS ok FROM exercise WHERE course_id = %s AND id = %s", (course_id, exercise_id)
    )
    if not exists:
        raise HTTPException(404, f"题目不存在：{course_id}/{exercise_id}")

    sets = ["content_version = content_version + 1"]
    params: list = []
    for col in ("prompt_md", "answer_md", "difficulty", "source_page"):
        val = getattr(body, col)
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if body.human_verified:
        sets.append("human_verified_at = now()")
        sets.append("human_verified_by = %s")
        params.append(x_actor)

    with db.cursor() as cur:
        cur.execute("SELECT set_config('app.actor', %s, true)", (x_actor,))
        cur.execute(
            "SELECT set_config('app.change_note', %s, true)", (body.change_note or "api patch",)
        )
        cur.execute(
            f"UPDATE exercise SET {', '.join(sets)} WHERE course_id = %s AND id = %s",
            (*params, course_id, exercise_id),
        )
        if body.node_ids is not None:
            cur.execute(
                "DELETE FROM exercise_node WHERE course_id = %s AND exercise_id = %s",
                (course_id, exercise_id),
            )
            for node_id in body.node_ids:
                cur.execute(
                    "INSERT INTO exercise_node (course_id, exercise_id, node_id) VALUES (%s, %s, %s)",
                    (course_id, exercise_id, node_id),
                )
    return {"course_id": course_id, "exercise_id": exercise_id, "updated": True}
