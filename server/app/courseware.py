"""按章/节生成课件（PPT），并按教师意见迭代修订。

教师这一侧的期望是「点一下，先给我一版能改的」：所以这里做的是**初稿**，
不是成品——每一页都带「待教师确认」，每一版都进 needs-review。

三个刻意的设计：

1. **内容归模型、排版归代码**。模型产出结构化页面计划（标题、要点、公式文本、
   来源 ID），pptx 由 slidedeck.py 渲染。排版错一次会出现在每一页上，代码好改；
   模型每次生成都可能错，改不了。
2. **只喂本地资料**。上下文来自这门课自己的知识点正文与习题（数据库），
   不联网、不引用库里没有的书页。资料不足时宁可少写一页，也不补一段想像。
3. **change_log 由代码 diff 生成**，不用模型自报。模型说自己"只改了第 3 页"是
   不可信的；比较两版计划得到的差异是可验证的。教师点开就能看到"建议了什么、
   实际动了哪几页"。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import db, llm
from .auth import Actor, require, require_login

router = APIRouter(prefix="/api/v1/courseware", tags=["courseware"])

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads")) / "courseware"

MAX_CONTEXT_CHARS = 14000     # 一次生成喂进去的本地资料上限（约 1 个半小时的课）
MAX_EXERCISES = 20
MIN_SLIDES, MAX_SLIDES = 6, 40
DEFAULT_PAGES = 12


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

GENERATE_SYSTEM = """你是大学数学课的助教，负责把一章/一节教材整理成课堂幻灯片的初稿。教师会在此基础上修改，所以你写的是"可改的初稿"，不是定稿。

硬规则：

1. 只能使用输入资料里出现的定义、命题、例子、习题与页号。资料里没有的定理、证明、页码、习题答案，一个字都不许补。某个知识点资料不足时，把这一页的 bullets 写成教师可接着填的骨架，并在 speakerNote 里写明"资料缺口：缺什么"。
2. 结构按授课顺序，不按教材目录顺序：学习目标 → 概念与条件 → 推导或例子 → 课堂练习 → 小结。页数由内容决定（目标 {pages} 页左右），不为凑页数重复。
3. 每一页要有实际内容：要点是完整的一句话或一个公式，不是"介绍某某"这种标题。
4. 公式写成**纯文本**（Unicode 数学符号），例如 "ω = dθ"、"∫_a^b f(x)dx"、"λ² + 1 = 0"。不要写 LaTeX 命令：\\frac、\\int、\\mathbb 这些在幻灯片上会原样显示成乱码。需要分式就写成 "a/b"，需要根号写 "√x"。
5. 每页给 sourceNodeIds，只能取输入里给过的节点 ID；引用了习题的页，把习题 ID 放进 exerciseIds。不确定就不写，不要编 ID。
6. speakerNote 写给教师看：这一页想讲什么、哪里需要板书、哪一句可能引起疑问。不要重复页面上的字。

只输出 JSON，不要解释，不要 markdown 代码块：
{{"title":"这一版课件的标题","subtitle":"副标题（可空）","slides":[
  {{"title":"页标题","kind":"content","paragraphs":["整段说明（可空）"],
    "bullets":["要点一","要点二"],"equations":[{{"text":"ω = dθ","latex":"\\\\omega = d\\\\theta"}}],
    "sourceNodeIds":["analysis-ii-g003"],"exerciseIds":[],"bookPages":"p.12","speakerNote":"..."}}]}}

kind 取值：cover（第一页封面）、section（小节过渡）、content、exercise（课堂练习）、summary（小结）。"""

REVISE_SYSTEM = """你在修订一份已有的课堂幻灯片初稿。教师提了具体意见，你要在不破坏其他页的前提下改到位。

硬规则：

1. 输出**完整的新页面计划**（不是只给改动的页），格式与原来一致。没被意见涉及的页，标题与要点保持原样——教师会对比，无关改动等于制造噪声。
2. 逐条落实教师的意见；意见里点明的页号优先处理。意见与资料冲突（比如要求证明资料里没有的结论、要求改一个正确公式）时，不要照改：保持原样，并把冲突写进该页 speakerNote，开头写"未采纳：……"。
3. 新增内容仍只用输入资料里的事实；新增页同样要给 sourceNodeIds。
4. 公式仍是纯文本 Unicode，不用 LaTeX 命令。
5. 页数不要因为一次修改大幅膨胀：能在一页里加两条要点，就不要新开三页。

