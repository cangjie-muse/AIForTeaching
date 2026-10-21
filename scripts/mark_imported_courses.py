"""把"迁移导入"的课程标成「已审已发布」，并留下可追溯的说明。

背景：data/course-*.json 那 14 门课（4807 道题）在建库之前就用于教学了。
按约定，迁移进来的内容不当作"新内容"重新审一遍，直接算已审已发布；但必须
留痕——否则以后有人问"这道题是谁审的"，答案是空白，和真的逐题审过没法区分。

所以这里做三件事：
  1. 课程：review_status='published' + published_at，写 publish_event；
  2. 题目：review_status='published' + human_verified_at/by（"人工已审"的档位）；
  3. 审计：每门课一条 audit_log，写明"迁移导入，未逐题重审"。

幂等：只处理还没发布的行，重复执行不会改任何东西。
用法：python scripts/mark_imported_courses.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app import db  # noqa: E402

ACTOR = "迁移导入"
NOTE = "迁移导入：沿用既有静态包内容，未逐题重审"


def main() -> None:
    courses = db.query(
        """
        SELECT c.id, c.title,
               (SELECT count(*) FROM exercise e WHERE e.course_id = c.id
                 AND e.review_status <> 'retired') AS exercises,
               (SELECT count(*) FROM knowledge_node n WHERE n.course_id = c.id
                 AND n.review_status <> 'retired') AS nodes
          FROM course c
         ORDER BY c.id
        """
    )
    if not courses:
        print("库里没有课程，先导入 db/seed.sql。")
        return

    print(f"库里 {len(courses)} 门课：")
    for row in courses:
        print(f"  {row['id']:<28} 题 {row['exercises']:>4} · 知识点 {row['nodes']:>4}")

    total_ex = 0
    total_nodes = 0
    touched = 0
    for course in courses:
        if course["exercises"] == 0 and course["nodes"] == 0:
            continue
        verified = db.execute(
            """
            UPDATE exercise
               SET review_status = 'published',
                   human_verified_at = coalesce(human_verified_at, now()),
                   human_verified_by = coalesce(human_verified_by, %s)
             WHERE course_id = %s
               AND review_status <> 'retired'
               AND (review_status <> 'published' OR human_verified_at IS NULL)
            """,
            (ACTOR, course["id"]),
        )
        nodes = db.execute(
            """
            UPDATE knowledge_node
               SET review_status = 'published'
             WHERE course_id = %s AND review_status <> 'retired'
               AND review_status <> 'published'
            """,
            (course["id"],),
        )
        published = db.execute(
            """
            UPDATE course
               SET review_status = 'published',
                   published_at = coalesce(published_at, now()),
                   published_by = coalesce(published_by, %s),
                   publish_note = coalesce(publish_note, %s),
                   updated_at = now()
             WHERE id = %s AND review_status <> 'published'
            """,
            (ACTOR, NOTE, course["id"]),
        )
        if not (verified or nodes or published):
            continue
        touched += 1
        total_ex += verified
        total_nodes += nodes
        db.execute(
            """
            INSERT INTO publish_event (course_id, actor, action, nodes, exercises, note)
            VALUES (%s, %s, 'publish', %s, %s, %s)
            """,
            (course["id"], ACTOR, course["nodes"], course["exercises"], NOTE),
        )
        db.execute(
            """
            INSERT INTO audit_log (actor, action, entity, entity_key, detail)
            VALUES (%s, 'course.import-trusted', 'course', %s, %s::jsonb)
            """,
            (ACTOR, course["id"],
             json.dumps({"exercises": verified, "nodes": nodes,
                         "note": NOTE}, ensure_ascii=False)),
        )
        print(f"  ✓ {course['id']}：题 {verified} 道标为已发布/人工已审，知识点 {nodes} 个已发布")

    print(f"\n完成：{touched} 门课有更新，共 {total_ex} 道题、{total_nodes} 个知识点。")


if __name__ == "__main__":
    db.pool.open()
    try:
        main()
    finally:
        db.pool.close()
