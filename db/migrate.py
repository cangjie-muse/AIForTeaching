#!/usr/bin/env python3
"""数据库迁移工具：只前进，不改写历史。

用法：
    python db/migrate.py --status                  # 看已应用与待应用
    python db/migrate.py --apply                   # 应用到 DATABASE_URL
    python db/migrate.py --concat                  # 由迁移重建 db/schema.sql
    python db/migrate.py --apply --url postgresql://...

设计取舍：
  * 不用 Alembic。表结构是手写 SQL，且团队只有一个人维护，
    Alembic 的 autogenerate 在中文注释与复合主键上容易产生噪音差异。
  * 每个迁移文件内的 `BEGIN;` / `COMMIT;` 会被剥掉，由本脚本统一包事务，
    避免"文件里的 COMMIT 提前提交一半"这类事故。
  * 已应用的迁移文件内容被改动时直接报错。迁移一旦上过生产就是历史，
    要改就新加一个文件。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
SCHEMA = ROOT / "schema.sql"

HEADER = """-- =============================================================================
-- 智慧武大 · 数学 AI 教学智能体 —— 数据库结构（由 db/migrate.py --concat 生成）
--
-- 不要手改本文件。改 db/migrations/NNNN_*.sql，然后重新生成：
--     python db/migrate.py --concat
--
-- 本文件是**非破坏性**的：只建，不删。要清空开发库请显式执行 db/reset.dev.sql。
-- 三条设计原则见 docs/数据模型与数据库设计.md。
-- =============================================================================
"""


def strip_txn(sql: str) -> str:
    """去掉迁移文件自身的 BEGIN;/COMMIT;，事务交给调用方。"""
    out = []
    for line in sql.splitlines():
        if line.strip().upper() in ("BEGIN;", "COMMIT;"):
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def files() -> list[Path]:
    return sorted(p for p in MIGRATIONS.glob("*.sql") if p.name[:4].isdigit())


def checksum(path: Path) -> str:
    return hashlib.sha256(strip_txn(path.read_text(encoding="utf-8")).encode()).hexdigest()[:16]


def concat() -> int:
    parts = [HEADER, "\nBEGIN;\n"]
    for path in files():
        parts.append(f"\n-- ---------------------------------------------------------------\n")
        parts.append(f"-- {path.name}\n")
        parts.append(f"-- ---------------------------------------------------------------\n")
        parts.append(strip_txn(path.read_text(encoding="utf-8")))
    parts.append("\nCOMMIT;\n")
    SCHEMA.write_text("".join(parts), encoding="utf-8")
    print(f"已生成 {SCHEMA.relative_to(ROOT.parent)}（{len(files())} 个迁移）")
    return 0


def get_conn(url: str):
    try:
        import psycopg
    except ImportError:
        return None
    return psycopg.connect(url)


def run_sql(url: str, sql: str, allow_psql: bool = True) -> None:
    conn = get_conn(url)
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(sql)
        conn.close()
        return
    if allow_psql:
        proc = subprocess.run(
            ["psql", url, "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
            input=sql.encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            sys.exit(proc.stderr.decode(errors="replace"))
        return
    sys.exit("既没有 psycopg 也没有 psql，无法连接数据库。")


BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
  version    text PRIMARY KEY,
  checksum   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def status(url: str | None) -> int:
    applied: dict[str, str] = {}
    if not url:
        print("（未提供 DATABASE_URL，无法查询已应用版本，下面只是文件清单）\n")
    if url:
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(url, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute(BOOTSTRAP)
                conn.commit()
                cur.execute("SELECT version, checksum FROM schema_migration")
                applied = {r["version"]: r["checksum"] for r in cur.fetchall()}
        except Exception as exc:  # 数据库连不上时仍然能看文件层状态
            print(f"（数据库不可用，仅列出文件层状态：{exc}）\n")

    for path in files():
        tag = "已应用" if path.name in applied else "待应用"
        warn = ""
        if path.name in applied and applied[path.name] != checksum(path):
            warn = "  ⚠ 已应用的迁移文件被改动过"
        print(f"  [{tag}] {path.name}{warn}")
    missing = set(applied) - {p.name for p in files()}
    for name in sorted(missing):
        print(f"  [库中存在但文件缺失] {name}")
    return 0


VECTOR_TYPE_RE = re.compile(r"vector\((\d+)\)", re.IGNORECASE)
HNSW_INDEX_RE = re.compile(
    r"^\s*CREATE\s+INDEX[^;]*USING\s+hnsw[^;]*;", re.IGNORECASE | re.MULTILINE
)
VECTOR_EXTENSION_RE = re.compile(
    r"^\s*CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?vector\"?[^;]*;",
    re.IGNORECASE | re.MULTILINE,
)


def pglite_compat(sql: str) -> tuple[str, list[str]]:
    """把 pgvector 相关语句降级，供 PGlite 建同名结构的开发库。

    PGlite（WASM PostgreSQL 16）没有 pgvector，但其他部分是真的 PG。
    降级两处：vector(N) → jsonb、跳过 hnsw 索引；扩展本身也跳过。
    这样接口能用真 PG 语法执行，唯一失真处是向量列类型与索引——
    而 API 层不碰向量运算，所以不影响接口测试的有效性。

    生产绝不能用这个开关：vector(1024) 是检索的基础。
    """
    changes = []
    if VECTOR_EXTENSION_RE.search(sql):
        sql = VECTOR_EXTENSION_RE.sub("-- [dev] 跳过扩展 vector：PGlite 未提供", sql)
        changes.append("跳过 vector 扩展")
    if VECTOR_TYPE_RE.search(sql):
        sql = VECTOR_TYPE_RE.sub("jsonb", sql)
        changes.append("vector(1024) → jsonb")
    if HNSW_INDEX_RE.search(sql):
        sql = HNSW_INDEX_RE.sub("-- [dev] 跳过 hnsw 索引：PGlite 未提供 pgvector", sql)
        changes.append("跳过 hnsw 索引")
    return sql, changes


def apply(url: str, pglite: bool = False) -> int:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
            conn.commit()
            cur.execute("SELECT version, checksum FROM schema_migration")
            applied = {r["version"]: r["checksum"] for r in cur.fetchall()}

        for path in files():
            digest = checksum(path)
            if path.name in applied:
                if applied[path.name] != digest:
                    sys.exit(f"{path.name} 已应用但内容被改动。迁移是历史，请新增文件。")
                continue
            print(f"应用 {path.name} …", end=" ", flush=True)
            sql = strip_txn(path.read_text(encoding="utf-8"))
            if pglite:
                sql, changes = pglite_compat(sql)
                if changes:
                    print(f"（PGlite 兼容：{'、'.join(changes)}）", end=" ")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migration (version, checksum) VALUES (%s, %s)",
                    (path.name, digest),
                )
            conn.commit()
            print("完成")
    print("全部迁移已应用。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库迁移")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--concat", action="store_true")
    ap.add_argument(
        "--pglite-compat",
        action="store_true",
        help="PGlite 开发库兼容：跳过 pgvector 扩展并把 vector(1024) 降级为 jsonb",
    )
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args()

    if args.concat:
        return concat()
    if args.status:
        return status(args.url)
    if args.apply:
        if not args.url:
            sys.exit("缺少 DATABASE_URL（或用 --url 指定）")
        return apply(args.url, args.pglite_compat)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
