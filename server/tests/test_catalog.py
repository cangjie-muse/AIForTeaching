"""发布出口的单元测试（不连数据库）。

测的是 build_course：把库里的行拼成前端课程对象。这件事只有一处容易错——
字段名对不上不会报错，只会让某个页面静默少一块内容。所以这里逐字段对一遍，
包括前端 data/courses.js 里实际存在的字段（chapterId / exerciseIds / children /
exerciseAnchor / relationships / stats），而不是"看起来差不多"。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.catalog import build_course  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  ✗ {name}  — {detail}")


COURSE = {
    "id": "analysis-ii", "title": "数学分析 II", "description": "按章节组织",
    "audience": "本科高年级", "subject": "专业课程", "textbook": "陈纪修《数学分析》",
    "content_updated_at": datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc),
    "updated_at": datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc),
    "published_at": datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc),
}

NODES = [
    {"id": "analysis-ii", "title": "数学分析 II", "level": 1, "kind": "container",
     "path": ["数学分析 II"], "parent_id": None, "chapter_id": None,
     "content_md": "课程根", "exercise_anchor": "analysis-ii", "source_raw": "同事课程图谱"},
    {"id": "ch-1", "title": "第一章 实数", "level": 2, "kind": "container",
     "path": ["数学分析 II", "第一章 实数"], "parent_id": "analysis-ii",
     "chapter_id": "ch-1", "content_md": "", "exercise_anchor": "ch-1", "source_raw": ""},
    {"id": "kp-1", "title": "确界原理", "level": 3, "kind": "knowledge",
     "path": ["数学分析 II", "第一章 实数", "确界原理"], "parent_id": "ch-1",
     "chapter_id": "ch-1", "content_md": "非空有上界的数集必有上确界。",
     "exercise_anchor": "kp-1", "source_raw": "教材《数学分析》上册，书页12"},
    {"id": "kp-2", "title": "阿基米德性", "level": 3, "kind": "knowledge",
     "path": ["数学分析 II", "第一章 实数", "阿基米德性"], "parent_id": "ch-1",
     "chapter_id": "ch-1", "content_md": "", "exercise_anchor": "kp-2", "source_raw": ""},
]

EXERCISES = [
    {"id": "ex-a", "prompt_md": "叙述确界原理", "answer_md": "见教材", "difficulty": 3,
     "source_raw": "教材《数学分析》上册，书页12，教材习题1.1-1"},
    {"id": "ex-b", "prompt_md": "举例", "answer_md": "", "difficulty": None,
     "source_raw": "自拟"},
]

LINKS = [{"exercise_id": "ex-a", "node_id": "kp-1"},
         {"exercise_id": "ex-a", "node_id": "kp-2"},
         {"exercise_id": "ex-b", "node_id": "kp-1"}]

print("课程对象结构：")

course = build_course(COURSE, NODES, EXERCISES, LINKS)
expected_keys = {"schemaVersion", "id", "title", "audience", "status", "description",
                 "subject", "textbook", "importedAt", "resourceBase", "nodes",
                 "exercises", "relationships", "stats", "warnings", "files",
                 "reviewStatus", "attachments", "publishedAt"}
check("顶层字段齐全", expected_keys <= set(course), str(sorted(expected_keys - set(course))))
check("id 与标题", course["id"] == "analysis-ii" and course["title"] == "数学分析 II")
check("状态为 ready（学生端按它过滤）", course["status"] == "ready", course["status"])
check("reviewStatus 标成已发布", course["reviewStatus"] == "已发布", course["reviewStatus"])
check("发布时间是 ISO 字符串", str(course["publishedAt"]).startswith("2026-10-21T04:00"),
      str(course["publishedAt"]))
check("三件套文件名", course["files"] == ["知识节点.xlsx", "知识点.md", "习题.md"], str(course["files"]))

print("\n节点：")

by_id = {node["id"]: node for node in course["nodes"]}
node_keys = {"id", "title", "path", "level", "source", "exerciseAnchor", "content",
             "parentId", "children", "kind", "chapterId", "exerciseIds"}
check("节点字段齐全", node_keys <= set(by_id["kp-1"]), str(sorted(node_keys - set(by_id["kp-1"]))))
check("children 反推正确", by_id["ch-1"]["children"] == ["kp-1", "kp-2"],
      str(by_id["ch-1"]["children"]))
check("根节点没有 parentId", by_id["analysis-ii"]["parentId"] is None,
      str(by_id["analysis-ii"]["parentId"]))
check("exerciseIds 反推正确", by_id["kp-1"]["exerciseIds"] == ["ex-a", "ex-b"],
      str(by_id["kp-1"]["exerciseIds"]))
check("一道题挂两个知识点时两边都列", by_id["kp-2"]["exerciseIds"] == ["ex-a"],
      str(by_id["kp-2"]["exerciseIds"]))
check("chapterId 传下来了", by_id["kp-1"]["chapterId"] == "ch-1", str(by_id["kp-1"]["chapterId"]))
check("锚点沿用 exercise_anchor", by_id["kp-1"]["exerciseAnchor"] == "kp-1")
check("正文原样带出", by_id["kp-1"]["content"] == "非空有上界的数集必有上确界。")
check("来源原样带出", "书页12" in by_id["kp-1"]["source"], by_id["kp-1"]["source"])

print("\n题目：")

exercise_keys = {"id", "prompt", "answer", "difficulty", "source", "nodeIds"}
ex = {item["id"]: item for item in course["exercises"]}
check("题目字段齐全", exercise_keys <= set(ex["ex-a"]), str(sorted(exercise_keys - set(ex["ex-a"]))))
check("nodeIds 带了两个", ex["ex-a"]["nodeIds"] == ["kp-1", "kp-2"], str(ex["ex-a"]["nodeIds"]))
check("难度为 None 时保留 None（不是 0）", ex["ex-b"]["difficulty"] is None,
      str(ex["ex-b"]["difficulty"]))

print("\n关系与统计：")

check("包含边数 = 有父节点的节点数", len(course["relationships"]) == 3,
      str(len(course["relationships"])))
check("关系指向父节点", all(item["type"] == "contains" for item in course["relationships"]))
check("统计节点数", course["stats"]["nodes"] == 4, str(course["stats"]))
check("统计知识点数", course["stats"]["knowledge"] == 2, str(course["stats"]))
check("统计章节数", course["stats"]["sections"] == 1, str(course["stats"]))
check("统计题目数", course["stats"]["exercises"] == 2, str(course["stats"]))
check("统计关联数", course["stats"]["associations"] == 3, str(course["stats"]))

print("\n边界：")

empty = build_course({**COURSE, "published_at": None}, [], [], [])
check("空课程不炸", empty["nodes"] == [] and empty["exercises"] == [])
check("空课程 reviewStatus 为待发布", empty["reviewStatus"] == "待发布", empty["reviewStatus"])
check("空课程统计为 0", empty["stats"]["nodes"] == 0 and empty["stats"]["associations"] == 0)

orphan = build_course(COURSE, NODES, EXERCISES, [])
check("没有关联时题目仍然出现", len(orphan["exercises"]) == 2)
check("没有关联时 nodeIds 为空", orphan["exercises"][0]["nodeIds"] == [],
      str(orphan["exercises"][0]["nodeIds"]))

single = build_course(COURSE, [NODES[0]], [], [])
check("只有一个根节点时不产生关系边", single["relationships"] == [])

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("发布出口单元测试全部通过。")
