"""课程发布的两级口径（假库，不连数据库）。

钉住两件事：

1. **发布 = 把「人工已审」的题一起放出**。逐题点发布在 486 道题的课上不现实，
   但也不能把 draft 一起发。所以只放 `teacher-reviewed` 这一档，
   `machine-checked` 与 `draft` 仍然隐藏。
2. **整课放出不冒充人工校验**：`/exercises/publish-all` 只改 review_status，
   绝不写 human_verified_at——否则「人工已审」这个标记就成了摆设，
   可见范围里"仅人工审核"那档会放进没被任何人看过的题。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import catalog  # noqa: E402


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeDB:
    """按 SQL 里的特征片段回答，够 publish / publish-all 用。"""

    def __init__(self, nodes=10, published=0, drafts=5, promoted=3, updated=7):
        self.nodes, self.published, self.drafts = nodes, published, drafts
        self.promoted, self.updated = promoted, updated
        self.writes: list[tuple[str, tuple | None]] = []

    def query_one(self, sql, params=None):
        if "FROM course WHERE id" in sql:
            return {"id": params[0], "title": "测试课"}
        if "AS nodes" in sql and "AS exercises" in sql:
            return {"nodes": self.nodes, "exercises": self.published, "drafts": self.drafts}
        if "AS nodes" in sql:
            return {"nodes": self.nodes}
        return None

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        if "review_status = 'published'" in sql and "teacher-reviewed" in sql:
            # 放出之后 counts 才会看到这些题——假库也要跟着变，否则测的是假状态
            self.published += self.promoted
            return self.promoted
        if "review_status <> 'retired'" in sql and "review_status <> 'published'" in sql:
            return self.updated
        return 1

    def sql_matching(self, needle: str) -> list[str]:
        return [sql for sql, _ in self.writes if needle in sql]


original = catalog.db
try:
    # ---- 课程发布：先放出人工已审的题 ----
    db = FakeDB(nodes=10, published=0, drafts=5, promoted=3)
    catalog.db = db
    result = catalog.publish("course-x", "acc-admin")
    check("发布前先把 teacher-reviewed 放成 published",
          any("teacher-reviewed" in sql for sql in db.sql_matching("UPDATE exercise")))
    check("返回这次一并放出了多少题", result["promoted"] == 3, str(result["promoted"]))
    check("发布成功", result["published"] is True)
    check("放出后学生能看到的题数把已审的算进去", result["exercises"] == 3, str(result["exercises"]))

    # 有题可放：promotion 之后 counts 里就有题了
    db = FakeDB(nodes=10, published=486, drafts=0, promoted=0)
    catalog.db = db
    result = catalog.publish("course-x", "acc-admin")
    check("已审的题放出后课程就能发布", result["exercises"] == 486, str(result["exercises"]))
    check("记得写 publish_event", bool(db.sql_matching("INSERT INTO publish_event")))
    check("记得写审计日志", bool(db.sql_matching("INSERT INTO audit_log")))

    # 一道题都没有：课程照样能发布，但必须把"学生只能看知识点"说出来。
    # 拦在这里等于让讲义型课程永远不可见——大堂课上常见的那种课。
    db = FakeDB(nodes=10, published=0, drafts=486, promoted=0)
    catalog.db = db
    result = catalog.publish("course-x", "acc-admin")
    check("没题也能发布", result["published"] is True)
    check("没题时给出提醒", bool(result.get("warning")), str(result.get("warning"))[:30])
    check("提醒里说清学生只能看知识点", "只能看知识点" in (result.get("warning") or ""))
    check("提醒里指路「内容审核」", "内容审核" in (result.get("warning") or ""))

    # 没有知识点的课不能发布
    db = FakeDB(nodes=0, published=5, drafts=0, promoted=0)
    catalog.db = db
    try:
        catalog.publish("course-x", "acc-admin")
        check("没有知识点时拒绝发布", False, "居然通过了")
    except HTTPException as error:
        check("没有知识点时拒绝发布", error.status_code == 400, error.detail[:30])

    # ---- 整课放出：不冒充人工校验 ----
    db = FakeDB(nodes=44, updated=269)
    catalog.db = db
    result = catalog.publish_exercises_all("course-x", "acc-admin", "整本书都认可")
    check("整课放出返回放出题数", result["published"] == 269, str(result["published"]))
    check("整课放出明确说自己不是人工校验", result["verified"] is False, str(result["verified"]))
    check("整课放出不写 human_verified_at",
          not db.sql_matching("human_verified"), "、".join(db.sql_matching("UPDATE exercise"))[:60])
    check("整课放出只动未停用且未发布的题",
          any("retired" in sql and "<> 'published'" in sql for sql in db.sql_matching("UPDATE exercise")))
    check("整课放出写审计日志", bool(db.sql_matching("exercise.publish-all")))
    check("审计里记下理由", any("整本书都认可" in (params or ("",))[-1]
                              for sql, params in db.writes if "audit_log" in sql))

    db = FakeDB(nodes=0, updated=0)
    catalog.db = db
    try:
        catalog.publish_exercises_all("course-x", "acc-admin")
        check("没有知识点时拒绝整课放出", False, "居然通过了")
    except HTTPException as error:
        check("没有知识点时拒绝整课放出", error.status_code == 400, error.detail[:30])
finally:
    catalog.db = original

print("")
if failures:
    print(f"发布口径测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("发布口径测试全部通过。")
