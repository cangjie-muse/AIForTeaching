"""教材自动生成资源包：上传 → 排队 → 调模型 → 校验 → 入库 → 进审核。

为什么不是"把提示词丢给模型一次性拿结果"：

`prompts/Course-Building-Standalone.md` 是给**智能体**的，它要求打开 PDF、逐页核对、
写 xlsx。它的产物格式我们已经能干净导入了（探针验证过），但它要求的能力——读文件、
写表格、翻页——不是一次 chat 调用能提供的。

所以这条链路把职责拆开：
  代码负责  PDF → 带页号的文本、页码、id、溯源、xlsx；
  模型负责  这一章有哪些知识点、每道题的题干与解答。

好处是机器可读字段不再靠模型自觉。实测数据里 809 条"溯源无法判定"、
1270 条"没有精确页码"，追根都是让模型写书目信息造成的——它写"主教材习题一·2"，
导入器就只能猜。这里页码由代码从真实 PDF 注入，溯源由代码按"有没有教材原题号"标注。

输出仍然是三件套（知识点.md / 习题.md / 知识节点.json + 转 xlsx 的脚本），
因为审核员看的是 Markdown，而审核队列要的是结构化字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, llm
from .auth import Actor, require

router = APIRouter(prefix="/api/v1", tags=["generation"])

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads"))
PACK_DIR = Path(os.environ.get("PACK_DIR", ROOT / "course-packs"))

PROMPT_VERSION = "gen-v1"
MAX_CHAPTER_CHARS = 14000        # 一章一次调用；超了截断并记 warning，不硬塞
MIN_NODE_CONTENT = 40            # 少于这个字数不像知识点，像占位符
DEV_FAKE_MODEL = os.environ.get("DEV_FAKE_MODEL") == "1"

GENERATE_SYSTEM = """你是武汉大学数学与统计学院的课程建设助手。给你一章教材的文字，
请整理成知识点与习题，输出**一个 JSON 对象**，不要输出别的内容。

JSON 结构：
{
  "chapter_title": "第1章 实数与函数",
  "chapter_summary": "本章讲什么，两三句",
  "nodes": [
    {"title": "上确界", "content_md": "定义、定理与例子，用 $LaTeX$",
     "page_from": 12, "page_to": 14, "tags": ["定义", "定理"]}
  ],
  "exercises": [
    {"source_no": "习题1.1-3", "page": 15, "prompt_md": "题干",
     "answer_md": "简解", "difficulty": 3, "node_title": "上确界",
     "from_textbook": true}
  ]
}

