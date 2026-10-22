#!/usr/bin/env python3
"""把课程发布出去，让全新部署不再是一座空站。

为什么需要这个脚本：平台是"审核通过才给学生看"的。种子里每门课的
`reviewStatus` 都是「待教师审核」，导入后落成 `review_status='draft'`；
习题同理。前端以数据库为准（`App.applyDbCatalog`），未发布的课会被整门过滤掉，
于是课程列表空着、只剩 36 门规划课，看起来像"部署失败"，实际是"还没人发布"。

默认只打印计划，不写库。要真发布必须显式 `--apply`。

    python scripts/publish_courses.py                      # 看看会做什么
    python scripts/publish_courses.py --apply                    # 课程 + 机器校验过的题
    python scripts/publish_courses.py --apply --exercises none   # 只发课程，题一道不动
    python scripts/publish_courses.py --apply --exercises auto   # 连待核的 draft 也发（慎用）
    python scripts/publish_courses.py --apply --course course-analysis

发布范围默认只到 `machine-checked`（脚本验过公式与外键），**不碰 `draft`**：
源数据里标了「待核 / 待教师审核」的那些题必须留给人看，替它们盖章等于把审核做成形式。

`--exercises auto` 会连 `draft` 一起发，只在"内部演示、且明确知道后果"时用；
两种情况都会写 `audit_log`（actor、状态范围、说明都在里面），事后查得到。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
from _env import ensure_runtime  # noqa: E402

ensure_runtime(Path(__file__).name, ROOT)

from app import catalog, db  # noqa: E402

BULK_NOTE = "批量发布（未经人工逐题审核）：仅机器校验通过的条目，用于首次部署把站跑起来"


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
               WHERE course_id = %s AND review_status = 'machine-checked') AS machine,
             (SELECT count(*) FROM exercise
               WHERE course_id = %s AND review_status = 'draft') AS drafts""",
        (course_id, course_id, course_id, course_id))


def exc_targets(mode: str, before: dict) -> str:
    """决定批量发布哪些状态的题，返回 SQL 的 IN 列表（含引号）。空串表示一个都不动。

    默认只发 `machine-checked`（脚本验过公式与外键），**不碰 `draft`**：
    那些是源数据里标了「待核 / 待教师审核」的题，替它们盖章就是把审核变成表演。
    """
    if mode == "none":
        return ""
    if mode == "machine":
        return "'machine-checked'"
    return "'machine-checked', 'draft'"


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
                  "machine": before["machine"], "drafts": before["drafts"],
                  "published": False, "bulk": 0, "skipped": ""}
        if apply:
            targets = exc_targets(exercises_mode, before)
            if before["exercises"] == 0 and targets:
                db.execute(
                    f"""UPDATE exercise SET review_status = 'published', updated_at = now()
                         WHERE course_id = %s AND review_status IN ({targets})""",
                    (row["id"],))
                db.execute(
                    """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                       VALUES (%s, 'exercise.bulk-publish', 'course', %s, %s::jsonb)""",
                    (actor, row["id"],
                     json.dumps({"statuses": targets, "note": note or BULK_NOTE},
                                ensure_ascii=False)))
                action["bulk"] = before["machine"] + (
                    before["drafts"] if "draft" in targets else 0)
            try:
                outcome = catalog.publish(row["id"], actor, note or BULK_NOTE)
            except Exception as error:  # HTTPException(400) 等：记下来，继续下一门
                action["skipped"] = str(getattr(error, "detail", error))
                results.append(action)
                continue
            action["published"] = True
            action["exercises"] = outcome["exercises"]
        results.append(action)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="发布课程（默认只打印计划）")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只看计划")
    parser.add_argument("--course", action="append", default=[], help="只处理指定课程，可重复")
    parser.add_argument("--exercises", choices=("machine", "auto", "none"), default="machine",
                        help="machine=只发机器校验通过的题（默认）；none=只发课程；"
                             "auto=连待核的 draft 一起发（等于替老师盖章，慎用）")
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

    print(f"{'课程':34} {'知识点':>6} {'已发题':>6} {'机器校验':>8} {'待核':>5}  结果")
    print("-" * 78)
    skipped = []
    for row in rows:
        if row["skipped"]:
            state = "跳过：" + row["skipped"].split("。")[0]
            skipped.append(row)
        else:
            state = "已发布" if row["published"] else "待发布"
        bulk = f"，批量发布 {row['bulk']} 题" if row["bulk"] else ""
        print(f"{row['course_id']:34} {row['nodes']:>6} {row['exercises']:>6} "
              f"{row['machine']:>8} {row['drafts']:>5}  {state}{bulk}")

    if not args.apply:
        print("\n以上是计划。确实要发布就加 --apply（会写 publish_event 与 audit_log）。")
        return 0

    print(f"\n已完成，操作人：{actor}")
    if skipped:
        ready = [row for row in skipped if row["machine"] == 0]
        other = [row for row in skipped if row["machine"] > 0]
        print(f"\n有 {len(skipped)} 门课没发出去：")
        for row in skipped:
            print(f"  {row['course_id']:32} {row['skipped']}")
        if ready:
            print("\n这些课一道机器校验通过的题都没有，按设计不能发布（学生端会只有知识点）。")
            print("要让它们上线，得先决定那批「待核」的题怎么办。两条路：")
            print("  · 逐题人工过：登录 →「内容审核」→ 通过并发布；")
            print("  · 接受未经人工确认（仅演示）：")
            print("      .venv/bin/python scripts/publish_courses.py --apply --exercises auto \\")
            print("        " + " ".join(f"--course {row['course_id']}" for row in ready))
        if other:
            print("\n其余是发布时报错，看上面的原因。")
    print("\n刷新浏览器（Ctrl+F5）即可看到已发布的课程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
