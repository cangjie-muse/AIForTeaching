#!/usr/bin/env python3
"""把课程中心那份规划清单写进数据库，让目录只有一个数据源。

原来目录同时读两个地方：库里的课，加上 js/course-catalog.js 里硬编码的
36 条规划课（只有标题和方向，没有内容）。两边靠标题猜着合并，于是统计
写 43、实际能打开的只有 33 门。

现在把规划课落成 course 行：

    status = 'planned'          —— 规划中，还没建设
    review_status = 'draft'     —— 学生看不到

已经存在的课不动。跑完之后 js 里那份清单只用来定方向名与主题色。

    .venv/bin/python scripts/seed_planned_courses.py            # 看计划
    .venv/bin/python scripts/seed_planned_courses.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

from _env import ensure_runtime  # noqa: E402

ensure_runtime(Path(__file__).name, ROOT)

from app import db  # noqa: E402

DIRECTION_LABEL = {
    "analysis": "分析",
    "algebra": "代数与数论",
    "geometry": "几何与拓扑",
    "pde": "微分方程",
    "probability": "概率统计",
    "computing": "计算与应用",
}


def planned_rows() -> list[tuple[str, str, str]]:
    """从 course-catalog.js 里读出 (id, title, group) 三元组。"""
    source = (ROOT / "js/course-catalog.js").read_text(encoding="utf-8")
    body = source[source.index("const rows = ["):]
    rows = re.findall(r"\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'", body)
    if not rows:
        raise SystemExit("没从 js/course-catalog.js 读到课程清单，文件结构可能变了")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="把规划课程写进数据库（默认只看计划）")
    parser.add_argument("--apply", action="store_true", help="真的写库")
    args = parser.parse_args()

    db.pool.open()
    try:
        rows = planned_rows()
        existing = {r["id"] for r in db.query("SELECT id FROM course")}
        missing = [(cid, title, group) for cid, title, group in rows if cid not in existing]

        print(f"清单 {len(rows)} 条，库里已有 {len(rows) - len(missing)} 条，缺 {len(missing)} 条：")
        for cid, title, group in missing:
            print(f"   {cid:<34}{title:<14}{DIRECTION_LABEL.get(group, group)}")

        if not missing:
            print("没有要补的。")
            return 0
        if not args.apply:
            print("\n这是计划，没有写库。要写就加 --apply。")
            return 0

        for cid, title, group in missing:
            db.execute(
                """INSERT INTO course (id, title, status, review_status, direction,
                                       description, subject, updated_at)
                   VALUES (%s, %s, 'planned', 'draft', %s, %s, '规划课程', now())
                   ON CONFLICT (id) DO NOTHING""",
                (cid, title, group,
                 f"{title}：规划设计中的课程，尚未建设内容。"))
        total = db.query_one("SELECT count(*) AS n FROM course")["n"]
        db.execute(
            """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
               VALUES ('system', 'course.seed-planned', 'course', 'planned-catalog', %s::jsonb)""",
            (json.dumps({"inserted": len(missing), "total": total,
                         "note": "把 js/course-catalog.js 的规划清单落库，目录只认数据库"},
                        ensure_ascii=False),))
        print(f"\n完成：补入 {len(missing)} 门规划课程，库里现在共 {total} 门。")
        print("刷新课程中心：规划课显示为「规划中」，学生端看不到。")
        return 0
    finally:
        db.pool.close()


if __name__ == "__main__":
    sys.exit(main())
