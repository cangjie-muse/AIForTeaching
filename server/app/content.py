"""课程内容：资源包导入 / 导出 / 在线修改。

这一层解决的是"同一门课有两份内容"的问题。原来「课程导入」纯前端，解析完存浏览器
IndexedDB，只对当前浏览器有效；「教材生成」写数据库。老师改一份，另一份不变，
学生端看到的又是静态课程包——三份内容互不知道。现在数据库是唯一真源：
导入走 API 落库，导出从库里生成，在线修改直接改库。

格式沿用的是现有资源包（知识节点.xlsx + 知识点.md + 习题.md），但**解析在浏览器**：
前端已经有 CourseImport 解析器，服务端不重复实现一遍 xlsx 与 Markdown 的解析规则，
也就不用装 openpyxl（这台机器装不上）。服务端只做校验、落库、生成导出用的 Markdown。

整体更新（mode='replace'）的语义：文件里没有再出现的旧行标记为 retired，不物理删除。
学生作答、审核决定、修订历史都挂在这些行上，删了不可恢复；retired 能让学生端和
课程包不再出现，又保留追溯。只退休"上一次导入带进来的行"，不动模型生成的草稿
（content_batch 为空）——导入一份包不该顺手把老师还没审的 AI 草稿全下架。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, exercise_tags
from .auth import Actor, require

# 溯源解析规则只写一份：db/build_seed.py 里的 parse_source 是导入既有 4807 道题时
# 定下来的口径（书名/书页/PDF 页/题号/来源类型），导出再导入必须沿用它，
# 否则同一道题的溯源会在两次导入之间漂移。
_DB_DIR = Path(__file__).resolve().parents[2] / "db"
if str(_DB_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_DIR))
from build_seed import parse_source  # noqa: E402

router = APIRouter(prefix="/api/v1", tags=["content"])

MAX_NODES = 3000
MAX_EXERCISES = 10000


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------

class NodeIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=300)
    path: list[str] = Field(default_factory=list)
    level: int = Field(default=3, ge=1, le=6)
    parent_id: str | None = None
    chapter_id: str | None = None
    content_md: str = ""
    exercise_anchor: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_raw: str | None = None


class ExerciseIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=120)
    prompt_md: str = Field(..., min_length=1)
    answer_md: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    source_raw: str | None = None
    node_ids: list[str] = Field(default_factory=list)


class CourseMetaIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    audience: str | None = None
    subject: str | None = None
    schema_version: str | None = None


class PackIn(BaseModel):
    course: CourseMetaIn
    nodes: list[NodeIn]
    exercises: list[ExerciseIn] = Field(default_factory=list)
    mode: str = Field(default="replace", pattern="^(replace|merge)$")
    # 导入页上填的课程名称。老师改过名（例如把「数值线代」写成「数值线性代数」）时
    # 以它为准；不传则用资源包里的课程名。前端已做过一次"名字与包是否一致"的提醒，
    # 这里不再重复判断——两边都判断，等于把一个决定做成两处。
    title_override: str | None = Field(default=None, max_length=120)


class NodePatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    content_md: str | None = None
    tags: list[str] | None = None
    # 传空串表示"想移动但没给目标"，单独报 400；不传这个字段才表示不动父节点。
    parent_id: str | None = Field(default=None, max_length=160)
    review_status: str | None = Field(default=None, pattern="^(draft|machine-checked|teacher-reviewed|published|retired)$")


class ExerciseNewIn(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    prompt_md: str = Field(..., min_length=1)
    answer_md: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    source_raw: str | None = None
    node_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _norm_path(path: list[str], level: int, title: str) -> list[str]:
    cleaned = [part.strip() for part in path if part and part.strip()]
    if not cleaned:
        cleaned = [title]
    return cleaned[:level] if level else cleaned


def validate_pack(pack: PackIn) -> dict:
    """把前端解析出来的包校验成可落库的形状。报错要具体到哪一行。"""
    if not pack.nodes:
        raise HTTPException(400, "资源包里没有知识点")
    if len(pack.nodes) > MAX_NODES:
        raise HTTPException(400, f"知识点超过 {MAX_NODES} 个，请拆分资源包")

    nodes: dict[str, dict] = {}
    for raw in pack.nodes:
        if raw.id in nodes:
            raise HTTPException(400, f"知识点 ID 重复：{raw.id}")
        path = _norm_path(raw.path, raw.level, raw.title)
        nodes[raw.id] = {
            "id": raw.id, "title": raw.title, "path": path,
            "level": raw.level, "parent_id": raw.parent_id,
            "chapter_id": raw.chapter_id,
            "content_md": raw.content_md or "",
            "exercise_anchor": raw.exercise_anchor or raw.id,
            "tags": [tag for tag in raw.tags if tag][:20],
            "source_raw": raw.source_raw,
        }

    roots = [node for node in nodes.values() if node["level"] == 1]
    if len(roots) != 1:
        raise HTTPException(400, f"资源包须且只能有一个一级节点（当前 {len(roots)} 个）")

    for node in nodes.values():
        if node["level"] > 1:
            parent = node["parent_id"]
            if not parent or parent not in nodes:
                # 前端解析器会填 parent_id；这里兜底用路径推断，避免整包被拒
                prefix = "\0".join(node["path"][:-1])
                matched = next((other["id"] for other in nodes.values()
                                if "\0".join(other["path"]) == prefix), None)
                if not matched:
                    raise HTTPException(400, f"知识点「{node['title']}」缺少父节点")
                node["parent_id"] = matched

    # 章节 = 二级节点。没有二级节点的包也要能导入（整包只有一个根时不建章节）。
    chapters = [node for node in nodes.values() if node["level"] == 2]
    chapter_ids = {chapter["id"] for chapter in chapters}
    for node in nodes.values():
        if node["level"] >= 2:
            chapter_id = node["chapter_id"]
            if chapter_id not in chapter_ids:
                walker = node
                while walker and walker["level"] > 2 and walker["parent_id"]:
                    walker = nodes.get(walker["parent_id"])
                node["chapter_id"] = walker["id"] if walker and walker["level"] == 2 else None
        else:
            node["chapter_id"] = None

    children: dict[str, int] = {}
    for node in nodes.values():
        if node["parent_id"]:
            children[node["parent_id"]] = children.get(node["parent_id"], 0) + 1
    for node in nodes.values():
        node["kind"] = "container" if children.get(node["id"]) else "knowledge"

    exercises: dict[str, dict] = {}
    warnings: list[str] = []
    for raw in pack.exercises:
        if raw.id in exercises:
            raise HTTPException(400, f"题目 ID 重复：{raw.id}")
        if not raw.prompt_md.strip():
            raise HTTPException(400, f"题目 {raw.id} 题干为空")
        unknown = [node_id for node_id in raw.node_ids if node_id not in nodes]
        if unknown:
            raise HTTPException(400, f"题目 {raw.id} 关联了不存在的知识点：{'、'.join(unknown[:3])}")
        if not raw.node_ids:
            warnings.append(f"题目 {raw.id} 没有关联知识点，学生端不会把它挂到任何知识点下。")
        parsed = parse_source({"source": raw.source_raw or ""})
        exercises[raw.id] = {
            "id": raw.id, "prompt_md": raw.prompt_md.strip(),
            "answer_md": (raw.answer_md or "").strip() or None,
            "difficulty": raw.difficulty,
            "source_raw": raw.source_raw,
            "node_ids": list(dict.fromkeys(raw.node_ids)),
            **{key: parsed[key] for key in
               ("source_book", "source_page", "source_pdf_page", "source_no",
                "provenance", "provenance_note")},
        }

    if len(exercises) > MAX_EXERCISES:
        raise HTTPException(400, f"题目超过 {MAX_EXERCISES} 道，请拆分资源包")

    orphans = [node["title"] for node in nodes.values()
               if node["kind"] == "knowledge"
               and not any(node["id"] in ex["node_ids"] for ex in exercises.values())]
    if orphans:
        warnings.append(f"{len(orphans)} 个知识点没有题目（例如「{orphans[0]}」）。")

    return {"course": pack.course.model_dump(), "nodes": list(nodes.values()),
            "chapters": chapters, "exercises": list(exercises.values()),
            "warnings": warnings}


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def import_pack(course_id: str, pack: PackIn, actor: str) -> dict:
    plan = validate_pack(pack)
    batch = f"{uuid.uuid4().hex[:12]}"
    mode = pack.mode
    meta = plan["course"]
    # 课程原来在不在：决定返回「新建」还是「已存在，已更新」。
    # 前端手上的课程列表可能是几分钟前的，这件事只有库能回答。
    existed = bool(db.query_one(
        "SELECT 1 AS ok FROM course WHERE id = %s", (course_id,)))
    title = (pack.title_override or meta["title"] or "").strip() or meta["title"]

    db.execute(
        """INSERT INTO course (id, title, description, audience, subject, status,
                               schema_version, review_status, content_source,
                               content_batch, content_updated_at)
           VALUES (%s, %s, %s, %s, %s, 'ready', %s, 'draft', 'import', %s, now())
           ON CONFLICT (id) DO UPDATE
             SET title = EXCLUDED.title,
                 description = coalesce(EXCLUDED.description, course.description),
                 audience = coalesce(EXCLUDED.audience, course.audience),
                 subject = coalesce(EXCLUDED.subject, course.subject),
                 content_source = CASE WHEN course.content_source = 'generated'
                                       THEN 'mixed' ELSE 'import' END,
                 content_batch = EXCLUDED.content_batch,
                 content_updated_at = now(),
                 updated_at = now()""",
        (course_id, title, meta.get("description"), meta.get("audience"),
         meta.get("subject"), meta.get("schema_version") or "1.0", batch),
    )

    for index, chapter in enumerate(plan["chapters"], start=1):
        db.execute(
            """INSERT INTO chapter (id, course_id, title, ord, path, content_batch)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (course_id, id) DO UPDATE
                 SET title = EXCLUDED.title, ord = EXCLUDED.ord,
                     path = EXCLUDED.path, content_batch = EXCLUDED.content_batch""",
            (chapter["id"], course_id, chapter["title"], index,
             chapter["path"], batch),
        )

    for node in plan["nodes"]:
        db.execute(
            """INSERT INTO knowledge_node
                 (course_id, id, chapter_id, parent_id, title, level, kind, path,
                  content_md, exercise_anchor, source_raw, tags, review_status,
                  content_batch)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
               ON CONFLICT (course_id, id) DO UPDATE
                 SET chapter_id = EXCLUDED.chapter_id, parent_id = EXCLUDED.parent_id,
                     title = EXCLUDED.title, level = EXCLUDED.level, kind = EXCLUDED.kind,
                     path = EXCLUDED.path, content_md = EXCLUDED.content_md,
                     exercise_anchor = EXCLUDED.exercise_anchor,
                     source_raw = EXCLUDED.source_raw, tags = EXCLUDED.tags,
                     content_batch = EXCLUDED.content_batch,
                     content_version = knowledge_node.content_version + 1,
                     updated_at = now()""",
            (course_id, node["id"], node["chapter_id"], node["parent_id"], node["title"],
             node["level"], node["kind"], node["path"], node["content_md"],
             node["exercise_anchor"], node["source_raw"], node["tags"], batch),
        )

    for ex in plan["exercises"]:
        db.execute(
            """INSERT INTO exercise
                 (course_id, id, chapter_id, prompt_md, answer_md, difficulty,
                  source_raw, source_book, source_page, source_pdf_page, source_no,
                  provenance, provenance_note, review_status, content_batch)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s)
               ON CONFLICT (course_id, id) DO UPDATE
                 SET chapter_id = EXCLUDED.chapter_id, prompt_md = EXCLUDED.prompt_md,
                     answer_md = EXCLUDED.answer_md, difficulty = EXCLUDED.difficulty,
                     source_raw = EXCLUDED.source_raw, source_book = EXCLUDED.source_book,
                     source_page = EXCLUDED.source_page,
                     source_pdf_page = EXCLUDED.source_pdf_page,
                     source_no = EXCLUDED.source_no, provenance = EXCLUDED.provenance,
                     provenance_note = EXCLUDED.provenance_note,
                     content_batch = EXCLUDED.content_batch,
                     review_status = CASE WHEN exercise.review_status = 'published'
                                          THEN 'published'::review_status
                                          ELSE 'draft'::review_status END,
                     content_version = exercise.content_version + 1,
                     updated_at = now()""",
            (course_id, ex["id"], _chapter_of(plan, ex), ex["prompt_md"], ex["answer_md"],
             ex["difficulty"], ex["source_raw"], ex["source_book"], ex["source_page"],
             ex["source_pdf_page"], ex["source_no"], ex["provenance"],
             ex["provenance_note"], batch),
        )
        db.execute(
            "DELETE FROM exercise_node WHERE course_id = %s AND exercise_id = %s",
            (course_id, ex["id"]),
        )
        for node_id in ex["node_ids"]:
            db.execute(
                """INSERT INTO exercise_node (course_id, exercise_id, node_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (course_id, ex["id"], node_id),
            )

    nodes_retired = exercises_retired = 0
    if mode == "replace":
        nodes_retired = db.execute(
            """UPDATE knowledge_node SET review_status = 'retired', updated_at = now()
                WHERE course_id = %s AND content_batch IS NOT NULL
                  AND content_batch <> %s AND review_status <> 'retired'""",
            (course_id, batch),
        )
        exercises_retired = db.execute(
            """UPDATE exercise SET review_status = 'retired', updated_at = now()
                WHERE course_id = %s AND content_batch IS NOT NULL
                  AND content_batch <> %s AND review_status <> 'retired'""",
            (course_id, batch),
        )

    counts = {
        "course_id": course_id, "batch": batch, "mode": mode,
        "course_title": title, "course_created": not existed,
        "nodes_upserted": len(plan["nodes"]),
        "chapters_upserted": len(plan["chapters"]),
        "exercises_upserted": len(plan["exercises"]),
        "nodes_retired": nodes_retired, "exercises_retired": exercises_retired,
        "warnings": plan["warnings"],
    }
    db.execute(
        """INSERT INTO content_import
             (course_id, batch, actor, mode, nodes_upserted, nodes_retired,
              exercises_upserted, exercises_retired, warnings)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
        (course_id, batch, actor, mode, counts["nodes_upserted"], nodes_retired,
         counts["exercises_upserted"], exercises_retired,
         json.dumps(plan["warnings"], ensure_ascii=False)),
    )
    return counts


def _chapter_of(plan: dict, ex: dict) -> str | None:
    """题目落在哪一章：取它关联的第一个知识点的章节。"""
    by_id = {node["id"]: node for node in plan["nodes"]}
    for node_id in ex["node_ids"]:
        chapter = by_id.get(node_id, {}).get("chapter_id")
        if chapter:
            return chapter
    return None


# ---------------------------------------------------------------------------
# 读取与导出
# ---------------------------------------------------------------------------

def read_content(course_id: str, include_retired: bool = False) -> dict:
    course = db.query_one(
        """SELECT id, title, description, audience, subject, status, review_status,
                  content_source, content_batch, content_updated_at
             FROM course WHERE id = %s""", (course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{course_id}")

    node_filter = "" if include_retired else " AND review_status <> 'retired'"
    exercises_filter = "" if include_retired else " AND ex.review_status <> 'retired'"

    # 审核状态一起带出来：改内容的人需要知道"这条审过没有、审过之后动过没有"，
    # 否则会在不知不觉中把已审核的内容改坏。
    nodes = db.query(
        f"""SELECT id, chapter_id, parent_id, title, level, kind, path, content_md,
                   exercise_anchor, tags, source_raw, review_status, human_verified_at
              FROM knowledge_node
             WHERE course_id = %s{node_filter}
             ORDER BY level, id""", (course_id,))
    exercises = db.query(
        f"""SELECT ex.id, ex.chapter_id, ex.prompt_md, ex.answer_md, ex.difficulty,
                   ex.source_raw, ex.source_book, ex.source_page, ex.source_pdf_page,
                   ex.source_no, ex.provenance, ex.provenance_note, ex.review_status,
                   ex.human_verified_at, coalesce(v.tier, 'none') AS tier
              FROM exercise ex
              LEFT JOIN v_exercise_verification v
                     ON v.course_id = ex.course_id AND v.id = ex.id
             WHERE ex.course_id = %s{exercises_filter}
             ORDER BY ex.id""", (course_id,))
    links = db.query(
        """SELECT exercise_id, node_id FROM exercise_node WHERE course_id = %s""",
        (course_id,))

    by_exercise: dict[str, list[str]] = {}
    for link in links:
        by_exercise.setdefault(link["exercise_id"], []).append(link["node_id"])
    for ex in exercises:
        ex["node_ids"] = by_exercise.get(ex["id"], [])
        ex["tags"] = exercise_tags.tags_for(ex)

    return {"course": course, "nodes": nodes, "exercises": exercises}


def render_knowledge_md(course_title: str, nodes: list[dict]) -> str:
    """导出用的知识点.md：格式与现有资源包一致（锚点在标题前）。"""
    lines = [f"# {course_title} 知识点", ""]
    for node in nodes:
        # 一级节点也要有锚点：导入器按行校验，每一行（含课程根节点）
        # 都必须能在 知识点.md 与 习题.md 里找到对应锚点，否则整包被拒。
        anchor = node["id"]
        lines.append(f'<a id="{anchor}"></a>')
        lines.append(f"{'#' * min(node['level'] + 1, 6)} {node['title']}")
        lines.append("")
        if node.get("source_raw"):
            lines.append(f"**知识说明：** {node['source_raw']}")
        if node.get("tags"):
            lines.append(f"**知识点标签：** {'、'.join(node['tags'])}")
        if node.get("content_md"):
            lines.append(node["content_md"].strip())
        else:
            lines.append("（本知识点暂无正文）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_exercise_md(course_title: str, nodes: list[dict], exercises: list[dict]) -> str:
    """导出用的习题.md：按知识点分组，题目带 source-exercise 稳定 ID。"""
    by_node: dict[str, list[dict]] = {}
    for ex in exercises:
        for node_id in ex["node_ids"]:
            by_node.setdefault(node_id, []).append(ex)

    lines = [f"# {course_title} 习题与简解", ""]
    for node in nodes:
        anchor = node["exercise_anchor"] or node["id"]
        lines.append(f'<a id="{anchor}"></a>')
        lines.append(f"## {node['title']}")
        lines.append("")
        items = by_node.get(node["id"], [])
        if not items:
            lines.append("（本知识点暂无习题）")
            lines.append("")
            continue
        for ex in items:
            lines.append(f"<!-- source-exercise:{ex['id']} -->")
            lines.append(f"1. {ex['prompt_md']}".replace("\n", "\n   "))
            lines.append("")
            lines.append(f"   **答案：** {(ex.get('answer_md') or '（待补）').replace(chr(10), chr(10) + '   ')}")
            lines.append("")
            difficulty = ex.get("difficulty") or 3
            lines.append(f"   **难度：{difficulty}/5；出处：** {ex.get('source_raw') or '未标注'}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 在线修改
# ---------------------------------------------------------------------------

MAX_NODE_LEVEL = 6


def move_node(course_id: str, node_id: str, parent_id: str, actor: str) -> dict:
    """把节点（连同子树）挪到另一个容器节点下面。

    只改 parent_id / level / path 三列，**不动 chapter_id**：真实导入数据里 chapter_id
    是空的，章节归属由 review 接口按 parent_id 往上走到 level=2 现算（见 review.graph_view）。
    往 chapter_id 里写节点 id 还会撞 chapter 表的外键。

    移动改变了"这个知识点挂在哪"，所以节点自己和整棵子树的人工审核一并撤销——
    和挂载变化同一个口径：位置变了，"审过"这句话就不成立了。
    """
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, parent_id, level, path, title, kind
                 FROM knowledge_node WHERE course_id = %s AND id = %s""",
            (course_id, node_id),
        )
        node = cur.fetchone()
        if not node:
            raise HTTPException(404, f"知识点不存在：{node_id}")
        cur.execute(
            "SELECT id, level, path, kind FROM knowledge_node WHERE course_id = %s AND id = %s",
            (course_id, parent_id),
        )
        parent = cur.fetchone()
        if not parent:
            raise HTTPException(404, f"要移到的节点不存在：{parent_id}")
        if parent["kind"] != "container":
            raise HTTPException(400, "只能挂到章或节底下：知识点下面不再挂知识点")
        if parent["id"] == node["id"]:
            raise HTTPException(400, "不能把节点挂到它自己下面")
        new_level = parent["level"] + 1
        if new_level > MAX_NODE_LEVEL:
            raise HTTPException(400, f"最多 {MAX_NODE_LEVEL} 层")

        cur.execute(
            """WITH RECURSIVE sub AS (
                   SELECT id, level, path FROM knowledge_node
                    WHERE course_id = %s AND id = %s
                   UNION ALL
                   SELECT n.id, n.level, n.path FROM knowledge_node n
                     JOIN sub ON n.parent_id = sub.id AND n.course_id = %s)
               SELECT id, level, path FROM sub""",
            (course_id, node_id, course_id),
        )
        subtree = cur.fetchall()
        if any(row["id"] == parent["id"] for row in subtree):
            raise HTTPException(400, "不能把节点移到它自己的子树里：那样树就成环了")

        delta = new_level - node["level"]
        old_path = list(node["path"] or [])
        new_path = list(parent["path"] or []) + [node["title"]]
        base = len(old_path)
        cur.execute(
            """UPDATE knowledge_node
                  SET parent_id = %s, level = %s, path = %s,
                      content_version = content_version + 1, updated_at = now(),
                      human_verified_at = NULL, human_verified_by = NULL
                WHERE course_id = %s AND id = %s""",
            (parent_id, new_level, new_path, course_id, node_id),
        )
        for row in subtree:
            if row["id"] == node_id:
                continue
            path = list(row["path"] or [])
            suffix = path[base:] if len(path) >= base else path
            cur.execute(
                """UPDATE knowledge_node
                      SET level = %s, path = %s, updated_at = now(),
                          human_verified_at = NULL, human_verified_by = NULL
                    WHERE course_id = %s AND id = %s""",
                (row["level"] + delta, new_path + suffix, course_id, row["id"]),
            )
        cur.execute(
            """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
               VALUES (%s, 'node.move', 'knowledge_node', %s, %s::jsonb)""",
            (actor, f"{course_id}/{node_id}", json.dumps({
                "from_parent": node["parent_id"],
                "to_parent": parent_id,
                "level": new_level,
                "nodes": len(subtree),
            }, ensure_ascii=False)),
        )
    return {
        "from_parent": node["parent_id"],
        "parent_id": parent_id,
        "level": new_level,
        "path": new_path,
        "nodes": len(subtree),
        "descendants": len(subtree) - 1,
    }


