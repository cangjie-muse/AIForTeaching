"""切章规则的单元测试（不依赖数据库、不依赖网络、不调模型）。

切章错了不会报错，只会让整章的页码溯源偏一页到十几页，审核员还以为是教材印错。
所以这里用真实的教材排版特征来测：页眉重复、正文里"见第 1 章"、中文数字章号、
扫描件抽出来没有标题行（要退回页窗切分）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.chapters import split_chapters  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def page(text: str) -> str:
    return text


# 一本很典型的教材：每页页眉都印「第1章 实数」，正文里还会提到别的章
BOOK = {
    1: "数学分析\n上册\n第1章 实数\n1.1 实数系",
    2: "第1章 实数\n有理数与无理数。见第2章关于确界的讨论。",
    3: "第1章 实数\n1.2 上确界\n设 S 非空有上界。",
    4: "第2章 极限\n2.1 数列极限",
    5: "第2章 极限\n收敛数列的性质。",
    6: "第3章 连续函数\n3.1 连续的定义",
    7: "第3章 连续函数\n闭区间上连续函数的性质。",
}


def main() -> int:
    print("切章规则：")

    plan = split_chapters(BOOK)
    check("按标题切分", plan["source"] == "heading", plan["source"])
    check("切出三章", len(plan["items"]) == 3, str([i["key"] for i in plan["items"]]))
    if len(plan["items"]) == 3:
        first, second, third = plan["items"]
        check("第1章从第1页开始", first["page_from"] == 1, str(first["page_from"]))
        check("第1章到第3页结束", first["page_to"] == 3, str(first["page_to"]))
        check("第2章从第4页开始", second["page_from"] == 4, str(second["page_from"]))
        check("第3章到书末结束", third["page_to"] == 7, str(third["page_to"]))
        check("章标题保留原文", first["title"] == "第1章 实数", first["title"])
        check("章 key 稳定可复现", [i["key"] for i in plan["items"]] == ["ch-1", "ch-2", "ch-3"],
              str([i["key"] for i in plan["items"]]))

    # 页眉重复不能当成新章：上面每页都有「第1章」，切出来仍然只有 3 章
    check("重复页眉不算新章", len(plan["items"]) == 3, str(len(plan["items"])))

    # 正文里的「见第2章」不算章标题：第1章不能在第 2 页就断掉
    check("正文里的交叉引用不算章标题", plan["items"][0]["page_to"] == 3,
          str(plan["items"][0]["page_to"]))

    print("\n中文数字章号：")
    cn_book = {
        1: "第一章 集合",
        2: "第一章 集合\n内容",
        3: "第十二章 级数",
        4: "第十二章 级数\n内容",
    }
    cn_plan = split_chapters(cn_book)
    keys = [item["key"] for item in cn_plan["items"]]
    check("「第一章」识别为 ch-1", keys[0] == "ch-1", keys[0])
    check("「第十二章」识别为 ch-12", keys[1] == "ch-12", keys[1])
    check("中文数字不会切成碎片", len(cn_plan["items"]) == 2, str(keys))

    print("\n英文与节号：")
    en_plan = split_chapters({
        1: "Chapter 1 Limits",
        2: "Chapter 1 Limits\nText",
        3: "Chapter 2 Continuity",
    })
    en_keys = [item["key"] for item in en_plan["items"]]
    check("Chapter N 能识别", en_keys == ["ch-1", "ch-2"], str(en_keys))

    section_plan = split_chapters({
        1: "§1 集合",
        2: "§2 映射",
        3: "§2 映射\n内容",
    })
    check("§N 能识别", [i["key"] for i in section_plan["items"]] == ["ch-1", "ch-2"],
          str([i["key"] for i in section_plan["items"]]))

    print("\n兜底：切不出标题时按页窗切")
    flat = {page: f"正文第 {page} 页，没有标题行。" for page in range(1, 31)}
    window_plan = split_chapters(flat, window=10)
    check("退回页窗切分", window_plan["source"] == "window", window_plan["source"])
    check("页窗切分有 warning", bool(window_plan["warning"]), window_plan["warning"])
    check("30 页按 10 页一段切成 3 段", len(window_plan["items"]) == 3,
          str([(i["page_from"], i["page_to"]) for i in window_plan["items"]]))
    check("页窗首段从第 1 页起", window_plan["items"][0]["page_from"] == 1,
          str(window_plan["items"][0]["page_from"]))
    check("页窗末段到书末", window_plan["items"][-1]["page_to"] == 30,
          str(window_plan["items"][-1]["page_to"]))

    print("\n边界：")
    empty = split_chapters({})
    check("空文本不报错但也没有章", empty["items"] == [] and empty["source"] == "empty",
          empty["source"])

    many = {page: f"第{page}章 标题{page}" for page in range(1, 51)}
    capped = split_chapters(many, max_chapters=40)
    check("章数超过上限时截断", len(capped["items"]) == 40, str(len(capped["items"])))
    check("截断时有说明", "只处理前 40 章" in capped["warning"], capped["warning"])
    check("最后一章延伸到书末", capped["items"][-1]["page_to"] == 50,
          str(capped["items"][-1]["page_to"]))

    long_line = split_chapters({1: "第1章 " + "很长的标题" * 20, 2: "第2章 短标题"})
    check("过长的一行不当章标题", long_line["source"] == "window", long_line["source"])

    print("")
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("切章规则测试全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