只输出 JSON，结构与输入一致：{"title":...,"subtitle":...,"slides":[...]}"""


def generate_messages(context: dict, *, focus: str, pages: int, audience: str) -> list[dict]:
    return [
        {"role": "system", "content": GENERATE_SYSTEM.format(pages=pages)},
        {"role": "user", "content": json.dumps({
            "course": context["course_title"],
            "chapter": context["chapter_title"],
            "audience": audience,
            "teacher_focus": focus or "",
            "knowledge_nodes": context["nodes"],
            "exercises": context["exercises"],
            "note": "以上是这门课数据库里的全部可用资料；没有的东西不要编。",
        }, ensure_ascii=False)},
    ]


def revise_messages(context: dict, base_plan: dict, instruction: str,
                    audience: str) -> list[dict]:
    return [
        {"role": "system", "content": REVISE_SYSTEM},
        {"role": "user", "content": json.dumps({
            "instruction": instruction,
            "audience": audience,
            "base_plan": base_plan,
            "knowledge_nodes": context["nodes"],
            "exercises": context["exercises"],
        }, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------------------
# 本地知识库
# ---------------------------------------------------------------------------

def _nodes(course_id: str, chapter_id: str) -> tuple[dict, list[dict]]:
    rows = db.query(
        """SELECT id, title, level, path, content_md
             FROM knowledge_node WHERE course_id = %s ORDER BY level, id""",
        (course_id,),
    )
    target = next((row for row in rows if row["id"] == chapter_id), None)
    if not target:
        raise ValueError(f"章节不存在：{chapter_id}")
    prefix = list(target["path"] or [])

    def inside(row: dict) -> bool:
        path = list(row["path"] or [])
        return path[:len(prefix)] == prefix

    return target, [row for row in rows if inside(row)]


def collect_context(course_id: str, chapter_id: str) -> dict:
    course = db.query_one("SELECT id, title FROM course WHERE id = %s", (course_id,))
    if not course:
        raise ValueError(f"课程不存在：{course_id}")
    target, nodes = _nodes(course_id, chapter_id)
    node_ids = [row["id"] for row in nodes]

    exercises = db.query(
        """SELECT e.id, e.prompt_md, e.answer_md, e.difficulty, e.source_no
             FROM exercise e
             JOIN exercise_node en ON en.course_id = e.course_id AND en.exercise_id = e.id
            WHERE e.course_id = %s AND en.node_id = ANY(%s)
              AND e.review_status = 'published'
            ORDER BY e.difficulty NULLS LAST, e.id
            LIMIT %s""",
        (course_id, node_ids, MAX_EXERCISES),
    )

    budget = MAX_CONTEXT_CHARS
    slim_nodes = []
    for row in nodes:
        body = (row.get("content_md") or "").strip()
        if len(body) > budget:
            body = body[:budget] + "…（后文因长度截断）"
        budget -= len(body)
        slim_nodes.append({"id": row["id"], "title": row["title"],
                           "level": row["level"], "content": body})

    context = {
        "course_id": course_id,
        "course_title": course["title"],
        "chapter_id": chapter_id,
        "chapter_title": target["title"],
        "nodes": slim_nodes,
        "exercises": [{"id": row["id"], "prompt": row["prompt_md"],
                       "answer": row["answer_md"], "difficulty": row.get("difficulty"),
                       "no": row.get("source_no")} for row in exercises],
    }
    context["source_sha256"] = _source_hash(context)
    return context


def _source_hash(context: dict) -> str:
    """资料指纹：换了知识点正文或题目，旧课件就该被标成"可能过期"。"""
    digest = hashlib.sha256()
    for row in context["nodes"]:
        digest.update(f"{row['id']}:{row['content']}".encode("utf-8"))
    for row in context["exercises"]:
        digest.update(f"{row['id']}:{row['prompt']}".encode("utf-8"))
    return digest.hexdigest()


def empty_context_warning(context: dict) -> str | None:
    if not any(row["content"] for row in context["nodes"]):
        return "这一章还没有知识点正文：生成出来只会是标题骨架，建议先导入或生成知识点"
    if not context["exercises"]:
        return "这一章没有已发布习题：课件里不会有课堂练习页"
    return None


# ---------------------------------------------------------------------------
# 页面计划：规范化 + 变更比对
# ---------------------------------------------------------------------------

def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_plan(raw, allowed_ids: set[str], pages: int) -> dict:
    """把模型输出收成确定的结构：类型、长度、ID 白名单、页数上限都在这里拦。"""
    if isinstance(raw, str):
        raw = _json_payload(raw)
    if not isinstance(raw, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    slides = raw.get("slides")
    if not isinstance(slides, list) or not slides:
        raise ValueError("模型没有返回 slides")

    cleaned: list[dict] = []
    for index, slide in enumerate(slides[:MAX_SLIDES]):
        if not isinstance(slide, dict):
            continue
        equations = []
        for item in (slide.get("equations") or [])[:6]:
            if isinstance(item, dict):
                text = _text(item.get("text"), 120)
                latex = _text(item.get("latex"), 200)
            else:
                text, latex = _text(item, 120), ""
            # text 里残留 LaTeX 命令时压成普通写法：幻灯片上 \frac{a}{b} 只会显示乱码
            text = _latex_to_text(text)
            if text:
                equations.append({"text": text, "latex": latex})
        node_ids = [str(value) for value in (slide.get("sourceNodeIds") or [])
                    if str(value) in allowed_ids]
        cleaned.append({
            "title": _text(slide.get("title"), 120) or f"第 {index + 1} 页",
            "kind": _text(slide.get("kind"), 20) or ("cover" if index == 0 else "content"),
            "subtitle": _text(slide.get("subtitle"), 160),
            "paragraphs": [_text(text, 600) for text in (slide.get("paragraphs") or [])[:3]
                           if _text(text, 600)],
            "bullets": [_text(text, 400) for text in (slide.get("bullets") or [])[:8]
                        if _text(text, 400)],
            "equations": equations,
            "sourceNodeIds": node_ids[:8],
            "exerciseIds": [str(value) for value in (slide.get("exerciseIds") or [])][:8],
            "bookPages": _text(slide.get("bookPages"), 60),
            "speakerNote": _text(slide.get("speakerNote"), 1200),
        })
    if not cleaned:
        raise ValueError("页面计划里没有可用的页")
    if cleaned[0]["kind"] != "cover":
        cleaned[0]["kind"] = "cover"

    return {
        "title": _text(raw.get("title"), 120) or "",
        "subtitle": _text(raw.get("subtitle"), 160),
        "slides": cleaned,
        "pageTarget": pages,
    }


_LATEX_PATTERNS = [
    (r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)"),
    (r"\\dfrac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)"),
    (r"\\sqrt\{([^{}]*)\}", r"√(\1)"),
    (r"\\(?:mathrm|mathbb|mathcal|text)\{([^{}]*)\}", r"\1"),
    (r"\\int", "∫"),
    (r"\\sum", "Σ"),
    (r"\\partial", "∂"),
    (r"\\omega", "ω"),
    (r"\\theta", "θ"),
    (r"\\lambda", "λ"),
    (r"\\alpha", "α"),
    (r"\\beta", "β"),
    (r"\\pi", "π"),
    (r"\\infty", "∞"),
    (r"\\cdot", "·"),
    (r"\\times", "×"),
    (r"\\le", "≤"),
    (r"\\ge", "≥"),
    (r"\\neq", "≠"),
    (r"\\to", "→"),
    (r"\\Rightarrow", "⇒"),
]


def _latex_to_text(text: str) -> str:
    """把模型偶尔漏出来的 LaTeX 命令降级成能看的文字。

    不是完整转换：只处理幻灯片上最常见、破坏性最大的几种。真正的 LaTeX
    留档在 equations[].latex 里，需要精确排版时从那里取。
    """
    if "\\" not in text:
        return text
    result = text
    for pattern, replacement in _LATEX_PATTERNS:
        result = re.sub(pattern, replacement, result)
    result = re.sub(r"\$", "", result)
    result = re.sub(r"\\([A-Za-z]+)", r"\1", result)
    return result.strip()


def diff_change_log(base: dict | None, new: dict) -> list[dict]:
    """比较两版计划，产出人可读的改动清单。

    只做到"页级 + 要点数量"，不做逐字 diff：教师关心的是"哪几页变了"，
    逐字差异已经被他写在建议里了。
    """
    if not base:
        return [{"kind": "create", "page": index + 1, "title": slide["title"],
                 "summary": f"初版第 {index + 1} 页"}
                for index, slide in enumerate(new.get("slides", []))][:40]

    old_slides = base.get("slides") or []
    new_slides = new.get("slides") or []
    changes: list[dict] = []
    for index in range(max(len(old_slides), len(new_slides))):
        before = old_slides[index] if index < len(old_slides) else None
        after = new_slides[index] if index < len(new_slides) else None
        if before is None:
            changes.append({"kind": "add", "page": index + 1, "title": after["title"],
                            "summary": f"新增：{after['title']}"})
        elif after is None:
            changes.append({"kind": "delete", "page": index + 1, "title": before["title"],
                            "summary": f"删除：{before['title']}"})
        else:
            bullets = len(after.get("bullets") or []) - len(before.get("bullets") or [])
            equations = len(after.get("equations") or []) - len(before.get("equations") or [])
            if before.get("title") != after.get("title"):
                changes.append({"kind": "move", "page": index + 1, "title": after["title"],
                                "summary": f"改标题：{before['title']} → {after['title']}"})
            elif bullets or equations:
                parts = []
                if bullets:
                    parts.append(f"要点 {bullets:+d}")
                if equations:
                    parts.append(f"公式 {equations:+d}")
                changes.append({"kind": "edit", "page": index + 1, "title": after["title"],
                                "summary": "、".join(parts)})
            elif before.get("paragraphs") != after.get("paragraphs"):
                changes.append({"kind": "edit", "page": index + 1, "title": after["title"],
                                "summary": "重写正文段落"})
    return changes[:60]


def _json_payload(text: str) -> dict:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fenced:
        raw = fenced.group(1).strip()
    start = min([index for index in (raw.find("{"), raw.find("[")) if index >= 0], default=-1)
    if start < 0:
        raise ValueError(f"模型没有返回 JSON：{raw[:200]}")
    try:
        data, _ = json.JSONDecoder().raw_decode(raw, start)
    except json.JSONDecodeError as error:
        raise ValueError(f"模型返回的不是合法 JSON：{raw[:200]}") from error
    if not isinstance(data, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return data


# ---------------------------------------------------------------------------
# 渲染与存储
# ---------------------------------------------------------------------------

def _call_text(messages: list[dict], temperature: float = 0.3) -> str:
    return llm.chat(messages, temperature=temperature)["content"]


def render_version(version_id: str, plan: dict, meta: dict) -> dict:
    """渲染 pptx 并落盘。渲染失败保留旧版本，不写半成品。"""
    from . import slidedeck

    data = slidedeck.render(plan, meta)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / f"{version_id}.pptx"
    target.write_bytes(data)
    relative = str(target.relative_to(UPLOAD_DIR.parent))
    digest = hashlib.sha256(data).hexdigest()
    db.execute(
        """UPDATE courseware_version
              SET pptx_path = %s, pptx_bytes = %s, sha256 = %s, slide_count = %s
            WHERE id = %s""",
        (relative, len(data), digest, len(plan["slides"]), version_id),
    )
    return {"path": relative, "bytes": len(data), "sha256": digest,
            "slide_count": len(plan["slides"])}


def _finish_version(version_id: str, *, plan: dict, title: str, model: str,
                    source_sha256: str, meta: dict,
                    change_log: list[dict] | None = None) -> dict:
    # 先写 plan 再渲染：渲染要读 slides 定页数，但落库的顺序不影响一致性——
    # 两者都在同一个任务里，任务失败时 status 会被改成 failed。
    db.execute(
        """UPDATE courseware_version
              SET status = 'needs-review', title = %s, slide_plan = %s::jsonb,
                  change_log = %s::jsonb, model = %s, source_sha256 = %s, error = NULL
            WHERE id = %s""",
        (title, json.dumps(plan, ensure_ascii=False),
         json.dumps(change_log or [], ensure_ascii=False), model, source_sha256, version_id),
    )
    return render_version(version_id, plan, meta)


# ---------------------------------------------------------------------------
# worker 入口
# ---------------------------------------------------------------------------

def run_job(job: dict) -> dict:
    from .generation import _finish, _step

    payload = job.get("payload") or {}
    version_id = payload.get("version_id")
    if not version_id:
        _finish(job["id"], "failed", error="任务缺 version_id")
        return {"id": job["id"], "status": "failed"}

    try:
        _step(job["id"], 15)
        row = db.query_one(
            """SELECT v.*, d.course_id, d.chapter_id, d.chapter_title, d.audience,
                      c.title AS course_title
                 FROM courseware_version v
                 JOIN courseware_deck d ON d.id = v.deck_id
                 JOIN course c ON c.id = d.course_id
                WHERE v.id = %s""",
            (version_id,))
        if not row:
            raise ValueError(f"课件版本不存在：{version_id}")

        context = collect_context(row["course_id"], row["chapter_id"])
        _step(job["id"], 30)
        audience = row.get("audience") or "本科高年级"
        page_target = row.get("page_target") or DEFAULT_PAGES
        revising = job["kind"] == "courseware-revise"
        base_plan = row.get("base_plan") or {}

        if revising:
            messages = revise_messages(context, base_plan,
                                       row.get("instruction") or "", audience)
        else:
            messages = generate_messages(context, focus=row.get("instruction") or "",
                                         pages=page_target, audience=audience)

        content = _call_text(messages)
        _step(job["id"], 70)
        allowed = {node["id"] for node in context["nodes"]}
        plan = normalize_plan(content, allowed, page_target)
        change_log = diff_change_log(base_plan if revising else None, plan)
        title = f"{context['chapter_title']} 课件"
        if revising:
            title = f"{title}（v{row['number']}）"
        elif plan.get("title"):
            title = plan["title"]

        _step(job["id"], 85)
        meta = {
            "course_title": row.get("course_title") or "",
            "chapter_title": row.get("chapter_title") or context["chapter_title"],
            "audience": audience,
            "book_pages": (plan["slides"][0].get("bookPages") if plan.get("slides") else "") or "",
        }
        stored = _finish_version(version_id, plan=plan, title=title,
                                 model=llm.chat_endpoint().model,
                                 source_sha256=context["source_sha256"],
                                 meta=meta, change_log=change_log)
        db.execute("UPDATE courseware_deck SET updated_at = now() WHERE id = %s",
                   (row["deck_id"],))
        result = {"version_id": version_id, "deck_id": row["deck_id"],
                  "slides": stored["slide_count"], "bytes": stored["bytes"],
                  "changes": len(change_log), "warning": empty_context_warning(context)}
        _step(job["id"], 98)
        _finish(job["id"], "succeeded", result=result)
        return {"id": job["id"], "status": "succeeded", **result}
    except Exception as error:  # noqa: BLE001 —— worker 不能因为一次渲染失败就退出
        message = f"{type(error).__name__}: {error}"
        db.execute("UPDATE courseware_version SET status = 'failed', error = %s WHERE id = %s",
                   (message[:500], version_id))
        _finish(job["id"], "failed", error=message)
        return {"id": job["id"], "status": "failed", "error": message}


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------

def _version_view(row: dict, *, with_plan: bool = False) -> dict:
    view = {
        "id": row["id"],
        "number": row["number"],
        "based_on": row.get("based_on"),
        "status": row["status"],
        "title": row["title"],
        "instruction": row.get("instruction"),
        "slide_count": row.get("slide_count") or 0,
        "pptx_bytes": row.get("pptx_bytes"),
        "model": row.get("model"),
        "error": row.get("error"),
        "created_at": row.get("created_at"),
        "change_log": row.get("change_log") or [],
        "source_changed": bool(row.get("source_sha256") and row.get("current_source_sha256")
                               and row["source_sha256"] != row["current_source_sha256"]),
    }
    if with_plan:
        view["plan"] = row.get("slide_plan") or {}
    return view


def _deck_row(deck_id: str) -> dict:
    row = db.query_one("SELECT * FROM courseware_deck WHERE id = %s", (deck_id,))
    if not row:
        raise HTTPException(404, f"课件不存在：{deck_id}")
    return row


def _guard_write(actor: Actor) -> None:
    if not actor.has("teacher", "admin"):
        raise HTTPException(403, "生成与修订课件需要教师权限")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.get("/chapters")
def list_chapters(course_id: str = Query(min_length=1),
                  actor: Actor = Depends(require_login)) -> dict:
    """章 → 节 两级目录，带每节的知识点/习题数与已有课件版本数。"""
    rows = db.query(
        """SELECT id, title, level, parent_id,
                  (SELECT count(*) FROM knowledge_node child
                    WHERE child.course_id = k.course_id AND child.parent_id = k.id) AS children,
                  length(coalesce(content_md, '')) AS body_length
             FROM knowledge_node k
            WHERE course_id = %s AND level IN (2, 3)
            ORDER BY level, id""",
        (course_id,),
    )
    decks = db.query(
        """SELECT d.chapter_id, d.id AS deck_id, count(v.id) AS versions,
                  max(v.number) AS latest
             FROM courseware_deck d
             LEFT JOIN courseware_version v ON v.deck_id = d.id AND v.status <> 'failed'
            WHERE d.course_id = %s
            GROUP BY d.chapter_id, d.id""",
        (course_id,),
    )
    by_chapter = {row["chapter_id"]: row for row in decks}
    chapters = []
    for row in rows:
        if row["level"] != 2:
            continue
        chapters.append({
            "id": row["id"], "title": row["title"], "body_length": row["body_length"] or 0,
            "deck": by_chapter.get(row["id"]), "sections": [],
        })
    index = {chapter["id"]: chapter for chapter in chapters}
    for row in rows:
        if row["level"] != 3:
            continue
        parent = index.get(row.get("parent_id"))
        entry = {"id": row["id"], "title": row["title"], "children": row["children"],
                 "body_length": row["body_length"] or 0, "deck": by_chapter.get(row["id"])}
        if parent:
            parent["sections"].append(entry)
        else:
            chapters.append({**entry, "sections": [], "orphan": True})
    return {"chapters": chapters}


class DeckIn(BaseModel):
    course_id: str = Field(min_length=1, max_length=80)
    chapter_id: str = Field(min_length=1, max_length=120)
    audience: str | None = Field(default=None, max_length=40)
    pages: int = Field(default=DEFAULT_PAGES, ge=MIN_SLIDES, le=MAX_SLIDES)
    focus: str | None = Field(default=None, max_length=600)


@router.post("", status_code=201)
def create_deck(body: DeckIn, actor: Actor = Depends(require("teacher"))) -> dict:
    _guard_write(actor)
    node = db.query_one(
        "SELECT id, title, level FROM knowledge_node WHERE course_id = %s AND id = %s",
        (body.course_id, body.chapter_id))
    if not node:
        raise HTTPException(404, f"这一章/节不存在：{body.chapter_id}")

    deck = db.query_one(
        "SELECT * FROM courseware_deck WHERE course_id = %s AND chapter_id = %s",
        (body.course_id, body.chapter_id))
    if deck:
        deck_id = deck["id"]
        number = (db.query_one(
            "SELECT coalesce(max(number), 0) AS n FROM courseware_version WHERE deck_id = %s",
            (deck_id,))["n"]) + 1
    else:
        deck_id = f"cw-{hashlib.sha1(f'{body.course_id}/{body.chapter_id}'.encode()).hexdigest()[:10]}"
        db.execute(
            """INSERT INTO courseware_deck
                 (id, course_id, chapter_id, chapter_title, node_level, title, audience, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (deck_id, body.course_id, body.chapter_id, node["title"], node["level"],
             f"{node['title']} 课件", body.audience, actor.id))
        number = 1

    version_id = f"{deck_id}-v{number}"
    db.execute(
        """INSERT INTO courseware_version
             (id, deck_id, number, status, title, instruction, page_target, created_by)
           VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s)""",
        (version_id, deck_id, number, f"{node['title']} 课件", body.focus or "",
         body.pages, actor.id))
    if body.audience:
        db.execute("UPDATE courseware_deck SET audience = %s WHERE id = %s",
                   (body.audience, deck_id))

    from . import generation
    job = generation.enqueue({"deck_id": deck_id, "version_id": version_id},
                             actor.id, kind="courseware-generate")
    return {"deck_id": deck_id, "version_id": version_id, "job": job}


