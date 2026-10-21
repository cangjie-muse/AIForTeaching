"""作答提交。

为什么和 qa.py 分开：qa.py 只检查"有没有作答"，这里负责"作答从哪来"。
把"谁能写"和"谁能读"分开，审计的时候能各看各的。

写的范围仅限本人：`student_id` 取自认证身份，请求体里不接受学生自己声明学号——
否则谁都能替别人提交，解锁条件立刻失效。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, require

router = APIRouter(prefix="/api/v1/attempts", tags=["attempts"])

MAX_ANSWER_CHARS = 20000   # 手写拍照另走附件，这里只收文本


class AttemptIn(BaseModel):
    course_id: str = Field(min_length=1, max_length=80)
    exercise_id: str = Field(min_length=1, max_length=120)
    answer_md: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    duration_sec: int | None = Field(default=None, ge=0, le=86400)


@router.post("", status_code=201)
def submit(body: AttemptIn, actor: Actor = Depends(require("student"))) -> dict:
    exercise = db.query_one(
        "SELECT 1 FROM exercise WHERE course_id = %s AND id = %s",
        (body.course_id, body.exercise_id),
    )
    if not exercise:
        # 不存在的题目不能建档：否则一个笔误就在库里留下永远对不上的作答
        raise HTTPException(
            status_code=404,
            detail=f"题目不存在：{body.course_id}/{body.exercise_id}。"
                   "如果前端课程包与后端题目库的 id 不一致，请先完成导入。",
        )

    counter = db.query_one(
        """SELECT coalesce(max(attempt_no), 0) + 1 AS next_no
           FROM exercise_attempt
           WHERE student_id = %s AND course_id = %s AND exercise_id = %s""",
        (actor.id, body.course_id, body.exercise_id),
    )
    row = db.query_one(
        """INSERT INTO exercise_attempt
             (student_id, course_id, exercise_id, attempt_no, answer_md, duration_sec)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id, attempt_no, submitted_at""",
        (actor.id, body.course_id, body.exercise_id,
         counter["next_no"] if counter else 1, body.answer_md, body.duration_sec),
    )
    return {
        "id": row["id"],
        "exercise_id": body.exercise_id,
        "attempt_no": row["attempt_no"],
        "submitted_at": row["submitted_at"],
        # 前端据此把"完整解答"点亮，不必再试一次 403
        "unlocks_full": True,
    }


@router.get("")
def list_attempts(
    course_id: str | None = Query(default=None),
    exercise_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    actor: Actor = Depends(require("student")),
) -> dict:
    """只返回本人的作答。教师查看学生作答应走另一个接口，且要有班级授权。"""
    rows: list[dict[str, Any]] = db.query(
        """SELECT id, course_id, exercise_id, attempt_no, submitted_at,
                  length(answer_md) AS answer_chars,
                  (teacher_score IS NOT NULL) AS graded
           FROM exercise_attempt
           WHERE student_id = %s
             AND (%s::text IS NULL OR course_id = %s)
             AND (%s::text IS NULL OR exercise_id = %s)
           ORDER BY submitted_at DESC, id DESC
           LIMIT %s""",
        (actor.id, course_id, course_id, exercise_id, exercise_id, limit),
    )
    return {"items": rows}
