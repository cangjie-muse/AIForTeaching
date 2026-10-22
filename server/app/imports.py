"""批量导入：账号、班级、班级名单。

为什么单开一个文件：
    三个入口的行校验规则各不相同，但"逐行给结果"的形状必须一样——老师手里是一张
    表，哪一行进去了、哪一行跳过了、哪一行为什么错，得能一行一行对上。所以这里只有
    一套结果构造，三处共用。

Excel 解析放在前端（浏览器里已经装了 ExcelJS），这里只收 JSON 行：
    xlsx 依赖只存在一处；预览（dry_run）与真正导入走的是同一个函数，
    屏幕上看到的就是点下去会发生的事。

写库不是原子的：一行一行来，成功的就落库。理由是每行的结果都独立，宁可
"第 7 行失败、前 6 行成功"并在结果里写清楚，也不要整批回滚让人重来一遍。
"""

from __future__ import annotations

import json
import re
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import auth, db
from .auth import Actor, require

router = APIRouter(prefix="/api/v1", tags=["imports"])

STAFF = ("teacher", "admin")

# 一次导入的上限：不是性能考虑，是"导错了要能重来"的考虑。
MAX_ACCOUNT_ROWS = 500
MAX_CLASS_ROWS = 100
MAX_ROSTER_ROWS = 2000

USERNAME_RE = re.compile(r"^[a-z0-9._-]{2,60}$")
ACADEMIC_YEAR_RE = re.compile(r"^(\d{4})-(\d{4})$")

ROLE_ALIASES = {
    "student": "student", "学生": "student", "同学": "student",
    "teacher": "teacher", "教师": "teacher", "老师": "teacher", "任课教师": "teacher",
    "admin": "admin", "管理员": "admin",
}

TERM_ALIASES = {
    "秋": "秋", "秋季": "秋", "秋季学期": "秋", "第一学期": "秋", "1": "秋",
    "春": "春", "春季": "春", "春季学期": "春", "第二学期": "春", "2": "春",
    "夏": "夏", "夏季": "夏", "夏季学期": "夏", "3": "夏",
}

# 去掉了 0 O 1 l I：这些字符在纸上、在投影上都认不清，
# 老师照着名单念密码时最容易出事。
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ImportRow(BaseModel):
    line: int = Field(default=0, ge=0, le=100000)
    cells: dict[str, str] = Field(default_factory=dict)


class ImportIn(BaseModel):
    rows: list[ImportRow] = Field(default_factory=list)
    dry_run: bool = True
    create_missing: bool = True
    change_note: str | None = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\u3000", " ").strip()