class ReviseIn(BaseModel):
    instruction: str = Field(min_length=2, max_length=2000)
    base_version: str | None = Field(default=None, max_length=80)


@router.post("/{deck_id}/revise", status_code=201)
def revise_deck(deck_id: str, body: ReviseIn,
                actor: Actor = Depends(require("teacher"))) -> dict:
    deck = _deck_row(deck_id)
    versions = db.query(
        "SELECT * FROM courseware_version WHERE deck_id = %s ORDER BY number DESC",
        (deck_id,))
    if not versions:
        raise HTTPException(400, "这份课件还没有任何版本：先生成初版")
    base = next((row for row in versions if row["id"] == body.base_version), None)
    if body.base_version and not base:
        raise HTTPException(404, f"基础版本不存在：{body.base_version}")
    base = base or next((row for row in versions if row["status"] in ("needs-review", "approved")),
                        versions[0])
    if base["status"] == "pending":
        raise HTTPException(409, "上一版还在生成，等它出来再改")

    number = max(row["number"] for row in versions) + 1
    version_id = f"{deck_id}-v{number}"
    db.execute(
        """INSERT INTO courseware_version
             (id, deck_id, number, based_on, status, title, instruction,
              page_target, base_plan, created_by)
           VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, %s::jsonb, %s)""",
        (version_id, deck_id, number, base["id"], f"{deck['chapter_title']} 课件",
         body.instruction, base.get("page_target") or DEFAULT_PAGES,
         json.dumps(base.get("slide_plan") or {}, ensure_ascii=False), actor.id))
    from . import generation
    job = generation.enqueue({"deck_id": deck_id, "version_id": version_id},
                             actor.id, kind="courseware-revise")
    return {"deck_id": deck_id, "version_id": version_id, "base_version": base["id"], "job": job}


