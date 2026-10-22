"""问答语料的管理入口：看现在能被检索到什么，需要时重建。

为什么不放在 admin.py：那个文件只管"模型接口配置"（端点、密钥、外传口径）。
这里管的是**检索语料**，两者权限一样（管理员写、教师只读），但改的东西完全不同，
混在一起会让 admin.py 既有密钥路径又有切片逻辑，审起来更慢。

为什么重建要有接口而不是一次性脚本：
    知识点正文会随图谱改名、挂题、审核而变化。语料必须能跟着重建，否则
    "学生问的知识点，问答里还是旧标题"。把它做成接口，发布后点一下就行，
    也留下了审计记录（谁在什么时候重建了 4499 块）。

权限：读 => reviewer/teacher/admin；重建 => admin。
重建是重写教科书式的操作（会删旧语料、级联清空 ai_citation.chunk_id），
不该让普通教师误触。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, qa_ingest
from .auth import Actor, require

router = APIRouter(prefix="/api/v1/admin/qa-corpus", tags=["qa-corpus"])


class RebuildIn(BaseModel):
    course_id: str | None = None   # 不传就是全部课程


@router.get("")
def corpus_status(actor: Actor = Depends(require("reviewer", "teacher", "admin"))) -> dict[str, Any]:
    """每门课有多少知识点有正文、切了多少块。前端据此显示"问答能不能用"。"""
    return qa_ingest.status()


@router.post("/rebuild")
def rebuild(body: RebuildIn, actor: Actor = Depends(require("admin"))) -> dict[str, Any]:
    if body.course_id:
        try:
            result = qa_ingest.rebuild_course(body.course_id)
        except KeyError as error:
            raise HTTPException(404, str(error.args[0] if error.args else error)) from error
        results = [result]
    else:
        results = qa_ingest.rebuild_all()

    failed = [item for item in results if item.get("error")]
    chunks = sum(int(item.get("chunks") or 0) for item in results)
    db.query(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'qa-corpus-rebuild', 'qa_corpus', %s,
                   jsonb_build_object('chunks', %s::int, 'courses', %s::int, 'failed', %s::int))""",
        (actor.id, body.course_id or "all", chunks, len(results), len(failed)),
    )
    return {
        "items": results,
        "chunks": chunks,
        "courses": len(results),
        "failed": failed,
    }

