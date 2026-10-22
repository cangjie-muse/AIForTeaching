"""知识图谱审核的节点管理：看节点、改标题、挂题、解绑、删节点。

拆成独立模块而不是塞进 review.py：那边是"队列 + 决定"的工作流，这里是"树本身"
的增删改。两边都要写审计与重置，但只有这边会动 knowledge_node 的结构。

三条口径（都写进了 docs/审核分层与可见范围.md）：

1. **挂载变了，人工审核就不算数。** 挂题、解绑、改标题、删节点都会把受影响的题
   退回未核（`mounting.reset_exercises`）；已发布的题保持发布，学生不断供。
2. **删节点是物理删除，但必须显式承认连带。** 有子节点或挂着题时接口先 409 报数，
   前端拿这个数字让人确认，再带 `cascade=true` 重发。没有确认就删掉一棵子树，
   是这一页最容易出的事故。
3. **课程根节点不能删。** 它不是一个知识点，删了整棵树就没有入口。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, mounting
from .auth import Actor, require

router = APIRouter(prefix="/api/v1/review", tags=["review-nodes"])


class MountIn(BaseModel):
    exercise_ids: list[str] = Field(min_length=1)


def _node_or_404(course_id: str, node_id: str) -> dict:
    row = db.query_one(
        """
        SELECT id, parent_id, title, kind, level, path, content_md, review_status,
               content_version, human_verified_at, human_verified_by
          FROM knowledge_node
         WHERE course_id = %s AND id = %s
        """,
        (course_id, node_id),
    )
    if not row:
        raise HTTPException(404, f"知识点不存在：{course_id}/{node_id}")
    return row


def _ancestors(course_id: str, node_id: str) -> list[dict]:
    return db.query(
        """
        WITH RECURSIVE up AS (
            SELECT id, parent_id, title, level
              FROM knowledge_node
             WHERE course_id = %s AND id = %s
            UNION ALL
            SELECT parent.id, parent.parent_id, parent.title, parent.level
              FROM knowledge_node parent
              JOIN up ON up.parent_id = parent.id
             WHERE parent.course_id = %s
        )
        SELECT id, parent_id, title, level FROM up ORDER BY level
        """,
        (course_id, node_id, course_id),
    )


def _node_exercises(course_id: str, node_ids: list[str]) -> list[dict]:
    if not node_ids:
        return []
    return db.query(
        """
        SELECT e.id, e.prompt_md, e.difficulty, e.review_status, e.human_verified_at,
               coalesce(v.tier, 'none') AS tier,
               array_agg(m.node_id ORDER BY m.node_id) AS node_ids
          FROM exercise_node m
          JOIN exercise e ON e.course_id = m.course_id AND e.id = m.exercise_id
          LEFT JOIN v_exercise_verification v
                 ON v.course_id = e.course_id AND v.id = e.id
         WHERE m.course_id = %s AND m.node_id = ANY(%s::text[])
         GROUP BY e.id, e.prompt_md, e.difficulty, e.review_status, e.human_verified_at, v.tier
         ORDER BY e.id
        """,
        (course_id, list(node_ids)),
    )


@router.get("/nodes/{course_id}/{node_id}")
def node_detail(course_id: str, node_id: str, actor: Actor = Depends(require("reviewer"))) -> dict:
    """一个知识点：自己、祖先链、直接子节点、挂着的题，以及能挪到哪儿。

    知识图谱页点「编辑这个节点」时打开的就是它（审核页只给深链，不在那儿渲染面板）。
    """
    node = _node_or_404(course_id, node_id)
    ancestors = _ancestors(course_id, node_id)
    chapter = next((row for row in ancestors if row["level"] == 2), None)
    parent = next((row for row in ancestors if row["level"] == node["level"] - 1), None)
    children = db.query(
        """
        SELECT id, title, kind, level, human_verified_at IS NOT NULL AS verified
          FROM knowledge_node
         WHERE course_id = %s AND parent_id = %s AND review_status <> 'retired'
         ORDER BY level, created_at, id
        """,
        (course_id, node_id),
    )
    subtree = mounting.subtree_ids(course_id, [node_id])
    # 能挪到哪儿：这门课的容器节点，去掉自己这一整棵子树（挪进去会成环）。
    # 由后端算而不是前端筛，是为了"能选的都合法"——前端筛错了就是一次 400。
    move_targets = db.query(
        """
        SELECT id, title, level, path FROM knowledge_node
         WHERE course_id = %s AND kind = 'container'
           AND review_status <> 'retired'
           AND id <> ALL(%s::text[])
           AND level < 6
         ORDER BY level, path
        """,
        (course_id, subtree),
    )
    exercises = _node_exercises(course_id, [node_id])
    # 改标题/删除影响的是整棵子树，不是只有这一层：文案里的数字要用这个。
    subtree_exercises = len(mounting.exercises_of_nodes(course_id, subtree))
    return {
        "node": {
            "id": node["id"],
            "title": node["title"],
            "kind": node["kind"],
            "level": node["level"],
            "review_status": node["review_status"],
            "content_version": node["content_version"],
            "verified": node["human_verified_at"] is not None,
            "verified_at": node["human_verified_at"],
            "verified_by": node["human_verified_by"],
            "content_md": node["content_md"],
        },
        "parent": {"id": parent["id"], "title": parent["title"]} if parent else None,
        "chapter": {"id": chapter["id"], "title": chapter["title"]} if chapter else None,
        "children": children,
        "exercises": exercises,
        "move_targets": move_targets,
        "counts": {
            "children": len(children),
            "descendants": len(subtree) - 1,
            "exercises": len(exercises),
            "subtree_exercises": subtree_exercises,
        },
    }


@router.post("/nodes/{course_id}/{node_id}/exercises")
def mount_exercises(
    course_id: str, node_id: str, body: MountIn, actor: Actor = Depends(require("reviewer"))
) -> dict:
    """把几道题挂到这个知识点上。重复挂不算错（幂等），但会记账。"""
    _node_or_404(course_id, node_id)
    rows = db.query(
        """
        SELECT id FROM exercise
         WHERE course_id = %s AND id = ANY(%s::text[]) AND review_status <> 'retired'
        """,
        (course_id, body.exercise_ids),
    )
    known = {row["id"] for row in rows}
    missing = [xid for xid in body.exercise_ids if xid not in known]
    if missing:
        raise HTTPException(404, f"这些题不存在或已停用：{'、'.join(missing[:5])}")
    attached = db.query(
        """
        INSERT INTO exercise_node (course_id, exercise_id, node_id)
        SELECT %s, x.id, %s FROM unnest(%s::text[]) AS x(id)
        ON CONFLICT (course_id, exercise_id, node_id) DO NOTHING
        RETURNING exercise_id
        """,
        (course_id, node_id, list(body.exercise_ids)),
    )
    reset = mounting.reset_exercises(
        course_id, list(body.exercise_ids), actor=actor.id, reason="新挂到知识点，需重新确认"
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (actor, action, entity, entity_key, detail)
            VALUES (%s, 'node.mount-exercises', 'knowledge_node', %s, %s::jsonb)
            """,
            (actor.id, f"{course_id}/{node_id}", json.dumps({
                "attached": [row["exercise_id"] for row in attached],
                "count": len(attached),
                "reset": reset,
            }, ensure_ascii=False)),
        )
    return {
        "course_id": course_id,
        "node_id": node_id,
        "attached": [row["exercise_id"] for row in attached],
        "skipped": len(body.exercise_ids) - len(attached),
        "reset": reset,
    }


