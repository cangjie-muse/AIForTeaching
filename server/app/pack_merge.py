"""单文件增量导入：只上传 知识点.md 或 习题.md 时，按锚点改库里已有的内容。

为什么不做成"把整包补齐再走整包导入"：整包导入会把整门课重写一遍——每个知识点
的 content_version 都 +1、批次全变、review_status 全部退回 draft。老师只改了一版
习题，不该让整本书的审核状态跟着归零。

所以这里是**定点修改**：
  · 习题.md → 只动这个锚点下挂的题，其他知识点的题一个字不碰；
  · 知识点.md → 只更新这些锚点的正文，结构（层级、父子）完全不动。

锚点对不上就是"这份文件不属于这门课"或"知识点被删过"，一律报出来让老师看见，
不能悄悄丢掉——丢了以后没人知道少了什么。
"""

from __future__ import annotations

import re
from typing import Any

from . import db
from .content import parse_source

# 与 js/import-core.js 的同名正则必须一致：网页整包导入和后端单文件导入
# 走两条路，解析规则一旦分叉，同一份习题.md 会解析出不同的题。
ANCHOR = re.compile(r"""<a\s+id=["']([^"']+)["']\s*>\s*</a>\s*\n(#{1,6})\s+([^\n]+)\n""")
MARKER = re.compile(r"<!--\s*source-exercise:([^\s]+)\s*-->")
# 只认行首题号。答案里缩进的 "   1. " 是解题步骤，算新题会把一道题拆成三道。
QUESTION = re.compile(r"^(\d+)\.\s", re.MULTILINE)
ANSWER = re.compile(r"\*\*答案：\*\*")
META = re.compile(r"\*\*难度：(\d)/5；出处：\*\*")
NUMBERED = re.compile(r"^\d+\.\s*")
INDENT = re.compile(r"^ {3}", re.MULTILINE)


def stable_id(text: str) -> str:
    """内容哈希当稳定 ID，算法与 js/import-core.js 的 stableQuestionId 逐位一致。

    两边必须一致：老师先用网页整包导入，再用单文件改一版，如果哈希不同，
    同一道题会被当成两道——旧题变孤儿、新题重复，而且数据上看不出错。
    """
    first, second = 2166136261, 2246822519
    for character in __import__("unicodedata").normalize("NFKC", text):
        code = ord(character)
        first = ((first ^ code) * 16777619) & 0xFFFFFFFF
        second = ((second ^ code) * 3266489917) & 0xFFFFFFFF
    return f"q-{first:08x}{second:08x}"


