"""审核工作台 API。

设计见 docs/审核工作台设计.md。要点：
  1. 队列不是人工建的，是从数据异常自动生成的（build_queue）。
  2. 每条决定都写 audit_log；不写审计的接口不允许存在。
  3. 修订历史由数据库触发器负责，API 不需要（也不应该）自己维护。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, lean_service
from .auth import Actor, require

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


class GraphVerifyIn(BaseModel):
    """知识图谱人工审核：按章整批通过，或按节点列表通过/撤销。"""

    course_id: str
    chapter_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    action: str = Field(default="verify", pattern="^(verify|unverify)$")
    note: str | None = None


@router.get("/summary")
def summary(
    course_id: str | None = None,
    _actor: Actor = Depends(require("reviewer")),
) -> dict:
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
def build_queue(
    course_id: str | None = None,
    limit: int = 2000,
    _actor: Actor = Depends(require("reviewer")),
) -> dict:
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
          SELECT 'page-imprecise'::review_category, 'minor', '页码待核（原文标了待核）'
          WHERE e.source_page IS NULL AND e.source_raw LIKE '%%待核%%'
          UNION ALL
          SELECT 'page-imprecise'::review_category, 'minor', '无精确书页，仅章节范围或缺失'
          -- 自拟题本来就没有教材页码，不算缺陷。把它标成问题，405 条噪声会淹掉
          -- 真正需要核对的页码错误；自拟题该看的是有没有独立核对过解答。
          WHERE e.source_page IS NULL
            AND e.provenance <> 'self-authored'
            AND e.source_raw NOT LIKE '%%待核%%'
          UNION ALL
          SELECT 'content-error'::review_category, 'minor', '原文含"待核"标记，需人工确认'
          -- 与上一条互斥：待核紧挨着"页"的算页码问题，其余（答案、题号、出处本身）
          -- 才归到内容待核。否则 1096 条待核全堆在一个分类里，审核员没法分工。
          WHERE e.source_raw LIKE '%%待核%%'
            AND NOT (e.source_raw ~ '页.{{0,24}}待核|待核.{{0,24}}页')
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
    _actor: Actor = Depends(require("reviewer")),
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
def decide(task_id: int, body: DecisionIn, actor: Actor = Depends(require("reviewer"))) -> dict:
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
        # 通过 = 人工已审。这一句如果不写，"通过"就只改了工作流状态：
        # 总表的可信等级、课程内容页的徽标、可见范围"仅人工审核"的统计全都不动，
        # 老师会以为按钮没生效。发布是另一件事（决定学生能不能看到），仍留单独一步。
        if body.action == "approve":
            cur.execute(
                """
                UPDATE exercise
                   SET human_verified_at = coalesce(human_verified_at, now()),
                       human_verified_by = coalesce(human_verified_by, %s),
                       review_status = CASE WHEN review_status = 'published'
                                            THEN review_status
                                            ELSE 'teacher-reviewed'::review_status END
                 WHERE course_id = %s AND id = %s
                """,
                (x_actor, task["course_id"], task["target_id"]),
            )
            cur.execute(
                """
                INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                VALUES (%s, 'approve-exercise', 'exercise', %s,
                        jsonb_build_object('note', %s::text))
                """,
                (x_actor, f"{task['course_id']}/{task['target_id']}", body.note),
            )

        # 退回/驳回 = 撤销人工审核。否则会出现"退回了，总表还写着人工已审"。
        # 已发布的题保持 published（撤回发布有单独的接口），只清人工校验标记。
        if body.action in ("request-changes", "reject"):
            cur.execute(
                """
                UPDATE exercise
                   SET human_verified_at = NULL,
                       human_verified_by = NULL,
                       review_status = CASE WHEN review_status = 'published'
                                            THEN review_status
                                            ELSE 'draft'::review_status END
                 WHERE course_id = %s AND id = %s
                """,
                (task["course_id"], task["target_id"]),
            )
            cur.execute(
                """
                INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                VALUES (%s, %s::text, 'exercise', %s,
                        jsonb_build_object('note', %s::text))
                """,
                (x_actor, f"{body.action}-exercise",
                 f"{task['course_id']}/{task['target_id']}", body.note),
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
    verified = db.query_one(
        """SELECT e.review_status,
                  (e.human_verified_at IS NOT NULL) AS human_verified,
                  coalesce(v.tier, 'none') AS tier
             FROM exercise e
             LEFT JOIN v_exercise_verification v
                    ON v.course_id = e.course_id AND v.id = e.id
            WHERE e.course_id = %s AND e.id = %s""",
        (task["course_id"], task["target_id"]),
    )
    return {
        "task_id": task_id,
        "state": new_state,
        "exercise": {
            "course_id": task["course_id"],
            "exercise_id": task["target_id"],
            "review_status": (verified or {}).get("review_status"),
            "human_verified": bool((verified or {}).get("human_verified")),
            "tier": (verified or {}).get("tier", "none"),
            # 通过不等于学生能看到：published 才是那扇门
            "visible_to_students": (verified or {}).get("review_status") == "published",
        },
    }


@router.get("/graph")
def graph(
    course_id: str | None = None,
    _actor: Actor = Depends(require("reviewer")),
) -> dict:
    """知识图谱的审核状态：按章分组，并给出每个节点，供审核界面的章节树渲染。

    两个坑：

    1. `knowledge_node.chapter_id` 在真实导入数据里是空的（导入器只填 parent_id），
       所以不能用 `coalesce(chapter_id, id)` 分组——那样每个节点都会自成一"章"，
       数学分析 II 会出现 135 个"章"，比原来更难看。这里按 parent_id 往上走，
       取 level=2 的祖先当章。
    2. 不传 course_id 时只返回汇总，不返回章节与节点：14 门课 4146 个节点，
       没有界面上需要它们。
    """
    totals = db.query_one(
        """
        SELECT count(*)::int AS nodes,
               count(*) FILTER (WHERE human_verified_at IS NOT NULL)::int AS verified,
               count(DISTINCT course_id)::int AS courses
          FROM knowledge_node
         WHERE review_status <> 'retired'
           AND (%s::text IS NULL OR course_id = %s)
        """,
        (course_id, course_id),
    ) or {"nodes": 0, "verified": 0, "courses": 0}
    if not course_id:
        return {"chapters": [], "totals": totals, "nodes": [], "root": None}

    rows = db.query(
        """
        SELECT id, parent_id, title, kind, level, review_status,
               human_verified_at, human_verified_by
          FROM knowledge_node
         WHERE course_id = %s AND review_status <> 'retired'
         ORDER BY level, created_at, id
        """,
        (course_id,),
    )
    # level=1 是课程根节点，不是"一章"。它单列成 root：学生端的树要从根长出来，
    # 所以根也得能通过，但它不该占一张章节卡片。
    root_row = next((row for row in rows if row["level"] == 1), None)
    rows = [row for row in rows if row["level"] >= 2]
    by_id = {row["id"]: row for row in rows}

    def chapter_of(node: dict) -> str:
        # 往上走到 level=2；走不到（数据里没有章这一层）就拿自己当章，至少不丢节点。
        current, guard = node, 0
        while current is not None and guard < 32:
            if current["level"] == 2:
                return current["id"]
            parent_id = current["parent_id"]
            if not parent_id or parent_id not in by_id:
                return node["id"] if node["level"] > 2 else current["id"]
            current, guard = by_id[parent_id], guard + 1
        return node["id"]

    chapter_ids = {row["id"]: chapter_of(row) for row in rows}
    counters: dict[str, dict] = {}
    nodes = []
    for row in rows:
        chapter = chapter_ids[row["id"]]
        verified = row["human_verified_at"] is not None
        nodes.append({
            "id": row["id"],
            "chapter_id": chapter,
            "parent_id": row["parent_id"],
            "title": row["title"],
            "kind": row["kind"],
            "level": row["level"],
            "review_status": row["review_status"],
            "verified": verified,
            "verified_at": row["human_verified_at"],
            "verified_by": row["human_verified_by"],
        })
        if row["id"] == chapter:
            continue  # 章节点本身不算"章里的知识点"
        bucket = counters.setdefault(chapter, {"nodes": 0, "verified": 0})
        bucket["nodes"] += 1
        if verified:
            bucket["verified"] += 1

    chapters = []
    for row in rows:
        if chapter_ids.get(row["id"]) != row["id"]:
            continue  # 只列真正的章（level=2 或没有祖先的顶层节点）
        bucket = counters.get(row["id"], {"nodes": 0, "verified": 0})
        chapters.append({
            "course_id": course_id,
            "chapter_id": row["id"],
            "title": row["title"],
            "nodes": bucket["nodes"],
            "verified": bucket["verified"],
            "self_verified": row["human_verified_at"] is not None,
        })
    root = None if root_row is None else {
        "id": root_row["id"],
        "title": root_row["title"],
        "verified": root_row["human_verified_at"] is not None,
    }
    return {"chapters": chapters, "totals": totals, "nodes": nodes, "root": root}


@router.post("/graph/verify")
def graph_verify(
    body: GraphVerifyIn,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """把一章（或若干节点）连同其整棵子树标为人工审核通过 / 撤销。

    为什么要连子树一起标：真实数据里 `chapter_id` 是空的，按它更新会一个节点也
    改不到（changed=0），老师点了"整章通过"却什么都没发生。逐节点通过同理——
    只标记顶层容器会让学生端看到"有章没节"的断树，所以一并往下走。
    """
    if not body.chapter_id and not body.node_ids:
        raise HTTPException(400, "要么给 chapter_id（整章），要么给 node_ids（逐个）")
    verified = body.action == "verify"
    seeds = [body.chapter_id] if body.chapter_id else list(body.node_ids or [])
    changed = db.execute(
        """
        WITH RECURSIVE subtree AS (
            SELECT id
              FROM knowledge_node
             WHERE course_id = %s AND id = ANY(%s::text[])
               AND review_status <> 'retired'
            UNION ALL
            SELECT child.id
              FROM knowledge_node child
              JOIN subtree ON child.parent_id = subtree.id
             WHERE child.course_id = %s AND child.review_status <> 'retired'
        )
        UPDATE knowledge_node
           SET human_verified_at = CASE WHEN %s THEN now() ELSE NULL END,
               human_verified_by = CASE WHEN %s THEN %s ELSE NULL END
         WHERE course_id = %s
           AND id IN (SELECT id FROM subtree)
           AND (human_verified_at IS NOT NULL) <> %s
        """,
        (body.course_id, seeds, body.course_id, verified, verified, actor.id,
         body.course_id, verified),
    )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, 'knowledge_node', %s, %s::jsonb)""",
        (actor.id, "graph.verify" if verified else "graph.unverify",
         f"{body.course_id}/{body.chapter_id or 'nodes'}",
         json.dumps({"chapter_id": body.chapter_id, "node_ids": body.node_ids,
                     "changed": changed, "note": body.note}, ensure_ascii=False)),
    )
    return {"course_id": body.course_id, "action": body.action, "changed": changed}


