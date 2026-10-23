"""课程名称与学科方向：一个 PATCH 接口。

为什么单独一个模块：改名与改归类都不属于"导入内容"，也不属于"发布"。
放在 content.py 会让人以为改个名字也会动内容；放在 catalog.py 又会跟发布绞在一起。

两条口径：

1. 谁可以改。管理员全改；教师只能改自己导入的课。判断依据是 course.created_by，
   早期导入的课没有记录导入人，视为无主——教师可改，否则那些课永远改不了名。
2. 改了什么要留痕。audit_log 里记改前改后的值，不是只记"改过"。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, require

router = APIRouter()

# 与前端 js/course-catalog.js 的 groups 一一对应。留空 = 待归类。
DIRECTIONS = {
    "analysis": "分析",
    "algebra": "代数与数论",
    "geometry": "几何与拓扑",
    "pde": "微分方程",
    "probability": "概率统计",
    "computing": "计算与应用",
}


class CourseMetaIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    direction: str | None = Field(default=None, max_length=40)


def _current(course_id: str) -> dict:
    row = db.query_one(
        "SELECT id, title, direction, created_by FROM course WHERE id = %s",
        (course_id,),
    )
    if not row:
        raise HTTPException(404, f"课程不存在：{course_id}")
    return row


@router.patch("/api/v1/courses/{course_id}")
def patch_course(course_id: str, body: CourseMetaIn,
                 actor: Actor = Depends(require("reviewer"))) -> dict:
    row = _current(course_id)
    owner = row.get("created_by")
    if not actor.has("admin") and owner and owner != actor.id:
        raise HTTPException(
            403,
            f"这门课由 {owner} 导入：改名与改归类只对导入人本人或管理员开放",
        )

    fields: dict[str, object] = {}
    detail: dict[str, object] = {}

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "课程名称不能为空")
        if title != (row.get("title") or ""):
            fields["title"] = title
            detail["title"] = {"from": row.get("title"), "to": title}

    if body.direction is not None:
        direction = body.direction.strip()
        if direction and direction not in DIRECTIONS:
            allowed = "、".join(f"{key}（{label}）" for key, label in DIRECTIONS.items())
            raise HTTPException(400, f"不认识的学科方向：{direction}；可选 {allowed}，留空表示待归类")
        current = row.get("direction") or ""
        if direction != current:
            fields["direction"] = direction or None
            detail["direction"] = {"from": current or None, "to": direction or None}

    if not fields:
        return {"course_id": course_id, "changed": [], "course": _shape(course_id)}

    assignments = ", ".join(f"{name} = %s" for name in fields)
    db.execute(
        f"UPDATE course SET {assignments}, updated_at = now() WHERE id = %s",
        (*fields.values(), course_id),
    )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'course.patch', 'course', %s, %s::jsonb)""",
        (actor.id, course_id, json.dumps(detail, ensure_ascii=False)),
    )
    return {"course_id": course_id, "changed": sorted(fields), "course": _shape(course_id)}


def _shape(course_id: str) -> dict:
    row = _current(course_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "direction": row.get("direction") or "",
        "direction_label": DIRECTIONS.get(row.get("direction") or "", "待归类"),
    }
