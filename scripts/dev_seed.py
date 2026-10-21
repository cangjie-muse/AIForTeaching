#!/usr/bin/env python3
"""给本地联调库灌一点像样的数据。

不是"测试夹具"，是"打开页面能看懂"的样例：
  - 有挂载完整、页码精确的题（不该出现在审核队列里）
  - 有未挂知识点、溯源 unknown、缺页、原文标"待核"的题（应该出现在队列里）
  - 知识点足够撑起图谱

幂等：ON CONFLICT DO NOTHING，重复跑不会重复插入。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import db  # noqa: E402

COURSE = "demo-analysis-2"
CHAPTER = "demo-ch-1"

NODES = [
    (CHAPTER, None, "第一章 极限", 2, "container", ["第一章 极限"]),
    ("demo-n-1", CHAPTER, "数列极限", 3, "knowledge", ["第一章 极限", "数列极限"]),
    ("demo-n-2", CHAPTER, "函数极限", 3, "knowledge", ["第一章 极限", "函数极限"]),
    ("demo-n-3", CHAPTER, "连续与间断", 3, "knowledge", ["第一章 极限", "连续与间断"]),
]

# (id, 题干, 难度, provenance, source_page, review_status, 挂哪些知识点)
EXERCISES = [
    ("demo-ex-1", "设 $a_n>0$，且 $\\lim_{n\\to\\infty} a_n = a > 0$，证明 "
                  "$\\lim_{n\\to\\infty}\\sqrt{a_n}=\\sqrt{a}$。", 2, "textbook", 18,
     "draft", ["demo-n-1"]),
    ("demo-ex-2", "用 $\\varepsilon$-$N$ 语言证明：$\\lim_{n\\to\\infty}\\frac{2n+1}{n+2}=2$。",
     1, "textbook", 19, "draft", ["demo-n-1", "demo-n-2"]),
    ("demo-ex-3", "讨论 $f(x)=\\frac{\\sin x}{x}$ 在 $x=0$ 处的连续性，"
                  "并说明如何补充定义使其连续。", 3, "textbook", None, "draft", ["demo-n-3"]),
    ("demo-ex-4", "求 $\\lim_{x\\to 0}\\frac{1-\\cos x}{x^2}$。", 2, "unknown", None, "draft", []),
    ("demo-ex-5", "判断 $f(x)=[x]$（取整函数）的间断点类型，并指出间断点集合。",
     3, "textbook", 27, "machine-checked", ["demo-n-3"]),
    ("demo-ex-6", "（待核）设 $\\{a_n\\}$ 单调递增有上界，证明其收敛，并求极限值。",
     4, "textbook", 31, "draft", ["demo-n-1"]),
    ("demo-ex-7", "证明：若 $\\lim_{x\\to x_0} f(x)=A$，则 $f$ 在 $x_0$ 的某去心邻域内有界。",
     3, "textbook", 22, "draft", ["demo-n-2"]),
    ("demo-ex-8", "闭区间上连续函数的性质有哪些？各举一例。", 1, "self-authored", None,
     "draft", ["demo-n-3", "demo-n-2"]),
]


def seed() -> None:
    db.query(
        """INSERT INTO course (id, title, audience, subject, status)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
        (COURSE, "数学分析（II）", "本科生", "数学", "ready"),
    )
    db.query(
        """INSERT INTO chapter (id, course_id, title, ord)
           VALUES (%s, %s, %s, %s) ON CONFLICT (course_id, id) DO NOTHING""",
        (CHAPTER, COURSE, "第一章 极限", 1),
    )
    for node_id, parent, title, level, kind, path in NODES:
        db.query(
            """INSERT INTO knowledge_node (course_id, id, chapter_id, parent_id, title, level, kind, path)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (course_id, id) DO NOTHING""",
            (COURSE, node_id, CHAPTER, parent, title, level, kind, path),
        )
    for ex_id, prompt, difficulty, provenance, page, status, nodes in EXERCISES:
        db.query(
            """INSERT INTO exercise (course_id, id, chapter_id, prompt_md, answer_md,
                                     difficulty, source_raw, provenance, source_page, review_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (course_id, id) DO NOTHING""",
            (COURSE, ex_id, CHAPTER, prompt, "（示例数据，答案待补）", difficulty,
             "教材原题", provenance, page, status),
        )
        for node_id in nodes:
            db.query(
                """INSERT INTO exercise_node (course_id, exercise_id, node_id)
                   VALUES (%s, %s, %s) ON CONFLICT (course_id, exercise_id, node_id) DO NOTHING""",
                (COURSE, ex_id, node_id),
            )
    seed_textbook()
    print(f"示例数据就绪：1 门课、{len(NODES)} 个节点、{len(EXERCISES)} 道题、"
          f"{len(CHUNKS)} 个教材片段")


CHUNKS = [
    (1, ["第一章 极限", "数列极限"], 18, 18,
     "定义 1.1（数列极限）设 {a_n} 为数列，a 为实数。若对任意给定的 ε>0，"
     "总存在正整数 N，使得当 n>N 时恒有 |a_n - a| < ε，则称数列 {a_n} 收敛于 a，"
     "记作 lim(n→∞) a_n = a。ε 刻画的是「任意接近」，N 的存在性刻画的是「最终保持」。"),
    (2, ["第一章 极限", "数列极限"], 19, 19,
     "例 1.2 用 ε-N 语言证明 lim(n→∞) (2n+1)/(n+2) = 2。"
     "关键是先把 |(2n+1)/(n+2) - 2| 化简为 3/(n+2)，再令其小于 ε，解出 n > 3/ε - 2，"
     "取 N = ceil(3/ε)。这里先化简再放大的顺序不能颠倒。"),
    (3, ["第一章 极限", "函数极限"], 22, 23,
     "函数极限与数列极限的区别：自变量取连续值，所以用 δ 控制 x 与 x0 的距离。"
     "常见错误是只验证了 x 从右侧趋近的情形，忽略了双侧。"),
]


def seed_textbook() -> None:
    """教材片段：没有它，问答链路只能拒答（这是设计使然）。"""
    db.query(
        """INSERT INTO textbook (id, course_id, title, edition, authors, publisher,
                                 copyright_status, pdf_sha256)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
        ("demo-tb-analysis", COURSE, "数学分析（示例片段）", "第 1 版", "示例",
         "示例出版社", "licensed", "d" * 64),
    )
    for seq, path, page_from, page_to, content in CHUNKS:
        db.query(
            """INSERT INTO textbook_chunk (textbook_id, seq, section_path, page_from, page_to, content_md)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (textbook_id, seq) DO NOTHING""",
            ("demo-tb-analysis", seq, path, page_from, page_to, content),
        )


if __name__ == "__main__":
    db.pool.open()
    try:
        seed()
    finally:
        db.pool.close()