def _new_password(length: int = 10) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _audit(actor: Actor, action: str, entity: str, entity_key: str, detail: dict) -> None:
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, %s, %s, %s::jsonb)""",
        (actor.label or actor.id, action, entity, entity_key,
         json.dumps(detail, ensure_ascii=False)),
    )


def _result(line: int, status: str, action: str, values: dict,
            reason: str = "", **extra) -> dict:
    row = {"line": line, "status": status, "action": action,
           "values": values, "reason": reason}
    row.update(extra)
    return row


def _summary(rows: list[dict]) -> dict:
    return {
        "rows": len(rows),
        "ok": sum(1 for row in rows if row["status"] == "ok"),
        "skip": sum(1 for row in rows if row["status"] == "skip"),
        "error": sum(1 for row in rows if row["status"] == "error"),
        "created": sum(1 for row in rows if row.get("created")),
    }


def _finish(rows: list[dict], dry_run: bool, kind: str) -> dict:
    return {"kind": kind, "dry_run": dry_run, "results": rows,
            "summary": _summary(rows)}


def _username_problem(raw: str) -> str:
    if not raw:
        return "账号（学号）不能为空"
    if not USERNAME_RE.match(raw):
        return "账号只能用字母、数字、点、下划线、短横线，2—60 位"
    return ""


def _role_of(raw: str, default: str = "student") -> tuple[str, str]:
    if not raw:
        return default, ""
    role = ROLE_ALIASES.get(raw) or ROLE_ALIASES.get(raw.lower())
    if not role:
        return "", f"角色只能写 学生 / 教师 / 管理员，收到「{raw}」"
    return role, ""


def _term_of(raw: str) -> tuple[str, str]:
    key = raw.replace("学期", "").strip() or raw.strip()
    kind = TERM_ALIASES.get(raw) or TERM_ALIASES.get(key)
    if not kind:
        return "", f"学期只能写 秋季学期 / 春季学期 / 夏季学期，收到「{raw}」"
    return kind, ""


def _year_of(raw: str) -> tuple[str, str]:
    text = raw.replace("学年", "").replace("学年度", "").strip()
    match = ACADEMIC_YEAR_RE.match(text)
    if not match or int(match.group(2)) != int(match.group(1)) + 1:
        return "", f"学年要写成 2026-2027（前后连续两年），收到「{raw}」"
    return text, ""


def _rows_or_400(payload: ImportIn, limit: int) -> list[ImportRow]:
    rows = [row for row in payload.rows if any(_text(v) for v in row.cells.values())]
    if not rows:
        raise HTTPException(400, "表里没有数据行（只有表头？）")
    if len(rows) > limit:
        raise HTTPException(400, f"一次最多导 {limit} 行，这批有 {len(rows)} 行，请拆开")
    return rows


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

def _create_account(username: str, display_name: str, role: str,
                    password: str, must_change: bool) -> str:
    """建账号。密码只在这里被哈希一次，原文随后进响应（给老师发），不落库。"""
    account_id = f"acc-{uuid.uuid4().hex[:12]}"
    hashed = auth.hash_password(password)
    db.execute(
        """INSERT INTO app_account
             (id, username, display_name, role, password_hash,
              password_updated_at, must_change_password)
           VALUES (%s, %s, %s, %s, %s, now(), %s)""",
        (account_id, username, display_name, role, hashed, must_change),
    )
    return account_id


@router.post("/accounts/import")
def import_accounts(payload: ImportIn,
                    actor: Actor = Depends(require("admin"))) -> dict:
    """批量建账号。已存在的账号跳过，不改密码也不改角色——批量改角色太危险。"""
    rows = _rows_or_400(payload, MAX_ACCOUNT_ROWS)
    existing = {
        row["username"]: row
        for row in db.query("SELECT id, username, display_name, role FROM app_account")
    }
    seen: dict[str, int] = {}
    results: list[dict] = []
    made: list[dict] = []

    for row in rows:
        cells = {key: _text(value) for key, value in row.cells.items()}
        username = cells.get("username", "").lower()
        display_name = cells.get("display_name", "")
        values = {"username": username, "display_name": display_name,
                  "role": cells.get("role", "")}

        problem = _username_problem(username)
        if not problem and username in seen:
            problem = f"文件里第 {seen[username]} 行已经有了同一个账号"
        role, role_problem = _role_of(cells.get("role", ""))
        password = cells.get("password", "")
        if not problem:
            if not display_name:
                problem = "缺姓名"
            elif len(display_name) > 60:
                problem = "姓名太长（上限 60 字）"
            elif role_problem:
                problem = role_problem
            elif password:
                password_problem = auth.password_problem(password)
                if password_problem:
                    problem = password_problem
        if problem:
            results.append(_result(row.line, "error", "create", values, problem))
            continue
        seen[username] = row.line

        if username in existing:
            results.append(_result(
                row.line, "skip", "create", values,
                f"账号已存在（{existing[username]['display_name']}），不改密码、不改角色"))
            continue

        initial = password or _new_password()
        if payload.dry_run:
            results.append(_result(row.line, "ok", "create", values,
                                   "将新建账号", will_create_password=True))
            continue
        account_id = _create_account(username, display_name, role, initial,
                                     must_change=not password)
        existing[username] = {"id": account_id, "username": username,
                              "display_name": display_name, "role": role}
        made.append({"line": row.line, "username": username,
                     "display_name": display_name, "role": role,
                     "password": initial, "generated": not password})
        results.append(_result(row.line, "ok", "create", values, "已新建",
                               id=account_id, password=initial,
                               generated=not password, created=True))

    if not payload.dry_run and made:
        _audit(actor, "account.import", "app_account", "batch", {
            "rows": len(rows), "created": len(made),
            "accounts": [row["username"] for row in made[:50]],
            "note": payload.change_note or "",
        })
    body = _finish(results, payload.dry_run, "accounts")
    body["passwords"] = made
    return body


# ---------------------------------------------------------------------------
# 班级
# ---------------------------------------------------------------------------

def _course_lookup(raw: str) -> tuple[dict | None, str]:
    text = _text(raw)
    if not text:
        return None, "缺课程"
    row = db.query_one("SELECT id, title FROM course WHERE id = %s", (text,))
    if row:
        return row, ""
    matches = db.query("SELECT id, title FROM course WHERE lower(title) = lower(%s)", (text,))
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"课程名「{text}」对应多门课，请改填课程 id"
    return None, f"没有这门课：「{text}」（填课程 id 或课程全名）"


def _teacher_lookup(raw: str, actor: Actor) -> tuple[str | None, str]:
    text = _text(raw)
    if not text:
        if any(role in STAFF for role in actor.roles):
            return actor.id, ""
        return None, "缺任课教师"
    row = db.query_one(
        """SELECT id, role FROM app_account
            WHERE lower(username) = lower(%s) AND disabled_at IS NULL""", (text,))
    if not row:
        rows = db.query(
            """SELECT id, role, count(*) OVER () AS n FROM app_account
                WHERE display_name = %s AND disabled_at IS NULL""", (text,))
        if len(rows) > 1:
            return None, f"教师姓名「{text}」有重名，请改填账号"
        row = rows[0] if rows else None
    if not row:
        return None, f"没有这个教师账号：「{text}」"
    if row["role"] not in STAFF:
        return None, f"「{text}」不是教师账号"
    return row["id"], ""


@router.post("/classes/import")
def import_classes(payload: ImportIn,
                   actor: Actor = Depends(require(*STAFF))) -> dict:
    """批量建班。同一课程同一学期同名班级跳过。"""
    rows = _rows_or_400(payload, MAX_CLASS_ROWS)
    results: list[dict] = []
    seen: dict[tuple, int] = {}
    made: list[dict] = []

    for row in rows:
        cells = {key: _text(value) for key, value in row.cells.items()}
        values = {"course": cells.get("course", ""), "name": cells.get("name", ""),
                  "academic_year": cells.get("academic_year", ""),
                  "term_kind": cells.get("term_kind", ""),
                  "teacher": cells.get("teacher", "")}
        course, problem = _course_lookup(cells.get("course", ""))
        name = values["name"]
        academic_year, kind = "", ""
        if not problem:
            if not name:
                problem = "缺班级名称"
            elif len(name) > 60:
                problem = "班级名称太长（上限 60 字）"
        if not problem:
            academic_year, problem = _year_of(values["academic_year"])
        if not problem:
            kind, problem = _term_of(values["term_kind"])
        teacher_id = None
        if not problem:
            teacher_id, problem = _teacher_lookup(values["teacher"], actor)
        if not problem and course:
            key = (course["id"], academic_year, kind, name)
            if key in seen:
                problem = f"文件里第 {seen[key]} 行已经是同一个班"
        if problem:
            results.append(_result(row.line, "error", "create", values, problem))
            continue
        seen[(course["id"], academic_year, kind, name)] = row.line

        clash = db.query_one(
            """SELECT id FROM teaching_class
                WHERE course_id = %s AND academic_year = %s
                  AND term_kind = %s AND name = %s""",
            (course["id"], academic_year, kind, name))
        if clash:
            results.append(_result(row.line, "skip", "create", values,
                                   "这个学期已经有同名班级"))
            continue

        class_id = f"class-{secrets.token_hex(5)}"
        if payload.dry_run:
            results.append(_result(row.line, "ok", "create", values,
                                   f"将建班：{course['title']} · {name}",
                                   course_id=course["id"],
                                   course_title=course["title"]))
            continue
        db.execute(
            """INSERT INTO teaching_class
                 (id, course_id, name, academic_year, term_kind, teacher_id,
                  created_by, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (class_id, course["id"], name, academic_year, kind, teacher_id,
             actor.id or None, _text(cells.get("note", "")) or None))
        made.append({"line": row.line, "class_id": class_id, "name": name,
                     "course_id": course["id"], "course_title": course["title"]})
        results.append(_result(row.line, "ok", "create", values,
                               f"已建班：{course['title']} · {name}",
                               id=class_id, course_id=course["id"],
                               course_title=course["title"], created=True))

    if not payload.dry_run and made:
        _audit(actor, "class.import", "teaching_class", "batch", {
            "rows": len(rows), "created": len(made),
            "classes": [row["name"] for row in made[:50]],
            "note": payload.change_note or "",
        })
    body = _finish(results, payload.dry_run, "classes")
    body["classes"] = made
    return body


