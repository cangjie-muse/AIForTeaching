"""单文件增量导入（知识点.md / 习题.md 单独上传）的单元测试。

不连库：用假 db 验证"会改什么"的判定与写语句，真库语义在 e2e 里另测。
最要紧的一条是 **稳定 ID 的哈希必须与前端逐位一致**——两边分叉的话，
先用网页整包导入、再用单文件改一版，同一道题会变成两道，数据上看不出错。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import pack_merge  # noqa: E402
from app.pack_merge import (  # noqa: E402
    apply_changes, parse_exercise_doc, parse_question, plan_changes, public,
    sections, split_questions, stable_id,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  ✗ {name}  — {detail}")


def raises(call, fragment: str) -> bool:
    try:
        call()
    except ValueError as error:
        return fragment in str(error)
    return False


# 与 js/import-core.js 算出来的值对拍（quad 号来自 node 实跑）
JS_QUESTION = ('1. 证明空集是开集。\n\n   **答案：** 由公理。\n\n'
               '   **难度：1/5；出处：** 教材 1.1')

print("稳定 ID 与前端逐位一致：")
check("哈希等于前端算出的值", stable_id(JS_QUESTION) == "q-bf44e26476db883e",
      stable_id(JS_QUESTION))
check("末尾多一个换行就是另一个 ID", stable_id(JS_QUESTION + "\n") != stable_id(JS_QUESTION))
check("ID 形状固定", len(stable_id("x")) == 18 and stable_id("x").startswith("q-"))
check("同一文本恒等", stable_id("同一段") == stable_id("同一段"))

print("\n锚点解析：")
DOC = (
    '<a id="kp-a"></a>\n### 定义\n正文 A\n\n'
    '<a id="kp-b"></a>\n## 章\n正文 B\n\n'
    '<a id="kp-c"></a>\n### 小节\n正文 C\n'
)
parsed = sections(DOC)
check("三个锚点", len(parsed) == 3)
check("取到标题", parsed["kp-a"]["title"] == "定义")
check("### 在 ## 同级或更高级标题处截断", parsed["kp-b"]["body"] == "正文 B")
check("正文去首尾空白", parsed["kp-a"]["body"] == "正文 A")
DUPLICATE = '<a id="kp-a"></a>\n### 又一次定义\n正文\n'
check("重复锚点报错", raises(lambda: sections(DOC + DUPLICATE), "重复锚点"))
check("没有锚点报错", raises(lambda: sections("# 只有标题\n正文"), "未发现"))

print("\n拆题只认行首题号：")
body = ("1. 第一题。\n\n   **答案：** 分两步。\n\n   1. 步骤一。\n   2. 步骤二。\n\n"
        "   **难度：2/5；出处：** 教材\n\n2. 第二题。\n\n   **答案：** 略。\n\n"
        "   **难度：1/5；出处：** 教材\n")
blocks = split_questions(body)
check("两道题", len(blocks) == 2, str(len(blocks)))
check("缩进的步骤留在第一题里", "步骤二" in blocks[0])
check("第二题不含第一题内容", "第一题" not in blocks[1])

print("\n单题解析：")
question = parse_question(blocks[0], "ex-1")
check("去掉行首题号", question["prompt_md"].startswith("第一题。"))
check("答案取出", question["answer_md"].startswith("分两步。"))
check("难度解析", question["difficulty"] == 2)
check("出处保留", question["source_raw"] == "教材")
check("缺答案报错", raises(lambda: parse_question("1. 只有题干。", "ex-2"), "缺少"))
check("缺难度报错",
      raises(lambda: parse_question("1. 题干。\n\n   **答案：** 略。", "ex-3"), "缺少"))
check("难度越界报错",
      raises(lambda: parse_question("1. 题干。\n\n   **答案：** 略。\n\n"
                                    "   **难度：7/5；出处：** 教材", "ex-4"), "1–5"))

print("\n整份习题.md：")
MARKED = (
    '<a id="kp-a"></a>\n### 定义\n'
    '<!-- source-exercise:book-p1-1 -->\n'
    '1. 证明空集是开集。\n\n   **答案：** 由公理。\n\n   **难度：1/5；出处：** 教材 1.1\n'
)
UNMARKED = (
    '<a id="kp-a"></a>\n### 定义\n'
    '1. 证明空集是开集。\n\n   **答案：** 由公理。\n\n   **难度：1/5；出处：** 教材 1.1\n'
)
with_marks = parse_exercise_doc(MARKED)
check("有标记用原始题号", list(with_marks["kp-a"][0].values())[0] == "book-p1-1")
check("有标记不算补的", with_marks["kp-a"][0]["synthesized"] is False)
no_marks = parse_exercise_doc(UNMARKED)
check("没标记补 ID", no_marks["kp-a"][0]["id"].startswith("q-"))
check("补的 ID 与前端同一个值",
      no_marks["kp-a"][0]["id"] == stable_id(
          '1. 证明空集是开集。\n\n   **答案：** 由公理。\n\n   **难度：1/5；出处：** 教材 1.1'))
check("补 ID 有标记", no_marks["kp-a"][0]["synthesized"] is True)
check("两次解析同一批 ID",
      [item["id"] for item in parse_exercise_doc(UNMARKED)["kp-a"]] ==
      [item["id"] for item in no_marks["kp-a"]])
check("有答案没题号报错",
      raises(lambda: parse_exercise_doc('<a id="kp-a"></a>\n### 定义\n'
                                        '题干。\n\n   **答案：** 略。\n\n'
                                        '   **难度：1/5；出处：** 教材\n'), "找不到题号"))
check("没题的锚点得到空列表",
      parse_exercise_doc('<a id="kp-a"></a>\n### 定义\n正文，没有题。\n')["kp-a"] == [])


# ---------------------------------------------------------------------------
# 假数据库：plan_changes / apply_changes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((" ".join(sql.split()), params))
        self.rowcount = 1


@contextmanager
def fake_cursor():
    yield CURRENT_CURSOR


CURRENT_CURSOR = FakeCursor()


class FakeDB:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.exercises: list[dict] = []
        self.links: list[dict] = []

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if "FROM knowledge_node" in sql:
            return self.nodes
        if "FROM exercise_node" in sql:
            return self.links
        if "FROM exercise" in sql:
            return self.exercises
        return []

    def cursor(self):
        return fake_cursor()


FAKE = FakeDB()
pack_merge.db = FAKE  # type: ignore[assignment]


def reset(nodes, exercises=None, links=None) -> None:
    FAKE.nodes = nodes
    FAKE.exercises = exercises or []
    FAKE.links = links or []
    CURRENT_CURSOR.statements.clear()


NODES = [
    {"id": "kp-a", "title": "定义", "content_md": "正文 A", "exercise_anchor": "kp-a",
     "chapter_id": "ch-1", "level": 3, "kind": "knowledge"},
    {"id": "kp-b", "title": "定理", "content_md": "正文 B", "exercise_anchor": "kp-b",
     "chapter_id": "ch-1", "level": 3, "kind": "knowledge"},
]
EXERCISES = [
    {"id": "book-p1-1", "prompt_md": "证明空集是开集。", "answer_md": "由公理。",
     "difficulty": 1, "source_raw": "教材 1.1", "review_status": "draft",
     "provenance": "book", "provenance_note": None},
    {"id": "book-p1-2", "prompt_md": "旧题。", "answer_md": "旧解。",
     "difficulty": 2, "source_raw": "教材 1.2", "review_status": "published",
     "provenance": "book", "provenance_note": None},
]
LINKS = [{"exercise_id": "book-p1-1", "node_id": "kp-a"},
         {"exercise_id": "book-p1-2", "node_id": "kp-a"}]

print("\n预览：知识点.md 只改正文，不动结构")
reset(NODES, EXERCISES, LINKS)
KNOW = ('<a id="kp-a"></a>\n### 定义\n正文改过了\n\n'
        '<a id="kp-b"></a>\n### 定理\n正文 B\n\n'
        '<a id="kp-ghost"></a>\n### 这门课没有的锚点\n正文\n')
report = plan_changes("course-1", knowledge_md=KNOW)
check("改了 1 个锚点", report["knowledge"]["changed"] == 1)
check("没变的不算改动", report["knowledge"]["unchanged"] == 1)
check("找不到的锚点被点名", report["unknown_anchors"] == ["kp-ghost"])
check("警告里说明跳过", any("找不到" in text for text in report["warnings"]))
check("预览不写库", CURRENT_CURSOR.statements == [])
check("内部字段不外泄", "_content_updates" in report and "_content_updates" not in public(report))

print("\n预览：习题.md 增改删各算各的")
reset(NODES, EXERCISES, LINKS)
EX = (
    '<a id="kp-a"></a>\n### 定义\n'
    '<!-- source-exercise:book-p1-1 -->\n'
    '1. 证明空集是开集。\n\n   **答案：** 由公理。\n\n   **难度：1/5；出处：** 教材 1.1\n\n'
    '<!-- source-exercise:book-p9-9 -->\n'
    '2. 新题。\n\n   **答案：** 新解。\n\n   **难度：3/5；出处：** 教材 9.9\n'
)
report = plan_changes("course-1", exercises_md=EX)
check("认出一题未变", report["exercises"]["unchanged"] == 1)
check("认出一题新增", report["exercises"]["added"] == 1)
check("旧题被摘掉", report["exercises"]["unlinked"] == 1)
check("另一门锚点没被牵连", "kp-b" not in report["exercises"])

print("\n落库：一个事务，写哪些语句")
CURRENT_CURSOR.statements.clear()
result = apply_changes("course-1", None, EX, "admin-1")
sql = " || ".join(text for text, _ in CURRENT_CURSOR.statements)
check("新题入库", "INSERT INTO exercise" in sql)
check("关联建立", "INSERT INTO exercise_node" in sql)
check("旧关联删除", "DELETE FROM exercise_node" in sql)
check("孤儿题停用而不是删除", "review_status = 'retired'" in sql and "DELETE FROM exercise WHERE" not in sql)
check("留痕 content.import-docs", "content.import-docs" in sql)
check("返回落库计数", result["applied"]["retired"] == 1, str(result["applied"]))

print("\n落库：知识点.md 的正文改动")
reset(NODES, EXERCISES, LINKS)
CURRENT_CURSOR.statements.clear()
apply_changes("course-1", KNOW, None, "admin-1")
sql = " || ".join(text for text, _ in CURRENT_CURSOR.statements)
check("只更新正文", "UPDATE knowledge_node" in sql)
check("版本号自增", "content_version = content_version + 1" in sql)
check("已发布的保持发布，其余退回 draft", "'published'::review_status" in sql)
check("不碰题目", "INSERT INTO exercise" not in sql)

print("\n边界：")
reset(NODES, EXERCISES, LINKS)
check("一个文件都不传就报错", raises(lambda: plan_changes("course-1"), "至少要上传一个文件"))
reset([])
check("课程没有知识点时给明确提示",
      raises(lambda: plan_changes("course-x", knowledge_md=KNOW), "单文件导入只能改已有的课"))
reset(NODES, EXERCISES, LINKS)
check("习题.md 解析不出题就报错",
      raises(lambda: plan_changes("course-1", exercises_md='<a id="kp-a"></a>\n### 定义\n正文\n'),
             "一道题都没解析出来"))

print("")
if failures:
    print(f"单文件增量导入测试失败 {len(failures)} 项：")
    for name in failures:
        print(f"  · {name}")
    sys.exit(1)
print("单文件增量导入测试全部通过。")