规则（必须遵守）：
1. 只依据给出的教材文字。文字里没有的内容不要补，宁可不写这一条。
2. 页码写**教材印刷页**（不是 PDF 页），取自文字里的页标记换算说明；拿不准就不写该字段。
3. `from_textbook`：这道题确实来自教材原文（有题号）才为 true；自己出的题为 false。
4. `difficulty` 取 1–5 的整数。
5. `node_title` 必须与 nodes 里某个 title 完全一致。
6. 数学式子用 $LaTeX$，行间用 $$LaTeX$$。不要输出 Markdown 代码块包裹的 JSON。
"""


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
class TextbookIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    course_id: str = Field(min_length=1, max_length=80)
    edition: str | None = Field(default=None, max_length=120)
    authors: str | None = Field(default=None, max_length=200)
    page_offset: int = Field(default=0, ge=-50, le=50)


class GenerateIn(BaseModel):
    page_from: int = Field(ge=1, le=5000)
    page_to: int = Field(ge=1, le=5000)
    chapter_hint: str | None = Field(default=None, max_length=80)
    new_course_id: str | None = Field(default=None, max_length=80)
    new_course_title: str | None = Field(default=None, max_length=120)


@dataclass
class Pack:
    chapter_title: str
    chapter_summary: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    exercises: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 校验：模型输出的每一条都要能落到表里
# ---------------------------------------------------------------------------
def _short_hash(text: str, size: int = 6) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:size]


def node_id(title: str) -> str:
    return f"kp-{_short_hash(title)}"


def chapter_id(title: str) -> str:
    return f"ch-{_short_hash(title)}"


def validate_payload(raw: dict, pdf_page_from: int, pdf_page_to: int) -> Pack:
    """把模型输出变成可入库的结构；能救的救，救不了的记 warning。"""
    if not isinstance(raw, dict):
        raise ValueError("模型没有返回 JSON 对象")

    title = str(raw.get("chapter_title") or "").strip()
    if len(title) < 2:
        raise ValueError("缺少章节标题（chapter_title）")

    pack = Pack(chapter_title=title, chapter_summary=str(raw.get("chapter_summary") or "").strip())

    seen_titles: set[str] = set()
    for item in raw.get("nodes") or []:
        if not isinstance(item, dict):
            continue
        node_title = str(item.get("title") or "").strip()
        content = str(item.get("content_md") or "").strip()
        if len(node_title) < 2 or node_title in seen_titles:
            pack.warnings.append(f"跳过知识点（标题为空或重复）：{node_title[:20]}")
            continue
        if len(content) < MIN_NODE_CONTENT:
            pack.warnings.append(f"知识点「{node_title}」正文过短，可能只是占位")
        seen_titles.add(node_title)
        page_from = item.get("page_from")
        page_to = item.get("page_to")
        pack.nodes.append({
            "id": node_id(node_title),
            "title": node_title,
            "content_md": content,
            "page_from": page_from if isinstance(page_from, int) else None,
            "page_to": page_to if isinstance(page_to, int) else page_from,
            "tags": [str(t)[:20] for t in (item.get("tags") or []) if str(t).strip()][:6],
        })

    if not pack.nodes:
        raise ValueError("模型没有给出任何知识点")

    titles = {node["title"] for node in pack.nodes}
    for index, item in enumerate(raw.get("exercises") or [], start=1):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt_md") or "").strip()
        answer = str(item.get("answer_md") or "").strip()
        if len(prompt) < 8 or len(answer) < 10:
            pack.warnings.append(f"跳过第 {index} 题：题干或解答过短")
            continue
        node_title = str(item.get("node_title") or "").strip()
        if node_title not in titles:
            # 挂不上知识点就是孤儿题，审核队列里会被标 missing-node；先记下来
            pack.warnings.append(f"第 {index} 题的知识点「{node_title}」不在本章知识点里")
        difficulty = item.get("difficulty")
        difficulty = difficulty if isinstance(difficulty, int) and 1 <= difficulty <= 5 else 3
        page = item.get("page") if isinstance(item.get("page"), int) else None
        if page is not None and not (pdf_page_from - 60 <= page <= pdf_page_to + 60):
            pack.warnings.append(f"第 {index} 题的页码 {page} 离本章范围太远")
        pack.exercises.append({
            "id": None,                       # 入库时按章编号
            "source_no": (str(item.get("source_no") or "").strip() or None),
            "page": page,
            "prompt_md": prompt,
            "answer_md": answer,
            "difficulty": difficulty,
            "node_title": node_title if node_title in titles else None,
            "from_textbook": bool(item.get("from_textbook")),
        })

    if not pack.exercises:
        pack.warnings.append("这一章没有生成任何习题")
    return pack


# ---------------------------------------------------------------------------
# 渲染：三件套（审核员看 Markdown，导入器看 JSON）
# ---------------------------------------------------------------------------
def render_knowledge_md(pack: Pack, course_title: str, source: dict) -> str:
    lines = [
        f"# {course_title} 知识点（{pack.chapter_title}）",
        "",
        f"**版本日期。** 自动生成，{(source.get('generated_at') or '')[:10]}。",
        "",
        f"**范围与状态。** {pack.chapter_summary or '本章知识点。'}",
        "",
        "> 本文件由平台调用模型生成，未经教师审核，不得直接用于教学。",
        "",
        "## 来源与页码",
        "",
        f"- **主教材**：{source.get('book_line') or '（未填写）'}",
        "",
        f'<a id="{chapter_id(pack.chapter_title)}"></a>',
        f"## {pack.chapter_title}",
        "",
    ]
    for node in pack.nodes:
        lines += [
            f'<a id="{node["id"]}"></a>',
            f"### {node['title']}",
            "",
            f"**图谱路径：** {course_title} / {pack.chapter_title} / {node['title']}",
            "",
            f"**教材范围：** 书页 {node['page_from']}–{node['page_to']}"
            if node["page_from"] else "**教材范围：** 未标注",
            "",
            f"**习题：** [题目与简解](习题.md#{node['id']})",
            "",
            node["content_md"],
            "",
        ]
    return "\n".join(lines).strip() + "\n"


def exercise_source_text(ex: dict, source: dict) -> str:
    """按导入器能解析的写法拼出处。

    这是整条链路的关键一步：书页、PDF 页、题号都由代码填，
    所以不会出现"模型写了出处但解析不出来"的情况。
    """
    if not ex["from_textbook"] or not ex["page"]:
        return ("课程建设自拟训练题，无教材原题与书页；"
                "**解答来源：** AI 生成，待人工核对。")
    book = source.get("book_short") or "本课教材"
    number = f"原题号{ex['source_no']}，" if ex["source_no"] else ""
    return (f"{book}，{number}书页{ex['page']}"
            f"（PDF第{source.get('pdf_page') or '?'}页）；"
            "**解答来源：** AI 生成，待人工核对。")


def render_exercise_md(pack: Pack, course_title: str, source: dict) -> str:
    lines = [
        f"# {course_title} 习题与简解（{pack.chapter_title}）",
        "",
        f"**版本日期。** 自动生成，{(source.get('generated_at') or '')[:10]}。",
        "",
        f"**范围与状态。** 共 {len(pack.exercises)} 题，均由模型生成，待教师审核。",
        "",
        "> 解答为模型生成，可能出错。审核通过前不进入学生端。",
        "",
        "## 来源与页码",
        "",
        f"- **主教材**：{source.get('book_line') or '（未填写）'}",
        "",
        f'<a id="{chapter_id(pack.chapter_title)}"></a>',
        f"## {pack.chapter_title}",
        "",
    ]
    for node in pack.nodes:
        lines += [f'<a id="{node["id"]}"></a>', f"### {node['title']}", "",
                  f"**知识内容：** [定义、结论与例子](知识点.md#{node['id']})", ""]
        for ex in [e for e in pack.exercises if e["node_title"] == node["title"]]:
            lines += [
                f"<!-- source-exercise:{ex['id']} -->",
                "1. " + ex["prompt_md"].replace("\n", "\n   "),
                "",
                "   **答案：** " + ex["answer_md"].replace("\n", "\n   "),
                "",
                f"   **难度：{ex['difficulty']}/5；出处：** {exercise_source_text(ex, source)}",
                "",
            ]
    return "\n".join(lines).strip() + "\n"


def node_rows(pack: Pack, course_title: str) -> list[list[str]]:
    """知识节点 xlsx 的行：六层节点 + 知识说明 + 知识点标签。"""
    rows = [["第1级", course_title, "", "", "", "", pack.chapter_summary or "", "章节总览"]]
    rows.append(["第2级", course_title, pack.chapter_title, "", "", "",
                 f"书页 {pack.nodes[0]['page_from'] or '?'}", "章节"])
    for node in pack.nodes:
        note = f"书页 {node['page_from']}" if node["page_from"] else "未标注页码"
        rows.append(["第3级", course_title, pack.chapter_title, node["title"], "", "",
                     note, "、".join(node["tags"]) or "知识点"])
    return rows

# ---------------------------------------------------------------------------
# 调模型
# ---------------------------------------------------------------------------
def _chapter_text(pages: dict[int, str], first: int, last: int) -> tuple[str, int]:
    """取这一章的文字，返回 (文本, 实际字数)。超长截断并保留页标记。"""
    blocks = []
    for page in range(first, last + 1):
        text = pages.get(page)
        if not text:
            continue
        blocks.append(f"<!-- page {page} -->\n{text}")
    joined = "\n\n".join(blocks)
    return joined[:MAX_CHAPTER_CHARS], len(joined)


def build_messages(chapter_text: str, chapter_hint: str | None, page_offset: int) -> list[dict]:
    hint = f"\n用户提示这一章可能是：{chapter_hint}。不确定就按内容自己定标题。\n" if chapter_hint else ""
    user = (
        f"下面是教材第 {chapter_text.count('<!-- page')} 页的文字。"
        f"页标记里的数字是 PDF 页；书页码 = PDF 页 - {page_offset}。\n"
        f"{hint}\n---- 教材文字开始 ----\n{chapter_text}\n---- 教材文字结束 ----"
    )
    return [
        {"role": "system", "content": GENERATE_SYSTEM},
        {"role": "user", "content": user},
    ]


def fake_payload(chapter_text: str) -> dict:
    """演示模式：结构真实、内容占位。APP_ENV=prod 时 main.py 拒绝启动。"""
    first_line = next((line.strip() for line in chapter_text.splitlines()
                       if line.strip() and not line.startswith("<!--")), "本章内容")
    title = first_line[:30] or "第一章"
    return {
        "chapter_title": f"【演示】{title}",
        "chapter_summary": "演示模式生成，不是模型输出。",
        "nodes": [{
            "title": "演示知识点",
            "content_md": "这是演示模式生成的知识点正文，用于验证上传、排队、校验、入库、"
                          "进审核这条链路。配置模型端点与 API Key 后换成真实内容。",
            "page_from": 1,
            "page_to": 1,
            "tags": ["演示"],
        }],
        "exercises": [{
            "source_no": "演示习题1",
            "page": 1,
            "prompt_md": "演示题目：请说明本页的主要结论。",
            "answer_md": "演示解答：本页主要结论见正文。真实生成会把这里换成推导过程。",
            "difficulty": 2,
            "node_title": "演示知识点",
            "from_textbook": True,
        }],
    }


def call_model(messages: list[dict]) -> tuple[dict, dict]:
    """返回 (解析后的 JSON, 调用元信息)。"""
    if DEV_FAKE_MODEL:
        raw = json.dumps(fake_payload(messages[-1]["content"]), ensure_ascii=False)
        return json.loads(raw), {"provider": "demo", "model": "demo", "data_scope": "internal"}

    result = llm.chat(messages)
    content = result.get("content") or ""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError("模型没有返回 JSON。原文开头：" + content[:200])
    return json.loads(match.group(0)), {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "data_scope": result.get("data_scope"),
        "usage": result.get("usage"),
    }


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def apply_pack(course_id: str, pack: Pack, source: dict, book: dict) -> dict:
    """把校验过的资源包写进库。重复跑同一个任务不会产生重复题目。"""
    chapter = chapter_id(pack.chapter_title)
    prefix = f"gen-{_short_hash(pack.chapter_title, 4)}-"

    ord_row = db.query_one(
        "SELECT coalesce(max(ord), 0) + 1 AS next FROM chapter WHERE course_id = %s", (course_id,))
    db.execute(
        """INSERT INTO chapter (id, course_id, title, ord, path)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (course_id, id) DO UPDATE SET title = EXCLUDED.title""",
        (chapter, course_id, pack.chapter_title, ord_row["next"] if ord_row else 1,
         [pack.chapter_title]),
    )

    # 章本身也要在 knowledge_node 里有一行（level 2、container），
    # 因为 knowledge_node.parent_id 是自引用外键，不能指向 chapter.id——
    # 指向 chapter.id 会直接外键失败。
    db.execute(
        """INSERT INTO knowledge_node
             (course_id, id, chapter_id, parent_id, title, level, kind, path, content_md)
           VALUES (%s, %s, %s, NULL, %s, 2, 'container', %s, %s)
           ON CONFLICT (course_id, id) DO UPDATE
             SET title = EXCLUDED.title, content_md = EXCLUDED.content_md""",
        (course_id, chapter, chapter, pack.chapter_title, [pack.chapter_title],
         pack.chapter_summary),
    )

    for node in pack.nodes:
        db.execute(
            """INSERT INTO knowledge_node
                 (course_id, id, chapter_id, parent_id, title, level, kind, path, content_md)
               VALUES (%s, %s, %s, %s, %s, 3, 'knowledge', %s, %s)
               ON CONFLICT (course_id, id) DO UPDATE
                 SET title = EXCLUDED.title, content_md = EXCLUDED.content_md""",
            (course_id, node["id"], chapter, chapter, node["title"],
             [pack.chapter_title, node["title"]], node["content_md"]),
        )

    # 先清掉这一章上一次生成的题，避免重跑堆积
    db.execute("DELETE FROM exercise WHERE course_id = %s AND id LIKE %s", (course_id, prefix + "%"))

    for index, ex in enumerate(pack.exercises, start=1):
        ex["id"] = f"{prefix}{index:03d}"
        raw_source = exercise_source_text(ex, source)
        provenance = "textbook" if (ex["from_textbook"] and ex["page"]) else "ai-generated"
        db.execute(
            """INSERT INTO exercise
                 (course_id, id, chapter_id, prompt_md, answer_md, difficulty,
                  source_raw, source_book, source_page, source_pdf_page, source_no,
                  provenance, provenance_note, review_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
               ON CONFLICT (course_id, id) DO UPDATE
                 SET prompt_md = EXCLUDED.prompt_md, answer_md = EXCLUDED.answer_md,
                     difficulty = EXCLUDED.difficulty, source_raw = EXCLUDED.source_raw,
                     source_page = EXCLUDED.source_page, provenance = EXCLUDED.provenance""",
            (course_id, ex["id"], chapter, ex["prompt_md"], ex["answer_md"], ex["difficulty"],
             raw_source, book.get("short"), ex["page"], source.get("pdf_page"), ex["source_no"],
             provenance, f"由 {PROMPT_VERSION} 自动生成，未审核"),
        )
        node = next((n for n in pack.nodes if n["title"] == ex["node_title"]), None)
        if node:
            db.execute(
                """INSERT INTO exercise_node (course_id, exercise_id, node_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (course_id, ex["id"], node["id"]),
            )
    return {"chapter_id": chapter, "nodes": len(pack.nodes), "exercises": len(pack.exercises)}


def write_pack_files(course_id: str, job_id: int, pack: Pack, source: dict,
                     book: dict, course_title: str) -> Path:
    target = PACK_DIR / course_id / "generated" / f"job-{job_id}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "知识点.md").write_text(render_knowledge_md(pack, course_title, source), encoding="utf-8")
    (target / "习题.md").write_text(render_exercise_md(pack, course_title, source), encoding="utf-8")
    rows = node_rows(pack, course_title)
    (target / "知识节点.json").write_text(
        json.dumps({"header": ["第1级", "第2级", "第3级", "第4级", "第5级", "第6级",
                               "知识说明", "知识点标签"], "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    (target / "生成说明.json").write_text(
        json.dumps({"prompt_version": PROMPT_VERSION, "source": source, "book": book,
                    "warnings": pack.warnings}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return target
# ---------------------------------------------------------------------------
# 任务队列
# ---------------------------------------------------------------------------
def _job_view(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "status": row["status"],
        "progress": row["progress"], "attempts": row["attempts"],
        "course_id": row.get("course_id"), "textbook_id": row.get("textbook_id"),
        "error": row.get("error"), "result": row.get("result"),
        "created_at": row["created_at"], "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def enqueue(payload: dict, actor: str) -> dict:
    row = db.query_one(
        """INSERT INTO pipeline_job (kind, payload, course_id, textbook_id, requested_by)
           VALUES ('generate', %s::jsonb, %s, %s, %s)
           RETURNING *""",
        (json.dumps(payload, ensure_ascii=False), payload.get("course_id"),
         payload.get("textbook_id"), actor),
    )
    return _job_view(row)


def claim_next() -> dict | None:
    """领取一个待处理任务。

    分两步写，而不是 `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)`：
    PGlite（测试用的 WASM PostgreSQL）对这种"子查询里加锁 + 外层 UPDATE"会返回错位的结果集，
    表现是 psycopg 报 "list index out of range"。两步写在真库上同样安全——
    第二步的 `AND status = 'queued'` 保证并发时只有一个人拿到。
    """
    candidate = db.query_one(
        """SELECT id FROM pipeline_job
            WHERE status = 'queued' AND kind = 'generate'
            ORDER BY created_at
            LIMIT 1"""
    )
    if not candidate:
        return None
    return db.query_one(
        """UPDATE pipeline_job
              SET status = 'running', started_at = now(), attempts = attempts + 1,
                  progress = 5
            WHERE id = %s AND status = 'queued'
        RETURNING *""",
        (candidate["id"],),
    )



def _step(job_id: int, progress: int, **fields) -> None:
    sets = ["progress = %s"]
    params: list[Any] = [progress]
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(value)
    params.append(job_id)
    db.execute(f"UPDATE pipeline_job SET {', '.join(sets)} WHERE id = %s", tuple(params))


def _ensure_course(payload: dict) -> tuple[str, str]:
    course_id = payload["course_id"]
    existing = db.query_one("SELECT id, title FROM course WHERE id = %s", (course_id,))
    if existing:
        return existing["id"], existing["title"]
    title = payload.get("new_course_title") or course_id
    db.execute(
        """INSERT INTO course (id, title, audience, status, schema_version, review_status)
           VALUES (%s, %s, '本科', 'draft', 1, 'draft')""",
        (course_id, title),
    )
    return course_id, title


def _load_pages(textbook: dict) -> dict[int, str]:
    """优先用已抽取的文本；没有就从 PDF 抽一次并存下来。"""
    from . import pdf as pdf_mod

    if textbook.get("text_path"):
        path = Path(textbook["text_path"])
        if path.exists():
            return pdf_mod.parse_markdown(path.read_text(encoding="utf-8"))

    pdf_path = UPLOAD_DIR / f"{textbook['pdf_sha256']}.pdf"
    pages_text, engine = pdf_mod.extract_pages(pdf_path)
    markdown = pdf_mod.to_markdown(pages_text)
    target = UPLOAD_DIR / f"{textbook['pdf_sha256']}.md"
    target.write_text(markdown, encoding="utf-8")
    db.execute(
        """UPDATE textbook SET text_path = %s, text_chars = %s, extracted_by = %s,
                  extracted_at = now() WHERE id = %s""",
        (str(target), len(markdown), engine, textbook["id"]),
    )
    return pdf_mod.parse_markdown(markdown)


def _finish(job_id: int, status: str, *, result: dict | None = None, error: str | None = None) -> None:
    db.execute(
        """UPDATE pipeline_job
              SET status = %s, progress = %s, finished_at = now(),
                  result = %s::jsonb, error = %s
            WHERE id = %s""",
        (status, 100 if status == "succeeded" else 0,
         json.dumps(result, ensure_ascii=False, default=str) if result else None,
         error, job_id),
    )


def run_job(job_id: int) -> dict:
    """执行一个生成任务。失败不抛异常，写进 job.error —— worker 要继续跑下一个。"""
    job = db.query_one("SELECT * FROM pipeline_job WHERE id = %s", (job_id,))
    if not job:
        raise ValueError(f"任务不存在：{job_id}")
    payload = job["payload"] or {}
    try:
        textbook = db.query_one("SELECT * FROM textbook WHERE id = %s", (payload.get("textbook_id"),))
        if not textbook:
            raise ValueError(f"教材不存在：{payload.get('textbook_id')}")

        _step(job_id, 20)
        pages = _load_pages(textbook)
        first = int(payload.get("page_from") or 1)
        last = int(payload.get("page_to") or (textbook.get("page_count") or first))
        chapter_text, chars = _chapter_text(pages, first, last)
        if not chapter_text.strip():
            raise ValueError(f"第 {first}–{last} 页没有抽到文字：可能是扫描件，需要先 OCR")

        _step(job_id, 45)
        offset = int(textbook.get("page_offset") or 0)
        raw, meta = call_model(build_messages(chapter_text, payload.get("chapter_hint"), offset))

        _step(job_id, 65)
        pack = validate_payload(raw, first, last)
        course_id, course_title = _ensure_course({**payload, "course_id": payload["course_id"]})

        book = {
            "short": textbook["title"],
            "line": "，".join(x for x in [textbook.get("authors"),
                                          f"《{textbook['title']}》",
                                          textbook.get("edition")] if x),
        }
        source = {
            "pdf_page": first, "page_from": first, "page_to": last,
            "book_line": book["line"], "book_short": book["short"],
            "generated_at": str(job["created_at"]),
            "prompt_version": PROMPT_VERSION,
            "model": meta.get("model"), "provider": meta.get("provider"),
            "chars": chars, "truncated": chars > MAX_CHAPTER_CHARS,
        }

        _step(job_id, 80)
        pack_dir = write_pack_files(course_id, job_id, pack, source, book, course_title)
        counts = apply_pack(course_id, pack, source, book)

        _finish(job_id, "succeeded", result={
            "course_id": course_id, "chapter_id": counts["chapter_id"],
            "nodes": counts["nodes"], "exercises": counts["exercises"],
            "warnings": pack.warnings, "pack_dir": str(pack_dir),
            "prompt_version": PROMPT_VERSION, "model": meta.get("model"),
            "data_scope": meta.get("data_scope"), "truncated": source["truncated"],
        })
    except Exception as error:  # noqa: BLE001 - 任何失败都要落进任务记录
        # 除了写进任务，还要打到 stderr：PGlite 在一次 SQL 报错后协议会错位，
        # 那时再去读任务表可能读不到东西，日志是唯一还能看到原始错误的地方。
        print(f"[任务 {job_id}] 失败：{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        _finish(job_id, "failed", error=f"{type(error).__name__}: {error}")
    row = db.query_one("SELECT * FROM pipeline_job WHERE id = %s", (job_id,))
    return _job_view(row) if row else {"id": job_id, "status": "failed",
                                       "error": "任务记录读不出来（数据库可能已回滚）"}


def process_once() -> dict | None:
    job = claim_next()
    return run_job(job["id"]) if job else None


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "80"))


@router.post("/textbooks", status_code=201)
async def upload_textbook(
    request: Request,
    title: str,
    course_id: str,
    edition: str | None = None,
    authors: str | None = None,
    page_offset: int = 0,
    actor: Actor = Depends(require("admin")),
) -> dict:
    """上传教材 PDF。

    用原始字节而不是 multipart：省掉 python-multipart 依赖，前端直接 fetch(file)
    就能传，服务器上少一个装不上的包。代价是不能一次传多个文件——批量导入走命令行。
    """
    from . import pdf as pdf_mod

    data = await request.body()
    if not data:
        raise HTTPException(400, "请求体为空：请把 PDF 文件内容作为 body 发过来")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB} MB")

    report = pdf_mod.probe_bytes(data)
    if not report["is_pdf"]:
        raise HTTPException(400, "不是 PDF 文件")

    digest = hashlib.sha256(data).hexdigest()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"{digest}.pdf").write_bytes(data)

    course = db.query_one("SELECT 1 FROM course WHERE id = %s", (course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{course_id}。请先在课程管理里建课，或生成时指定新课程。")

    textbook_id = f"{course_id}-{digest[:8]}"
    db.execute(
        """INSERT INTO textbook
             (id, course_id, title, edition, authors, pdf_sha256, pdf_bytes, page_count,
              has_text_layer, uploaded_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE
             SET title = EXCLUDED.title, edition = EXCLUDED.edition,
                 authors = EXCLUDED.authors, page_count = EXCLUDED.page_count,
                 has_text_layer = EXCLUDED.has_text_layer""",
        (textbook_id, course_id, title, edition, authors, digest, len(data),
         report["page_count"], report["has_text_layer"], actor.id),
    )
    return {
        "textbook_id": textbook_id,
        "page_count": report["page_count"],
        "has_text_layer": report["has_text_layer"],
        "note": report["note"],
        "probe_engine": report["engine"],
        "engines_available": pdf_mod.available_engines(),
        "bytes": len(data),
    }


@router.post("/textbooks/{textbook_id}/generate", status_code=202)
def start_generation(textbook_id: str, body: GenerateIn,
                     actor: Actor = Depends(require("admin"))) -> dict:
    textbook = db.query_one("SELECT * FROM textbook WHERE id = %s", (textbook_id,))
    if not textbook:
        raise HTTPException(404, f"教材不存在：{textbook_id}")
    if body.page_to < body.page_from:
        raise HTTPException(400, "起始页不能大于结束页")

    course_id = body.new_course_id or textbook["course_id"]
    payload = {
        "textbook_id": textbook_id,
        "course_id": course_id,
        "new_course_title": body.new_course_title,
        "page_from": body.page_from, "page_to": body.page_to,
        "chapter_hint": body.chapter_hint,
    }
    job = enqueue(payload, actor.id)
    return {"job": job, "hint": "worker 会领取这个任务；用 GET /api/v1/jobs/{id} 看进度。"}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, actor: Actor = Depends(require("reviewer"))) -> dict:
    row = db.query_one("SELECT * FROM pipeline_job WHERE id = %s", (job_id,))
    if not row:
        raise HTTPException(404, f"任务不存在：{job_id}")
    return _job_view(row)


@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = 20,
              actor: Actor = Depends(require("reviewer"))) -> dict:
    rows = db.query(
        """SELECT * FROM pipeline_job
           WHERE (%s::text IS NULL OR status = %s::text)
           ORDER BY created_at DESC LIMIT %s""",
        (status, status, max(1, min(limit, 100))),
    )
    return {"items": [_job_view(row) for row in rows]}