"""把知识点正文切成问答语料——「教材还没切片」时的可用替代品。

为什么需要这一步（而不是等教材 PDF）：

    问答检索只认 textbook_chunk：每条片段挂在某本教材上，引用显示"第 N 页"。
    14 门课的教材一本都没入库，chunk_count 全是 0，所以每次提问都被拒答。
    而知识点正文已经在 knowledge_node.content_md 里，合计 138 万字，其中 10 门
    课是教材级长文（单节点上千字、带 $LaTeX$ 和书页锚点）。

    这一步不是"假装有教材"，而是把**我们自己整理的课程知识库**接进同一条链路：
    同一张表、同一套引用、同一套拒答规则，只是 source_kind='node'、
    引用显示「知识点 · 章节路径」而不是页码。等真教材切片进来，
    source_kind='pdf' 那批自动优先，知识点语料留着兜底，不用改问答代码。

三个取舍：

1. **不猜页码。** 节点溯源里的页码是"章节起页"（书页197起），不是这个知识点
   所在的页。拿章节起页当引用页码，等于给每条回答盖一个错章——比不写页码更糟。
   所以知识点语料一律 page_from/page_to = NULL，引用改标 section_path。

2. **按段落切，不按字数硬切。** 数学文本的"条件—结论—证明"跨段就对不上，
   硬切 512 字会把条件切到上一块。这里先按空行分段，把整段凑到 900 字左右，
   实在超长的段再按句号切；`$$...$$` 行间公式尽量整块留在同一块里。

3. **重建是整体替换。** 每门课用固定 id `nodes-<course_id>`，重建时先删这本
   "教材"（级联删块和锚点）再重插。这样反复执行不会堆垃圾，也不需要 diff。
   代价是历史 ai_citation.chunk_id 会被置空（ON DELETE SET NULL）——引用里
   已存的 page/excerpt 仍然在，消息不会因此变空。
"""

from __future__ import annotations

import re
from typing import Any

from . import db

MAX_CHUNK_CHARS = 900     # 一块的目标上限；再大就超出检索粒度，再小就丢上下文
MIN_CHUNK_CHARS = 40      # 短于这个的尾块并进上一块，免得出现"半句话"片段
CORPUS_PREFIX = "nodes-"
CORPUS_TITLE_SUFFIX = "· 知识点汇编"

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[。；！？!?])")
_DISPLAY_MATH_END = re.compile(r"(?<=\$\$)")


# ---------------------------------------------------------------------------
# 切块（纯函数，测试直接打这里）
# ---------------------------------------------------------------------------


def _split_sentences(text: str, max_chars: int) -> list[str]:
    """按句子攒块；单个句子比块上限还长时（长公式串）只能硬切。"""
    pieces: list[str] = []
    buffer = ""
    for sentence in _SENTENCE.split(text):
        if not sentence:
            continue
        while len(sentence) > max_chars:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if len(buffer) + len(sentence) <= max_chars:
            buffer += sentence
        else:
            if buffer:
                pieces.append(buffer)
            buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _split_block(block: str, max_chars: int) -> list[str]:
    """切一个超长段落：先按行间公式边界，再按句子。"""
    if len(block) <= max_chars:
        return [block]
    pieces: list[str] = []
    buffer = ""
    for part in _DISPLAY_MATH_END.split(block):
        if not part:
            continue
        if len(buffer) + len(part) <= max_chars:
            buffer += part
            continue
        if buffer:
            pieces.append(buffer)
        buffer = ""
        if len(part) <= max_chars:
            buffer = part
            continue
        for sentence_piece in _split_sentences(part, max_chars):
            if len(buffer) + len(sentence_piece) <= max_chars:
                buffer += sentence_piece
            else:
                if buffer:
                    pieces.append(buffer)
                buffer = sentence_piece
    if buffer:
        pieces.append(buffer)
    return [piece for piece in pieces if piece.strip()]


def split_content(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Markdown 正文 → 若干片段。空正文返回空列表。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks = [block.strip() for block in _PARAGRAPH.split(normalized)]
    pieces: list[str] = []
    for block in blocks:
        if block:
            pieces.extend(_split_block(block, max_chars))

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + 2 + len(piece) <= max_chars:
            buffer = f"{buffer}\n\n{piece}"
        else:
            chunks.append(buffer)
            buffer = piece
        if len(buffer) >= max_chars:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)

    # 尾块太短就并进上一块：单独一个"证毕。"当片段只会污染检索
    if len(chunks) > 1 and len(chunks[-1]) < MIN_CHUNK_CHARS:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]}\n\n{tail}"
    return [chunk.strip() for chunk in chunks if chunk.strip()]


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


def corpus_id(course_id: str) -> str:
    return f"{CORPUS_PREFIX}{course_id}"


