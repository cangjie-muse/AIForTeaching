"""班级与选课 API：谁在哪门课的哪个班上。

这一层只回答"名单"和"归属"两个问题，不参与"学生能看到哪些课"的判定——
当前阶段学生可见范围仍由课程发布状态 + 可见范围口径（visibility_policy）决定。
以后要按选课收紧可见范围，改 catalog 的过滤条件即可，这里的数据已经就位。

权限：teacher / admin。学生一律 403——名单是教务工作，不是学生自助。
写操作全部落 audit_log：学期末对不上名单时，"谁把谁加进来"必须查得到。

学期的存法：`academic_year`（2026-2027）+ `term_kind`（秋/春/夏），显示成
"2026年秋季学期"。拆开存是因为文案会变、排序不能靠字符串（见迁移 0016 的注释）。
"""

from __future__ import annotations

import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import Actor, require

router = APIRouter(prefix="/api/v1", tags=["classes"])

ACADEMIC_YEAR_RE = re.compile(r"^(\d{4})-(\d{4})$")
TERM_KINDS = ("秋", "春", "夏")
STAFF = ("teacher", "admin")


def term_label(academic_year: str, term_kind: str) -> str:
    """学年 + 学期 → 老师口中的说法。

    秋季学期开学在学年第一年（2026年9月），春季与夏季在第二年（2027年2月、6月），
    所以前缀年份要按学期取，不能一律用学年的第一个年份。
    """
    start, end = academic_year.split("-")
    if term_kind == "秋":
        return f"{start}年秋季学期"
    if term_kind == "春":
        return f"{end}年春季学期"
    return f"{end}年夏季学期"


def _parse_term(academic_year: str, term_kind: str) -> tuple[str, str]:
    """校验并标准化学期。写错就 400，不做模糊猜测。"""
    year = (academic_year or "").strip()
    kind = (term_kind or "").strip()
    match = ACADEMIC_YEAR_RE.match(year)
    if not match or int(match.group(2)) != int(match.group(1)) + 1:
        raise HTTPException(400, "学年格式应为 2026-2027（前一年-后一年，必须连续两个学年）")
    if kind not in TERM_KINDS:
        raise HTTPException(400, "学期只能是秋季学期、春季学期或夏季学期")
    return year, kind