def patch_node(course_id: str, node_id: str, body: NodePatchIn, actor: str) -> dict:
    row = db.query_one(
        "SELECT id, title, content_md, tags, review_status, parent_id, level, path "
        "FROM knowledge_node WHERE course_id = %s AND id = %s", (course_id, node_id))
    if not row:
        raise HTTPException(404, f"知识点不存在：{node_id}")
    if body.parent_id is not None and not body.parent_id.strip():
        raise HTTPException(400, "parent_id 不能是空串：不想移动就别传这个字段")

    sets, params = [], []
    title_changed = (body.title is not None and bool(body.title.strip())
                     and body.title.strip() != row["title"])
    if body.title is not None and body.title.strip():
        sets.append("title = %s")
        params.append(body.title.strip())
    if body.content_md is not None:
        sets.append("content_md = %s")
        params.append(body.content_md)
    if body.tags is not None:
        sets.append("tags = %s")
        params.append([tag for tag in body.tags if tag][:20])
    if body.review_status is not None:
        sets.append("review_status = %s")
        params.append(body.review_status)
    # 什么字段都没给、也不想移动：原样返回，不留审计（没有变化就没有可留的痕）。
    if not sets and body.parent_id is None:
        return row

    if sets:
        sets.append("content_version = content_version + 1")
        sets.append("updated_at = now()")
        # 节点自己的审核结论是"这个标题挂在这个位置，我认了"。标题一改，这句话就不成立了。
        # 所以改标题要连自己的审核标记一起清掉，不能只在行上继续显示「已通过」。
        if title_changed:
            sets.append("human_verified_at = NULL")
            sets.append("human_verified_by = NULL")
        params.extend([course_id, node_id])
        db.execute(
            f"UPDATE knowledge_node SET {', '.join(sets)} WHERE course_id = %s AND id = %s",
            tuple(params),
        )
    # 移动放在内容更新之后：path 要用新标题重算（改标题同时换位置时不能按旧标题算）。
    move = None
    if body.parent_id is not None:
        move = move_node(course_id, node_id, body.parent_id.strip(), actor)
    # 改的是"这道题挂在哪、这一章叫什么"——挂在这棵子树上的题，人工审核的前提没了。
    # 只改 review_status（停用/启用）不算内容变化，不重置。
    content_fields = sorted(
        field for field in body.model_dump(exclude_none=True) if field != "review_status"
    )
    reset = {"exercises": 0, "cleared": 0}
    if content_fields:
        from . import mounting  # 延迟导入：content 与 mounting 都不该在模块级互相纠缠
        reset = mounting.reset_mounted_nodes(
            course_id, [node_id], actor=actor, reason=f"知识点改动：{'、'.join(content_fields)}"
        )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'node.patch', 'knowledge_node', %s, %s::jsonb)""",
        (actor, f"{course_id}/{node_id}",
         json.dumps({"fields": sorted(body.model_dump(exclude_none=True)), "reset": reset,
                     "move": move}, ensure_ascii=False)),
    )
    row = db.query_one(
        "SELECT id, title, content_md, tags, review_status, parent_id, level, path "
        "FROM knowledge_node WHERE course_id = %s AND id = %s", (course_id, node_id))
    return {**row, "exercises_reset": reset["cleared"], "title_reverified_reset": title_changed,
            "move": move}


def create_node(course_id: str, body: NodeIn, actor: str) -> dict:
    parent = None
    if body.parent_id:
        parent = db.query_one(
            "SELECT id, level, chapter_id, path FROM knowledge_node "
            "WHERE course_id = %s AND id = %s", (course_id, body.parent_id))
        if not parent:
            raise HTTPException(404, f"父知识点不存在：{body.parent_id}")
    level = (parent["level"] + 1) if parent else 1
    if level > 6:
        raise HTTPException(400, "最多 6 层")
    path = list(parent["path"]) + [body.title] if parent else [body.title]
    chapter_id = (parent["id"] if parent and parent["level"] == 2
                  else (parent or {}).get("chapter_id"))
    db.execute(
        """INSERT INTO knowledge_node
             (course_id, id, chapter_id, parent_id, title, level, kind, path,
              content_md, exercise_anchor, tags, review_status)
           VALUES (%s, %s, %s, %s, %s, %s, 'knowledge', %s, %s, %s, %s, 'draft')""",
        (course_id, body.id, chapter_id, body.parent_id, body.title, level, path,
         body.content_md, body.exercise_anchor or body.id, body.tags),
    )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'node.create', 'knowledge_node', %s, %s::jsonb)""",
        (actor, f"{course_id}/{body.id}", json.dumps({"level": level}, ensure_ascii=False)),
    )
    return {"id": body.id, "level": level, "path": path}