def rebuild_course(course_id: str) -> dict[str, Any]:
    """重建一门课的知识点语料。返回 {course_id, nodes, chunks}。"""
    with db.cursor() as cur:
        cur.execute("SELECT id, title FROM course WHERE id = %s", (course_id,))
        course = cur.fetchone()
        if not course:
            raise KeyError(f"没有这门课：{course_id}")

        cur.execute(
            """SELECT id, title, path, content_md
               FROM knowledge_node
               WHERE course_id = %s
               ORDER BY level, array_to_string(path, ' / '), id""",
            (course_id,),
        )
        nodes = cur.fetchall()

        textbook_id = corpus_id(course_id)
        # 先删旧语料：块和 node_chunk 都会级联删掉，不会越重建越多
        cur.execute("DELETE FROM textbook WHERE id = %s", (textbook_id,))
        cur.execute(
            """INSERT INTO textbook (id, course_id, title, authors, edition, publisher,
                                     copyright_status, source_kind, pdf_sha256, page_offset)
               VALUES (%s, %s, %s, %s, NULL, NULL, 'self-authored', 'node', NULL, 0)""",
            (textbook_id, course_id, f"{course['title']} {CORPUS_TITLE_SUFFIX}".strip(),
             "本平台整理"),
        )

        seq = 0
        chunk_count = 0
        nodes_with_text = 0
        for node in nodes:
            pieces = split_content(node["content_md"] or "")
            if not pieces:
                continue
            nodes_with_text += 1
            values = ", ".join(["(%s, %s, %s, NULL, NULL, %s, %s)"] * len(pieces))
            params: list[Any] = []
            for piece in pieces:
                seq += 1
                # 每块都带上知识点标题：正文里常常只有"它的"这个指代，标题本身
                # 才是检索词（问"同时幂法"，正文里可能一次都没写）。顺带让片段
                # 自解释——给模型看的一段话，开头就说明它讲的是哪个知识点。
                body = f"**{node['title']}**\n\n{piece}"
                params.extend([textbook_id, seq, node["path"], body, "$" in body])
            cur.execute(
                f"""INSERT INTO textbook_chunk
                        (textbook_id, seq, section_path, page_from, page_to, content_md, has_formula)
                    VALUES {values}
                    RETURNING id""",
                tuple(params),
            )
            chunk_ids = [row["id"] for row in cur.fetchall()]
            chunk_count += len(chunk_ids)
            anchors = ", ".join(["(%s, %s, %s, 'heading-match', NULL)"] * len(chunk_ids))
            anchor_params: list[Any] = []
            for chunk_id in chunk_ids:
                anchor_params.extend([course_id, node["id"], chunk_id])
            cur.execute(
                f"""INSERT INTO node_chunk (course_id, node_id, chunk_id, method, confidence)
                    VALUES {anchors}""",
                tuple(anchor_params),
            )

    return {"course_id": course_id, "nodes": nodes_with_text, "chunks": chunk_count}


def rebuild_all() -> list[dict[str, Any]]:
    """重建全部课程。逐门提交，一门失败不影响其他课。"""
    rows = db.query("SELECT id FROM course ORDER BY id")
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            results.append(rebuild_course(row["id"]))
        except Exception as error:                       # noqa: BLE001 —— 汇总后一起报
            results.append({"course_id": row["id"], "error": str(error)[:200]})
    return results


def needs_corpus(row: dict[str, Any]) -> bool:
    """这门课该有语料却没有：有正文的知识点 > 0，但块数是 0。

    为什么要单独判：资源包导入只写 knowledge_node，不写 textbook_chunk。
    导入完不复建，问答里这门课永远是「0 片段」，学生一问就拒答。
    """
    return int(row.get("nodes_with_text") or 0) > 0 and int(row.get("chunks") or 0) == 0


def missing_courses() -> list[dict[str, Any]]:
    return [row for row in status()["items"] if needs_corpus(row)]


def rebuild_missing() -> list[dict[str, Any]]:
    """只补该有语料却没有的课，已经有的不动。

    比"全部重建"安全：全量重建会删掉已有块重建，正在被引用的答案会短暂无依据；
    补缺只碰空的那几门。
    """
    results: list[dict[str, Any]] = []
    for row in missing_courses():
        try:
            results.append(rebuild_course(row["course_id"]))
        except Exception as error:                       # noqa: BLE001 —— 汇总后一起报
            results.append({"course_id": row["course_id"], "error": str(error)[:200]})
    return results


def status() -> dict[str, Any]:
    """每门课：多少知识点有正文、切了多少块、问答现在能不能检索到。"""
    rows = db.query(
        """
        SELECT c.id AS course_id, c.title,
               (SELECT count(*) FROM knowledge_node k
                 WHERE k.course_id = c.id AND length(btrim(k.content_md)) > 0) AS nodes_with_text,
               (SELECT count(*) FROM knowledge_node k WHERE k.course_id = c.id) AS nodes,
               (SELECT count(*) FROM textbook_chunk ch
                  JOIN textbook t ON t.id = ch.textbook_id
                 WHERE t.course_id = c.id) AS chunks,
               (SELECT t.id FROM textbook t
                 WHERE t.course_id = c.id AND t.source_kind = 'node' LIMIT 1) AS corpus_id
        FROM course c
        ORDER BY c.id
        """
    )
    return {
        "items": rows,
        "total_chunks": sum(int(row["chunks"] or 0) for row in rows),
        "courses_ready": sum(1 for row in rows if int(row["chunks"] or 0) > 0),
    }