@router.delete("/nodes/{course_id}/{node_id}/exercises/{exercise_id}")
def unmount_exercise(
    course_id: str, node_id: str, exercise_id: str, actor: Actor = Depends(require("reviewer"))
) -> dict:
    """把一个知识点与一道题的挂载解开。题本身不动（不是删题）。"""
    _node_or_404(course_id, node_id)
    removed = db.query(
        """
        DELETE FROM exercise_node
         WHERE course_id = %s AND node_id = %s AND exercise_id = %s
        RETURNING exercise_id
        """,
        (course_id, node_id, exercise_id),
    )
    if not removed:
        raise HTTPException(404, "这道题没有挂在这个知识点上")
    reset = mounting.reset_exercises(
        course_id, [exercise_id], actor=actor.id, reason="解除挂载，需重新确认"
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (actor, action, entity, entity_key, detail)
            VALUES (%s, 'node.unmount-exercise', 'knowledge_node', %s, %s::jsonb)
            """,
            (actor.id, f"{course_id}/{node_id}", json.dumps(
                {"exercise_id": exercise_id, "reset": reset}, ensure_ascii=False)),
        )
    return {"course_id": course_id, "node_id": node_id, "exercise_id": exercise_id,
            "detached": True, "reset": reset}


@router.delete("/nodes/{course_id}/{node_id}")
def delete_node(
    course_id: str,
    node_id: str,
    cascade: bool = False,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """删节点。有子节点或挂着题时，先回 409 让人看清连带范围。"""
    node = _node_or_404(course_id, node_id)
    if node["level"] <= 1:
        raise HTTPException(400, "课程根节点不能删：删了整棵树就没有入口了")
    subtree = mounting.subtree_ids(course_id, [node_id])
    exercises = mounting.exercises_of_nodes(course_id, subtree)
    if not cascade and (len(subtree) > 1 or exercises):
        raise HTTPException(409, {
            "detail": "这个节点还有子节点或挂载的题，删了会一并处理",
            "node_id": node_id,
            "title": node["title"],
            "nodes": len(subtree),
            "exercises": len(exercises),
            "subtree": subtree[:20],
            "exercise_ids": exercises[:20],
        })
    # 先重置：删完 exercise_node 行就没了，再想找"哪些题受了影响"要多写一条查询。
    reset = mounting.reset_exercises(
        course_id, exercises, actor=actor.id, reason=f"知识点被删除：{node['title']}"
    )
    db.execute(
        "DELETE FROM knowledge_node WHERE course_id = %s AND id = ANY(%s::text[])",
        (course_id, subtree),
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (actor, action, entity, entity_key, detail)
            VALUES (%s, 'node.delete', 'knowledge_node', %s, %s::jsonb)
            """,
            (actor.id, f"{course_id}/{node_id}", json.dumps({
                "title": node["title"],
                "nodes": len(subtree),
                "node_ids": subtree[:50],
                "exercises": len(exercises),
                "exercise_ids": exercises[:50],
                "detached": len(exercises),
                "reset": reset,
            }, ensure_ascii=False)),
        )
    return {
        "course_id": course_id,
        "node_id": node_id,
        "deleted": len(subtree),
        "detached_exercises": len(exercises),
        "reset": reset,
    }


