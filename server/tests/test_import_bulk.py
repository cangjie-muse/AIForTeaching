"""一万道题能不能导入的测试（假库，不连数据库）。

事故背景：习题.md 上传时被拦下——「题目超过 10000 道，请拆分资源包」。
一万道题对一门课的题库不算离谱（吉米多维奇一类习题集远不止这个数），
真实原因是当时每条语句各自提交：一万题 = 三万余次往返，导入要跑几分钟。

所以这条测试钉三件事：
  1. 一万两千题能导进去，不再被上限挡住；
  2. 写入是批量的——语句条数与题量无关（否则早晚还得把上限降回去）；
  3. 上限仍然存在，并且是在逐题解析之前就拒绝，报错里说清楚怎么处理。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import content  # noqa: E402
from app.content import CourseMetaIn, ExerciseIn, NodeIn, PackIn, import_pack  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self.batches: list[tuple[str, list]] = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self.rowcount = 1
        return self

    def executemany(self, sql, seq):
        rows = list(seq)
        self.batches.append((sql, rows))
        self.rowcount = len(rows)
        return self


class FakeDB:
    def __init__(self) -> None:
        self.cursor = FakeCursor()

    @contextmanager
    def transaction(self):
        yield self.cursor

    def query_one(self, sql, params=None):
        if "FROM course WHERE id" in sql:
            return None
        return None

    def execute(self, sql, params=None):
        self.cursor.statements.append((sql, params))
        return 1

    def batch_of(self, fragment: str):
        """找出 executemany 里对应的那批数据。"""
        for sql, rows in self.cursor.batches:
            if fragment in sql:
                return rows
        return None


def build_pack(count: int) -> PackIn:
    nodes = [
        NodeIn(id="course-bank", title="习题集", path=["习题集"], level=1),
        NodeIn(id="bank-ch1", title="第一章", path=["习题集", "第一章"], level=2,
               parent_id="course-bank", chapter_id="bank-ch1"),
        NodeIn(id="bank-kp1", title="知识点一", path=["习题集", "第一章", "知识点一"],
               level=3, parent_id="bank-ch1", chapter_id="bank-ch1"),
    ]
    exercises = [
        ExerciseIn(id=f"q-{index:05d}", prompt_md=f"第 {index} 题：证明某个命题。",
                   answer_md="由定义可得。", difficulty=2, source_raw="教材 1.1",
                   node_ids=["bank-kp1"])
        for index in range(count)
    ]
    return PackIn(course=CourseMetaIn(title="习题集", schema_version="1.0"),
                  nodes=nodes, exercises=exercises)


original = content.db
try:
    print("一万两千题：")
    db = FakeDB()
    content.db = db
    result = import_pack("course-bank", build_pack(12000), "acc-teacher")

    check("一万两千题导进去了", result["exercises_upserted"] == 12000, str(result["exercises_upserted"]))
    exercise_rows = db.batch_of("INSERT INTO exercise") or []
    check("题目走 executemany 而不是逐条 execute", len(exercise_rows) == 12000,
          f"{len(exercise_rows)} 行 / {len(db.cursor.batches)} 批")
    check("语句条数与题量无关（少于 12 条）", len(db.cursor.statements) <= 12,
          f"{len(db.cursor.statements)} 条语句")
    check("题目落在知识点所在的那一章", exercise_rows[0][2] == "bank-ch1", str(exercise_rows[0][2]))
    links = db.batch_of("INSERT INTO exercise_node") or []
    check("关联也是批量的", len(links) == 12000, f"{len(links)} 条关联")
    check("旧关联按整包一次删除",
          any("DELETE FROM exercise_node" in sql
              and "ANY(%s)" in sql
              for sql, _ in db.cursor.statements))

    print("\n上限仍然存在，而且不再白解析一遍：")
    original_cap = content.MAX_EXERCISES
    content.MAX_EXERCISES = 5
    try:
        try:
            import_pack("course-bank", build_pack(6), "acc-teacher")
            check("超限被拒绝", False, "居然导进去了")
        except HTTPException as exc:
            check("超限被拒绝", exc.status_code == 400, str(exc.status_code))
            check("报错说清题数与上限", "6" in exc.detail and "5" in exc.detail, exc.detail)
            check("报错给出处理办法", "拆" in exc.detail, exc.detail)
    finally:
        content.MAX_EXERCISES = original_cap

    check("正式上限不低于两万（一万题不能再被挡）", content.MAX_EXERCISES >= 20000,
          str(content.MAX_EXERCISES))
finally:
    content.db = original

print("")
if failures:
    print(f"批量导入测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("批量导入测试全部通过。")
