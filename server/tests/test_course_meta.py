"""课程改名与改归类的单元测试（假库，不连数据库）。

钉四件事：

1. 改名称/改方向只在真的变了时才写库——点一次保存就写一条审计，
   全是"改成一样的值"的噪声，等于没有审计；
2. 审计里记的是改前改后的值，不是只记"改过"；
3. 不认识的方向要被挡住（否则课程中心会多出一个空分组，课就"消失"了）；
4. 教师不能改别人导入的课，管理员可以；无主的旧课教师可以改。

真库上的 e2e 在 e2e_api.py 里另跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import course_meta  # noqa: E402
from app.auth import Actor  # noqa: E402
from app.course_meta import CourseMetaIn, patch_course  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeDB:
    """一个只有一行 course 的库：查询返回它，写语句被记下来。"""

    def __init__(self, title: str = "交换代数", direction: str | None = None,
                 created_by: str | None = None) -> None:
        self.row = {"id": "algebra-exchange", "title": title,
                    "direction": direction, "created_by": created_by}
        self.statements: list[tuple[str, tuple | None]] = []

    def query_one(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM course WHERE id" in sql:
            return dict(self.row)
        return None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.strip().startswith("UPDATE course SET"):
            # 让后续的 _shape() 读到新值，模拟库真的更新了
            for name in ("title", "direction"):
                if f"{name} = %s" in sql:
                    index = [part.split(" = ")[0] for part in sql.split("SET ")[1].split(",")].index(name.strip())
                    self.row[name] = params[index]
            return 1
        return 1

    def update_params(self):
        for sql, params in self.statements:
            if sql.strip().startswith("UPDATE course SET"):
                return sql, params
        return None, None

    def audit(self):
        for sql, params in self.statements:
            if "INSERT INTO audit_log" in sql:
                return sql, params
        return None, None


def run(db: FakeDB, body: CourseMetaIn, actor: Actor):
    course_meta.db = db
    return patch_course("algebra-exchange", body, actor)


TEACHER = Actor(id="teacher-1", roles=frozenset({"teacher"}), source="session")
ADMIN = Actor(id="admin-1", roles=frozenset({"admin"}), source="session")
OTHER = Actor(id="teacher-2", roles=frozenset({"teacher"}), source="session")

print("改名：")
db = FakeDB()
result = run(db, CourseMetaIn(title=" 交换代数导论 "), TEACHER)
sql, params = db.update_params()
check("改名写进 UPDATE course", sql is not None and "title = %s" in sql, str(sql))
check("名字两边空格被去掉", db.row["title"] == "交换代数导论", db.row["title"])
check("返回里带改后的课程", result["course"]["title"] == "交换代数导论", result["course"]["title"])
check("返回里说明改了哪一项", result["changed"] == ["title"], str(result["changed"]))
audit_sql, audit_params = db.audit()
detail = json.loads(audit_params[2]) if audit_params else {}
check("审计记了改前改后", detail.get("title") == {"from": "交换代数", "to": "交换代数导论"}, str(detail))
check("审计动作名是 course.patch", "'course.patch'" in (audit_sql or ""), str(audit_sql))

print("\n改归类：")
db = FakeDB(direction=None)
result = run(db, CourseMetaIn(direction="algebra"), TEACHER)
check("方向写进 UPDATE course", db.row["direction"] == "algebra", str(db.row["direction"]))
check("返回里带方向名", result["course"]["direction_label"] == "代数与数论",
      result["course"]["direction_label"])
check("审计记了方向从无到有", json.loads(db.audit()[1][2])["direction"] == {"from": None, "to": "algebra"})

db = FakeDB(direction="algebra")
result = run(db, CourseMetaIn(direction=""), TEACHER)
check("清空方向 = 待归类", db.row["direction"] is None, str(db.row["direction"]))
check("待归类的名字是「待归类」", result["course"]["direction_label"] == "待归类",
      result["course"]["direction_label"])

print("\n什么都没变：")
db = FakeDB(title="交换代数", direction="algebra")
result = run(db, CourseMetaIn(title="交换代数", direction="algebra"), TEACHER)
check("同值不写库", db.update_params()[0] is None, str(db.update_params()[0]))
check("同值不写审计", db.audit()[0] is None)
check("返回 changed 为空", result["changed"] == [], str(result["changed"]))

print("\n挡住的写法：")
try:
    run(FakeDB(), CourseMetaIn(title="   "), TEACHER)
    check("空名字被挡住", False, "没有抛错")
except HTTPException as exc:
    check("空名字被挡住", exc.status_code == 400, exc.detail)

try:
    run(FakeDB(), CourseMetaIn(direction="math"), TEACHER)
    check("不认识的方向被挡住", False, "没有抛错")
except HTTPException as exc:
    check("不认识的方向被挡住", exc.status_code == 400, exc.detail[:40])
    check("报错里列出可选方向", "analysis" in exc.detail and "computing" in exc.detail)

print("\n谁能改：")
try:
    run(FakeDB(created_by="teacher-9"), CourseMetaIn(title="新名字"), TEACHER)
    check("别人导入的课改不了", False, "居然改了")
except HTTPException as exc:
    check("别人导入的课改不了", exc.status_code == 403, exc.detail[:40])

db = FakeDB(created_by="teacher-9")
run(db, CourseMetaIn(title="新名字"), ADMIN)
check("管理员可以改任何课", db.row["title"] == "新名字", db.row["title"])

db = FakeDB(created_by=None)
run(db, CourseMetaIn(title="新名字"), OTHER)
check("无主的旧课教师可以改", db.row["title"] == "新名字", db.row["title"])

db = FakeDB(created_by="teacher-1")
run(db, CourseMetaIn(title="新名字"), TEACHER)
check("自己导入的课可以改", db.row["title"] == "新名字", db.row["title"])

print("\n课程不存在：")
try:
    course_meta.db = FakeDB()
    course_meta.db.query_one = lambda sql, params=None: None
    patch_course("nope", CourseMetaIn(title="x"), TEACHER)
    check("不存在的课返回 404", False, "没有抛错")
except HTTPException as exc:
    check("不存在的课返回 404", exc.status_code == 404, exc.detail)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("课程改名与归类测试全部通过。")