def create_exercise(course_id: str, body: ExerciseNewIn, actor: str) -> dict:
    node_ids = []
    for node_id in body.node_ids:
        node = db.query_one(
            "SELECT id, chapter_id FROM knowledge_node WHERE course_id = %s AND id = %s",
            (course_id, node_id))
        if not node:
            raise HTTPException(404, f"知识点不存在：{node_id}")
        node_ids.append(node)
    exercise_id = body.id or f"ex-{uuid.uuid4().hex[:10]}"
    parsed = parse_source({"source": body.source_raw or ""})
    chapter_id = node_ids[0]["chapter_id"] if node_ids else None
    db.execute(
        """INSERT INTO exercise
             (course_id, id, chapter_id, prompt_md, answer_md, difficulty, source_raw,
              source_book, source_page, source_pdf_page, source_no, provenance,
              provenance_note, review_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')""",
        (course_id, exercise_id, chapter_id, body.prompt_md, body.answer_md,
         body.difficulty, body.source_raw, parsed["source_book"], parsed["source_page"],
         parsed["source_pdf_page"], parsed["source_no"], parsed["provenance"],
         parsed["provenance_note"]),
    )
    for node in node_ids:
        db.execute(
            """INSERT INTO exercise_node (course_id, exercise_id, node_id)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (course_id, exercise_id, node["id"]),
        )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'exercise.create', 'exercise', %s, %s::jsonb)""",
        (actor, f"{course_id}/{exercise_id}",
         json.dumps({"node_ids": [node["id"] for node in node_ids]}, ensure_ascii=False)),
    )
    return {"id": exercise_id, "node_ids": [node["id"] for node in node_ids]}


