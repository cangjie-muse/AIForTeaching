#!/usr/bin/env python3
"""把 data/course-*.json 转成 db/seed.sql（PostgreSQL INSERT 脚本）。

这个脚本存在的意义是证明"现有内容可以直接入库"：它不做任何内容改写，
只做三件事——
  1. 把每门课独立命名空间的 id 补上 course_id，变成复合主键；
  2. 把 level=2 的节点提升为 chapter 表；
  3. 从自由文本 source 里尽力解析出结构化的教材页码/题号，解析不出就留空并标 unknown。

用法：
    python db/build_seed.py                # 生成 db/seed.sql
    python db/build_seed.py --stats        # 只打印统计，不写文件

依赖：Python 3.10+ 标准库，无需 psycopg。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "db" / "seed.sql"

# --------------------------------------------------------------------------
# SQL 字面量
# --------------------------------------------------------------------------
_tag_seq = 0


def q(value):
    """把任意文本转成 PostgreSQL 字面量；用带标签的 dollar quoting 避免转义地狱。"""
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    global _tag_seq
    while True:
        _tag_seq += 1
        tag = f"$t{_tag_seq}$"
        if tag not in value:
            return f"{tag}{value}{tag}"


def arr(values):
    if not values:
        return "'{}'::text[]"
    return "ARRAY[" + ", ".join(q(v) for v in values) + "]::text[]"


def num(value):
    return "NULL" if value is None else str(value)


def js(value):
    return "NULL" if value is None else q(json.dumps(value, ensure_ascii=False)) + "::jsonb"


# --------------------------------------------------------------------------
# 溯源文本解析：现有 source 是自由文本，能解析多少算多少
# --------------------------------------------------------------------------
BOOK_RE = re.compile(r"《([^》]+)》")
# 西文教材不用书名号：现有出处写成
#   "John B. Conway, Functions of One Complex Variable, 2nd ed., Springer-Verlag，书页4，PDF第1页"
# 或先带一个来源短标签 "A：Roman Vershynin, High-Dimensional Probability, Second Edition，…"。
# 只取第一个中文逗号之前的作者+书名段，并要求其中至少两个拉丁字母——
# 免得把"课程建设自拟训练题"这种中文说明当成书名。
WESTERN_BOOK_RE = re.compile(r"^\s*(?:[A-Za-z]\s*[：:]\s*)?([A-Za-z][^，；;]{8,140})")
PAGE_RANGE_RE = re.compile(r"书页\s*(\d+)\s*[–—\-~至到]\s*(\d+)")
PAGE_RE = re.compile(r"书页\s*(\d+)")
# PDF 页码有两种写法："PDF页43"（旧契约示例）和"PDF第43页"（现有数据里绝大多数）。
# 只认前一种时，559 条带 PDF 页的题一条也取不到。
PDF_RANGE_RE = re.compile(
    r"PDF\s*(?:第\s*(\d+)\s*[–—\-~至到]\s*(\d+)\s*页|页\s*(\d+)\s*[–—\-~至到]\s*(\d+))"
)
PDF_RE = re.compile(r"PDF\s*(?:第\s*(\d+)\s*页|页\s*(\d+))")
NO_RE = re.compile(r"原题号([^；;，,\n]+)")
# 没写"原题号"但写了教材编号的情况："教材例6.1.8"、"教材定理4.1"
NO_ALT_RE = re.compile(r"(?:教材例|教材定理|教材习题|教材命题|教材推论)\s*([0-9A-Za-z§.．\-]+)")
# 西文教材的题号："Chapter 1 §3 Exercises 3"
EN_NO_RE = re.compile(r"Exercises?\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)


def parse_source(ex: dict) -> dict:
    """从 exercise['source'] 解析教材、页码、题号与来源类型。"""
    raw = ex.get("source") or ""
    out = {
        "source_book": None,
        "source_page": None,
        "source_pdf_page": None,
        "source_no": None,
        "provenance": "unknown",
        "provenance_note": None,
    }

    book = BOOK_RE.search(raw)
    if book:
        out["source_book"] = book.group(1).strip()
    else:
        western = WESTERN_BOOK_RE.match(raw)
        if western and len(re.findall(r"[A-Za-z]", western.group(1))) >= 6:
            out["source_book"] = western.group(1).strip()

    # 整章范围（如"书页1–99"）不是精确页，不能当成题目所在页
    if PAGE_RANGE_RE.search(raw):
        out["provenance_note"] = "页码为整章范围，非题目精确页"
    else:
        m = PAGE_RE.search(raw)
        if m:
            out["source_page"] = int(m.group(1))

    if PDF_RANGE_RE.search(raw) and out["source_page"] is None:
        out["provenance_note"] = (out["provenance_note"] or "") + "；PDF 页为范围"
    else:
        m = PDF_RE.search(raw)
        if m and not PDF_RANGE_RE.search(raw):
            # 两种写法共用一条正则："PDF第43页" 与 "PDF页43"
            out["source_pdf_page"] = int(m.group(1) or m.group(2))

    no = NO_RE.search(raw) or NO_ALT_RE.search(raw) or EN_NO_RE.search(raw)
    if no:
        out["source_no"] = no.group(1).strip()

    # 判据必须具体。典型反例：教材原题的解答写"自拟简解"，
    # 若只看"自拟"二字，225 道教材原题会被误判成自拟题 3505 道。
    if "导出" in raw and ("xlsx" in raw.lower() or "导出表" in raw):
        out["provenance"] = "colleague-export"
        if not out["source_no"]:
            row = re.search(r"第\s*(\d+)\s*行", raw)
            if row:
                out["source_no"] = f"导出表第{row.group(1)}行"
    elif "自拟训练题" in raw or "教材页码不适用" in raw or "课程建设自拟" in raw:
        out["provenance"] = "self-authored"
    elif out["source_book"] and out["source_no"] and out["source_page"]:
        out["provenance"] = "textbook"
    elif out["source_book"] and "原题" in raw:
        out["provenance"] = "textbook"
    # 有书名（西文也算）、有精确书页、并且明说是教材/习题集，即可判为教材原题。
    # 不加这一条时，西文书目（无书名号、无"原题号"字样）会全部落进 unknown，
    # 而它们恰恰是溯源最清楚的一批。
    elif out["source_book"] and out["source_page"] and (
        "教材" in raw or re.search(r"Exercises?\b", raw, re.IGNORECASE)
    ):
        out["provenance"] = "textbook"

    return out


def review_of(raw: str | None) -> str:
    """带"待核"的条目一律停在 draft，不能假装已审核。"""
    if raw and ("待核" in raw or "待教师" in raw):
        return "draft"
    return "machine-checked"


# --------------------------------------------------------------------------
# 转换
# --------------------------------------------------------------------------
def load_courses() -> list[dict]:
    files = sorted(DATA.glob("course-*.json"))
    if not files:
        sys.exit(f"未找到数据文件：{DATA}/course-*.json")
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def build(courses: list[dict]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """返回 {表名: [完整 INSERT 语句]}。"""
    stmts: dict[str, list[str]] = {
        "course": [], "chapter": [], "knowledge_node": [],
        "exercise": [], "exercise_node": [], "course_asset": [],
    }
    stats = {"page": 0, "pdf": 0, "no": 0, "parsed": 0}

    for c in courses:
        cid = c["id"]
        nodes = c.get("nodes") or []
        exercises = c.get("exercises") or []

        stmts["course"].append(
            "INSERT INTO course (id, title, audience, description, subject, status, "
            "schema_version, resource_base, source_files, review_status, source_imported_at) VALUES ("
            f"{q(cid)}, {q(c.get('title'))}, {q(c.get('audience'))}, {q(c.get('description'))}, "
            f"{q(c.get('subject'))}, {q(c.get('status'))}, {q(c.get('schemaVersion'))}, "
            f"{q(c.get('resourceBase'))}, {arr(c.get('files'))}, "
            f"{q(review_of(c.get('reviewStatus')))}::review_status, "
            f"{q(c.get('importedAt'))}::timestamptz) "
            "ON CONFLICT (id) DO NOTHING;"
        )

        # 章节：level=2 的节点
        sections = [n for n in nodes if n.get("level") == 2]
        for order, n in enumerate(sections, start=1):
            stmts["chapter"].append(
                "INSERT INTO chapter (id, course_id, title, ord, path) VALUES ("
                f"{q(n['id'])}, {q(cid)}, {q(n.get('title'))}, {order}, {arr(n.get('path'))}) "
                "ON CONFLICT DO NOTHING;"
            )
        chapter_ids = {n["id"] for n in sections}

        # 节点：父节点必须先插入，按 level 升序
        for n in sorted(nodes, key=lambda x: x.get("level") or 0):
            chapter_id = n.get("chapterId")
            if chapter_id not in chapter_ids:
                chapter_id = None
            parent_id = n.get("parentId")
            stmts["knowledge_node"].append(
                "INSERT INTO knowledge_node (course_id, id, chapter_id, parent_id, title, level, "
                "kind, path, content_md, exercise_anchor, source_raw, review_status) VALUES ("
                f"{q(cid)}, {q(n['id'])}, {q(chapter_id)}, {q(parent_id)}, {q(n.get('title'))}, "
                f"{num(n.get('level'))}, {q(n.get('kind') or 'knowledge')}, {arr(n.get('path'))}, "
                f"{q(n.get('content') or '')}, {q(n.get('exerciseAnchor'))}, {q(n.get('source'))}, "
                f"{q(review_of(n.get('source')))}::review_status) "
                "ON CONFLICT DO NOTHING;"
            )

        for e in exercises:
            p = parse_source(e)
            if p["source_page"]:
                stats["page"] += 1
            if p["source_pdf_page"]:
                stats["pdf"] += 1
            if p["source_no"]:
                stats["no"] += 1
            if p["provenance"] != "unknown":
                stats["parsed"] += 1
            chapter_id = e.get("chapterId")
            if chapter_id not in chapter_ids:
                # exercise 没有 chapterId 字段时，从关联节点推断
                chapter_id = None
                for nid in e.get("nodeIds") or []:
                    node = next((x for x in nodes if x["id"] == nid), None)
                    if node and node.get("chapterId") in chapter_ids:
                        chapter_id = node["chapterId"]
                        break
            stmts["exercise"].append(
                "INSERT INTO exercise (course_id, id, chapter_id, prompt_md, answer_md, difficulty, "
                "source_raw, source_book, source_page, source_pdf_page, source_no, provenance, "
                "provenance_note, review_status) VALUES ("
                f"{q(cid)}, {q(e['id'])}, {q(chapter_id)}, {q(e.get('prompt') or '')}, "
                f"{q(e.get('answer'))}, {num(e.get('difficulty'))}, {q(e.get('source'))}, "
                f"{q(p['source_book'])}, {num(p['source_page'])}, {num(p['source_pdf_page'])}, "
                f"{q(p['source_no'])}, {q(p['provenance'])}::exercise_provenance, "
                f"{q(p['provenance_note'])}, {q(review_of(e.get('source')))}::review_status) "
                "ON CONFLICT DO NOTHING;"
            )
            for nid in e.get("nodeIds") or []:
                stmts["exercise_node"].append(
                    "INSERT INTO exercise_node (course_id, exercise_id, node_id) VALUES ("
                    f"{q(cid)}, {q(e['id'])}, {q(nid)}) ON CONFLICT DO NOTHING;"
                )

        for path in c.get("attachments") or []:
            stmts["course_asset"].append(
                "INSERT INTO course_asset (course_id, path) VALUES ("
                f"{q(cid)}, {q(path)}) ON CONFLICT DO NOTHING;"
            )

    return stmts, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    courses = load_courses()
    stmts, stats = build(courses)

    total = {k: len(v) for k, v in stmts.items()}
    print(f"课程 {len(courses)} 门")
    for k, v in total.items():
        print(f"  {k:16} {v:6} 行")
    print(f"溯源解析：页码 {stats['page']}、PDF页 {stats['pdf']}、题号 {stats['no']}、"
          f"可判定来源 {stats['parsed']}")

    if args.stats:
        return

    lines = [
        "-- 由 db/build_seed.py 自动生成，请勿手工编辑。",
        "-- 数据来源：data/course-*.json（原始输入为 course-packs 下的三件套）",
        "BEGIN;",
    ]
    for table in ("course", "chapter", "knowledge_node", "exercise", "exercise_node", "course_asset"):
        lines.append(f"\n-- {table}: {total[table]} 行")
        lines.extend(stmts[table])
    lines.append("COMMIT;")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}（{OUT.stat().st_size / 1024 / 1024:.1f} MB）")


if __name__ == "__main__":
    main()
