"""教学智能体后端 API（最小骨架）。

当前只实现三个只读端点，目的是先让数据库里的内容"活起来"：
    GET /api/v1/health
    GET /api/v1/courses
    GET /api/v1/courses/{course_id}/graph

/graph 返回的 JSON 与前端 GraphModel 需要的结构一致，便于后续把
window.COURSE_DATA 换成 fetch，而不改渲染代码。

完整接口清单见 config/api-contract.json（23 个端点，逐步实现）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="智慧武大 · 数学教学智能体 API", version="0.1.0", lifespan=lifespan)

# 开发期允许本地静态站（8765）与 file:// 之外的来源跨域访问。
# 上线前必须收紧为学院域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://127.0.0.1:8765,http://localhost:8765").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def health() -> dict:
    try:
        row = db.query_one("SELECT count(*) AS n FROM course")
        return {"status": "ok", "db": True, "courses": row["n"] if row else 0}
    except Exception as exc:  # 数据库不可用时仍返回结构化结果，便于探活
        return {"status": "degraded", "db": False, "error": str(exc)}


@app.get("/api/v1/courses")
def list_courses() -> dict:
    rows = db.query(
        """
        SELECT c.id, c.title, c.audience, c.subject, c.status, c.review_status,
               (SELECT count(*) FROM knowledge_node k
                 WHERE k.course_id = c.id AND k.kind = 'knowledge') AS knowledge_count,
               (SELECT count(*) FROM exercise e WHERE e.course_id = c.id) AS exercise_count
        FROM course c
        ORDER BY c.id
        """
    )
    return {"items": rows}


@app.get("/api/v1/courses/{course_id}/graph")
def course_graph(course_id: str) -> dict:
    """节点 + 层级边 + 共题边，结构对齐前端 GraphModel。"""
    course = db.query_one("SELECT id, title FROM course WHERE id = %s", (course_id,))
    if not course:
        raise HTTPException(status_code=404, detail=f"课程不存在：{course_id}")

    nodes = db.query(
        """
        SELECT id, parent_id, chapter_id, title, level, kind, path, exercise_anchor
        FROM knowledge_node
        WHERE course_id = %s
        ORDER BY level, id
        """,
        (course_id,),
    )
    contains = db.query(
        "SELECT from_node AS source, to_node AS target FROM v_contains WHERE course_id = %s",
        (course_id,),
    )
    shared = db.query(
        """
        SELECT from_node AS source, to_node AS target, exercise_id
        FROM v_shared_exercise
        WHERE course_id = %s
        """,
        (course_id,),
    )
    return {
        "course": course,
        "nodes": nodes,
        "relationships": [dict(r, type="contains") for r in contains],
        "shared": shared,
    }