def retire(course_id: str, kind: str, item_id: str, actor: str) -> dict:
    table = "knowledge_node" if kind == "node" else "exercise"
    row = db.query_one(
        f"SELECT id FROM {table} WHERE course_id = %s AND id = %s", (course_id, item_id))
    if not row:
        raise HTTPException(404, f"不存在：{item_id}")
    db.execute(
        f"UPDATE {table} SET review_status = 'retired', updated_at = now() "
        "WHERE course_id = %s AND id = %s", (course_id, item_id))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, %s, %s, %s, '{}'::jsonb)""",
        (actor, f"{kind}.retire", 'knowledge_node' if kind == 'node' else 'exercise',
         f"{course_id}/{item_id}"),
    )
    return {"id": item_id, "review_status": "retired"}


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.post("/courses/{course_id}/content/import")
def import_course_content(course_id: str, body: PackIn,
                          actor: Actor = Depends(require("admin"))) -> dict:
    try:
        counts = import_pack(course_id, body, actor.id)
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - 导入失败要让老师看到具体原因
        raise HTTPException(400, f"导入失败：{type(error).__name__}: {error}") from error
    return {"ok": True, **counts}


class DocsIn(BaseModel):
    """单文件增量导入：只传 知识点.md 或 习题.md。"""
    knowledge_md: str | None = Field(default=None, max_length=20_000_000)
    exercises_md: str | None = Field(default=None, max_length=20_000_000)
    dry_run: bool = True


@router.post("/courses/{course_id}/content/import-docs")
def import_documents(course_id: str, body: DocsIn,
                     actor: Actor = Depends(require("admin"))) -> dict:
    """只上传两个 md 里的一个时走这里：按锚点改已有内容，结构不动。

    默认 dry_run：先给一份"会改什么"的报告，老师点确认再落库。
    没有知识节点表的整包导入走 /content/import，那条路会重写整门课。
    """
    from . import pack_merge

    if not body.knowledge_md and not body.exercises_md:
        raise HTTPException(400, "至少要上传一个文件：知识点.md 或 习题.md")
    try:
        if body.dry_run:
            report = pack_merge.plan_changes(course_id, body.knowledge_md, body.exercises_md)
            return {"ok": True, "dry_run": True, **pack_merge.public(report)}
        return {"ok": True, "dry_run": False,
                **pack_merge.apply_changes(course_id, body.knowledge_md,
                                           body.exercises_md, actor.id)}
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/courses/{course_id}/content")
def get_course_content(course_id: str, include_retired: bool = False,
                       actor: Actor = Depends(require("reviewer"))) -> dict:
    return read_content(course_id, include_retired)


@router.get("/courses/{course_id}/content/export")
def export_course_content(course_id: str, actor: Actor = Depends(require("admin"))) -> dict:
    content = read_content(course_id, include_retired=False)
    course, nodes, exercises = content["course"], content["nodes"], content["exercises"]
    return {
        "course": course,
        "nodes": nodes,
        "exercises": exercises,
        "knowledge_md": render_knowledge_md(course["title"], nodes),
        "exercise_md": render_exercise_md(course["title"], nodes, exercises),
        "hint": "前端用 nodes 生成 知识节点.xlsx，两份 Markdown 原样保存，即得到可再导入的三件套。",
    }


@router.patch("/courses/{course_id}/nodes/{node_id}")
def patch_knowledge_node(course_id: str, node_id: str, body: NodePatchIn,
                         actor: Actor = Depends(require("reviewer"))) -> dict:
    """改知识点。教师也能改：审核图谱时发现"标题不对/挂错层"，就地改比退回给管理员快。

    写 audit_log 的 `node.patch`，谁改的、改了哪些字段都留痕。
    """
    return patch_node(course_id, node_id, body, actor.id)


class ExercisePatchIn(BaseModel):
    prompt_md: str | None = Field(default=None, min_length=1)
    answer_md: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    source_raw: str | None = None
    node_ids: list[str] | None = None


@router.patch("/courses/{course_id}/exercises/{exercise_id}")
def patch_course_exercise(course_id: str, exercise_id: str, body: ExercisePatchIn,
                          actor: Actor = Depends(require("admin"))) -> dict:
    """改题。出处文本改了要重算溯源：书页/题号是从它里面解析出来的。"""
    row = db.query_one(
        "SELECT id FROM exercise WHERE course_id = %s AND id = %s", (course_id, exercise_id))
    if not row:
        raise HTTPException(404, f"题目不存在：{exercise_id}")

    sets, params = [], []
    if body.prompt_md is not None:
        sets.append("prompt_md = %s")
        params.append(body.prompt_md.strip())
    if body.answer_md is not None:
        sets.append("answer_md = %s")
        params.append(body.answer_md.strip() or None)
    if body.difficulty is not None:
        sets.append("difficulty = %s")
        params.append(body.difficulty)
    if body.source_raw is not None:
        parsed = parse_source({"source": body.source_raw})
        sets.extend(["source_raw = %s", "source_book = %s", "source_page = %s",
                     "source_pdf_page = %s", "source_no = %s", "provenance = %s",
                     "provenance_note = %s"])
        params.extend([body.source_raw, parsed["source_book"], parsed["source_page"],
                       parsed["source_pdf_page"], parsed["source_no"], parsed["provenance"],
                       parsed["provenance_note"]])
    if not sets:
        return {"id": exercise_id, "changed": 0}

    sets.append("content_version = content_version + 1")
    sets.append("updated_at = now()")
    params.extend([course_id, exercise_id])
    db.execute(f"UPDATE exercise SET {', '.join(sets)} WHERE course_id = %s AND id = %s",
               tuple(params))

    if body.node_ids is not None:
        db.execute("DELETE FROM exercise_node WHERE course_id = %s AND exercise_id = %s",
                   (course_id, exercise_id))
        for node_id in body.node_ids:
            db.execute("""INSERT INTO exercise_node (course_id, exercise_id, node_id)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                       (course_id, exercise_id, node_id))

    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'exercise.patch', 'exercise', %s, '{}'::jsonb)""",
        (actor.id, f"{course_id}/{exercise_id}"))
    return db.query_one(
        """SELECT id, prompt_md, answer_md, difficulty, source_raw, source_page,
                  source_no, provenance, review_status
             FROM exercise WHERE course_id = %s AND id = %s""",
        (course_id, exercise_id))


@router.post("/courses/{course_id}/nodes", status_code=201)
def post_knowledge_node(course_id: str, body: NodeIn,
                        actor: Actor = Depends(require("admin"))) -> dict:
    return create_node(course_id, body, actor.id)


@router.post("/courses/{course_id}/exercises", status_code=201)
def post_exercise(course_id: str, body: ExerciseNewIn,
                  actor: Actor = Depends(require("admin"))) -> dict:
    return create_exercise(course_id, body, actor.id)


@router.post("/courses/{course_id}/nodes/{node_id}/retire")
def retire_node(course_id: str, node_id: str,
                actor: Actor = Depends(require("admin"))) -> dict:
    return retire(course_id, "node", node_id, actor.id)


@router.post("/courses/{course_id}/exercises/{exercise_id}/retire")
def retire_exercise(course_id: str, exercise_id: str,
                    actor: Actor = Depends(require("admin"))) -> dict:
    return retire(course_id, "exercise", exercise_id, actor.id)
