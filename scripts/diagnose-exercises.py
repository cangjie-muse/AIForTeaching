#!/usr/bin/env python3
"""核对习题到底在不在：按课程数题、按状态数、看挂没挂到知识点。

为什么要有这个脚本：

    「导入完习题，下午就没了」。这种描述里至少混着三种完全不同的情况：

    1. 库里真有，只是页面按状态/知识点筛掉了（导入的题默认是「草稿」，
       习题库和课程页都可能只显示已发布的）；
    2. 库里有，但没挂到知识点上（锚点对不上）——按知识点看就是 0 道；
    3. 真的被后来的导入覆盖了（整体更新 replace 会把文件里没有的题标成停用）。

    三者的处理完全相反，所以先把事实数出来，别猜。

用法（仓库根目录）：

    .venv/bin/python scripts/diagnose-exercises.py
    .venv/bin/python scripts/diagnose-exercises.py --course analysis

git pull 与数据库无关：更新代码不会删数据。迁移只做加表/加列/加约束。
这个脚本就是用来证明或推翻"数据没了"的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATUS_LABEL = {
    "draft": "草稿",
    "teacher-reviewed": "教师已审",
    "published": "已发布",
    "retired": "已停用",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="核对习题数量与挂载情况")
    parser.add_argument("--course", default=None, help="只查这一门课（课程 id）")
    args = parser.parse_args()

    from app import db

    db.pool.open()

    where, params = ("", ()) if not args.course else ("WHERE c.id = %s", (args.course,))
    try:
        courses = db.query(
            f"""SELECT c.id, c.title,
                   (SELECT count(*) FROM knowledge_node k
                     WHERE k.course_id = c.id AND k.kind = 'knowledge'
                       AND k.review_status <> 'retired') AS nodes,
                   (SELECT count(*) FROM exercise e WHERE e.course_id = c.id) AS total,
                   (SELECT count(*) FROM exercise e WHERE e.course_id = c.id
                      AND e.review_status = 'published') AS published,
                   (SELECT count(*) FROM exercise e WHERE e.course_id = c.id
                      AND NOT EXISTS (SELECT 1 FROM exercise_node en
                                       WHERE en.exercise_id = e.id)) AS unlinked
              FROM course c {where}
             ORDER BY c.id""",
            params,
        )
    except Exception as error:  # noqa: BLE001
        print(f"✗ 读不到课程：{error}")
        return 1

    if not courses:
        print("没有匹配的课程。")
        return 1

    grand_total = grand_unlinked = 0
    print(f"{'课程 id':<34}{'知识点':>7}{'习题':>8}{'已发布':>7}{'没挂载':>8}  ")
    for row in courses:
        grand_total += int(row["total"] or 0)
        grand_unlinked += int(row["unlinked"] or 0)
        print(f"{str(row['id']):<34}{int(row['nodes'] or 0):>7}{int(row['total'] or 0):>8}"
              f"{int(row['published'] or 0):>7}{int(row['unlinked'] or 0):>8}  "
              f"{row.get('title') or ''}")

    print(f"\n合计 {grand_total} 道题，其中 {grand_unlinked} 道没有挂到任何知识点。")

    print("\n按状态：")
    rows = db.query(
        """SELECT e.course_id, e.review_status, count(*) AS n
             FROM exercise e
            GROUP BY e.course_id, e.review_status
            ORDER BY e.course_id, e.review_status"""
    )
    for row in rows:
        if args.course and row["course_id"] != args.course:
            continue
        label = STATUS_LABEL.get(row["review_status"], row["review_status"])
        print(f"  {row['course_id']:<32}{label:<8}{int(row['n']):>8}")

    print("\n怎么读这张表：")
    print("  · 「习题」不为 0 而页面上看不见 → 多半是状态过滤：导入的题默认草稿，")
    print("    课程页/习题库只显示已发布时，数字看着就是 0。")
    print("  · 「没挂载」接近总数 → 习题进去了但锚点对不上知识点，")
    print("    按知识点浏览时一道也看不到；这种要用单文件增量导入重传习题.md，")
    print("    或先在知识点上建好锚点。")
    print("  · 「已停用」很多 → 被后来的整体更新（replace）覆盖过：文件里没有的题")
    print("    会被标记停用，仍在库里可追溯，但学生端不再出现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