def sections(markdown: str) -> dict[str, dict[str, str]]:
    """Markdown → {锚点: {title, body}}，与前端 sections() 同规则。"""
    matches = list(ANCHOR.finditer(markdown))
    result: dict[str, dict[str, str]] = {}
    for index, match in enumerate(matches):
        anchor, hashes, title = match.group(1), match.group(2), match.group(3).strip()
        if anchor in result:
            raise ValueError(f"Markdown 出现重复锚点：{anchor}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end():end]
        heading = re.compile(rf"^#{{1,{len(hashes)}}}\s+", re.MULTILINE).search(body)
        if heading:
            body = body[:heading.start()]
        result[anchor] = {"title": title, "body": body.strip()}
    if not result:
        raise ValueError('Markdown 中未发现 <a id="…"></a> 知识点锚点。')
    return result


def split_questions(body: str) -> list[str]:
    starts = list(QUESTION.finditer(body))
    return [body[match.start():(starts[i + 1].start() if i + 1 < len(starts) else len(body))]
            for i, match in enumerate(starts)]


def parse_question(raw: str, exercise_id: str) -> dict[str, Any]:
    """一道题 → {id, prompt, answer, difficulty, source}，与前端 parseExercise 同规则。"""
    body = INDENT.sub("", NUMBERED.sub("", raw.strip(), count=1))
    answer = ANSWER.search(body)
    meta = META.search(body)
    if not answer or not meta or meta.start() <= answer.start():
        raise ValueError(f"习题 {exercise_id} 缺少「答案」或「难度：n/5；出处」字段。")
    difficulty = int(meta.group(1))
    if not 1 <= difficulty <= 5:
        raise ValueError(f"习题 {exercise_id} 难度须在 1–5 之间。")
    prompt = body[:answer.start()].strip()
    if not prompt:
        raise ValueError(f"习题 {exercise_id} 题干为空。")
    return {
        "id": exercise_id,
        "prompt_md": prompt,
        "answer_md": body[answer.end():meta.start()].strip() or None,
        "difficulty": difficulty,
        "source_raw": body[meta.end():].strip(),
        "synthesized": False,
    }


def parse_exercise_doc(markdown: str) -> dict[str, list[dict[str, Any]]]:
    """习题.md → {锚点: [题, …]}。有标记用标记，没标记按内容补 ID。"""
    parsed: dict[str, list[dict[str, Any]]] = {}
    for anchor, section in sections(markdown).items():
        body = section["body"]
        marks = list(MARKER.finditer(body))
        if marks:
            items = []
            for index, mark in enumerate(marks):
                end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
                items.append(parse_question(body[mark.end():end], mark.group(1)))
            parsed[anchor] = items
            continue
        blocks = split_questions(body)
        if not blocks:
            if ANSWER.search(body):
                raise ValueError(
                    f"锚点 {anchor} 的题目既没有 source-exercise 标记，也找不到题号，无法拆分。")
            parsed[anchor] = []
            continue
        items = []
        for block in blocks:
            item = parse_question(block, stable_id(block))
            item["synthesized"] = True
            items.append(item)
        parsed[anchor] = items
    return parsed


def _nodes(course_id: str) -> dict[str, dict[str, Any]]:
    """锚点 → 节点。锚点优先用 exercise_anchor（原数据锚点），退回 id。"""
    rows = db.query(
        """SELECT id, title, content_md, exercise_anchor, chapter_id, level, kind
             FROM knowledge_node WHERE course_id = %s""", (course_id,))
    by_anchor: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_anchor[row["exercise_anchor"] or row["id"]] = row
    return by_anchor


def _existing(course_id: str) -> tuple[dict[str, dict], dict[str, set[str]]]:
    exercises = {row["id"]: row for row in db.query(
        """SELECT id, prompt_md, answer_md, difficulty, source_raw, review_status,
                  provenance, provenance_note
             FROM exercise WHERE course_id = %s""", (course_id,))}
    links: dict[str, set[str]] = {}
    for row in db.query(
            "SELECT exercise_id, node_id FROM exercise_node WHERE course_id = %s",
            (course_id,)):
        links.setdefault(row["node_id"], set()).add(row["exercise_id"])
    return exercises, links


def plan_changes(course_id: str, knowledge_md: str | None = None,
                 exercises_md: str | None = None) -> dict[str, Any]:
    """算出"会改什么"，不写库。预览和落库共用这一份计算。"""
    if not knowledge_md and not exercises_md:
        raise ValueError("至少要上传一个文件：知识点.md 或 习题.md。")

    nodes = _nodes(course_id)
    if not nodes:
        raise ValueError(f"课程 {course_id} 还没有知识点，单文件导入只能改已有的课；"
                         "第一次导入请连同 知识节点.xlsx 一起上传。")
    existing, links = _existing(course_id)

    report: dict[str, Any] = {
        "course_id": course_id,
        "knowledge": None, "exercises": None,
        "unknown_anchors": [], "warnings": [],
    }

    content_updates: list[tuple[str, str]] = []
    if knowledge_md:
        sections_map = sections(knowledge_md)
        unknown = [anchor for anchor in sections_map if anchor not in nodes]
        changed = unchanged = 0
        for anchor, section in sections_map.items():
            node = nodes.get(anchor)
            if not node:
                continue
            body = section["body"]
            if body == (node["content_md"] or ""):
                unchanged += 1
                continue
            changed += 1
            content_updates.append((node["id"], body))
        report["knowledge"] = {
            "sections": len(sections_map), "changed": changed, "unchanged": unchanged,
            "unknown": len(unknown),
        }
        report["unknown_anchors"].extend(unknown)

    exercise_rows: list[dict[str, Any]] = []
    unlink: list[tuple[str, str, str]] = []
    if exercises_md:
        doc = parse_exercise_doc(exercises_md)
        unknown = [anchor for anchor in doc if anchor not in nodes]
        added = updated = unchanged = unlinked = 0
        if not any(doc.values()):
            raise ValueError("习题.md 里一道题都没解析出来：请检查题号与「答案／难度」字段。")
        for anchor, items in doc.items():
            node = nodes.get(anchor)
            if not node:
                continue
            keep = {item["id"] for item in items}
            for old_id in links.get(node["id"], set()):
                if old_id not in keep:
                    unlink.append((node["id"], old_id, anchor))
                    unlinked += 1
            for item in items:
                old = existing.get(item["id"])
                if old is None:
                    added += 1
                elif (old["prompt_md"] == item["prompt_md"]
                      and (old["answer_md"] or "") == (item["answer_md"] or "")
                      and old["difficulty"] == item["difficulty"]
                      and (old["source_raw"] or "") == item["source_raw"]):
                    unchanged += 1
                else:
                    updated += 1
                exercise_rows.append({**item, "node_id": node["id"],
                                      "chapter_id": node["chapter_id"]})
        report["exercises"] = {
            "anchors": len(doc), "added": added, "updated": updated,
            "unchanged": unchanged, "unlinked": unlinked, "unknown": len(unknown),
        }
        report["unknown_anchors"].extend(unknown)

    synthesized = sum(1 for row in exercise_rows if row.get("synthesized"))
    if synthesized:
        report["warnings"].append(
            f"{synthesized} 处题目没有 source-exercise 标记，已按题目内容补稳定 ID。")
    if report["unknown_anchors"]:
        shown = report["unknown_anchors"][:8]
        report["warnings"].append(
            f"{len(report['unknown_anchors'])} 个锚点在本课程里找不到，已跳过："
            f"{'、'.join(shown)}{'…' if len(report['unknown_anchors']) > len(shown) else ''}")

    report["_content_updates"] = content_updates
    report["_exercise_rows"] = exercise_rows
    report["_unlink"] = unlink
    report["_existing"] = existing
    return report


def public(report: dict[str, Any]) -> dict[str, Any]:
    """去掉内部字段，只留给前端看的。"""
    return {key: value for key, value in report.items() if not key.startswith("_")}


def apply_changes(course_id: str, knowledge_md: str | None, exercises_md: str | None,
                  actor: str) -> dict[str, Any]:
    """落库。一个事务：中途报错就整体回滚，不留半份改动。"""
    report = plan_changes(course_id, knowledge_md, exercises_md)
    content_updates = report["_content_updates"]
    exercise_rows = report["_exercise_rows"]
    unlink = report["_unlink"]
    existing = report["_existing"]

    with db.cursor() as cur:
        for node_id, body in content_updates:
            cur.execute(
                """UPDATE knowledge_node
                      SET content_md = %s,
                          content_version = content_version + 1,
                          review_status = CASE WHEN review_status = 'published'
                                               THEN 'published'::review_status
                                               ELSE 'draft'::review_status END,
                          updated_at = now()
                    WHERE course_id = %s AND id = %s""",
                (body, course_id, node_id))

        for node_id, exercise_id, _anchor in unlink:
            cur.execute(
                "DELETE FROM exercise_node WHERE course_id = %s AND node_id = %s "
                "AND exercise_id = %s", (course_id, node_id, exercise_id))

        for row in exercise_rows:
            parsed = parse_source({"source": row["source_raw"] or ""})
            old = existing.get(row["id"])
            # 更新时保留原有级别判定：一次改题面不该把"已人工核对"降级成 unknown
            provenance = (old or {}).get("provenance") or parsed["provenance"]
            cur.execute(
                """INSERT INTO exercise
                     (course_id, id, chapter_id, prompt_md, answer_md, difficulty,
                      source_raw, source_book, source_page, source_pdf_page, source_no,
                      provenance, provenance_note, review_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                   ON CONFLICT (course_id, id) DO UPDATE
                     SET chapter_id = EXCLUDED.chapter_id,
                         prompt_md = EXCLUDED.prompt_md,
                         answer_md = EXCLUDED.answer_md,
                         difficulty = EXCLUDED.difficulty,
                         source_raw = EXCLUDED.source_raw,
                         source_book = EXCLUDED.source_book,
                         source_page = EXCLUDED.source_page,
                         source_pdf_page = EXCLUDED.source_pdf_page,
                         source_no = EXCLUDED.source_no,
                         provenance = EXCLUDED.provenance,
                         provenance_note = EXCLUDED.provenance_note,
                         review_status = CASE WHEN exercise.review_status = 'published'
                                              THEN 'published'::review_status
                                              ELSE 'draft'::review_status END,
                         content_version = exercise.content_version + 1,
                         updated_at = now()""",
                (course_id, row["id"], row["chapter_id"], row["prompt_md"], row["answer_md"],
                 row["difficulty"], row["source_raw"], parsed["source_book"],
                 parsed["source_page"], parsed["source_pdf_page"], parsed["source_no"],
                 provenance, parsed["provenance_note"]))
            cur.execute(
                """INSERT INTO exercise_node (course_id, exercise_id, node_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (course_id, row["id"], row["node_id"]))

        # 从某个知识点摘下来的题，如果不再属于任何知识点，就停用。
        # 不删：题目本身可能还有学生作答记录，删掉会连带丢失。
        retired = 0
        for _node_id, exercise_id, _anchor in unlink:
            cur.execute(
                """UPDATE exercise SET review_status = 'retired', updated_at = now()
                    WHERE course_id = %s AND id = %s AND review_status <> 'retired'
                      AND NOT EXISTS (SELECT 1 FROM exercise_node
                                       WHERE course_id = %s AND exercise_id = %s)""",
                (course_id, exercise_id, course_id, exercise_id))
            retired += cur.rowcount

        cur.execute(
            """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
               VALUES (%s, 'content.import-docs', 'course', %s,
                       jsonb_build_object('knowledge_md', %s::bool, 'exercises_md', %s::bool,
                                          'content_updated', %s::int,
                                          'exercises_added', %s::int,
                                          'exercises_updated', %s::int,
                                          'unlinked', %s::int, 'retired', %s::int))""",
            (actor, course_id, bool(knowledge_md), bool(exercises_md),
             len(content_updates),
             (report["exercises"] or {}).get("added", 0),
             (report["exercises"] or {}).get("updated", 0),
             len(unlink), retired))

    result = public(report)
    result["applied"] = {
        "content_updated": len(content_updates),
        "unlinked": len(unlink),
        "retired": retired,
    }
    return result
