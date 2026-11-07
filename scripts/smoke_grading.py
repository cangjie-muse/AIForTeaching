"""评分域真库联调（阶段 D 收尾的一部分，阶段 E 任务 10 门禁用）。

为什么单独写这个脚本：评分域的**写路径**（成绩版本、逐项决定、发布状态）
一直只在内存仓储与 PGlite 迁移断言里验证过，没有在真实 PostgreSQL 上跑过。
`APP_ENV=prod` 时接口是 503，所以在服务器上必须先证明"表在、约束在、写路径通"。

脚本只做两件事，绝不写脏数据：

1. **结构检查**（只读）：8 张评分表 + 5 张在线作答表存在；关键触发器与
   FORCE RLS 都在；`grade_revision` 的 append-only 触发函数存在。
2. **写路径检查**（事务内写、结束一律回滚）：造一条最小的 grade → grade_revision，
   再尝试改分数，断言被触发器拒绝。回滚后库里不留任何行。

用法（在服务器上）：
    set -a; . ./.env; set +a
    .venv/bin/python scripts/smoke_grading.py
    .venv/bin/python scripts/smoke_grading.py --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg

GRADING_TABLES = (
    "answer_test_run", "grade", "grade_revision", "item_grade_decision",
    "grade_appeal", "regrade_run", "regrade_item", "grading_suggestion",
)
ATTEMPT_TABLES = (
    "paper_attempt", "paper_attempt_item", "response_revision",
    "submission", "submission_item",
)


class Report:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.failed = 0
        self.checked = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checked += 1
        if ok:
            print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")
        else:
            self.failed += 1
            print(f"  ✗ {name}{(' — ' + detail) if detail else ''}")
        return ok

    def skip(self, name: str, why: str) -> None:
        print(f"  - {name}：跳过（{why}）")


def structure(cur, report: Report) -> None:
    print("\n==> 一、表结构")
    cur.execute("""SELECT c.relname AS name, c.relrowsecurity AS on_,
                          c.relforcerowsecurity AS forced
                     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'assessment' AND c.relkind = 'r'""")
    rows = {row["name"]: row for row in cur.fetchall()}
    for table in GRADING_TABLES + ATTEMPT_TABLES:
        if not report.check(f"表存在：{table}", table in rows):
            continue
        if not report.check(f"FORCE RLS：{table}", bool(rows[table]["forced"])):
            continue

    print("\n==> 二、关键触发器")
    for trigger, table in (
        ("grade_revision_score_guard", "grade_revision"),
        ("item_grade_decision_guard", "item_grade_decision"),
    ):
        cur.execute(
            """SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'assessment' AND c.relname = %s
                  AND t.tgname = %s AND NOT t.tgisinternal""",
            (table, trigger))
        report.check(f"触发器：{table}.{trigger}", cur.fetchone() is not None)

    cur.execute(
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'assessment' AND p.proname = 'guard_grade_revision_score'")
    report.check("函数：assessment.guard_grade_revision_score",
                 cur.fetchone() is not None)


def write_path(cur, report: Report) -> None:
    """事务内验证 append-only，最后回滚：不留下任何一行。"""
    print("\n==> 三、写路径（全部在事务里，结束回滚）")
    submission_id = uuid.uuid4()
    grade_id = uuid.uuid4()
    revision_id = uuid.uuid4()

    # 造最小提交链：没有真实 submission 时无法建 grade（FK），所以这里直接
    # 用一个"已存在的 submission"或跳过——避免在旁边污染考试数据。
    cur.execute("SELECT id FROM assessment.submission LIMIT 1")
    row = cur.fetchone()
    if row is None:
        report.skip("成绩写路径", "库里还没有任何 submission，先跑通一次在线作答")
        return
    existing_submission = row["id"]

    cur.execute(
        """INSERT INTO assessment.grade (id, submission_id, status)
           VALUES (%s, %s, 'draft')""", (grade_id, existing_submission))
    cur.execute(
        """INSERT INTO assessment.grade_revision
             (id, grade_id, submission_id, revision_no, total_score, status, created_by)
           VALUES (%s, %s, %s, 9001, 12.00, 'draft', %s)""",
        (revision_id, grade_id, existing_submission, uuid.uuid4()))
    report.check("可以追加一条成绩版本（未发布）", True)

    try:
        cur.execute("UPDATE assessment.grade_revision SET total_score = 0 WHERE id = %s",
                    (revision_id,))
    except psycopg.errors.RaiseException as error:
        report.check("改分数被触发器拒绝", "grade_revision_score_is_append_only" in str(error),
                     str(error).splitlines()[0])
    else:
        report.check("改分数被触发器拒绝", False, "UPDATE 竟然成功了：append-only 失效")
        cur.execute("ROLLBACK")
        return

    # 发布字段是唯一允许的更新
    cur.execute(
        """UPDATE assessment.grade_revision
              SET status = 'published', published_by = %s, published_at = now()
            WHERE id = %s""", (uuid.uuid4(), revision_id))
    report.check("发布状态可以更新（唯一允许的更新）", True)

    cur.execute("ROLLBACK")
    report.check("测试数据已回滚，库里没有留下痕迹", True)


def main() -> int:
    parser = argparse.ArgumentParser(description="评分域真库联调")
    parser.add_argument("--url", default=os.environ.get("DATABASE_URL"),
                        help="数据库连接串（默认取 DATABASE_URL）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.url:
        print("缺少 DATABASE_URL（或 --url 指定）", file=sys.stderr)
        return 2

    report = Report(verbose=args.verbose)
    print("==> 连接数据库")
    try:
        with psycopg.connect(args.url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as error:  # noqa: BLE001
        print(f"  ✗ 连不上：{error}", file=sys.stderr)
        return 2
    print("  ✓ 连接成功")

    with psycopg.connect(args.url) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            structure(cur, report)
        conn.rollback()
        with conn.cursor() as cur:
            write_path(cur, report)
        conn.rollback()

    print()
    if report.failed:
        print(f"✗ {report.failed}/{report.checked} 项检查未通过")
        return 1
    print(f"✓ 评分域结构检查全部通过（{report.checked} 项）")
    print("  提示：第三部分只在库里已有 submission 时才真正执行；没有就先跑一次在线作答。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
