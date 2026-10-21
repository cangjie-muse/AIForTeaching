"""把 db/seed.sql 导入数据库。

为什么单独一个脚本：`npm run verify:db` 只做**静态**检查（解析 SQL 文本、数行数），
从来没有把这份 12 MB、14935 条 INSERT 的脚本真正执行过。
静态检查发现不了语法错误、约束冲突、类型不匹配——那些只会在服务器上第一次导入时炸出来。

用法：
    python scripts/import_seed.py db/seed.sql
    python scripts/import_seed.py db/seed.sql --truncate   # 先清空业务表（测试库专用）

连接串取 DATABASE_URL。整个文件在一个事务里执行：中途失败就整体回滚，
不会留下"导了一半"的库——那比没导更糟，因为看起来是好的。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

# 顺序要照顾外键：先删子表
TABLES = [
    "exercise_node", "exercise", "knowledge_node", "chapter",
    "textbook_chunk", "textbook", "course_asset", "course",
]


def counts(conn) -> dict[str, int]:
    result: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in TABLES:
            try:
                cur.execute(f"SELECT count(*) FROM {table}")
                result[table] = cur.fetchone()[0]
            except Exception:
                conn.rollback()
                result[table] = -1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 seed.sql")
    parser.add_argument("path", nargs="?", default=str(ROOT / "db" / "seed.sql"))
    parser.add_argument("--truncate", action="store_true", help="导入前清空业务表（仅测试库）")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("没有 DATABASE_URL", file=sys.stderr)
        return 1

    path = Path(args.path)
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 1

    sql = path.read_text(encoding="utf-8")
    size_mb = len(sql.encode("utf-8")) / 1024 / 1024

    import psycopg

    with psycopg.connect(url) as conn:
        if args.truncate:
            # 班级与选课挂在 course(id) 上，TRUNCATE ... CASCADE 会连它们一起清掉。
            # 这不是我们想悄悄发生的事：先数出来、说清楚，再清。
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM teaching_class")
                classes = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM enrollment")
                seats = cur.fetchone()[0]
            if classes or seats:
                print(f"注意：清空课程会连带删除 {classes} 个班级、{seats} 条选课记录"
                      "（外键 CASCADE）。学习记录与账号不受影响。")
            with conn.cursor() as cur:
                cur.execute("TRUNCATE " + ", ".join(TABLES) + " CASCADE")
            conn.commit()
            print("已清空业务表")

        print(f"导入 {path.name}（{size_mb:.1f} MB，{sql.count('INSERT INTO')} 条 INSERT）…")
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

        result = counts(conn)

    for table, number in result.items():
        print(f"  {table:<16} {number:>6} 行")
    print("导入完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
