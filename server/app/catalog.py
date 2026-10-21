"""学生端发布出口：课程级发布 + 公开目录。

为什么还要"课程级发布"：条目级发布（审核页点「发布」）早就有了，但它只决定**单条**内容
能不能露出来。学生端要的是"这门课现在是什么样"，得有一个整门课的开关：
内容改到一半、或者刚导入一批还没审的题，不能因为库里有一道已发布的题就把半成品推给学生。

所以是两级门禁：
  1. 课程级 course.review_status='published' —— 没发布，公开目录里根本不出现；
  2. 条目级 exercise.review_status='published' —— 课程发布了，也只放出老师逐题点过的题。

知识点不设条目级门禁：知识点的正文在「课程内容」页由老师直接改，改完即定稿；
再要求逐条发布只会让树缺枝。retired 的节点与题目始终隐藏。

公开目录不带鉴权（学生要能读），因此它只返回已发布的数据；任何一条 draft 都不会出现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, require

router = APIRouter(prefix="/api/v1", tags=["catalog"])

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# 组装（纯函数，便于脱库测试）
# ---------------------------------------------------------------------------

def build_course(course: dict, nodes: list[dict], exercises: list[dict],
                 links: list[dict]) -> dict:
    """把库里的行拼成前端课程对象（与 data/courses.js 的结构一致）。

    字段名必须对齐：前端的图谱、大纲、习题页、学习记录都按这套字段读。
    少一个字段不会报错，只会让某个页面静默少一块内容——所以这里逐字段写全。
    """
    by_exercise: dict[str, list[str]] = {}
    for link in links:
        by_exercise.setdefault(link["exercise_id"], []).append(link["node_id"])

    children: dict[str, list[str]] = {}
    node_ids = {node["id"] for node in nodes}
    for node in nodes:
        if node.get("parent_id") in node_ids:
            children.setdefault(node["parent_id"], []).append(node["id"])

    exercise_ids: dict[str, list[str]] = {}
    for exercise in exercises:
        for node_id in by_exercise.get(exercise["id"], []):
            exercise_ids.setdefault(node_id, []).append(exercise["id"])

    node_rows = []
    for node in nodes:
        node_rows.append({
            "id": node["id"],
            "title": node["title"],
            "path": list(node.get("path") or []),
            "level": node["level"],
            "source": node.get("source_raw") or "",
            "exerciseAnchor": node.get("exercise_anchor") or node["id"],
            "content": node.get("content_md") or "",
            "parentId": node.get("parent_id"),
            "children": sorted(children.get(node["id"], [])),
            "kind": node["kind"],
            "chapterId": node.get("chapter_id"),
            "exerciseIds": sorted(set(exercise_ids.get(node["id"], []))),
        })

    exercise_rows = []
    for exercise in exercises:
        exercise_rows.append({
            "id": exercise["id"],
            "prompt": exercise["prompt_md"],
            "answer": exercise.get("answer_md") or "",
            "difficulty": exercise.get("difficulty"),
            "source": exercise.get("source_raw") or "",
            "nodeIds": sorted(set(by_exercise.get(exercise["id"], []))),
        })

    relationships = [
        {"source": node["parent_id"], "target": node["id"], "type": "contains"}
        for node in nodes if node.get("parent_id") in node_ids
    ]

    associations = sum(len(exercise["nodeIds"]) for exercise in exercise_rows)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": course["id"],
        "title": course["title"],
        "audience": course.get("audience") or "本科课程",
        "status": "ready",
        "description": course.get("description") or "",
        "subject": course.get("subject") or "专业课程",
        "textbook": course.get("textbook") or "",
        "importedAt": _iso(course.get("content_updated_at") or course.get("updated_at")),
        "resourceBase": course.get("resource_base") or "",
        "nodes": node_rows,
        "exercises": exercise_rows,
        "relationships": relationships,
        "stats": {
            "nodes": len(node_rows),
            "knowledge": sum(1 for node in node_rows if node["kind"] == "knowledge"),
            "sections": sum(1 for node in node_rows if node["level"] == 2),
            "exercises": len(exercise_rows),
            "associations": associations,
            "variantGroups": 0,
        },
        "warnings": [],
        "files": ["知识节点.xlsx", "知识点.md", "习题.md"],
        "reviewStatus": "已发布" if course.get("published_at") else "待发布",
        "attachments": [],
        "publishedAt": _iso(course.get("published_at")),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

PUBLISHED_COURSES_SQL = """
SELECT c.id, c.title, c.description, c.audience, c.subject, c.content_updated_at,
       c.updated_at, c.published_at
  FROM course c
 WHERE c.review_status = 'published' AND c.published_at IS NOT NULL
 ORDER BY c.id
"""

PUBLISHED_NODES_SQL = """
SELECT id, chapter_id, parent_id, title, level, kind, path, content_md,
       exercise_anchor, source_raw
  FROM knowledge_node
 WHERE course_id = %s AND review_status <> 'retired'
 ORDER BY level, id
"""

PUBLISHED_EXERCISES_SQL = """
SELECT id, prompt_md, answer_md, difficulty, source_raw
  FROM exercise
 WHERE course_id = %s AND review_status = 'published'
 ORDER BY id