@router.get("/{deck_id}")
def get_deck(deck_id: str, version: str | None = Query(default=None),
             actor: Actor = Depends(require_login)) -> dict:
    deck = _deck_row(deck_id)
    rows = db.query(
        """SELECT * FROM courseware_version WHERE deck_id = %s ORDER BY number DESC""",
        (deck_id,))
    try:
        context = collect_context(deck["course_id"], deck["chapter_id"])
        current = context["source_sha256"]
    except ValueError:
        current = None
    for row in rows:
        row["current_source_sha256"] = current
    selected = next((row for row in rows if row["id"] == version), None)
    selected = selected or next((row for row in rows
                                 if row["status"] in ("needs-review", "approved")), None)
    selected = selected or (rows[0] if rows else None)
    return {
        "deck": {**deck, "source_changed": bool(selected and selected.get("source_sha256")
                                                and current
                                                and selected["source_sha256"] != current)},
        "versions": [_version_view(row) for row in rows],
        "selected": _version_view(selected, with_plan=True) if selected else None,
        "can_edit": actor.has("teacher", "admin"),
    }


@router.get("/versions/{version_id}/pptx")
def download_pptx(version_id: str, actor: Actor = Depends(require_login)) -> Response:
    row = db.query_one("SELECT * FROM courseware_version WHERE id = %s", (version_id,))
    if not row:
        raise HTTPException(404, f"版本不存在：{version_id}")
    if not row.get("pptx_path"):
        raise HTTPException(409, "这一版还没有生成文件")
    path = UPLOAD_DIR.parent / row["pptx_path"]
    if not path.exists():
        raise HTTPException(410, "文件已不在服务器上")
    return Response(
        content=path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{version_id}.pptx"'},
    )


class StatusIn(BaseModel):
    status: str = Field(pattern="^(approved|needs-review)$")


@router.post("/versions/{version_id}/status")
def set_status(version_id: str, body: StatusIn,
               actor: Actor = Depends(require("teacher"))) -> dict:
    row = db.query_one("SELECT * FROM courseware_version WHERE id = %s", (version_id,))
    if not row:
        raise HTTPException(404, f"版本不存在：{version_id}")
    if body.status == "approved" and row["status"] not in ("needs-review", "approved"):
        raise HTTPException(409, "只有已生成完的版本才能确认")
    db.execute("UPDATE courseware_version SET status = %s WHERE id = %s",
               (body.status, version_id))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'courseware.status', 'courseware_version', %s, %s::jsonb)""",
        (actor.id, version_id,
         json.dumps({"from": row["status"], "to": body.status}, ensure_ascii=False)),
    )
    return {"version_id": version_id, "status": body.status}


@router.delete("/versions/{version_id}")
def delete_version(version_id: str, actor: Actor = Depends(require("admin"))) -> dict:
    db.execute("DELETE FROM courseware_version WHERE id = %s", (version_id,))
    return {"deleted": version_id}
