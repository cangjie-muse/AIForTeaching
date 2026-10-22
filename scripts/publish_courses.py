#!/usr/bin/env python3
"""把课程发布出去，让全新部署不再是一座空站。

为什么需要这个脚本：平台是"审核通过才给学生看"的。种子里每门课的
`reviewStatus` 都是「待教师审核」，导入后落成 `review_status='draft'`；
习题同理。前端以数据库为准（`App.applyDbCatalog`），未发布的课会被整门过滤掉，
于是课程列表空着、只剩 36 门规划课，看起来像"部署失败"，实际是"还没人发布"。

默认只打印计划，不写库。要真发布必须显式 `--apply`。

    python scripts/publish_courses.py                      # 看看会做什么
    python scripts/publish_courses.py --apply              # 全部课程 + 机器校验过的习题
    python scripts/publish_courses.py --apply --exercises none   # 只发课程，题留给人审
    python scripts/publish_courses.py --apply --course course-analysis

关于 `--exercises auto`：它把 `draft`/`machine-checked` 的题直接置为 `published`，
**跳过了人工逐题审核**。这会让「内容审核」页里那些题不再有待审状态，也会让
学生看到未经教师确认的题。只在"内部演示 / 首次把站跑起来"时用，
并且如实写进 `audit_log`（actor、数量、说明都在里面）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app import catalog, db  # noqa: E402

BULK_NOTE = "批量发布（未经人工逐题审核）：机器校验通过，用于首次部署把站跑起来"


def find_actor(name: str | None) -> str:
    if name:
        row = db.query_one(
            "SELECT username FROM app_account WHERE username = %s AND disabled_at IS NULL",
            (name.strip().lower(),))
        if not row:
            raise SystemExit(f"没有这个账号或已停用：{name}")
        return row["username"]
    row = db.query_one(
        """SELECT username FROM app_account
            WHERE role = 'admin' AND disabled_at IS NULL
            ORDER BY created_at LIMIT 1""")
    if not row:
        raise SystemExit("库里没有管理员账号，先跑 scripts/create_admin.py")
    return row["username"]


def counts(course_id: str) -> dict:
    return db.query_one(
        """SELECT
             (SELECT count(*) FROM knowledge_node
               WHERE course_id = %s AND review_status <> 'retired') AS nodes,
             (SELECT count(*) FROM exercise
               WHERE course_id = %s AND review_status = 'published') AS exercises,
             (SELECT count(*) FROM exercise
               WHERE course_id = %s
                 AND review_status IN ('draft', 'machine-checked')) AS drafts""",
        (course_id, course_id, course_id))


def courses_to_publish(only: list[str]) -> list[dict]:
    if only:
        rows = []
        for course_id in only:
            row = db.query_one(
                "SELECT id, title, review_status FROM course WHERE id = %s", (course_id,))
            if not row:
                raise SystemExit(f"没有这门课：{course_id}")
            rows.append(row)
        return rows
    return db.query(
        """SELECT id, title, review_status FROM course
            WHERE review_status <> 'published' ORDER BY id""")


def run(apply: bool, courses: list[str], exercises_mode: str, actor: str,
        note: str | None) -> list[dict]:
    """返回每门课做了什么。拆出来是为了能测（不连真库）。"""
    results = []
    for row in courses_to_publish(courses):
        before = counts(row["id"])
        action = {"course_id": row["id"], "title": row["title"],
                  "nodes": before["nodes"], "exercises": before["exercises"],
                  "drafts": before["drafts"], "published": False, "bulk": 0}
        if apply:
            if exercises_mode == "auto" and before["exercises"] == 0 and before["drafts"] > 0:
                db.execute(
                    """UPDATE exercise SET review_status = 'published', updated_at = now()
                        WHERE course_id = %s
                          AND review_status IN ('draft', 'machine-checked')""",
                    (row["id"],))
                db.execute(
                    """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                       VALUES (%s, 'exercise.bulk-publish', 'course', %s, %s::jsonb)""",
                    (actor, row["id"],
                     json.dumps({"exercises": before["drafts"], "note": note or BULK_NOTE},
                                ensure_ascii=False)))
                action["bulk"] = before["drafts"]
            outcome = catalog.publish(row["id"], actor, note or BULK_NOTE)
            action["published"] = True
            action["exercises"] = outcome["exercises"]
        results.append(action)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="发布课程（默认只打印计划）")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只看计划")
    parser.add_argument("--course", action="append", default=[], help="只处理指定课程，可重复")
    parser.add_argument("--exercises", choices=("auto", "none"), default="auto",
                        help="auto=顺带批量发布机器校验过的题；none=只发课程")
    parser.add_argument("--actor", help="审计记录里的操作人（默认第一个管理员）")
    parser.add_argument("--note", help="写进发布记录与审计日志的说明")
    args = parser.parse_args()

    db.pool.open()
    try:
        actor = find_actor(args.actor)
        rows = run(args.apply, args.course, args.exercises, actor, args.note)
    finally:
        db.pool.close()

    if not rows:
        print("没有需要发布的课程（都已经是 published）。")
        return 0

    print(f"{'课程':34} {'知识点':>6} {'已发题':>6} {'草稿题':>6}  结果")
    print("-" * 78)
    for row in rows:
        state = "已发布" if row["published"] else "待发布"
        bulk = f"，批量发布 {row['bulk']} 题" if row["bulk"] else ""
        print(f"{row['course_id']:34} {row['nodes']:>6} {row['exercises']:>6} "
              f"{row['drafts']:>6}  {state}{bulk}")

    if not args.apply:
        print("\n以上是计划。确实要发布就加 --apply（会写 publish_event 与 audit_log）。")
        return 0

    print(f"\n已完成，操作人：{actor}")
    print("刷新浏览器（Ctrl+F5）即可看到课程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
