#!/usr/bin/env python3
"""把一门（或全部）课程的习题一次性标为「人工已审」并发布。

**这个脚本等于替你说"这些题我都看过了"。** 它不检查题目内容，只写状态：

    review_status   = 'teacher-reviewed'（草稿 → 人工已审）
    human_verified_at = now()，human_verified_by = 操作账号
    → 再由 catalog.publish() 把它们放出去（学生可见）

为什么仍然提供：一万道题逐题点不现实，而你需要的可能只是"先把站跑起来"。
但后果必须写清楚，所以：

  · 默认只打印计划，不写库；真执行要显式 --apply；
  · 审计日志留一条 exercise.bulk-approve，写明"未经逐题审阅"；
  · 已经人工审过的题不会被改写（只动 draft / machine-checked）。

用法（仓库根目录）：

    .venv/bin/python scripts/approve-exercises.py                    # 看计划
    .venv/bin/python scripts/approve-exercises.py --course analysis --apply
    .venv/bin/python scripts/approve-exercises.py --all --apply

建议先备份：bash deploy/backup.sh
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

NOTE = "批量标为人工已审（未经逐题人工审阅，用于先把课程跑起来）"


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


def targets(only: list[str], everything: bool) -> list[dict]:
    if everything:
        return db.query(
            """SELECT c.id, c.title,
                      (SELECT count(*) FROM exercise e WHERE e.course_id = c.id
                        AND e.review_status IN ('draft', 'machine-checked')) AS pending,
                      (SELECT count(*) FROM exercise e WHERE e.course_id = c.id) AS total
                 FROM course c ORDER BY c.id""")
    rows = []
    for course_id in only:
        row = db.query_one(
            """SELECT c.id, c.title,
                      (SELECT count(*) FROM exercise e WHERE e.course_id = c.id
                        AND e.review_status IN ('draft', 'machine-checked')) AS pending,
                      (SELECT count(*) FROM exercise e WHERE e.course_id = c.id) AS total
                 FROM course c WHERE c.id = %s""", (course_id,))
        if not row:
            raise SystemExit(f"没有这门课：{course_id}")
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="批量标为人工已审并发布（默认只看计划）")
    parser.add_argument("--course", action="append", default=[], help="只处理指定课程，可重复")
    parser.add_argument("--all", action="store_true", help="处理全部课程")
    parser.add_argument("--apply", action="store_true", help="真的写库；不加只看计划")
    parser.add_argument("--actor", help="审计记录里的操作人（默认第一个管理员）")
    args = parser.parse_args()

    if not args.course and not args.all:
        raise SystemExit("要给范围：--course <课程 id>（可重复）或 --all")

    db.pool.open()
    try:
        actor = find_actor(args.actor)
        rows = targets(args.course, args.all)

        print(f"{'课程':34}{'待审':>7}{'总题数':>8}")
        for row in rows:
            print(f"{row['id']:34}{int(row['pending']):>7}{int(row['total']):>8}  {row['title'] or ''}")
        total = sum(int(row["pending"]) for row in rows)
        print(f"\n会把 {total} 道题标为「人工已审」并发布，操作人：{actor}")
        if not total:
            print("没有需要处理的题。")
            return 0
        if not args.apply:
            print("\n这是计划，没有写库。确实要做就加 --apply。")
            print("提醒：这个动作等于声明这些题你都看过；内容对错不会被检查。")
            return 0

        done = 0
        for row in rows:
            updated = db.execute(
                """UPDATE exercise
                      SET review_status = 'teacher-reviewed',
                          human_verified_at = now(),
                          human_verified_by = %s,
                          updated_at = now()
                    WHERE course_id = %s
                      AND review_status IN ('draft', 'machine-checked')""",
                (actor, row["id"]))
            db.execute(
                """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
                   VALUES (%s, 'exercise.bulk-approve', 'course', %s, %s::jsonb)""",
                (actor, row["id"], json.dumps({"approved": updated, "note": NOTE},
                                              ensure_ascii=False)))
            published = ""
            try:
                outcome = catalog.publish(row["id"], actor, NOTE)
                published = f"，发布后学生可见 {outcome['exercises']} 道"
            except Exception as error:  # noqa: BLE001 —— 发布失败不该中断后面的课
                published = f"，发布失败：{getattr(error, 'detail', error)}"
            done += updated
            print(f"  ✓ {row['id']}：{updated} 道标为人工已审{published}")

        print(f"\n完成，共 {done} 道。刷新页面（Ctrl+F5）核对：")
        print("  审核总表里「可信等级」应为人工已核，「人工审核」列出现时间与账号。")
        return 0
    finally:
        db.pool.close()


if __name__ == "__main__":
    sys.exit(main())
