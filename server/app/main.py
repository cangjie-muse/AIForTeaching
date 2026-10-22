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
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import auth, db, llm
from .accounts import router as accounts_router
from .admin import router as admin_router
from .attempts import router as attempts_router
from .catalog import router as catalog_router
from .classes import router as classes_router
from .content import router as content_router
from .generation import router as generation_router
from .imports import router as imports_router
from .qa import router as qa_router
from .review import router as review_router
from .review_nodes import router as review_nodes_router
from .visibility import router as visibility_router

APP_ENV = os.environ.get("APP_ENV", "dev").lower()


def _check_auth_config() -> None:
    """拒绝"生产环境还在信任请求头"。这比事后审计便宜得多。"""
    if APP_ENV == "prod" and auth.AUTH_MODE == "dev":
        raise RuntimeError(
            "生产环境（APP_ENV=prod）不允许 AUTH_MODE=dev："
            "任何人只要加一个 X-Roles: admin 头就能改模型配置。"
            "请先接入统一认证（AUTH_MODE=cas）。"
        )
    if APP_ENV == "prod" and os.environ.get("DEV_FAKE_MODEL") == "1":
        raise RuntimeError(
            "生产环境不允许 DEV_FAKE_MODEL=1：那会让学伴返回固定演示文本，"
            "学生拿到的是假答案。"
        )
    if auth.AUTH_MODE == "dev":
        fallback = sorted(auth.DEV_ROLES) or "无（必须登录）"
        print(
            f"[警告] AUTH_MODE=dev：身份优先取会话 cookie，没有 cookie 时信任请求头，"
            f"兜底角色 {fallback}。仅限本地开发，生产必须改为 password 或 cas。",
            file=sys.stderr,
        )
    if APP_ENV == "prod" and auth.AUTH_MODE == "password":
        # 账号密码登录在校内网够用，但要提醒：强口令与 HTTPS 是前提。
        print(
            "[提示] AUTH_MODE=password：请确认已启用 HTTPS（cookie 带 Secure），"
            "并要求师生首次登录改密。",
            file=sys.stderr,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_auth_config()
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="智慧武大 · 数学教学智能体 API", version="0.1.0", lifespan=lifespan)

# 开发期允许本地静态站（8765）与 file:// 之外的来源跨域访问。
# 上线前必须收紧为学院域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://127.0.0.1:8765,http://localhost:8765").split(","),
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(accounts_router)
app.include_router(review_router)
app.include_router(review_nodes_router)
app.include_router(admin_router)
app.include_router(qa_router)
app.include_router(attempts_router)
app.include_router(generation_router)
app.include_router(content_router)
app.include_router(catalog_router)
app.include_router(visibility_router)
app.include_router(classes_router)
app.include_router(imports_router)


_ROOT = Path(__file__).resolve().parents[2]


def _mount_dev_static(target: FastAPI) -> None:
    """开发期由后端直接托管前端静态站，让页面与 API 同源。

    为什么同源：审核页要带 X-Actor / X-Roles，浏览器会先发 OPTIONS 预检；
    前端在 8765、后端在 8000 时，只要有一处 CORS 配置漏了就整页报错，
    而错误信息是浏览器的 "Failed to fetch"，看不出原因。
    同一个端口就没有这层问题。

    只挂白名单目录：仓库里还有 secrets/、.env、db/、server/，
    整站挂载等于把它们全部暴露在 HTTP 上。
    """
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    for name in ("js", "styles", "assets", "data", "config", "vendor",
                 "course-packs", "reports"):
        folder = _ROOT / name
        if folder.is_dir():
            target.mount(f"/{name}", StaticFiles(directory=folder), name=f"dev-{name}")

    @target.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(_ROOT / "index.html")


if APP_ENV != "prod":
    _mount_dev_static(app)


@app.get("/api/v1/health")
def health() -> dict:
    try:
        row = db.query_one("SELECT count(*) AS n FROM course")
        return {
            "status": "ok",
            "db": True,
            "courses": row["n"] if row else 0,
            # 模型配置自检：不泄露密钥，只报端点在内网还是外网
            "models": llm.describe(),
        }
    except Exception as exc:  # 数据库不可用时仍返回结构化结果，便于探活
        return {"status": "degraded", "db": False, "error": str(exc), "models": llm.describe()}


@app.get("/api/v1/courses")
def list_courses() -> dict:
    rows = db.query(
        """
        SELECT c.id, c.title, c.audience, c.subject, c.status, c.review_status,
               (SELECT count(*) FROM knowledge_node k
                 WHERE k.course_id = c.id AND k.kind = 'knowledge') AS knowledge_count,
               (SELECT count(*) FROM exercise e WHERE e.course_id = c.id) AS exercise_count,
               -- 有教材片段的课才可能被问答检索到：前端据此说明"哪门课能答"
               (SELECT count(*) FROM textbook_chunk ch
                  JOIN textbook t ON t.id = ch.textbook_id
                 WHERE t.course_id = c.id) AS chunk_count
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