def _audit(actor: Actor, action: str, entity: str, entity_key: str, detail: dict) -> None:
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, %s, %s, %s::jsonb)""",
        (actor.label or actor.id, action, entity, entity_key,
         json.dumps(detail, ensure_ascii=False)),
    )


def _class_or_404(class_id: str) -> dict:
    row = db.query_one(
        """SELECT c.id, c.course_id, c.name, c.academic_year, c.term_kind,
                  c.teacher_id, c.created_by, c.created_at, c.updated_at, c.note,
                  co.title AS course_title,
                  t.display_name AS teacher_name
             FROM teaching_class c
             JOIN course co ON co.id = c.course_id
             LEFT JOIN app_account t ON t.id = c.teacher_id
            WHERE c.id = %s""",
        (class_id,),
    )
    if not row:
        raise HTTPException(404, "没有这个班级")
    return row


def _public_class(row: dict, students: int = 0) -> dict:
    return {
        "id": row["id"],
        "course_id": row["course_id"],
        "course_title": row["course_title"],
        "name": row["name"],
        "academic_year": row["academic_year"],
        "term_kind": row["term_kind"],
        "term": term_label(row["academic_year"], row["term_kind"]),
        "teacher_id": row["teacher_id"],
        "teacher_name": row["teacher_name"] or "",
        "student_count": students,
        "created_at": row["created_at"],
        "note": row.get("note") or "",
    }


class ClassIn(BaseModel):
    course_id: str
    name: str = Field(min_length=1, max_length=60)
    academic_year: str
    term_kind: str
    teacher_id: str | None = None
    note: str | None = None


class ClassPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    academic_year: str | None = None
    term_kind: str | None = None
    teacher_id: str | None = None
    note: str | None = None


class StudentsIn(BaseModel):
    student_ids: list[str] = Field(min_length=1)


@router.get("/classes")
def list_classes(
    course_id: str | None = None,
    academic_year: str | None = None,
    term_kind: str | None = None,
    _actor: Actor = Depends(require(*STAFF)),
) -> dict:
    """班级列表。带 course_id 就是"这门课有哪些班"。"""
    where, params = [], []
    if course_id:
        where.append("c.course_id = %s")
        params.append(course_id)
    if academic_year:
        where.append("c.academic_year = %s")
        params.append(academic_year)
    if term_kind:
        where.append("c.term_kind = %s")
        params.append(term_kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db.query(
        f"""SELECT c.id, c.course_id, c.name, c.academic_year, c.term_kind,
                   c.teacher_id, c.created_by, c.created_at, c.updated_at, c.note,
                   co.title AS course_title,
                   t.display_name AS teacher_name,
                   (SELECT count(*)::int FROM enrollment e WHERE e.class_id = c.id) AS student_count
              FROM teaching_class c
              JOIN course co ON co.id = c.course_id
              LEFT JOIN app_account t ON t.id = c.teacher_id
              {clause}
             ORDER BY c.academic_year DESC,
                      CASE c.term_kind WHEN '夏' THEN 3 WHEN '春' THEN 2 ELSE 1 END DESC,
                      c.course_id, c.name""",
        tuple(params),
    )
    items = [_public_class(row, row["student_count"]) for row in rows]
    return {
        "items": items,
        "total": len(items),
        "students": sum(item["student_count"] for item in items),
    }


@router.post("/classes")
def create_class(payload: ClassIn, actor: Actor = Depends(require(*STAFF))) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "班级名称不能为空")
    academic_year, term_kind = _parse_term(payload.academic_year, payload.term_kind)
    label = term_label(academic_year, term_kind)
    course = db.query_one("SELECT id, title FROM course WHERE id = %s", (payload.course_id,))
    if not course:
        raise HTTPException(404, f"没有这门课：{payload.course_id}")

    teacher_id = payload.teacher_id or (actor.id or None)
    if teacher_id:
        teacher = db.query_one("SELECT id, role FROM app_account WHERE id = %s", (teacher_id,))
        if not teacher:
            raise HTTPException(400, f"任课教师账号不存在：{teacher_id}")
        if teacher["role"] not in ("teacher", "admin"):
            raise HTTPException(400, "任课教师必须是教师或管理员账号")

    if db.query_one(
        """SELECT id FROM teaching_class
            WHERE course_id = %s AND academic_year = %s AND term_kind = %s AND name = %s""",
        (payload.course_id, academic_year, term_kind, name),
    ):
        raise HTTPException(409, f"{course['title']} 在{label}已经有同名班级「{name}」")

    class_id = f"class-{secrets.token_hex(5)}"
    db.execute(
        """INSERT INTO teaching_class
             (id, course_id, name, academic_year, term_kind, teacher_id, created_by, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (class_id, payload.course_id, name, academic_year, term_kind, teacher_id,
         actor.id or None, (payload.note or "").strip() or None),
    )
    _audit(actor, "class.create", "teaching_class", class_id, {
        "course_id": payload.course_id, "course_title": course["title"],
        "name": name, "academic_year": academic_year, "term_kind": term_kind,
        "term": label, "teacher_id": teacher_id,
    })
    return _public_class(_class_or_404(class_id), 0)


@router.patch("/classes/{class_id}")
def patch_class(
    class_id: str,
    payload: ClassPatch,
    actor: Actor = Depends(require(*STAFF)),
) -> dict:
    row = _class_or_404(class_id)
    fields, params = [], []
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "班级名称不能为空")
        fields.append("name = %s")
        params.append(name)
    if payload.academic_year is not None or payload.term_kind is not None:
        academic_year, term_kind = _parse_term(
            payload.academic_year if payload.academic_year is not None else row["academic_year"],
            payload.term_kind if payload.term_kind is not None else row["term_kind"],
        )
        fields.extend(["academic_year = %s", "term_kind = %s"])
        params.extend([academic_year, term_kind])
    if payload.teacher_id is not None:
        if payload.teacher_id:
            teacher = db.query_one("SELECT id, role FROM app_account WHERE id = %s",
                                   (payload.teacher_id,))
            if not teacher:
                raise HTTPException(400, f"任课教师账号不存在：{payload.teacher_id}")
            if teacher["role"] not in ("teacher", "admin"):
                raise HTTPException(400, "任课教师必须是教师或管理员账号")
        fields.append("teacher_id = %s")
        params.append(payload.teacher_id or None)
    if payload.note is not None:
        fields.append("note = %s")
        params.append(payload.note.strip() or None)
    if not fields:
        raise HTTPException(400, "没有要修改的字段")

    fields.append("updated_at = now()")
    params.append(class_id)
    try:
        db.execute(f"UPDATE teaching_class SET {', '.join(fields)} WHERE id = %s", tuple(params))
    except Exception as error:  # 唯一索引撞名
        if "teaching_class_course_term_name_idx" in str(error):
            raise HTTPException(409, "同课程同学期已经有同名班级") from error
        raise
    _audit(actor, "class.update", "teaching_class", class_id, payload.model_dump(exclude_none=True))
    students = db.query_one("SELECT count(*)::int AS n FROM enrollment WHERE class_id = %s",
                            (class_id,))
    return _public_class(_class_or_404(class_id), (students or {}).get("n", 0))