"""


def published_course(course_id: str) -> dict | None:
    course = db.query_one(
        """SELECT id, title, description, audience, subject, content_updated_at,
                  updated_at, published_at
             FROM course
            WHERE id = %s AND review_status = 'published' AND published_at IS NOT NULL""",
        (course_id,))
    if not course:
        return None
    nodes = db.query(PUBLISHED_NODES_SQL, (course_id,))
    exercises = db.query(PUBLISHED_EXERCISES_SQL, (course_id,))
    links = db.query(
        "SELECT exercise_id, node_id FROM exercise_node WHERE course_id = %s",
        (course_id,))
    return build_course(course, nodes, exercises, links)


def catalog() -> list[dict]:
    """公开目录：只有已发布的课，且只报已发布的数量。"""
    rows = db.query(PUBLISHED_COURSES_SQL)
    items = []
    for row in rows:
        visible = db.query_one(
            """SELECT count(*) AS n FROM exercise
                WHERE course_id = %s AND review_status = 'published'""",
            (row["id"],))
        items.append({
            "id": row["id"],
            "title": row["title"],
            "description": row["description"] or "",
            "published_at": _iso(row["published_at"]),
            "exercises": visible["n"] if visible else 0,
        })
    return items


# ---------------------------------------------------------------------------
# 发布 / 撤回
# ---------------------------------------------------------------------------

class PublishIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def publish(course_id: str, actor: str, note: str | None = None) -> dict:
    course = db.query_one("SELECT id, title FROM course WHERE id = %s", (course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{course_id}")

    counts = db.query_one(
        """SELECT
             (SELECT count(*) FROM knowledge_node
               WHERE course_id = %s AND review_status <> 'retired') AS nodes,
             (SELECT count(*) FROM exercise
               WHERE course_id = %s AND review_status = 'published') AS exercises,
             (SELECT count(*) FROM exercise
               WHERE course_id = %s AND review_status IN ('draft', 'machine-checked')) AS drafts""",
        (course_id, course_id, course_id))

    if counts["nodes"] == 0:
        raise HTTPException(400, "这门课还没有知识点，发布后学生看到的是空课程")
    if counts["exercises"] == 0:
        raise HTTPException(
            400,
            "还没有任何题目被审核通过并发布。先在「内容审核」里逐题点发布，"
            "否则学生端只能看到知识点、看不到题",
        )

    db.execute(
        """UPDATE course
              SET review_status = 'published', published_at = now(), published_by = %s,
                  publish_note = %s, updated_at = now()
            WHERE id = %s""",
        (actor, note, course_id))
    db.execute(
        """INSERT INTO publish_event (course_id, actor, action, nodes, exercises, note)
           VALUES (%s, %s, 'publish', %s, %s, %s)""",
        (course_id, actor, counts["nodes"], counts["exercises"], note))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'course.publish', 'course', %s, %s::jsonb)""",
        (actor, course_id,
         json.dumps({"nodes": counts["nodes"], "exercises": counts["exercises"],
                     "drafts_hidden": counts["drafts"]}, ensure_ascii=False)))

    return {"course_id": course_id, "published": True,
            "nodes": counts["nodes"], "exercises": counts["exercises"],
            "drafts_hidden": counts["drafts"]}


def unpublish(course_id: str, actor: str, note: str | None = None) -> dict:
    course = db.query_one("SELECT id FROM course WHERE id = %s", (course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{course_id}")
    db.execute(
        """UPDATE course
              SET review_status = 'draft', published_at = NULL, publish_note = %s,
                  updated_at = now()
            WHERE id = %s""",
        (note, course_id))
    db.execute(
        """INSERT INTO publish_event (course_id, actor, action, note)
           VALUES (%s, %s, 'unpublish', %s)""",
        (course_id, actor, note))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'course.unpublish', 'course', %s, '{}'::jsonb)""",
        (actor, course_id))
    return {"course_id": course_id, "published": False}


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.get("/catalog")
def get_catalog() -> dict:
    """公开目录（学生端读）。不带鉴权，只返回已发布内容。"""
    return {"items": catalog()}


@router.get("/catalog/{course_id}")
def get_catalog_course(course_id: str) -> dict:
    course = published_course(course_id)
    if not course:
        raise HTTPException(404, f"课程未发布或不存在：{course_id}")
    return course


@router.get("/courses/{course_id}/publish/status")
def publish_status(course_id: str, actor: Actor = Depends(require("reviewer"))) -> dict:
    """发布前的自检：会放出多少、藏了多少。老师点发布之前必须看到这个。"""
    row = db.query_one(
        """SELECT c.id, c.title, c.review_status, c.published_at,
                  (SELECT count(*) FROM knowledge_node
                    WHERE course_id = c.id AND review_status <> 'retired') AS nodes,
                  (SELECT count(*) FROM exercise
                    WHERE course_id = c.id AND review_status = 'published') AS published,
                  (SELECT count(*) FROM exercise
                    WHERE course_id = c.id AND review_status IN ('draft', 'machine-checked',
                                                                  'teacher-reviewed', 'retired'))
                    AS hidden
             FROM course c WHERE c.id = %s""",
        (course_id,))
    if not row:
        raise HTTPException(404, f"课程不存在：{course_id}")
    row["published_at"] = _iso(row["published_at"])
    last = db.query_one(
        """SELECT actor, action, created_at FROM publish_event
            WHERE course_id = %s ORDER BY created_at DESC LIMIT 1""",
        (course_id,))
    row["last_event"] = {**last, "created_at": _iso(last["created_at"])} if last else None
    return row


@router.post("/courses/{course_id}/publish")
def post_publish(course_id: str, body: PublishIn | None = None,
                 actor: Actor = Depends(require("admin"))) -> dict:
    return publish(course_id, actor.id, body.note if body else None)


@router.post("/courses/{course_id}/unpublish")
def post_unpublish(course_id: str, body: PublishIn | None = None,
                   actor: Actor = Depends(require("admin"))) -> dict:
    return unpublish(course_id, actor.id, body.note if body else None)
