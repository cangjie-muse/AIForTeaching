"""导入时"课程是新建还是更新"的单元测试（假库，不连数据库）。

要钉住的只有一件事：返回给页面的「新建 / 已存在」必须来自库里的实际状态，
而不是前端那份可能几分钟前的课程列表。另外，老师在导入页填的课程名称要能
写进 course.title——不然填了也没用。

落库语义（retired、批次）在 e2e_api.py 里用真库验证；这里只用假库看"问了什么、
写了什么"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import content  # noqa: E402
from app.content import CourseMetaIn, NodeIn, PackIn, import_pack  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeDB:
    """只回答两件事：这门课在不在、执行了哪些写语句。"""

    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.statements: list[tuple[str, tuple | None]] = []

    def query_one(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM course WHERE id" in sql:
            return {"ok": 1} if self.exists else None
        return None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return 1

    def courses_insert(self):
        """找出写入 course 表的那条语句的参数。"""
        for sql, params in self.statements:
            if "INSERT INTO course" in sql:
                return params
        return None


def pack(title: str = "拓扑学", override: str | None = None) -> PackIn:
    return PackIn(
        course=CourseMetaIn(title=title, schema_version="1.0"),
        nodes=[NodeIn(id="course-topology", title=title, path=[title], level=1)],
        exercises=[],
        title_override=override,
    )


print("导入时课程是新建还是更新：")

original = content.db
try:
    db = FakeDB(exists=False)
    content.db = db
    result = import_pack("course-topology", pack(), "acc-admin")
    check("库里没有这门课 → 报告新建", result["course_created"] is True, str(result["course_created"]))
    check("返回最终课程名", result["course_title"] == "拓扑学", result["course_title"])
    check("写入 course 时用的是包里的名字", (db.courses_insert() or ("",))[1] == "拓扑学")

    db = FakeDB(exists=True)
    content.db = db
    result = import_pack("course-topology", pack(), "acc-admin")
    check("库里已有这门课 → 报告已存在", result["course_created"] is False, str(result["course_created"]))

    db = FakeDB(exists=False)
    content.db = db
    result = import_pack("course-topology", pack(override="点集拓扑"), "acc-admin")
    check("填了课程名称 → 以填的为准",
          db.courses_insert()[1] == "点集拓扑", str(db.courses_insert()[1]))
    check("返回的课程名也是填的那个", result["course_title"] == "点集拓扑", result["course_title"])

    db = FakeDB(exists=True)
    content.db = db
    result = import_pack("course-topology", pack(override="  "), "acc-admin")
    check("名称只有空白 → 退回包里的名字",
          db.courses_insert()[1] == "拓扑学", repr(db.courses_insert()[1]))

    db = FakeDB(exists=True)
    content.db = db
    import_pack("course-topology", pack(), "acc-admin")
    check("查过一次课程是否存在",
          any("FROM course WHERE id" in sql for sql, _ in db.statements))
    check("存在性检查发生在写 course 之前",
          next(i for i, (sql, _) in enumerate(db.statements) if "FROM course WHERE id" in sql)
          < next(i for i, (sql, _) in enumerate(db.statements) if "INSERT INTO course" in sql))
finally:
    content.db = original

print("")
if failures:
    print(f"导入课程状态测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("导入课程状态测试全部通过。")
