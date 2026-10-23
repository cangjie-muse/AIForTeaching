"""学生端发布出口：课程级发布 + 公开目录。

为什么还要"课程级发布"：条目级发布（审核页点「发布」）早就有了，但它只决定**单条**内容
能不能露出来。学生端要的是"这门课现在是什么样"，得有一个整门课的开关：
内容改到一半、或者刚导入一批还没审的题，不能因为库里有一道已发布的题就把半成品推给学生。

所以是两级门禁：
  1. 课程级 course.review_status='published' —— 没发布，公开目录里根本不出现；
  2. 条目级 exercise.review_status='published' —— 课程发布了，也只放出老师逐题点过的题。

3. 验证等级闸（visibility_policy）：已发布的题还要满足当前允许的可信等级——
   「仅人工审核通过」/「Lean 验证 + 人工审核」/「全部（含未验证）」。三道是 AND。
   这一道单独存在的原因：发布是"能不能出现"，等级是"哪一档可信度才允许出现"。
   口径分两级：全局默认 + 课程覆盖（课程有覆盖就用覆盖，否则跟随全局）。
   图谱另有一档 graph_tier（all / human）：节点也要人工审核过才放。

知识点不设条目级门禁：知识点的正文在「课程内容」页由老师直接改，改完即定稿；
再要求逐条发布只会让树缺枝。retired 的节点与题目始终隐藏。

公开目录不带鉴权（学生要能读），因此它只返回已发布的数据；任何一条 draft 都不会出现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, exercise_tags, visibility
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
            # 三维组合标签（难度档 / 来源 / 可信度），派生而非落库，
            # 规则见 exercise_tags.py。前端拿 codes 做筛选、labels 做徽标。
            "tags": exercise_tags.tags_for(exercise),
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
SELECT n.id, n.chapter_id, n.parent_id, n.title, n.level, n.kind, n.path, n.content_md,
       n.exercise_anchor, n.source_raw
  FROM knowledge_node n
 WHERE n.course_id = %s AND n.review_status <> 'retired'
   AND {node_filter}
 ORDER BY n.level, n.id
"""

PUBLISHED_EXERCISES_SQL = """
SELECT e.id, e.prompt_md, e.answer_md, e.difficulty, e.source_raw, e.provenance, v.tier
  FROM exercise e
  JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
 WHERE e.course_id = %s AND e.review_status = 'published'
   AND {tier_filter}
 ORDER BY e.id
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
    # 三道闸：课程已发布、条目已发布、当前口径允许的等级。
    # 口径按课程解析：这门课有单独设置就用它，否则跟随全局。
    node_filter, node_params = visibility.node_filter(course_id)
    nodes = db.query(
        PUBLISHED_NODES_SQL.format(node_filter=node_filter),
        (course_id, *node_params))
    tier_filter, tier_params = visibility.exercise_filter(course_id)
    exercises = db.query(
        PUBLISHED_EXERCISES_SQL.format(tier_filter=tier_filter),
        (course_id, *tier_params))
    links = db.query(
        "SELECT exercise_id, node_id FROM exercise_node WHERE course_id = %s",
        (course_id,))
    return build_course(course, nodes, exercises, links)


def catalog() -> list[dict]:
    """公开目录：只有已发布的课，且只报已发布的数量。"""
    rows = db.query(PUBLISHED_COURSES_SQL)
    items = []
    for row in rows:
        tier_filter, tier_params = visibility.exercise_filter(row["id"])
        visible = db.query_one(
            f"""SELECT count(*) AS n FROM exercise e
                 JOIN v_exercise_verification v ON v.course_id = e.course_id AND v.id = e.id
                WHERE e.course_id = %s AND e.review_status = 'published'
                  AND {tier_filter}""",
            (row["id"], *tier_params))
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

    # 发布 = 把"人工已审"的题一起放出去。
    #
    # 逐题点发布在 486 道题的课上不现实，但发布又必须有门槛，于是口径定成：
    # 审核是逐题的动作（「内容审核」里通过/批量通过），发布是整门课的开关。
    # 只放出 teacher-reviewed 这一档：machine-checked 是脚本自己验的、draft 还没人看过，
    # 替它们盖章等于把审核做成形式（同 scripts/publish_courses.py 的默认口径）。
    promoted = db.execute(
        """UPDATE exercise SET review_status = 'published', updated_at = now()
            WHERE course_id = %s AND review_status = 'teacher-reviewed'""",
        (course_id,))

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
            "还没有题目可以发布：先去「内容审核」把看过的题通过（或批量通过），"
            "再回来点发布——通过了的题会在这一步一起放出给学生",

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
            "drafts_hidden": counts["drafts"], "promoted": promoted}


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


class PublishAllIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/courses/{course_id}/exercises/publish-all")
def publish_all_exercises(course_id: str, body: PublishAllIn | None = None,
                          actor: Actor = Depends(require("admin"))) -> dict:
    """把一门课里所有未停用的题直接标为「已发布」。

    这是"整本书的题老师认可"时的一次性动作，不是审核的替代品：human_verified 不动，
    所以可信度仍显示「未经人工校验」，可见范围里"仅人工审核"那档也不会把它们放出来。
    审计日志记下是谁、放了多少、写了什么理由。
    """
    return publish_exercises_all(course_id, actor.id, body.note if body else None)


def publish_exercises_all(course_id: str, actor: str, note: str | None = None) -> dict:
    course = db.query_one("SELECT id, title FROM course WHERE id = %s", (course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{course_id}")
    nodes = db.query_one(
        """SELECT (SELECT count(*) FROM knowledge_node
                    WHERE course_id = %s AND review_status <> 'retired') AS nodes""",
        (course_id,))
    if not nodes or not nodes["nodes"]:
        raise HTTPException(400, "这门课还没有知识点，先导入内容")
    updated = db.execute(
        """UPDATE exercise SET review_status = 'published', updated_at = now()
            WHERE course_id = %s AND review_status <> 'retired'
              AND review_status <> 'published'""",
        (course_id,))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'exercise.publish-all', 'course', %s, %s::jsonb)""",
        (actor, course_id,
         json.dumps({"published": updated, "note": note,
                     "verified": False}, ensure_ascii=False)),
    )
    return {"course_id": course_id, "published": updated,
            "nodes": nodes["nodes"], "verified": False}


@router.post("/courses/{course_id}/publish")
def post_publish(course_id: str, body: PublishIn | None = None,
                 actor: Actor = Depends(require("admin"))) -> dict:
    return publish(course_id, actor.id, body.note if body else None)


@router.post("/courses/{course_id}/unpublish")
def post_unpublish(course_id: str, body: PublishIn | None = None,
                   actor: Actor = Depends(require("admin"))) -> dict:
    return unpublish(course_id, actor.id, body.note if body else None)
