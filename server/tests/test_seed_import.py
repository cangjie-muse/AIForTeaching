"""真实导入 db/seed.sql 之后再检查内容。

这是 `npm run verify:db` 补不上的那一半：静态检查只能证明 SQL 文本长得像 SQL，
证明不了它能被执行。这里跑在真的 PostgreSQL（PGlite）上，导入完成后逐项核对。

断言分两类：
  1. 结构性——行数、外键、枚举取值，错了说明导入或生成脚本有缺陷；
  2. 内容质量——溯源与页码的覆盖率，错了说明出处解析退化了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from import_seed import main as import_seed_main  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def main() -> int:
    print("导入 seed.sql：")
    if import_seed_main() != 0:
        print("导入失败，后续检查没有意义。")
        return 1

    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        def scalar(sql: str, params: tuple = ()):
            cur.execute(sql, params)
            return cur.fetchone()[0]

        print("\n结构：")
        check("课程 14 门", scalar("SELECT count(*) FROM course") == 14,
              str(scalar("SELECT count(*) FROM course")))
        check("习题 4807 道", scalar("SELECT count(*) FROM exercise") == 4807,
              str(scalar("SELECT count(*) FROM exercise")))
        check("知识点 4485 个", scalar("SELECT count(*) FROM knowledge_node") == 4485,
              str(scalar("SELECT count(*) FROM knowledge_node")))
        check("题目挂载关系非空", scalar("SELECT count(*) FROM exercise_node") > 5000)

        # 外键完整性：导入顺序错了会在这里暴露成孤儿行
        orphans = scalar("""
            SELECT count(*) FROM exercise_node n
            LEFT JOIN exercise e ON e.course_id = n.course_id AND e.id = n.exercise_id
            WHERE e.id IS NULL
        """)
        check("没有孤儿挂载", orphans == 0, f"{orphans} 条")

        bad_chapter = scalar("""
            SELECT count(*) FROM exercise e
            LEFT JOIN chapter c ON c.course_id = e.course_id AND c.id = e.chapter_id
            WHERE c.id IS NULL
        """)
        check("题目都在存在的章下", bad_chapter == 0, f"{bad_chapter} 条")

        # 枚举值写错时导入会失败，这里再确认分布是预期的四类
        cur.execute("SELECT provenance, count(*) FROM exercise GROUP BY 1 ORDER BY 2 DESC")
        dist = dict(cur.fetchall())
        check("溯源只出现四种取值", set(dist) == {"textbook", "colleague-export", "self-authored", "unknown"},
              str(dist))
        check("教材原题最多", dist.get("textbook", 0) > 3000, str(dist.get("textbook")))
        check("无法判定控制在 400 以内", dist.get("unknown", 0) <= 400, str(dist.get("unknown")))

        print("\n内容质量（出处解析）：")
        page = scalar("SELECT count(*) FROM exercise WHERE source_page IS NOT NULL")
        pdf_page = scalar("SELECT count(*) FROM exercise WHERE source_pdf_page IS NOT NULL")
        no = scalar("SELECT count(*) FROM exercise WHERE source_no IS NOT NULL")
        check("书页覆盖率 ≥ 70%", page >= 3300, f"{page}/4807")
        check("PDF 页覆盖率 ≥ 75%", pdf_page >= 3600, f"{pdf_page}/4807")
        check("题号覆盖率 ≥ 90%", no >= 4300, f"{no}/4807")

        print("\n质量守恒：")
        # 溯源是从出处文本推出来的，所以要反查一致性：判成自拟的题，出处里
        # 必须真的写了"自拟"；判成导出表的，必须真的提到"导出"。
        # 反过来"自拟题不该有页码"不成立——自拟题经常引用教材某页作为知识依据。
        fake_self = scalar("""
            SELECT count(*) FROM exercise
            WHERE provenance = 'self-authored'
              AND source_raw NOT LIKE %s AND source_raw NOT LIKE %s
        """, ("%自拟%", "%不适用%"))
        check("自拟题的出处确实写了自拟", fake_self == 0, f"{fake_self} 条")

        fake_export = scalar("""
            SELECT count(*) FROM exercise
            WHERE provenance = 'colleague-export' AND source_raw NOT LIKE %s
        """, ("%导出%",))
        check("导出表的出处确实写了导出", fake_export == 0, f"{fake_export} 条")

        # 待核的题必须停在 draft，不能显示成已核
        # 查询串里不能直接写 '%待核%'：psycopg 会把 % 当成参数占位符解析
        leaked = scalar("""
            SELECT count(*) FROM exercise
            WHERE source_raw LIKE %s AND review_status <> 'draft'
        """, ("%待核%",))
        check("带待核标记的题都停在 draft", leaked == 0, f"{leaked} 条")

        empty_prompt = scalar("SELECT count(*) FROM exercise WHERE coalesce(prompt_md,'') = ''")
        check("没有空题面", empty_prompt == 0, f"{empty_prompt} 条")

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("seed.sql 导入与内容检查全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
