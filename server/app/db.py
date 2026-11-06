"""数据库连接：一个最小连接池，不引入 ORM。

表结构是手写 SQL（db/schema.sql），查询也直接写 SQL——对这个规模的项目，
ORM 带来的抽象收益低于它带来的调试成本。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from . import envfile
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# 先读仓库根的 .env：systemd 会注入，手动跑脚本不会。
# 不加载的话默认连接串是开发口令，报错却是"密码认证失败"，很容易误判成数据库坏了。
envfile.load()

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
def transaction(*, assessment_service: bool = False,
                course_ids: Iterable[str] | None = None) -> Iterator[Any]:
    """一个事务里做完一批写操作。

    execute() 每条语句都自己提交：整包导入一万道题就是三万余次往返，
    几分钟起步——所以旧代码干脆把题量上限定在 10000。批量导入走这里：
    拿一个 cursor，executemany 写完，退出时一次提交。
    顺带的好处是原子性：中途抛错整包回滚，不会留下半个课程。

    RLS 作用域（assessment 命名空间）：
        paper_version / paper_section / paper_slot / paper_render 都是
        `FORCE ROW LEVEL SECURITY`，非超级用户（包括表 owner）默认一行都读不到、
        也写不进去，除非在**同一个事务**里先设好下面两个会话变量之一：
            SET LOCAL app.assessment_service = 'on'      —— 服务账号，全量
            SELECT set_config('app.course_ids', '...', true) —— 教师/学生的课程范围
        注意是 SET LOCAL，不是 SET：SET 只作用于会话，连接池把连接借给下一个
        请求时作用域还在，等于把 A 课程的数据带给了 B 请求。set_config(..., true)
        与 SET LOCAL 等价，但能走参数占位符——课程 id 来自外部输入，拼进 SQL 就是
        注入面。assessment_service=True 时本函数在事务的第一条语句就设置它。

    这两个参数默认关闭，老调用方（content.py / homework.py）行为不变。
    """
    with pool.connection() as conn, conn.cursor() as cur:
        if assessment_service:
            # 常量，直接写死；不经过字符串拼装，避免"作用域被参数化"变成注入点。
            cur.execute("SET LOCAL app.assessment_service = 'on'")
        if course_ids:
            scope = ",".join(str(course_id).strip() for course_id in course_ids)
            if not scope:
                raise ValueError("course_ids 不能为空")
            cur.execute(
                "SELECT set_config('app.course_ids', %s, true)",
                (scope,),
            )
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
