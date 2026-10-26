"""学科分类接口的测试（假库，不连数据库）。

分类原来是常量：前端 js/course-catalog.js 六组、后端 course_meta.DIRECTIONS 六条，
多加一档就得两边都改、再部署。现在它是 course_direction 表里的数据，
管理员在课程中心点「＋ 新建分类」就能加。

钉四件事：
  1. 清单接口按 ord 排，前端靠顺序编号 01、02……；
  2. 新分类的 id 由名称算出，同一个名字永远同一个 id（重复提交被拦住）；
  3. 只有管理员能建分类；
  4. 课程归类时，库里新加的方向必须被接受——否则加了分类也没法把课放进去。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import course_meta  # noqa: E402
from app.auth import Actor  # noqa: E402
from app.course_meta import (  # noqa: E402
    CourseMetaIn, DirectionIn, create_direction, delete_direction, list_directions,
    patch_course,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeDB:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows if rows is not None else [])
        self.course_count: dict[str, int] = {}   # direction id → 有几门课在用
        self.statements: list[tuple[str, tuple | None]] = []

    def query(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM course_direction" in sql:
            return sorted(self.rows, key=lambda row: (row["ord"], row["id"]))
        return []

    def query_one(self, sql, params=None):
        self.statements.append((sql, params))
        if "max(ord)" in sql:
            return {"next": (max((row["ord"] for row in self.rows), default=0) + 1)}
        if "FROM course_direction WHERE id" in sql:
            match = [row for row in self.rows if row["id"] == params[0]]
            return dict(match[0]) if match else None
        if "count(*) AS n FROM course" in sql:
            return {"n": self.course_count.get(params[0], 0)}
        return None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.strip().startswith("INSERT INTO course_direction"):
            self.rows.append({"id": params[0], "label": params[1],
                              "description": params[2], "ord": params[3]})
        if sql.strip().startswith("DELETE FROM course_direction"):
            self.rows = [row for row in self.rows if row["id"] != params[0]]
        return 1


def seed() -> list[dict]:
    return [
        {"id": "analysis", "label": "分析", "description": "", "ord": 1},
        {"id": "algebra", "label": "代数与数论", "description": "", "ord": 2},
        {"id": "geometry", "label": "几何与拓扑", "description": "", "ord": 3},
        {"id": "pde", "label": "微分方程", "description": "", "ord": 4},
        {"id": "probability", "label": "概率统计", "description": "", "ord": 5},
        {"id": "computing", "label": "计算与应用", "description": "", "ord": 6},
    ]


def admin() -> Actor:
    return Actor(id="acc-admin", display_name="管理员", roles=frozenset({"admin"}))


original = course_meta.db
try:
    print("清单接口：")
    db = FakeDB(seed())
    course_meta.db = db
    items = list_directions()["items"]
    check("返回六条", len(items) == 6, str(len(items)))
    check("按 ord 排，编号从 1 开始",
          [item["number"] for item in items] == [1, 2, 3, 4, 5, 6])
    check("带上中文名", items[0]["label"] == "分析", items[0]["label"])

    print("\n新建分类：")
    db = FakeDB(seed())
    course_meta.db = db
    created = create_direction(DirectionIn(label="数理逻辑"), admin())
    check("返回 id 与名称", created["label"] == "数理逻辑" and created["id"].startswith("dir-"),
          str(created))
    check("排到最后", db.rows[-1]["ord"] == 7, str(db.rows[-1]["ord"]))
    check("写了审计", any("direction.create" in sql for sql, _ in db.statements))
    check("审计记了名称", any("数理逻辑" in str(params) for _, params in db.statements))

    # 同一个名字永远算出同一个 id：换一个干净的库再建一次，id 必须一致
    other = FakeDB(seed())
    course_meta.db = other
    second = create_direction(DirectionIn(label="  数理逻辑  "), admin())
    check("同名（带空格）算出同一个 id", second["id"] == created["id"],
          f'{second["id"]} / {created["id"]}')
    course_meta.db = db

    try:
        create_direction(DirectionIn(label="数理逻辑"), admin())
        check("同名重复创建被拒绝", False, "居然建成了")
    except HTTPException as exc:
        check("同名重复创建被拒绝", exc.status_code == 400 and "已经有" in exc.detail, exc.detail)

    try:
        create_direction(DirectionIn(label="   "), admin())
        check("空白名称被拒绝", False, "居然建成了")
    except HTTPException as exc:
        check("空白名称被拒绝", exc.status_code == 400, exc.detail)

    print("\n保留名：")
    db = FakeDB(seed())
    course_meta.db = db
    for reserved in ("待归类", "新导入课程（待归类）"):
        try:
            create_direction(DirectionIn(label=reserved), admin())
            check(f"「{reserved}」不能当分类名", False, "居然建成了")
        except HTTPException as exc:
            check(f"「{reserved}」不能当分类名", exc.status_code == 400 and "内置" in exc.detail,
                  exc.detail[:40])

    print("\n删除分类：")
    db = FakeDB(seed())
    course_meta.db = db
    try:
        delete_direction("no-such", False, admin())
        check("删不存在的分类返回 404", False, "居然删掉了")
    except HTTPException as exc:
        check("删不存在的分类返回 404", exc.status_code == 404, exc.detail)

    # 有人用着：先 409 报课数，带 force 才连课一起改
    db = FakeDB(seed())
    db.course_count["pde"] = 3
    course_meta.db = db
    try:
        delete_direction("pde", False, admin())
        check("有课在用 → 409 并报课数", False, "居然直接删了")
    except HTTPException as exc:
        check("有课在用 → 409 并报课数",
              exc.status_code == 409 and exc.detail.get("courses") == 3, str(exc.detail))
    check("409 时没有动分类", any(row["id"] == "pde" for row in db.rows), "")

    result = delete_direction("pde", True, admin())
    check("force 后分类真的删了", not any(row["id"] == "pde" for row in db.rows), "")
    check("force 后报告搬走了几门课", result["courses_moved"] == 3, str(result["courses_moved"]))
    check("课被改成待归类而不是删掉",
          any("UPDATE course SET direction = NULL" in sql for sql, _ in db.statements), "")
    check("写了删除审计", any("direction.delete" in sql for sql, _ in db.statements), "")

    # 没人用：直接删
    db = FakeDB(seed())
    course_meta.db = db
    result = delete_direction("computing", False, admin())
    check("没人用就能直接删", result["label"] == "计算与应用", str(result))
    check("没课时不写 UPDATE course",
          not any("UPDATE course SET direction = NULL" in sql for sql, _ in db.statements), "")

    print("\n权限：")
    # 权限挂在依赖注入上（Depends(require("admin"))），直接调函数是绕过它的。
    # 所以要对着路由声明断言，而不是"调用一下看看 403"。
    source = inspect.getsource(create_direction)
    check("建分类要管理员", 'require("admin")' in source, "缺了 Depends(require(\"admin\"))")
    check("清单接口不需要登录", "Depends" not in inspect.getsource(list_directions), "清单不该要身份")
    check("删分类要管理员", 'require("admin")' in inspect.getsource(delete_direction), "")

    print("\n课程改用新分类：")
    # 直接调底层校验：patch_course 依赖注入的 actor 是普通参数，可以直接传
    db = FakeDB(seed())
    course_meta.db = db
    original_current = course_meta._current
    course_state = {"id": "course-logic", "title": "数理逻辑导论",
                    "direction": None, "created_by": "acc-admin"}
    # 假库不真的执行 UPDATE，所以这里让 _current 读一个可变字典；
    # "写没写进去"靠下面断言语句参数，"回读成什么"靠把字典改成已更新的状态。
    course_meta._current = lambda course_id: dict(course_state)
    try:
        created = create_direction(DirectionIn(label="数理逻辑"), admin())
        result = patch_course("course-logic", CourseMetaIn(direction=created["id"]), admin())
        check("库里的新方向被接受", result["changed"] == ["direction"], str(result["changed"]))
        check("写库用的是新方向的 id",
              any("UPDATE course SET" in sql and created["id"] in str(params)
                  for sql, params in db.statements), "")
        course_state["direction"] = created["id"]
        shaped = course_meta._shape("course-logic")
        check("回读的标签是中文名", shaped["direction_label"] == "数理逻辑",
              shaped["direction_label"])

        try:
            patch_course("course-logic", CourseMetaIn(direction="nope"), admin())
            check("不认识的方向仍被拒绝", False, "居然通过了")
        except HTTPException as exc:
            check("不认识的方向仍被拒绝", exc.status_code == 400, exc.detail)
    finally:
        course_meta._current = original_current
finally:
    course_meta.db = original

print("")
if failures:
    print(f"学科分类测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("学科分类测试全部通过。")
