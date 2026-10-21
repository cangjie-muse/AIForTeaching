"""知识点 ↔ 习题挂载的公共动作。

为什么单独一个模块：挂载一变（换节点、解绑、改知识点标题、删节点），这道题的
「人工已审」就不再成立——审的是旧挂载下的那道题。这条规则要出现在四个接口里，
各写一遍迟早会漏一个，学生端就会出现"知识点已经删了，题还标着人工已审"。

重置的口径和「退回修改」一致：清 human_verified_at / human_verified_by，
**已发布的题保持 published**（学生不断供），其余回到 draft。要不要下架是
可见范围那一档的事，不该由"改了个标题"顺带决定。
"""

from __future__ import annotations

import json

from . import db

MAX_AUDIT_IDS = 50


def subtree_ids(course_id: str, node_ids: list[str]) -> list[str]:
    """给定节点 + 其全部后代（含 retired 之外的所有层级）。"""
    if not node_ids:
        return []
    rows = db.query(
        """
        WITH RECURSIVE subtree AS (
            SELECT id
              FROM knowledge_node
             WHERE course_id = %s AND id = ANY(%s::text[])
            UNION ALL
            SELECT child.id
              FROM knowledge_node child
              JOIN subtree ON child.parent_id = subtree.id
             WHERE child.course_id = %s
        )
        SELECT DISTINCT id FROM subtree
        """,
        (course_id, list(node_ids), course_id),
    )
    return [row["id"] for row in rows]


def exercises_of_nodes(course_id: str, node_ids: list[str]) -> list[str]:
    if not node_ids:
        return []
    rows = db.query(
        """
        SELECT DISTINCT exercise_id
          FROM exercise_node
         WHERE course_id = %s AND node_id = ANY(%s::text[])
         ORDER BY exercise_id
        """,
        (course_id, list(node_ids)),
    )
    return [row["exercise_id"] for row in rows]


def reset_exercises(
    course_id: str,
    exercise_ids: list[str],
    actor: str | None = None,
    reason: str = "挂载变更",
) -> dict:
    """撤销这些题的人工审核。返回 {exercises, cleared}。"""
    if not exercise_ids:
        return {"exercises": 0, "cleared": 0}
    ids = list(dict.fromkeys(exercise_ids))
    cleared_row = db.query_one(
        """
        SELECT count(*)::int AS n
          FROM exercise
         WHERE course_id = %s AND id = ANY(%s::text[])
           AND human_verified_at IS NOT NULL
        """,
        (course_id, ids),
    )
    with db.cursor() as cur:
        cur.execute("SELECT set_config('app.actor', %s, true)", (actor or "system",))
        cur.execute("SELECT set_config('app.change_note', %s, true)", (reason,))
        cur.execute(
            """
            UPDATE exercise
               SET human_verified_at = NULL,
                   human_verified_by = NULL,
                   review_status = CASE WHEN review_status = 'published'
                                        THEN review_status
                                        ELSE 'draft'::review_status END
             WHERE course_id = %s AND id = ANY(%s::text[])
            """,
            (course_id, ids),
        )
        cur.execute(
            """
            INSERT INTO audit_log (actor, action, entity, entity_key, detail)
            VALUES (%s, 'exercise.mount-reset', 'exercise', %s, %s::jsonb)
            """,
            (actor or "system", course_id, json.dumps({
                "reason": reason,
                "count": len(ids),
                "exercise_ids": ids[:MAX_AUDIT_IDS],
            }, ensure_ascii=False)),
        )
    return {"exercises": len(ids), "cleared": int((cleared_row or {}).get("n") or 0)}


def reset_mounted_nodes(
    course_id: str, node_ids: list[str], actor: str | None = None, reason: str = "知识点变更"
) -> dict:
    """节点（含子树）上挂的题全部退回未核。"""
    exercises = exercises_of_nodes(course_id, subtree_ids(course_id, node_ids))
    return reset_exercises(course_id, exercises, actor=actor, reason=reason)
