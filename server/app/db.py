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
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def cursor() -> Iterator[Any]:
    with pool.connection() as conn, conn.cursor() as cur:
        yield cur


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None
