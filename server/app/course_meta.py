"""课程名称与学科方向：一个 PATCH 接口。

为什么单独一个模块：改名与改归类都不属于"导入内容"，也不属于"发布"。
放在 content.py 会让人以为改个名字也会动内容；放在 catalog.py 又会跟发布绞在一起。

两条口径：

1. 谁可以改。管理员全改；教师只能改自己导入的课。判断依据是 course.created_by，
   早期导入的课没有记录导入人，视为无主——教师可改，否则那些课永远改不了名。
2. 改了什么要留痕。audit_log 里记改前改后的值，不是只记"改过"。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, require

router = APIRouter()

# 分类现在是数据，不是常量：真正的清单在 course_direction 表里（0023 迁移建的）。
# 这份常量只在两种情况下兜底：迁移还没跑、或者表被清空。它必须与
# js/course-catalog.js 里的 groups 一致，否则离线包与库里的分组会对不上。
DIRECTIONS = {
    "analysis": "分析",
    "algebra": "代数与数论",
    "geometry": "几何与拓扑",
    "pde": "微分方程",
    "probability": "概率统计",
    "computing": "计算与应用",
}


def _directions() -> list[dict]:
    rows = db.query(
        "SELECT id, label, description, ord FROM course_direction ORDER BY ord, id"
    )
    if rows:
        return rows
    return [{"id": key, "label": label, "description": "", "ord": index + 1}
            for index, (key, label) in enumerate(DIRECTIONS.items())]


def _direction_ids() -> set[str]:
    return {row["id"] for row in _directions()}


class DirectionIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=80)


@router.get("/api/v1/directions")
def list_directions() -> dict:
    """学科分类清单。课程中心的分组、筛选条、归类下拉都读它。

    不要求登录：课程目录本来就允许匿名浏览，分类名算不上敏感信息。
    """
    return {"items": [
        {"id": row["id"], "label": row["label"],
         "description": row.get("description") or "", "number": index + 1}
        for index, row in enumerate(_directions())
    ]}


@router.post("/api/v1/directions")
def create_direction(body: DirectionIn,
                     actor: Actor = Depends(require("admin"))) -> dict:
    """新建一个学科分类。管理员专用。

    id 由名称哈希得出：管理员只需要写「数理逻辑」，不必再想一个英文标识。
    同一个名字永远得到同一个 id，重复提交会被下面的唯一约束挡住。
    """
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "分类名称不能为空")
    known = {row["label"] for row in _directions()}
    if label in known:
        raise HTTPException(400, f"已经有「{label}」这个分类了")
    direction_id = "dir-" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    ord_value = db.query_one(
        "SELECT coalesce(max(ord), 0) + 1 AS next FROM course_direction")
    db.execute(
        """INSERT INTO course_direction (id, label, description, ord, created_by)
           VALUES (%s, %s, %s, %s, %s)""",
        (direction_id, label, (body.description or "").strip() or None,
         int(ord_value["next"]) if ord_value else 1, actor.id),
    )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'direction.create', 'course_direction', %s, %s::jsonb)""",
        (actor.id, direction_id, json.dumps({"label": label}, ensure_ascii=False)),
    )
    return {"id": direction_id, "label": label}


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
        allowed_ids = _direction_ids()
        if direction and direction not in allowed_ids:
            allowed = "、".join(
                f"{row['id']}（{row['label']}）" for row in _directions())
            raise HTTPException(
                400, f"不认识的学科方向：{direction}；可选 {allowed}，留空表示待归类")
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
    labels = {item["id"]: item["label"] for item in _directions()}
    return {
        "id": row["id"],
        "title": row["title"],
        "direction": row.get("direction") or "",
        "direction_label": labels.get(row.get("direction") or "", "待归类"),
    }
