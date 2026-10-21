"""生成链路的单元测试（不依赖数据库、不依赖网络）。

测的是"模型输出能不能变成能入库的东西"，因为这里错了不会报错：
模型少写一个字段、题号写成中文、页码写到另一章，界面看起来都正常，
只有审核员打开时才发现一堆挂不上知识点的孤儿题。

样本是模型真实会犯的错，不是编的。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "db"))

from app.generation import (  # noqa: E402
    Pack, apply_pack, assign_ids, exercise_source_text, fake_payload, node_id,
    pack_ids, render_exercise_md, render_knowledge_md, validate_payload,
)
from build_seed import parse_source  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


GOOD = {
    "chapter_title": "第1章 实数与函数",
    "chapter_summary": "实数系的基本性质与函数概念。",
    "nodes": [
        {"title": "上确界", "content_md": "设 $S$ 非空有上界，则存在最小上界，称为上确界。"
                                          "这是实数完备性的核心表述：上确界存在等价于"
                                          "戴德金分割，也等价于柯西列收敛。",
         "page_from": 12, "page_to": 14, "tags": ["定义", "定理"]},
        {"title": "函数", "content_md": "设 $A,B$ 非空，若对每个 $x\\in A$ 按某规则有唯一 "
                                        "$y\\in B$ 与之对应，称该规则为函数。函数相等要求"
                                        "定义域相同且每点函数值相同。",
         "page_from": 15, "page_to": 16, "tags": ["定义"]},
    ],
    "exercises": [
        {"source_no": "习题1.1-3", "page": 15, "prompt_md": "证明上确界唯一。",
         "answer_md": "设 $a,b$ 都是上确界，则 $a\\le b$ 且 $b\\le a$，故 $a=b$。",
         "difficulty": 2, "node_title": "上确界", "from_textbook": True},
        {"page": None, "prompt_md": "举例说明两个函数相等的含义。",
         "answer_md": "定义域相同且每点函数值相同，二者才相等。",
         "node_title": "函数", "from_textbook": False},
    ],
}

print("模型输出校验：")

pack = validate_payload(GOOD, 10, 20)
check("知识点全部收下", len(pack.nodes) == 2, str(len(pack.nodes)))
check("习题全部收下", len(pack.exercises) == 2, str(len(pack.exercises)))
check("节点 id 由标题派生且稳定", pack.nodes[0]["id"] == node_id("上确界"), pack.nodes[0]["id"])
check("缺 difficulty 的题默认 3", pack.exercises[1]["difficulty"] == 3)
check("正常输入没有警告", not pack.warnings, str(pack.warnings))

# 模型常见的四类错误
bad = dict(GOOD)
bad["nodes"] = GOOD["nodes"] + [{"title": "上确界", "content_md": "重复标题。",
                                 "page_from": 1, "page_to": 1}]
bad["exercises"] = GOOD["exercises"] + [
    {"prompt_md": "短", "answer_md": "短", "node_title": "上确界"},          # 题干太短
    {"prompt_md": "挂错知识点的题：证明极限唯一。", "answer_md": "略，见正文推导过程。",
     "node_title": "极限", "difficulty": 9, "page": 900, "from_textbook": True},
]
pack = validate_payload(bad, 10, 20)
check("重复知识点被丢掉", len(pack.nodes) == 2, str(len(pack.nodes)))
check("题干过短的题被丢掉", len(pack.exercises) == 3, str(len(pack.exercises)))
check("挂不上知识点的题保留但记警告",
      any("不在本章知识点里" in w for w in pack.warnings), str(pack.warnings))
check("越界页码被记警告", any("页码 900" in w for w in pack.warnings), str(pack.warnings))
check("越界难度被拉回 1–5", pack.exercises[-1]["difficulty"] == 3,
      str(pack.exercises[-1]["difficulty"]))

# 空章节必须报错，而不是生成一个空课程
for broken, label in [({"chapter_title": ""}, "缺标题"), ({"chapter_title": "第1章", "nodes": []}, "没知识点")]:
    try:
        validate_payload(broken, 1, 10)
        check(f"{label}应报错", False)
    except ValueError:
        check(f"{label}应报错", True)

print("\n出处写法（必须能被导入器解析）：")

source = {"book_line": "陈纪修等《数学分析》第三版上册", "book_short": "《数学分析》",
          "pdf_page": 20, "generated_at": "2026-10-21"}
textbook_ex = {"from_textbook": True, "page": 15, "source_no": "习题1.1-3"}
raw = exercise_source_text(textbook_ex, source)
parsed = parse_source({"source": raw})
check("教材题有书名（书名号已去掉）", parsed["source_book"] == "数学分析", str(parsed["source_book"]))
check("教材题有书页", parsed["source_page"] == 15, str(parsed["source_page"]))
check("教材题有题号", parsed["source_no"] == "习题1.1-3", str(parsed["source_no"]))
check("教材题判为 textbook", parsed["provenance"] == "textbook", parsed["provenance"])

self_ex = {"from_textbook": False, "page": None, "source_no": None}
raw = exercise_source_text(self_ex, source)
parsed = parse_source({"source": raw})
check("自拟题判为 self-authored", parsed["provenance"] == "self-authored", parsed["provenance"])
check("自拟题不带页码", parsed["source_page"] is None, str(parsed["source_page"]))

print("\n三件套渲染：")

pack = validate_payload(GOOD, 10, 20)
knowledge = render_knowledge_md(pack, "数学分析 I", source)
exercises = render_exercise_md(pack, "数学分析 I", source)
check("知识点文件有锚点", f'<a id="{pack.nodes[0]["id"]}"></a>' in knowledge)
check("知识点文件有跨文件链接", "习题.md#" in knowledge)
check("习题文件有 source-exercise", "<!-- source-exercise:" in exercises)
check("习题文件有单行元数据", "**难度：2/5；出处：**" in exercises)
check("习题文件标了未审核", "待教师审核" in exercises)

print("\n演示模式：")
fake = fake_payload("<!-- page 1 -->\n第一章 实数\n<!-- page 2 -->\n内容")
check("演示输出能过校验", validate_payload(fake, 1, 2).nodes[0]["title"] == "演示知识点")
check("演示输出自报家门", "演示" in fake["chapter_title"])

print("\n两章同名时 id 不能撞（撞了会互相覆盖）：")

same_title_a = validate_payload(GOOD, 10, 20)
same_title_b = validate_payload(GOOD, 30, 40)      # 标题一模一样，只是页码不同
chapter_a, prefix_a = pack_ids(same_title_a, {"page_from": 10, "page_to": 20}, "ch-1")
chapter_b, prefix_b = pack_ids(same_title_b, {"page_from": 30, "page_to": 40}, "ch-2")
check("同名两章拿到不同章 id", chapter_a != chapter_b, f"{chapter_a} / {chapter_b}")
check("同名两章拿到不同题目前缀", prefix_a != prefix_b, f"{prefix_a} / {prefix_b}")

assign_ids(same_title_a, {"page_from": 10, "page_to": 20}, "ch-1")
assign_ids(same_title_b, {"page_from": 30, "page_to": 40}, "ch-2")
check("同名两章的题号不冲突", same_title_a.exercises[0]["id"] != same_title_b.exercises[0]["id"],
      f"{same_title_a.exercises[0]['id']} / {same_title_b.exercises[0]['id']}")

# 同一个 chapter_key 重跑要拿到同样的 id，否则重跑会重复出题
assign_ids(same_title_a, {"page_from": 10, "page_to": 20}, "ch-1")
check("同一章重跑 id 稳定", same_title_a.exercises[0]["id"] == f"{prefix_a}001",
      same_title_a.exercises[0]["id"])

single = validate_payload(GOOD, 10, 20)
single_chapter, single_prefix = pack_ids(single, {"page_from": 10, "page_to": 20}, None)
check("单章模式用页码范围做前缀", single_prefix.startswith("gen-p10-20-"), single_prefix)

print("\n演示模式的标题要来自章节正文，不是来自提示语：")

prompt = ("下面是教材第 2 页的文字。页标记里的数字是 PDF 页；书页码 = PDF 页 - 0。\n"
          "---- 教材文字开始 ----\n<!-- page 3 -->\n第2章 极限\n内容\n---- 教材文字结束 ----")
demo = fake_payload(prompt)
check("演示标题取章节首行", "第2章 极限" in demo["chapter_title"], demo["chapter_title"])
check("不会把提示语当标题", "下面是教材" not in demo["chapter_title"], demo["chapter_title"])

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("生成链路单元测试全部通过。")
