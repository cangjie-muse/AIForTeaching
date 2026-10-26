"""课件生成与修订的单元测试（假库 + 假模型，不连数据库、不渲染真 pptx）。

钉住的规则：

1. 模型输出必须被收成确定结构：LaTeX 命令降级成纯文本、来源 ID 只能是本章有的、
   页数有上限——幻灯片上出现 `\\frac` 是硬伤，界面上不能靠教师发现；
2. 变更清单由代码 diff 两版计划得到，不是模型自报；
3. 资料指纹变了要能被识别（课件"过期"）；
4. 生成失败时版本标 failed 并写原因，不留半成品；
5. 只有教师/管理员能生成与确认。

真渲染（python-pptx）在 scripts/test-courseware-render.py 里单跑，因为它依赖
本地可能没装的库。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import courseware  # noqa: E402
from app.auth import Actor  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeDB:
    def __init__(self, *, nodes=None, exercises=None, courses=None, versions=None,
                 decks=None, deck_versions=None):
        self.nodes = nodes or []
        self.exercises = exercises or []
        self.courses = courses or [{"id": "analysis-ii", "title": "数学分析II"}]
        self.versions = versions or {}
        self.decks = decks or []
        self.deck_versions = deck_versions or []
        self.statements: list[tuple[str, tuple | None]] = []

    @staticmethod
    def _flat(sql: str) -> str:
        return " ".join(sql.split())

    def query(self, sql, params=None):
        self.statements.append((sql, params))
        text = self._flat(sql)
        if "FROM knowledge_node" in text:
            return [dict(row) for row in self.nodes]
        if "FROM exercise e" in text:
            return [dict(row) for row in self.exercises]
        if "FROM courseware_deck d" in text:
            return [dict(row) for row in self.decks]
        if "FROM courseware_version" in text:
            return [dict(row) for row in self.deck_versions]
        return []

    def query_one(self, sql, params=None):
        self.statements.append((sql, params))
        text = self._flat(sql)
        if "FROM course WHERE id" in text or "SELECT id, title FROM course" in text:
            match = next((row for row in self.courses if row["id"] == params[0]), None)
            return dict(match) if match else None
        if "FROM knowledge_node WHERE course_id" in text:
            match = next((row for row in self.nodes
                          if row["id"] == params[1]), None)
            return dict(match) if match else None
        if "max(number)" in text:
            return {"n": max([row["number"] for row in self.deck_versions], default=0)}
        if "FROM courseware_version v" in text:
            return dict(self.versions.get(params[0], {})) or None
        if "FROM courseware_version WHERE id" in text:
            match = self.versions.get(params[0])
            return dict(match) if match else None
        if "FROM courseware_deck WHERE" in text:
            match = next((row for row in self.decks
                          if row["chapter_id"] == params[1]), None)
            return dict(match) if match else None
        return None

    @staticmethod
    def _assignments(sql: str, params) -> dict:
        import re
        match = re.search(r"SET (.*?) WHERE", " ".join(sql.split()), re.S)
        if not match:
            return {}
        values = iter(params or ())
        result = {}
        for part in match.group(1).split(","):
            name, _, value = part.strip().partition(" = ")
            value = value.strip()
            # `%s::jsonb` 也要认出占位符：带 cast 的写法在 sql 里到处都是，
            # 只匹配纯 `%s` 会让这些列静默不更新，测试却在错的地方通过。
            bare = value.split("::")[0].strip()
            if bare == "%s":
                result[name] = next(values, None)
            elif value.startswith("'") and value.endswith("'"):
                result[name] = value[1:-1]
            elif value.upper() == "NULL":
                result[name] = None
        return result

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        text = self._flat(sql)
        if text.startswith("UPDATE courseware_version"):
            row = self.versions.get(params[-1])
            if row is not None:
                changes = self._assignments(sql, params)
                for name in ("slide_plan", "change_log", "base_plan"):
                    if name in changes and isinstance(changes[name], str):
                        changes[name] = json.loads(changes[name])
                row.update(changes)
        return 1


def teacher() -> Actor:
    return Actor(id="acc-t", roles=frozenset({"teacher"}), display_name="张老师")


def student() -> Actor:
    return Actor(id="acc-s", roles=frozenset({"student"}), display_name="李同学")


def nodes_fixture():
    return [
        {"id": "analysis-ii-g013", "title": "定积分", "level": 2,
         "path": ["数学分析II", "定积分"], "content_md": "本章讲定积分。"},
        {"id": "analysis-ii-g014", "title": "定积分概念", "level": 3,
         "path": ["数学分析II", "定积分", "定积分概念"],
         "content_md": "分割、求和、取极限。" * 20},
        {"id": "analysis-ii-g016", "title": "微积分学基本公式", "level": 3,
         "path": ["数学分析II", "定积分", "微积分学基本公式"], "content_md": ""},
        {"id": "analysis-ii-g999", "title": "别章的节", "level": 3,
         "path": ["数学分析II", "另一章", "别的节"], "content_md": "不该出现"},
    ]


print("页面计划规范化：")
plan = courseware.normalize_plan(json.dumps({"title": "定积分", "slides": [
    {"title": "封面", "kind": "content", "bookPages": "p.1"},
    {"title": "定义", "bullets": ["分割", "求和"], "paragraphs": ["曲边梯形面积"],
     "equations": [{"text": "\\frac{a}{b}", "latex": "\\frac{a}{b}"}],
     "sourceNodeIds": ["analysis-ii-g014", "analysis-ii-不存在"], "speakerNote": "先提问"},
]}, ensure_ascii=False), {"analysis-ii-g014"}, 12)
check("第一页强制成封面", plan["slides"][0]["kind"] == "cover", plan["slides"][0]["kind"])
check("LaTeX 分式降级成纯文本", plan["slides"][1]["equations"][0]["text"] == "(a)/(b)",
      plan["slides"][1]["equations"][0]["text"])
check("latex 源式保留", plan["slides"][1]["equations"][0]["latex"] == "\\frac{a}{b}", "")
check("来源 ID 只留本章有的", plan["slides"][1]["sourceNodeIds"] == ["analysis-ii-g014"],
      str(plan["slides"][1]["sourceNodeIds"]))
check("讲稿带上", plan["slides"][1]["speakerNote"] == "先提问", "")

many = courseware.normalize_plan({"slides": [{"bullets": [f"要点 {i}"]} for i in range(60)]},
                                 set(), 12)
check("页数封顶 40", len(many["slides"]) == 40, str(len(many["slides"])))
check("缺标题时补页号", many["slides"][3]["title"] == "第 4 页", many["slides"][3]["title"])

for bad, label in ((({"slides": []}), "没有 slides 报错"),
                   (("这不是 JSON"), "非 JSON 报错")):
    try:
        courseware.normalize_plan(bad, set(), 12)
        check(label, False, "居然通过了")
    except ValueError as exc:
        check(label, bool(str(exc)), str(exc)[:40])

print("\n变更清单：")
base = {"slides": [{"title": "封面", "kind": "cover"},
                   {"title": "定义", "bullets": ["a", "b"], "paragraphs": []},
                   {"title": "练习", "bullets": ["x"], "paragraphs": []}]}
new = {"slides": [{"title": "封面", "kind": "cover"},
                  {"title": "定义", "bullets": ["a", "b", "c"], "paragraphs": []},
                  {"title": "课堂练习", "bullets": ["x"], "paragraphs": []},
                  {"title": "小结", "bullets": ["y"], "paragraphs": []}]}
log = courseware.diff_change_log(base, new)
kinds = [item["kind"] for item in log]
check("要点增加被记为 edit", kinds.count("edit") == 1, str(kinds))
check("改标题被记为 move", "move" in kinds, str(kinds))
check("新增页被记为 add", "add" in kinds, str(kinds))
check("改动描述写清页码", any(item["page"] == 2 and "要点 +1" in item["summary"] for item in log),
      json.dumps(log, ensure_ascii=False)[:120])

truncated = {"slides": base["slides"]}
log = courseware.diff_change_log(truncated, base)
check("没改动就不产生记录", log == [], json.dumps(log, ensure_ascii=False)[:80])

log = courseware.diff_change_log(None, new)
check("初版每个变更都记下来", len(log) == 4 and log[0]["kind"] == "create", str(len(log)))

print("\n本地知识库上下文：")
original_db = courseware.db
db = FakeDB(nodes=nodes_fixture(), exercises=[
    {"id": "ex-1", "prompt_md": "用定义计算", "answer_md": "答案", "difficulty": 2,
     "source_no": "1"}])
courseware.db = db
context = courseware.collect_context("analysis-ii", "analysis-ii-g013")
titles = [row["title"] for row in context["nodes"]]
check("章包含自己与两节", titles == ["定积分", "定积分概念", "微积分学基本公式"], str(titles))
check("不含别章的节", "别章的节" not in titles, str(titles))
check("习题带进上下文", context["exercises"][0]["id"] == "ex-1", "")
check("章节标题取节点标题", context["chapter_title"] == "定积分", context["chapter_title"])
hash_before = context["source_sha256"]

db.nodes[1]["content_md"] = "换了正文"
context2 = courseware.collect_context("analysis-ii", "analysis-ii-g013")
check("正文变了指纹就变", context2["source_sha256"] != hash_before, "")

db.exercises = []
warning = courseware.empty_context_warning(
    courseware.collect_context("analysis-ii", "analysis-ii-g013"))
check("没有习题时给出提示", "课堂练习" in (warning or ""), str(warning))

for row in db.nodes:
    row["content_md"] = ""
warning = courseware.empty_context_warning(
    courseware.collect_context("analysis-ii", "analysis-ii-g013"))
check("整章没正文时提示先导入", "还没有知识点正文" in (warning or ""), str(warning))

db.nodes = nodes_fixture()
db.exercises = [{"id": "ex-1", "prompt_md": "用定义计算", "answer_md": "答案",
                 "difficulty": 2, "source_no": "1"}]

try:
    courseware.collect_context("analysis-ii", "不存在")
    check("章节不存在时报错", False, "居然通过了")
except ValueError as exc:
    check("章节不存在时报错", "章节不存在" in str(exc), str(exc)[:30])

print("\n权限：")
try:
    courseware._guard_write(student())
    check("学生不能生成课件", False, "居然放行了")
except HTTPException as exc:
    check("学生不能生成课件", exc.status_code == 403, str(exc.status_code))
courseware._guard_write(teacher())
check("教师可以生成", True)

print("\n任务：成功与失败")
original_finish = courseware._finish_version
original_render = courseware.render_version
original_chat = courseware._call_text
original_llm = courseware.llm


class FakeLLM:
    class _Endpoint:
        model = "deepseek-flash"

    @staticmethod
    def chat_endpoint():
        return FakeLLM._Endpoint()

    @staticmethod
    def chat(messages, temperature=0.3):
        return {"content": json.dumps({"title": "定积分 课件", "slides": [
            {"title": "定积分", "kind": "cover"},
            {"title": "学习目标", "bullets": ["理解分割—求和—取极限"]},
            {"title": "定义", "paragraphs": ["曲边梯形面积"], "equations": [
                {"text": "∫_a^b f(x)dx", "latex": "\\int_a^b f(x)\\,dx"}]},
            {"title": "课堂练习", "bullets": ["用定义计算"], "exerciseIds": ["ex-1"]},
            {"title": "小结", "bullets": ["定积分是和的极限"]},
            {"title": "结束", "bullets": ["下一次讲可积条件"]},
        ]}, ensure_ascii=False)}


courseware.llm = FakeLLM
rendered: dict = {}


def fake_render(version_id, plan, meta):
    rendered["plan"] = plan
    rendered["meta"] = meta
    return {"path": f"courseware/{version_id}.pptx", "bytes": 4096,
            "sha256": "x" * 64, "slide_count": len(plan["slides"])}


courseware.render_version = fake_render

db = FakeDB(nodes=nodes_fixture(), exercises=[
    {"id": "ex-1", "prompt_md": "用定义计算", "answer_md": "答案", "difficulty": 2,
     "source_no": "1"}])
courseware.db = db
version_row = {"id": "cw-abc-v1", "deck_id": "cw-abc", "number": 1, "status": "pending",
               "title": "定积分 课件", "instruction": "", "page_target": 12,
               "base_plan": None, "slide_plan": {}, "course_id": "analysis-ii",
               "chapter_id": "analysis-ii-g013", "chapter_title": "定积分",
               "audience": "本科二年级", "course_title": "数学分析II"}
db.versions["cw-abc-v1"] = version_row


class JobDB(FakeDB):
    def query_one(self, sql, params=None):
        if "FROM courseware_version v" in self._flat(sql):
            return dict(version_row)
        return super().query_one(sql, params)


db.__class__ = JobDB
from app import generation
generation_db_before = generation.db
generation.db = db
out = courseware.run_job({"id": 11, "kind": "courseware-generate",
                          "payload": {"version_id": "cw-abc-v1"}})
check("任务成功", out["status"] == "succeeded", str(out.get("error"))[:60])
check("渲染出的页数与计划一致", rendered["plan"] and len(rendered["plan"]["slides"]) == 6,
      str(len((rendered.get("plan") or {}).get("slides", []))))
check("渲染带课程与章节名", rendered["meta"]["course_title"] == "数学分析II", "")
check("版本状态转为待确认", version_row["status"] == "needs-review", version_row["status"])
check("写入资料指纹", bool(version_row.get("source_sha256")), "")
check("写入初版变更清单", len(version_row.get("change_log") or []) == 6,
      str(len(version_row.get("change_log") or [])))

# 失败路径
version_row2 = dict(version_row, id="cw-abc-v2", number=2, status="pending",
                    base_plan={"slides": [{"title": "定积分", "kind": "cover"}]},
                    instruction="把小结拆开")
db.versions["cw-abc-v2"] = version_row2


def broken_render(version_id, plan, meta):
    raise RuntimeError("pptx 保存失败")


courseware.render_version = broken_render
out = courseware.run_job({"id": 12, "kind": "courseware-revise",
                          "payload": {"version_id": "cw-abc-v2"}})
check("渲染失败任务标失败", out["status"] == "failed", str(out.get("error"))[:40])
check("版本标失败并写原因", version_row2["status"] == "failed"
      and "pptx 保存失败" in (version_row2.get("error") or ""), str(version_row2.get("error"))[:40])

print("\n路由与依赖：")
check("路由带 /api/v1 前缀", courseware.router.prefix == "/api/v1/courseware",
      courseware.router.prefix)
source = (ROOT / "server/app/generation.py").read_text(encoding="utf-8")
check("worker 认两种课件任务",
      "courseware-generate" in source and "courseware-revise" in source, "")
check("课件渲染依赖写进 requirements",
      "python-pptx" in (ROOT / "server/requirements.txt").read_text(encoding="utf-8"), "")

generation.db = generation_db_before
courseware.db = original_db
courseware.render_version = original_render
courseware._finish_version = original_finish
courseware._call_text = original_chat
courseware.llm = original_llm

print("")
if failures:
    print(f"课件测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("课件测试全部通过。")