@router.delete("/classes/{class_id}")
def delete_class(class_id: str, actor: Actor = Depends(require(*STAFF))) -> dict:
    row = _class_or_404(class_id)
    students = db.query_one("SELECT count(*)::int AS n FROM enrollment WHERE class_id = %s",
                            (class_id,))
    _audit(actor, "class.delete", "teaching_class", class_id, {
        "course_id": row["course_id"], "name": row["name"],
        "academic_year": row["academic_year"], "term_kind": row["term_kind"],
        "student_count": (students or {}).get("n", 0),
    })
    # enrollment 有 ON DELETE CASCADE，删班连带清名单；审计里已经记下人数。
    db.execute("DELETE FROM teaching_class WHERE id = %s", (class_id,))
    return {"deleted": class_id, "students_removed": (students or {}).get("n", 0)}


@router.get("/classes/{class_id}/students")
def class_students(class_id: str, _actor: Actor = Depends(require(*STAFF))) -> dict:
    _class_or_404(class_id)
    rows = db.query(
        """SELECT e.student_id, e.enrolled_at, e.added_by,
                  a.username, a.display_name, a.disabled_at,
                  adder.display_name AS added_by_name
             FROM enrollment e
             JOIN app_account a ON a.id = e.student_id
             LEFT JOIN app_account adder ON adder.id = e.added_by
            WHERE e.class_id = %s
            ORDER BY a.username""",
        (class_id,),
    )
    return {
        "items": [
            {
                "student_id": row["student_id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "enrolled_at": row["enrolled_at"],
                "added_by": row["added_by_name"] or row["added_by"] or "",
                "disabled": bool(row["disabled_at"]),
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.post("/classes/{class_id}/students")
def add_students(
    class_id: str,
    payload: StudentsIn,
    actor: Actor = Depends(require(*STAFF)),
) -> dict:
    _class_or_404(class_id)
    wanted = list(dict.fromkeys(sid.strip() for sid in payload.student_ids if sid.strip()))
    if not wanted:
        raise HTTPException(400, "没有要添加的学生")
    rows = db.query(
        "SELECT id, username, role FROM app_account WHERE id = ANY(%s)",
        (wanted,),
    )
    found = {row["id"]: row for row in rows}
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise HTTPException(400, f"账号不存在：{', '.join(missing[:5])}")
    wrong = [found[sid]["username"] for sid in wanted if found[sid]["role"] != "student"]
    if wrong:
        raise HTTPException(400, f"这些账号不是学生：{', '.join(wrong[:5])}")

    before = db.query_one("SELECT count(*)::int AS n FROM enrollment WHERE class_id = %s",
                          (class_id,))["n"]
    for sid in wanted:
        db.execute(
            """INSERT INTO enrollment (class_id, student_id, added_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (class_id, student_id) DO NOTHING""",
            (class_id, sid, actor.id or None),
        )
    after = db.query_one("SELECT count(*)::int AS n FROM enrollment WHERE class_id = %s",
                         (class_id,))["n"]
    added = after - before
    _audit(actor, "enrollment.add", "teaching_class", class_id, {
        "requested": len(wanted), "added": added, "students": wanted,
    })
    return {"class_id": class_id, "added": added, "skipped": len(wanted) - added,
            "total": after}


@router.delete("/classes/{class_id}/students/{student_id}")
def remove_student(
    class_id: str,
    student_id: str,
    actor: Actor = Depends(require(*STAFF)),
) -> dict:
    _class_or_404(class_id)
    removed = db.execute(
        "DELETE FROM enrollment WHERE class_id = %s AND student_id = %s",
        (class_id, student_id),
    )
    if not removed:
        raise HTTPException(404, "这个学生不在这门课的班上")
    _audit(actor, "enrollment.remove", "teaching_class", class_id, {"student_id": student_id})
    total = db.query_one("SELECT count(*)::int AS n FROM enrollment WHERE class_id = %s",
                         (class_id,))["n"]
    return {"class_id": class_id, "student_id": student_id, "removed": True, "total": total}


@router.get("/students")
def list_students(
    query: str = "",
    limit: int = 200,
    _actor: Actor = Depends(require(*STAFF)),
) -> dict:
    """学生账号清单：给"加人进班"的选择器用。只返回学生，不返回教师与管理员。"""
    limit = max(1, min(limit, 500))
    like = f"%{query.strip()}%"
    rows = db.query(
        """SELECT id, username, display_name, disabled_at
             FROM app_account
            WHERE role = 'student'
              AND (%s = '%%' OR username ILIKE %s OR display_name ILIKE %s)
            ORDER BY username
            LIMIT %s""",
        (like, like, like, limit),
    )
    return {
        "items": [
            {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "disabled": bool(row["disabled_at"]),
            }
            for row in rows
        ],
        "total": len(rows),
    }