@router.get("/node-options")
def node_options(
    course_id: str,
    q: str | None = None,
    limit: int = 300,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """习题侧挂载用的候选知识点（扁平，带章名，供 datalist 联想）。"""
    limit = max(1, min(limit, 500))
    pattern = f"%{(q or '').strip()}%" if (q or '').strip() else None
    rows = db.query(
        """
        SELECT n.id, n.title, n.kind, n.level,
               coalesce(ch.title, '') AS chapter_title
          FROM knowledge_node n
          LEFT JOIN knowledge_node ch
                 ON ch.course_id = n.course_id AND ch.id = n.chapter_id
         WHERE n.course_id = %s AND n.review_status <> 'retired'
           AND (%s::text IS NULL OR n.title ILIKE %s OR n.id ILIKE %s)
         ORDER BY n.level, n.created_at, n.id
         LIMIT %s
        """,
        (course_id, pattern, pattern, pattern, limit),
    )
    return {"items": rows, "count": len(rows)}


@router.get("/exercise-options")
def exercise_options(
    course_id: str,
    q: str | None = None,
    limit: int = 20,
    actor: Actor = Depends(require("reviewer")),
) -> dict:
    """节点侧挂题用的候选题目：按题号/题干搜本课的题。"""
    limit = max(1, min(limit, 50))
    pattern = f"%{(q or '').strip()}%" if (q or '').strip() else None
    rows = db.query(
        """
        SELECT e.id, e.prompt_md, e.difficulty, e.review_status,
               coalesce(v.tier, 'none') AS tier,
               coalesce(array_agg(DISTINCT m.node_id) FILTER (WHERE m.node_id IS NOT NULL), '{}')
                 AS node_ids
          FROM exercise e
          LEFT JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
          LEFT JOIN exercise_node m ON m.course_id = e.course_id AND m.exercise_id = e.id
         WHERE e.course_id = %s AND e.review_status <> 'retired'
           AND (%s::text IS NULL OR e.id ILIKE %s OR e.prompt_md ILIKE %s)
         GROUP BY e.id, e.prompt_md, e.difficulty, e.review_status, v.tier
         ORDER BY e.id
         LIMIT %s
        """,
        (course_id, pattern, pattern, pattern, limit),
    )
    return {"items": rows, "count": len(rows)}