# ---------------------------------------------------------------------------
# 班级名单
# ---------------------------------------------------------------------------

@router.post("/classes/{class_id}/students/import")
def import_students(class_id: str, payload: ImportIn,
                    actor: Actor = Depends(require(*STAFF))) -> dict:
    """往一个班里导名单。库里没有的学号自动建学生账号（这是老师要的一步到位）。

    建出来的账号默认带随机初始密码、强制首次登录改密；密码只在这一次的响应里出现，
    落库的只有哈希。
    """
    klass = db.query_one(
        """SELECT c.id, c.name, c.course_id, co.title AS course_title
             FROM teaching_class c JOIN course co ON co.id = c.course_id
            WHERE c.id = %s""", (class_id,))
    if not klass:
        raise HTTPException(404, "没有这个班级")
    rows = _rows_or_400(payload, MAX_ROSTER_ROWS)

    enrolled = {
        row["student_id"]
        for row in db.query("SELECT student_id FROM enrollment WHERE class_id = %s",
                            (class_id,))
    }
    accounts = {
        row["username"]: row
        for row in db.query(
            "SELECT id, username, display_name, role, disabled_at FROM app_account")
    }
    seen: dict[str, int] = {}
    results: list[dict] = []
    made: list[dict] = []
    added = created = 0

    for row in rows:
        cells = {key: _text(value) for key, value in row.cells.items()}
        username = cells.get("username", "").lower()
        display_name = cells.get("display_name", "")
        values = {"username": username, "display_name": display_name}
        account = accounts.get(username)

        problem = _username_problem(username)
        if not problem and username in seen:
            problem = f"文件里第 {seen[username]} 行已经有了同一个学号"
        if not problem and account and account["role"] != "student":
            problem = f"这个账号是{auth.ROLE_LABELS.get(account['role'], account['role'])}，不是学生"
        if not problem and account and account["disabled_at"]:
            problem = "这个账号已停用，先恢复再拉进班"
        if not problem and account and account["id"] in enrolled:
            problem = "skip:已经在班里"
        if not problem and not account:
            if not payload.create_missing:
                problem = "账号不存在（这次没有勾选自动建号）"
            elif not display_name:
                problem = "库里没有这个学号，又没填姓名，建不了账号"
            elif len(display_name) > 60:
                problem = "姓名太长（上限 60 字）"
            elif cells.get("password"):
                problem = auth.password_problem(cells["password"]) or ""
        if problem:
            status = "skip" if problem.startswith("skip:") else "error"
            reason = problem[5:].strip() if status == "skip" else problem
            results.append(_result(row.line, status, "add", values, reason))
            continue
        seen[username] = row.line

        if payload.dry_run:
            if account:
                results.append(_result(row.line, "ok", "add", values, "将加入这个班"))
            else:
                results.append(_result(row.line, "ok", "add", values,
                                       "将新建学生账号并加入（首次登录要改密码）",
                                       will_create_account=True))
            continue

        password = ""
        if account:
            student_id = account["id"]
        else:
            password = cells.get("password") or _new_password()
            student_id = _create_account(username, display_name, "student", password,
                                         must_change=not cells.get("password"))
            accounts[username] = {"id": student_id, "username": username,
                                  "display_name": display_name, "role": "student",
                                  "disabled_at": None}
            created += 1
            made.append({"line": row.line, "username": username,
                         "display_name": display_name, "password": password})
        db.execute(
            """INSERT INTO enrollment (class_id, student_id, added_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (class_id, student_id) DO NOTHING""",
            (class_id, student_id, actor.id or None))
        enrolled.add(student_id)
        added += 1
        results.append(_result(
            row.line, "ok", "add", values,
            "已建账号并加入" if password else "已加入",
            id=student_id, password=password or None,
            created=bool(password)))

    if not payload.dry_run:
        _audit(actor, "enrollment.import", "teaching_class", class_id, {
            "rows": len(rows), "added": added, "created_accounts": created,
            "class_name": klass["name"], "course_id": klass["course_id"],
            "students": [row["username"] for row in made[:50]],
        })
    body = _finish(results, payload.dry_run, "students")
    body["class_id"] = class_id
    body["class_name"] = klass["name"]
    body["course_title"] = klass["course_title"]
    body["added"] = added
    body["created_accounts"] = created
    body["passwords"] = made
    return body
