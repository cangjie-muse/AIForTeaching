"""出处解析的单元测试（不依赖数据库、不依赖网络）。

为什么单独测：`parse_source` 把自由文本出处拆成教材、书页、PDF 页、题号、溯源类型。
它错了不会报错，只会让审核队列里多出几百条"溯源缺失"或让页码全空——
而这正是上线前 4807 条数据里真实发生过的事。

样本全部取自现有数据（`data/course-*.json`）的真实写法，不是编的。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓ 会直接抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))

from build_seed import parse_source, review_of  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def parse(raw: str) -> dict:
    return parse_source({"source": raw})


print("出处解析：")

# 1. 西文教材，没有书名号。上线前这类全部落进 unknown（809 条里的大头）。
conway = parse(
    "John B. Conway, Functions of One Complex Variable, 2nd ed., Springer-Verlag，"
    "Chapter 1 §3 Exercises 3，书页4，PDF第1页"
)
check("西文教材取出书名", conway["source_book"] is not None, str(conway["source_book"]))
check("西文教材取出书页", conway["source_page"] == 4, str(conway["source_page"]))
check("“PDF第N页”也能取到", conway["source_pdf_page"] == 1, str(conway["source_pdf_page"]))
check("西文教材题号取 Exercises N", conway["source_no"] == "3", str(conway["source_no"]))
check("西文教材判为教材原题", conway["provenance"] == "textbook", conway["provenance"])

# 2. 带来源短标签的西文书目
vershynin = parse(
    "A：Roman Vershynin, High-Dimensional Probability, Second Edition，"
    "Chapter 2 Exercise 2.1，书页18，PDF第30页"
)
check("短标签 A： 不影响取书名",
      vershynin["source_book"] is not None and "Vershynin" in vershynin["source_book"],
      str(vershynin["source_book"]))
check("短标签西文书判为教材原题", vershynin["provenance"] == "textbook", vershynin["provenance"])

# 3. 中文教材：书名号 + 原题号
chen = parse(
    "陈纪修、於崇华、金路《数学分析》第三版上册，教材例6.1.8，书页209，PDF第229页；"
    "已目视核对原页，原题号例6.1.8；**解答来源：** 教材正文解法核对后写成中文简解。"
)
check("中文教材取出书名", "数学分析" in (chen["source_book"] or ""), str(chen["source_book"]))
check("书页与 PDF 页都对", (chen["source_page"], chen["source_pdf_page"]) == (209, 229),
      f"{chen['source_page']}/{chen['source_pdf_page']}")
check("原题号取到", chen["source_no"] == "例6.1.8", str(chen["source_no"]))
check("中文教材判为教材原题", chen["provenance"] == "textbook", chen["provenance"])

# 4. 旧契约写法 "PDF页43" 仍然要认（不能为了新写法丢掉旧写法）
old_style = parse("《线性代数》第一版，书页29，PDF页43；原题号习题1.2")
check("旧写法“PDF页43”仍然可用", old_style["source_pdf_page"] == 43, str(old_style["source_pdf_page"]))

# 5. 整章范围不是精确页
span = parse("《数学分析》第一版，书页1–99，原题号习题1.1")
check("整章范围不算精确书页", span["source_page"] is None, str(span["source_page"]))
check("整章范围留了说明", "范围" in (span["provenance_note"] or ""), str(span["provenance_note"]))

# 6. 自拟题：没有页码是正常的，必须判成 self-authored
selfmade = parse(
    "课程建设自拟训练题；知识依据陈纪修等《数学分析》第三版相关章节，"
    "无教材原题号或原题页码，原题号自拟训练题 3；**解答来源：** 课程建设自拟并独立计算核对。"
)
check("课程建设自拟题判为自拟", selfmade["provenance"] == "self-authored", selfmade["provenance"])

# 7. 同事导出表：题号取行号
exported = parse("同事《数学分析2_习题导出.xlsx》第155行（导出表未标教材原号）")
check("导出表判为 colleague-export", exported["provenance"] == "colleague-export", exported["provenance"])
check("导出表用行号作题号", "155" in (exported["source_no"] or ""), str(exported["source_no"]))

# 8. 只写“教材例x”没写“原题号”
only_example = parse("《数学分析》第三版上册，教材例9.1.1，书页2，PDF第23页")
check("教材例也能取题号", only_example["source_no"] == "9.1.1", str(only_example["source_no"]))

# 9. 书名号优先于西文段：不能把出版信息当成书名
priority = parse("《实变函数论》第二版，周民强，书页88，PDF第99页，原题号习题3.2")
check("书名号优先", priority["source_book"] == "实变函数论", str(priority["source_book"]))

# 10. “待核”是审核状态，不是内容
check("含待核的题停在 draft", review_of("书页待核，PDF第12页") == "draft")
check("干净出处可以进 machine-checked", review_of("书页12，PDF第20页") == "machine-checked")

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("出处解析测试全部通过。")
