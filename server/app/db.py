"""数据库连接：一个最小连接池，不引入 ORM。

表结构是手写 SQL（db/schema.sql），查询也直接写 SQL——对这个规模的项目，
ORM 带来的抽象收益低于它带来的调试成本。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://whu:whu_dev_password@127.0.0.1:5432/whu_teaching",
)

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=int(os.environ.get("DB_POOL_MAX", "8")),
    # 关掉客户端自动 prepare：psycopg 在第 5 次执行同一语句后会在服务端建预处理语句，
    # 而连接在多路复用的后端（如测试用的 PGlite socket）上复用同一会话时会撞名，
    # 报 "prepared statement _pg3_0 already exists"。
    # 本项目查询量很小，服务端预处理的收益可以忽略，换来的是行为可预测。
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    open=False,
)


@contextmanager
def cursor() -> Iterator[Any]:
    with pool.connection() as conn, conn.cursor() as cur:
        yield cur


@contextmanager
def transaction() -> Iterator[Any]:
    """一个事务里做完一批写操作。

    execute() 每条语句都自己提交：整包导入一万道题就是三万余次往返，
    几分钟起步——所以旧代码干脆把题量上限定在 10000。批量导入走这里：
    拿一个 cursor，executemany 写完，退出时一次提交。
    顺带的好处是原子性：中途抛错整包回滚，不会留下半个课程。
    """
    with pool.connection() as conn, conn.cursor() as cur:
        yield cur


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        # 没有 RETURNING 的 INSERT/UPDATE/DELETE 不会产生结果集，
        # 直接 fetchall() 会抛 ProgrammingError。写操作本来就不该关心返回行，
        # 这里统一按"空结果"处理，避免调用方每次都要区分查询与写入。
        if cur.description is None:
            return []
        return cur.fetchall()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """执行写操作，返回影响行数。"""
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def query_one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None
