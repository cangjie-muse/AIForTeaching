#!/usr/bin/env python
"""发布脚本的行为（不连库）。

重点验三件事，都是"看起来没事、实际很糟"的那类：
  · 默认（不加 --apply）绝不能写库；
  · --exercises none 时不能偷偷批量发布题目（那等于绕过审核）；
  · `auto` 只在"一道题都没发布"时才批量发布，已经发过的课不重复动。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publish_courses", root / "scripts" / "publish_courses.py")
publish_courses = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish_courses)

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


class FakeDb:
    def __init__(self, course_rows, count_rows):
        self.course_rows = course_rows
        self.count_rows = count_rows      # course_id -> counts dict
        self.writes: list[tuple[str, tuple]] = []

    def query(self, sql, params=()):
        return self.course_rows

    def query_one(self, sql, params=()):
        if "knowledge_node" in sql:
            return self.count_rows[params[0]]
        return {"username": "admin", "id": params[0], "title": "课", "review_status": "draft"}

    def execute(self, sql, params=()):
        self.writes.append((sql, params))
        return 1

    class pool:
        @staticmethod
        def open():
            pass

        @staticmethod
        def close():
            pass


class FakeCatalog:
    def __init__(self):
        self.calls: list[tuple] = []

    def publish(self, course_id, actor, note):
        self.calls.append((course_id, actor, note))
        return {"course_id": course_id, "published": True, "nodes": 10,
                "exercises": 100, "drafts_hidden": 0}


def setup(counts):
    fake_db = FakeDb(
        course_rows=[{"id": "course-a", "title": "A", "review_status": "draft"},
                     {"id": "course-b", "title": "B", "review_status": "draft"}],
        count_rows=counts)
    fake_catalog = FakeCatalog()
    publish_courses.db = fake_db
    publish_courses.catalog = fake_catalog
    return fake_db, fake_catalog


print("默认（不 apply）：一行都不许写")
counts_all = {cid: {"nodes": 10, "exercises": 0, "drafts": 100} for cid in ("course-a", "course-b")}
fake_db, fake_catalog = setup(counts_all)
rows = publish_courses.run(False, [], "auto", "admin", None)
check("两门课都进了计划", len(rows) == 2)
check("没有 UPDATE/INSERT", not fake_db.writes, f"{len(fake_db.writes)} 条写操作")
check("没有调用 publish", not fake_catalog.calls)
check("计划里标明未发布", all(not row["published"] for row in rows))

print("\n--exercises none：只发课程，不动题目")
fake_db, fake_catalog = setup(counts_all)
publish_courses.run(True, [], "none", "admin", None)
updates = [sql for sql, _ in fake_db.writes]
check("没有任何 exercise 的 UPDATE", not any("UPDATE exercise" in sql for sql in updates))
check("两门课都调了 publish", len(fake_catalog.calls) == 2)
check("审计说明必填（有默认文案）", all(note for _, _, note in fake_catalog.calls),
      str(fake_catalog.calls[0][2])[:24] + "…")

print("\nauto：草稿题存在且已发题为 0 时，批量发布并留审计")
fake_db, fake_catalog = setup(counts_all)
publish_courses.run(True, [], "auto", "admin", None)
exercise_updates = [(sql, p) for sql, p in fake_db.writes if "UPDATE exercise" in sql]
check("每门课各一次批量发布", len(exercise_updates) == 2)
check("只动 draft / machine-checked",
      all("review_status IN ('draft', 'machine-checked')" in sql for sql, _ in exercise_updates))
audits = [(sql, p) for sql, p in fake_db.writes if "audit_log" in sql]
check("每次批量发布都写审计", len(audits) == 2)
check("审计里写明未经人工审核", any("未经人工逐题审核" in str(p) for _, p in audits))

print("\nauto：已经有已发布题的课不再批量发布")
counts_partial = {"course-a": {"nodes": 10, "exercises": 50, "drafts": 30},
                  "course-b": {"nodes": 10, "exercises": 0, "drafts": 30}}
fake_db, _ = setup(counts_partial)
publish_courses.run(True, [], "auto", "admin", None)
targets = [p[0] for sql, p in fake_db.writes if "UPDATE exercise" in sql]
check("只处理了没有已发题的 course-b", targets == ["course-b"], str(targets))

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"发布脚本测试全部通过（{checks} 项）。")
