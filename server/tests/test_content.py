"""课程内容导入/导出的单元测试（不连数据库）。

这里只测能脱离数据库验证的部分：校验规则、章节推断、溯源解析、Markdown 渲染。
落库与整体更新（retired）的语义在 e2e_api.py 里用真库验证——那种事只有真跑一次
才算数，用假库测等于自己骗自己。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app.content import (  # noqa: E402
    MAX_NODES, ExerciseIn, NodeIn, PackIn, CourseMetaIn, render_exercise_md,
    render_knowledge_md, validate_pack,
)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  ✗ {name}  — {detail}")


def course(title: str = "数学分析 II") -> CourseMetaIn:
    return CourseMetaIn(title=title, description="按章节组织", subject="数学")


def node(node_id: str, title: str, level: int, parent_id: str | None = None,
         path: list[str] | None = None, content: str = "正文", tags: list[str] | None = None) -> NodeIn:
    return NodeIn(id=node_id, title=title, level=level, parent_id=parent_id,
                  path=path if path is not None else [title], content_md=content,
                  tags=tags or [])


def exercise(ex_id: str, nodes: list[str], source: str = "教材《数学分析》上册，书页15，教材习题1.1-3") -> ExerciseIn:
    return ExerciseIn(id=ex_id, prompt_md=f"{ex_id} 的题干", answer_md="解答",
                      difficulty=3, source_raw=source, node_ids=nodes)


def pack(nodes: list[NodeIn], exercises: list[ExerciseIn] | None = None,
         mode: str = "replace") -> PackIn:
    return PackIn(course=course(), nodes=nodes, exercises=exercises or [], mode=mode)


print("结构校验：")

try:
    validate_pack(pack([]))
    check("空包被拒", False)
except HTTPException as error:
    check("空包被拒", error.status_code == 400, str(error.detail))

try:
    validate_pack(pack([node("a", "甲", 3), node("b", "乙", 3)]))
    check("没有一级节点被拒", False)
except HTTPException as error:
    check("没有一级节点被拒", "一级节点" in str(error.detail), str(error.detail))

try:
    validate_pack(pack([node("root", "课程", 1), node("x", "甲", 2), NodeIn(id="x", title="乙", level=2)]))
    check("ID 重复被拒", False)
except HTTPException as error:
    check("ID 重复被拒", "重复" in str(error.detail), str(error.detail))

ok = validate_pack(pack([
    node("course-x", "数学分析 II", 1),
    node("ch-1", "第一章 实数", 2, "course-x", ["数学分析 II", "第一章 实数"]),
    node("kp-1", "确界原理", 3, "ch-1", ["数学分析 II", "第一章 实数", "确界原理"],
         tags=["定义", "定理"]),
    node("kp-2", "阿基米德性", 4, "kp-1", ["数学分析 II", "第一章 实数", "确界原理", "阿基米德性"]),
]))
check("合法包通过", True)
check("章节取二级节点", [c["id"] for c in ok["chapters"]] == ["ch-1"], str([c["id"] for c in ok["chapters"]]))
by_id = {item["id"]: item for item in ok["nodes"]}
check("有子节点的标 container", by_id["kp-1"]["kind"] == "container", by_id["kp-1"]["kind"])
check("叶子标 knowledge", by_id["kp-2"]["kind"] == "knowledge", by_id["kp-2"]["kind"])
check("四级节点挂到最近的二级章节", by_id["kp-2"]["chapter_id"] == "ch-1", str(by_id["kp-2"]["chapter_id"]))
check("标签入库", by_id["kp-1"]["tags"] == ["定义", "定理"], str(by_id["kp-1"]["tags"]))
check("没有题时给警告", any("没有题目" in w for w in ok["warnings"]), str(ok["warnings"])[:60])

# 前端解析器会填 parent_id；万一没填，要能按路径推断出来，不能整包拒掉
loose = validate_pack(PackIn(course=course(), nodes=[
    node("root", "课程", 1), node("ch", "第一章", 2, None, ["课程", "第一章"]),
    node("k", "知识点", 3, None, ["课程", "第一章", "知识点"]),
]))
loose_by_id = {item["id"]: item for item in loose["nodes"]}
check("缺 parent_id 时按路径推断", loose_by_id["k"]["parent_id"] == "ch", str(loose_by_id["k"]["parent_id"]))

print("\n题目校验：")

try:
    validate_pack(pack([node("root", "课程", 1)], [exercise("e1", ["nope"])]))
    check("挂到不存在的知识点被拒", False)
except HTTPException as error:
    check("挂到不存在的知识点被拒", "不存在的知识点" in str(error.detail), str(error.detail))

try:
    validate_pack(pack([node("root", "课程", 1)], [
        ExerciseIn(id="e1", prompt_md="  ", node_ids=["root"])]))
    check("空题干被拒", False)
except HTTPException as error:
    check("空题干被拒", "题干为空" in str(error.detail), str(error.detail))

ok2 = validate_pack(pack([node("root", "课程", 1), node("k", "知识点", 2, "root", ["课程", "知识点"])],
                         [exercise("gen-1-abc-001", ["k"]), ExerciseIn(id="e2", prompt_md="无主之题", node_ids=[])]))
ex = {item["id"]: item for item in ok2["exercises"]}
check("溯源解析出书名", ex["gen-1-abc-001"]["source_book"] == "数学分析", str(ex["gen-1-abc-001"]["source_book"]))
check("溯源解析出书页", ex["gen-1-abc-001"]["source_page"] == 15, str(ex["gen-1-abc-001"]["source_page"]))
check("溯源解析出题号", ex["gen-1-abc-001"]["source_no"] == "1.1-3", str(ex["gen-1-abc-001"]["source_no"]))
check("教材原题判为 textbook", ex["gen-1-abc-001"]["provenance"] == "textbook",
      ex["gen-1-abc-001"]["provenance"])
check("无主之题给警告", any("没有关联知识点" in w for w in ok2["warnings"]), str(ok2["warnings"])[:80])

print("\n导出 Markdown（必须能被导入器再读回来）：")

nodes = ok["nodes"]
exercises = validate_pack(pack(
    [node("course-x", "数学分析 II", 1),
     node("ch-1", "第一章 实数", 2, "course-x", ["数学分析 II", "第一章 实数"]),
     node("kp-1", "确界原理", 3, "ch-1", ["数学分析 II", "第一章 实数", "确界原理"])],
    [exercise("gen-ch-1-abc-001", ["kp-1"])],
))["exercises"]

knowledge_md = render_knowledge_md("数学分析 II", nodes)
exercise_md = render_exercise_md("数学分析 II", nodes, exercises)

ANCHOR = re.compile(r'<a\s+id="([^"]+)"\s*>\s*</a>\s*\n(#{1,6})\s+([^\n]+)\n')


def sections(text: str) -> dict[str, str]:
    """与 js/import-core.js 的 sections() 同一套规则：锚点紧跟标题。"""
    matches = list(ANCHOR.finditer(text))
    result = {}
    for index, match in enumerate(matches):
        body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        result[match.group(1)] = body
    return result


ksections = sections(knowledge_md)
check("知识点.md 锚点齐全", {"ch-1", "kp-1"} <= set(ksections), str(sorted(ksections)))
check("知识点.md 带标签行", "**知识点标签：**" in ksections["kp-1"])
check("知识点.md 带正文", "正文" in ksections["kp-1"])

esections = sections(exercise_md)
check("习题.md 锚点齐全", "ch-1" in esections and "kp-1" in esections, str(sorted(esections)))
check("习题.md 题目带稳定 ID", "<!-- source-exercise:gen-ch-1-abc-001 -->" in esections["kp-1"])
check("习题.md 元数据行可解析", "**难度：3/5；出处：**" in esections["kp-1"])

META = re.compile(r"\*\*难度：(\d)/5；出处：\*\*\s*(.*)")
prompt = esections["kp-1"].split("<!-- source-exercise:gen-ch-1-abc-001 -->")[1]
check("题干能被切出来", "gen-ch-1-abc-001 的题干" in prompt, prompt.strip()[:40])
check("答案能被切出来", "**答案：**" in prompt)
match = META.search(prompt)
check("难度能被读回", bool(match) and match.group(1) == "3", match.group(0)[:30] if match else "未匹配")
check("出处能被读回", bool(match) and "数学分析" in match.group(2), match.group(2)[:40] if match else "未匹配")

check("知识点.md 有结尾换行", knowledge_md.endswith("\n"))
check("习题.md 有结尾换行", exercise_md.endswith("\n"))

print("\n边界：")

try:
    validate_pack(pack([node(f"n{i}", f"节点{i}", 3) for i in range(MAX_NODES + 1)]))
    check("超量包被拒", False)
except HTTPException as error:
    check("超量包被拒", "超过" in str(error.detail), str(error.detail)[:40])

only_root = validate_pack(pack([node("root", "只有根", 1)]))
check("只有一个根也能过（不建章节）", only_root["chapters"] == [], str(only_root["chapters"]))
check("只有一个根时它是叶子知识点", only_root["nodes"][0]["kind"] == "knowledge",
      only_root["nodes"][0]["kind"])

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("课程内容导入导出单元测试全部通过。")