@router.patch("/exercises/{course_id}/{exercise_id}")
def patch_exercise(
    course_id: str,
    exercise_id: str,
    body: ExercisePatch,
    actor: Actor = Depends(require("reviewer")),
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


@router.get("/ledger")
def ledger(
    course_id: str | None = None,
    tier: str | None = None,
    question: str | None = None,
    limit: int = 100,
    offset: int = 0,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """一门课全部题目的审核总表（不只待审的）。

    和队列的分工：队列回答"接下来审哪几道"，总表回答"这门课 400 道题各自什么状态"。
    没有总表，老师只能一行行翻，或者相信四个汇总数字——这也正是"哪些题还没人审"
    这种问题最容易糊过去的地方。
    """
    if tier and tier not in {"none", "lean", "human", "strongest"}:
        raise HTTPException(400, f"未知的可信等级：{tier}")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    pattern = f"%{question.strip()}%" if question and question.strip() else None

    where = """
       WHERE e.review_status <> 'retired'
         AND (%s::text IS NULL OR e.course_id = %s)
         AND (%s::text IS NULL OR coalesce(v.tier, 'none') = %s)
         AND (%s::text IS NULL OR e.id ILIKE %s OR e.prompt_md ILIKE %s)
    """
    params = (course_id, course_id, tier, tier, pattern, pattern, pattern)

    rows = db.query(
        f"""
        WITH latest_task AS (
          SELECT DISTINCT ON (course_id, target_id)
                 course_id, target_id, state, assignee, decided_at
            FROM review_task
           WHERE target = 'exercise'
           ORDER BY course_id, target_id, id DESC
        )
        SELECT e.id, e.course_id, e.review_status, e.difficulty, e.provenance,
               e.human_verified_at, e.human_verified_by, e.updated_at,
               coalesce(v.tier, 'none') AS tier,
               coalesce(v.lean_confirmed, false) AS lean_confirmed,
               t.state AS task_state,
               (e.human_verified_at IS NOT NULL
                AND e.updated_at > e.human_verified_at) AS edited_after_review,
               left(e.prompt_md, 90) AS prompt
          FROM exercise e
          LEFT JOIN v_exercise_verification v
                 ON v.course_id = e.course_id AND v.id = e.id
          LEFT JOIN latest_task t
                 ON t.course_id = e.course_id AND t.target_id = e.id
        {where}
         ORDER BY CASE coalesce(v.tier, 'none')
                    WHEN 'none' THEN 0 WHEN 'lean' THEN 1
                    WHEN 'human' THEN 2 ELSE 3 END,
                  e.id
         LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
    )

    total = db.query_one(
        f"""SELECT count(*)::int AS n
              FROM exercise e
              LEFT JOIN v_exercise_verification v
                     ON v.course_id = e.course_id AND v.id = e.id
            {where}""",
        params,
    )
    counts = db.query(
        """SELECT coalesce(v.tier, 'none') AS tier, count(*)::int AS n
             FROM exercise e
             LEFT JOIN v_exercise_verification v
                    ON v.course_id = e.course_id AND v.id = e.id
            WHERE e.review_status <> 'retired'
              AND (%s::text IS NULL OR e.course_id = %s)
            GROUP BY 1""",
        (course_id, course_id),
    )
    order = {"none": 0, "lean": 1, "human": 2, "strongest": 3}
    return {
        "course_id": course_id,
        "items": rows,
        "total": int(total["n"]) if total else 0,
        "counts": sorted(counts, key=lambda row: order.get(row["tier"], 9)),
        "limit": limit,
        "offset": offset,
        "filter": {"tier": tier, "question": question},
    }


@router.get("/exercises/{course_id}/{exercise_id}/verification")
def exercise_verification(
    course_id: str,
    exercise_id: str,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """"这道题标注过什么"：可信等级、人工审核时间与人、Lean 证明状态、审核后是否被改过。

    为什么单独一个接口：发布之后题目从审核队列里消失（task.state = published），
    按队列查就查不到了；但老师最常问的恰恰是"这道已发布的题，到底谁审过、审的哪一版"。
    """
    row = db.query_one(
        """
        SELECT e.course_id, e.id AS exercise_id, e.review_status, e.provenance,
               e.human_verified_at, e.human_verified_by, e.lean_verified_at,
               e.content_version, e.updated_at,
               coalesce(v.tier, 'none') AS tier,
               coalesce(v.lean_confirmed, false) AS lean_confirmed
          FROM exercise e
          LEFT JOIN v_exercise_verification v
                 ON v.course_id = e.course_id AND v.id = e.id
         WHERE e.course_id = %s AND e.id = %s
        """,
        (course_id, exercise_id),
    )
    if not row:
        raise HTTPException(404, f"题目不存在：{course_id}/{exercise_id}")

    proofs = db.query(
        """
        SELECT ord, compiled, compiled_at, faithful_status, faithful_by, toolchain,
               (statement_lean IS NOT NULL AND statement_lean <> '') AS has_statement,
               proof_sha256
          FROM lean_proof
         WHERE course_id = %s AND exercise_id = %s
         ORDER BY ord
        """,
        (course_id, exercise_id),
    )
    task = db.query_one(
        """SELECT id AS task_id, state, priority, assignee, created_at, decided_at
             FROM review_task
            WHERE course_id = %s AND target_id = %s
            ORDER BY id DESC LIMIT 1""",
        (course_id, exercise_id),
    )
    verified_at = row.get("human_verified_at")
    updated_at = row.get("updated_at")
    row["edited_after_review"] = bool(verified_at and updated_at and updated_at > verified_at)
    row["proofs"] = proofs
    row["task"] = task
    row["lean_available"] = lean_service.available()
    return row


@router.get("/exercises/{course_id}/{exercise_id}/revisions")
def revisions(
    course_id: str,
    exercise_id: str,
    _actor: Actor = Depends(require("reviewer")),
) -> dict:
    """修订历史 + 当前版本，供审核界面做差异对照。

    触发器在每次 UPDATE **之前**快照旧值，所以 exercise_revision 里存的是"改之前长什么样"，
    当前内容在 exercise 表里。审核员看到的差异因此是"最近一次修订前 → 现在"。
    """
    current = db.query_one(
        """SELECT prompt_md, answer_md, difficulty, source_page, content_version,
                  human_verified_at, provenance
           FROM exercise WHERE course_id = %s AND id = %s""",
        (course_id, exercise_id),
    )
    if not current:
        raise HTTPException(404, f"题目不存在：{course_id}/{exercise_id}")

    nodes = db.query(
        "SELECT node_id FROM exercise_node WHERE course_id = %s AND exercise_id = %s ORDER BY node_id",
        (course_id, exercise_id),
    )
    current["node_ids"] = [row["node_id"] for row in nodes]

    rows = db.query(
        """SELECT version, prompt_md, answer_md, difficulty, source_page, node_ids,
                  provenance, changed_by, changed_at, change_note
           FROM exercise_revision
           WHERE course_id = %s AND exercise_id = %s
           ORDER BY version DESC
           LIMIT 20""",
        (course_id, exercise_id),
    )
    return {"current": current, "revisions": rows, "count": len(rows)}
