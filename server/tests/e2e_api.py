#!/usr/bin/env python3
"""后端 API 端到端测试：真 FastAPI + 真 psycopg + 真 PostgreSQL 协议。

数据库由 scripts/e2e-api.cjs 拉起（PGlite 通过 socket 暴露成 PG 端口），
所以这里跑的是真实的 SQL 往返，不是 mock。

运行：npm run test:api

顺序上有一条硬约束：**会触发数据库错误的用例必须放在最后**。
PGlite 的 socket 实现在一次 SQL 报错后协议会错位（后续 SELECT 读不到结果集，
但服务器其实执行了），所以 409 这类用例一旦提前，后面所有读库断言都不可信。
线上 PostgreSQL 没有这个问题，这是测试基建的局限，不是应用代码的缺陷。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, llm, qa  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{' — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


# 头值只能是 ASCII：中文姓名用百分号编码传，服务端 unquote 还原
ADMIN = {"X-Actor": "%E4%BF%A1%E6%81%AF%E5%8C%96%E4%B8%93%E5%91%98", "X-Roles": "admin"}
REVIEWER = {"X-Actor": "reviewer-1", "X-Roles": "reviewer"}
STUDENT = {"X-Actor": "student-1", "X-Roles": "student"}
STUDENT2 = {"X-Actor": "student-2", "X-Roles": "student"}


def seed_fixture() -> None:
    """最小夹具：1 门课、1 章、2 知识点、3 道题。"""
    db.query(
        "INSERT INTO course (id, title, audience, subject, status) VALUES (%s,%s,%s,%s,%s)",
        ("e2e-course", "端到端测试课", "本科生", "数学", "ready"),
    )
    db.query(
        "INSERT INTO chapter (id, course_id, title, ord) VALUES (%s,%s,%s,%s)",
        ("ch-1", "e2e-course", "第一章 极限", 1),
    )
    db.query(
        """INSERT INTO knowledge_node (course_id, id, chapter_id, parent_id, title, level, kind, path)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        ("e2e-course", "ch-1", "ch-1", None, "第一章 极限", 2, "container", ["第一章 极限"]),
    )
    db.query(
        """INSERT INTO knowledge_node (course_id, id, chapter_id, parent_id, title, level, kind, path, content_md)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        ("e2e-course", "n-1", "ch-1", "ch-1", "数列极限", 3, "knowledge",
         ["第一章 极限", "数列极限"], "若对任意 $\\varepsilon>0$ 存在 $N$……"),
    )
    for i, status in enumerate(["draft", "machine-checked", "machine-checked"], start=1):
        db.query(
            """INSERT INTO exercise (course_id, id, chapter_id, prompt_md, answer_md,
                                     difficulty, source_raw, provenance, review_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            ("e2e-course", f"ex-{i}", "ch-1", f"第 {i} 题：求极限", "解：……", 2,
             f"教材第 {i} 页", "textbook", status),
        )
        db.query(
            "INSERT INTO exercise_node (course_id, exercise_id, node_id) VALUES (%s,%s,%s)",
            ("e2e-course", f"ex-{i}", "n-1"),
        )


def endpoint(name: str, kind: str, base_url: str, model: str, **extra) -> dict:
    payload = {"name": name, "kind": kind, "base_url": base_url, "model": model,
               "data_scope": "external", "notes": "端到端测试用", **extra}
    return payload


def run() -> None:
    with TestClient(app) as client:
        seed_fixture()

        print("\n只读接口：")
        health = client.get("/api/v1/health")
        check("GET /health 可用", health.status_code == 200, str(health.status_code))
        body = health.json()
        check("健康检查连上数据库", body.get("db") is True)
        models = body.get("models") or {}
        check(
            "健康检查报告模型配置但不含密钥",
            "egress_mode" in models and "api_key" not in str(models).lower(),
        )

        courses = client.get("/api/v1/courses")
        check("GET /courses 可用", courses.status_code == 200)
        check("课程列表含夹具课程", any(c["id"] == "e2e-course" for c in courses.json()["items"]))

        graph = client.get("/api/v1/courses/e2e-course/graph")
        check("GET /courses/{id}/graph 可用", graph.status_code == 200)
        payload = graph.json()
        check(
            "图谱返回节点与包含边",
            len(payload.get("nodes", [])) >= 2 and len(payload.get("relationships", [])) >= 1,
            f"{len(payload.get('nodes', []))} 节点 / {len(payload.get('relationships', []))} 边",
        )

        print("\n配置接口权限：")
        check("未授权身份默认 admin（开发模式）", client.get("/api/v1/admin/whoami").json()["can_edit"] is True)
        chinese = client.get("/api/v1/admin/whoami", headers=ADMIN).json()
        check("百分号编码的中文身份被还原", chinese["actor"] == "信息化专员", chinese["actor"])
        reviewer_me = client.get("/api/v1/admin/whoami", headers=REVIEWER).json()
        check("审核员可看不可改", reviewer_me["can_view"] is True and reviewer_me["can_edit"] is False)
        student_me = client.get("/api/v1/admin/whoami", headers=STUDENT).json()
        check("学生无权限", student_me["can_view"] is False and student_me["can_edit"] is False)

        denied = client.post(
            "/api/v1/admin/model-endpoints", headers=REVIEWER,
            json=endpoint("x", "chat", "https://api.deepseek.com", "m"),
        )
        check("审核员新增端点被 403 拒绝", denied.status_code == 403, str(denied.status_code))
        check("学生读取端点列表被 403 拒绝",
              client.get("/api/v1/admin/model-endpoints", headers=STUDENT).status_code == 403)

        print("\n端点增删改查：")
        created = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("deepseek-chat", "chat", "https://api.deepseek.com", "deepseek-flash",
                          notes="学院同意使用国内大模型 API", enabled=True),
        )
        check("新增外部对话端点", created.status_code == 201, created.text[:120])
        row = created.json()
        check("初始状态为未配置密钥", row.get("key_configured") is False)
        check("返回里没有密钥字段", "api_key_path" not in row)

        emb = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("bailian-embedding", "embedding",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "qwen3.7-text-embedding", dim=1024, enabled=True),
        )
        check("新增 1024 维向量端点", emb.status_code == 201, emb.text[:120])

        mismatch = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("mislabel", "chat", "https://api.deepseek.com", "m", data_scope="internal"),
        )
        check("地址与数据范围不一致被 400 拒绝", mismatch.status_code == 400, mismatch.text[:80])

        no_note = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json={"name": "nonote", "kind": "chat", "base_url": "https://api.deepseek.com",
                  "model": "m", "data_scope": "external", "enabled": False},
        )
        check("外部端点缺说明被拒绝", no_note.status_code == 400, no_note.text[:80])

        bad_dim = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("emb-bad", "embedding", "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "qwen3.7-text-embedding", dim=1536, enabled=False),
        )
        check("非 1024 维度被 400 拒绝", bad_dim.status_code == 400, bad_dim.text[:80])

        patched = client.patch(
            "/api/v1/admin/model-endpoints/deepseek-chat", headers=ADMIN,
            json={"model": "deepseek-v4-pro"},
        )
        check("PATCH 修改模型", patched.status_code == 200 and patched.json()["model"] == "deepseek-v4-pro")

        print("\n密钥：写入文件、绝不回显")
        key_value = "sk-e2e-test-0123456789abcdef"
        set_key = client.put(
            "/api/v1/admin/model-endpoints/deepseek-chat/key", headers=ADMIN,
            json={"api_key": key_value},
        )
        check("写入密钥成功", set_key.status_code == 200, set_key.text[:80])
        check("写入响应不含密钥原文", key_value not in set_key.text)

        listed = client.get("/api/v1/admin/model-endpoints", headers=ADMIN).json()["items"]
        target = next(item for item in listed if item["name"] == "deepseek-chat")
        check("列表显示已配置密钥", target["key_configured"] is True)
        check("列表不含密钥路径", "api_key_path" not in target)
        check("列表不含密钥原文", key_value not in str(listed))

        for role_headers, label in [(ADMIN, "管理员"), (REVIEWER, "审核员")]:
            got = client.get("/api/v1/admin/model-endpoints", headers=role_headers)
            check(f"{label}读取时不泄露密钥", key_value not in got.text)

        secrets_dir = Path(os.environ["SECRETS_DIR"])
        key_file = secrets_dir / "deepseek-chat.key"
        check("密钥落在文件系统", key_file.exists(), str(key_file))
        if key_file.exists():
            check("文件内容正确", key_file.read_text(encoding="utf-8").strip() == key_value)

        cleared = client.delete("/api/v1/admin/model-endpoints/deepseek-chat/key", headers=ADMIN)
        check("删除密钥成功", cleared.status_code == 200)
        check("删除后文件消失", not key_file.exists())

        print("\n审核接口：")
        summary = client.get("/api/v1/review/summary", headers=REVIEWER)
        check("GET /review/summary 可用", summary.status_code == 200, summary.text[:100])

        built = client.post("/api/v1/review/queue/build", headers=REVIEWER, params={"course_id": "e2e-course"})
        check("建队接口可用", built.status_code == 200, built.text[:120])

        queue = client.get("/api/v1/review/queue", headers=REVIEWER).json()
        items = queue.get("items", [])
        check("队列返回待审题目", len(items) >= 2, f"{len(items)} 条")
        check("队列条目带问题清单", all("issues" in item for item in items))
        # 审核页依赖这些字段名；改后端列名时这里必须一起改
        required = {"task_id", "course_id", "exercise_id", "state", "priority",
                    "prompt_md", "difficulty", "provenance", "source_page",
                    "tier", "node_ids", "issues"}
        missing = required - set(items[0].keys()) if items else required
        check("队列字段满足审核页契约", not missing, f"缺 {sorted(missing)}" if missing else "")

        if items:
            task_id = items[0]["task_id"]
            claimed = client.post(
                f"/api/v1/review/tasks/{task_id}/decision", headers=REVIEWER,
                json={"action": "claim", "note": "我来看这道"},
            )
            check("领取任务", claimed.status_code == 200 and claimed.json()["state"] == "in-review",
                  claimed.text[:100])

            approved = client.post(
                f"/api/v1/review/tasks/{task_id}/decision", headers=REVIEWER,
                json={"action": "approve", "note": "检查通过"},
            )
            check("审核通过", approved.status_code == 200 and approved.json()["state"] == "approved",
                  approved.text[:100])

        patched_ex = client.patch(
            "/api/v1/review/exercises/e2e-course/ex-1", headers=REVIEWER,
            json={"prompt_md": "第 1 题（已改）：求极限", "source_page": 12},
        )
        check("改题干与页码", patched_ex.status_code == 200, patched_ex.text[:120])

        revisions = db.query(
            "SELECT count(*) AS n FROM exercise_revision WHERE course_id=%s AND exercise_id=%s",
            ("e2e-course", "ex-1"),
        )
        check("修订快照落库（触发器）", revisions and revisions[0]["n"] >= 1,
              f"{(revisions[0]['n'] if revisions else 0)} 版")

        audits = db.query("SELECT action FROM audit_log ORDER BY id")
        actions = {row["action"] for row in audits}
        check(
            "审计记录覆盖配置与审核动作",
            {"create-model-endpoint", "set-model-key", "update-model-endpoint"} <= actions,
            ", ".join(sorted(actions)),
        )
        check("审计里没有密钥原文", key_value not in str(db.query("SELECT detail FROM audit_log")))

        print("\n出校策略：")
        blocked = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("unapproved", "chat", "https://evil-proxy.example.com/v1", "m", enabled=False),
        )
        check("未批准供应商可登记（登记不等于放行）", blocked.status_code == 201, blocked.text[:80])
        tested = client.post("/api/v1/admin/model-endpoints/unapproved/test", headers=ADMIN)
        check("自检拒绝未批准供应商", tested.status_code == 400, tested.text[:100])

        print("\n问答链路：")

        # 教材与切片：一份已授权、一份仅校内。
        # 这两份数据同时用来验证"版权未确认的原文不外传"。
        db.query(
            """INSERT INTO textbook (id, course_id, title, pdf_sha256, copyright_status)
               VALUES (%s,%s,%s,%s,%s)""",
            ("tb-ok", "e2e-course", "数学分析（已授权）", "a" * 64, "licensed"),
        )
        db.query(
            """INSERT INTO textbook (id, course_id, title, pdf_sha256, copyright_status)
               VALUES (%s,%s,%s,%s,%s)""",
            ("tb-internal", "e2e-course", "数学分析（仅校内）", "b" * 64, "internal-only"),
        )
        db.query(
            """INSERT INTO textbook_chunk (textbook_id, seq, section_path, page_from, page_to, content_md)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            ("tb-ok", 1, ["第一章 极限", "数列极限"], 18, 18,
             "定义：若对任意 ε>0，存在正整数 N，当 n>N 时 |a_n - a| < ε，"
             "则称数列 {a_n} 收敛于 a。数列极限与函数极限的区别在于自变量取离散值。"),
        )
        db.query(
            """INSERT INTO textbook_chunk (textbook_id, seq, section_path, page_from, page_to, content_md)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            ("tb-internal", 1, ["内部讲义"], 3, 3,
             "内部讲义：数列极限的十个习题答案汇总，仅供教师参考，不得外传。"),
        )

        captured = {"messages": [], "calls": 0}

        def fake_chat(messages, temperature=0.2):
            captured["messages"].append(messages)
            captured["calls"] += 1
            return {"provider": "fake", "model": "fake-chat", "data_scope": "approved",
                    "content": "先想想 ε 的几何意义：它控制的是「多靠近」。",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

        real_chat = llm.chat
        llm.chat = fake_chat
        try:
            asked = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限怎么定义", "course_id": "e2e-course", "mode": "guide"},
            )
            check("引导模式可回答", asked.status_code == 200, asked.text[:120])
            payload = asked.json() if asked.status_code == 200 else {}
            check("回答带教材引用", len(payload.get("citations") or []) >= 1,
                  f"{len(payload.get('citations') or [])} 条引用")
            context_sent = captured["messages"][-1][1]["content"] if captured["messages"] else ""
            check("引用了已授权教材", "已授权" in str(payload.get("citations")), str(payload.get("citations"))[:80])
            check("仅校内教材的原文没有外传", "内部讲义" not in context_sent and "不得外传" not in context_sent)
            check("外传字符数被记录并返回", payload.get("context_chars", 0) > 0, str(payload.get("context_chars")))

            internal_only = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "十个习题答案汇总", "course_id": "e2e-course", "mode": "guide"},
            )
            check("只有仅校内教材命中时拒答", internal_only.json().get("refused") is True,
                  internal_only.text[:100])

            denied_full = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限怎么定义", "course_id": "e2e-course",
                      "exercise_id": "ex-1", "mode": "full"},
            )
            check("未作答时不给完整解答（403）", denied_full.status_code == 403, denied_full.text[:100])

            db.query(
                """INSERT INTO exercise_attempt (student_id, course_id, exercise_id, answer_md)
                   VALUES (%s,%s,%s,%s)""",
                ("student-1", "e2e-course", "ex-1", "我的尝试：先取 N = [1/ε] + 1。"),
            )
            unlocked = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限怎么定义", "course_id": "e2e-course",
                      "exercise_id": "ex-1", "mode": "full"},
            )
            check("作答后解锁完整解答", unlocked.status_code == 200, unlocked.text[:120])
            full_system = captured["messages"][-1][0]["content"] if captured["messages"] else ""
            check("完整模式换了提示词", "完整" in full_system or "逐步" in full_system)

            missing_ids = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限", "mode": "full"},
            )
            check("完整模式缺题目信息被 400", missing_ids.status_code == 400, missing_ids.text[:80])

            calls = db.query("SELECT succeeded, prompt_version, error FROM model_call ORDER BY id")
            check("每次外传都留了账", len(calls) == 2, f"{len(calls)} 条")
            # 拒答与 403 都在调模型之前就返回了：既不出校，也不花钱
            check("拒答与未解锁不调用模型", captured["calls"] == 2, f"{captured['calls']} 次调用")
            check("账本里记了字符数", all("chars" in (row["prompt_version"] or "") for row in calls))

            def failing_chat(messages, temperature=0.2):
                raise RuntimeError("模型返回 500: upstream error")

            llm.chat = failing_chat
            broken = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限怎么定义", "course_id": "e2e-course", "mode": "guide"},
            )
            check("模型失败映射为 502", broken.status_code == 502, broken.text[:100])
            last = db.query_one("SELECT succeeded, error FROM model_call ORDER BY id DESC LIMIT 1")
            check("失败的调用也记账", last["succeeded"] is False and "500" in (last["error"] or ""),
                  str(last)[:100])
        finally:
            llm.chat = real_chat

        rows = [{"section_path": ["a"], "page_from": 1, "page_to": 1, "content_md": "x" * 500,
                 "chunk_id": 1, "textbook_title": "t"}]
        text, used, citations = qa.compose_context(rows, budget=200)
        check("上下文预算硬生效", used <= 200 and len(text) <= 200, f"{used} 字符")
        _, zero_used, _ = qa.compose_context(rows, budget=50)
        check("剩余预算太小时整段丢弃", zero_used == 0, f"{zero_used} 字符")

        print("\n作答提交：")

        first = client.post(
            "/api/v1/attempts", headers=STUDENT,
            json={"course_id": "e2e-course", "exercise_id": "ex-3",
                  "answer_md": "我的思路：先取 N = [1/ε] + 1。", "duration_sec": 90},
        )
        check("学生可提交作答", first.status_code == 201, first.text[:120])
        payload = first.json() if first.status_code == 201 else {}
        check("首次提交记为第 1 次", payload.get("attempt_no") == 1, str(payload.get("attempt_no")))
        check("提交即声明解锁完整解答", payload.get("unlocks_full") is True)

        second = client.post(
            "/api/v1/attempts", headers=STUDENT,
            json={"course_id": "e2e-course", "exercise_id": "ex-3", "answer_md": "再交一次。"},
        )
        check("重复提交累加次数而不是覆盖", second.json().get("attempt_no") == 2,
              str(second.json().get("attempt_no")))

        missing = client.post(
            "/api/v1/attempts", headers=STUDENT,
            json={"course_id": "e2e-course", "exercise_id": "no-such-exercise", "answer_md": "x"},
        )
        check("不存在的题目不建档（404）", missing.status_code == 404, missing.text[:100])

        blank = client.post(
            "/api/v1/attempts", headers=STUDENT,
            json={"course_id": "e2e-course", "exercise_id": "ex-3", "answer_md": ""},
        )
        check("空作答被拒绝", blank.status_code == 422, str(blank.status_code))

        mine = client.get("/api/v1/attempts?course_id=e2e-course&exercise_id=ex-3", headers=STUDENT)
        check("能查回自己的作答记录", mine.status_code == 200 and len(mine.json()["items"]) == 2,
              str(len(mine.json()["items"])) if mine.status_code == 200 else mine.text[:80])

        # 换一个学生来查：作答属于个人，不能互相看见（审核员/教师走另一条授权路径）
        other = client.get("/api/v1/attempts?exercise_id=ex-3", headers=STUDENT2)
        check("别的学生查不到我的作答", other.status_code == 200 and other.json()["items"] == [],
              other.text[:100])

        # 提交后，同一道题可以解锁完整解答。这里用假模型，避免依赖真实密钥。
        def fake_unlock(messages, temperature=0.2):
            return {"provider": "fake", "model": "fake-chat", "data_scope": "approved",
                    "content": "完整解答（测试）", "usage": {}}

        real_chat = llm.chat
        llm.chat = fake_unlock
        try:
            unlocked_by_api = client.post(
                "/api/v1/qa/ask", headers=STUDENT,
                json={"question": "数列极限怎么定义", "course_id": "e2e-course",
                      "exercise_id": "ex-3", "mode": "full"},
            )
            check("通过作业接口提交后即可解锁完整解答",
                  unlocked_by_api.status_code == 200, unlocked_by_api.text[:100])
        finally:
            llm.chat = real_chat

        # --- 以下会产生真正的数据库错误，必须最后跑（见文件头说明）---
        print("\n数据库约束（放在最后：会让 PGlite 协议错位）：")
        dup = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("deepseek-chat", "chat", "https://api.deepseek.com", "m"),
        )
        check("重名端点被 409 拒绝", dup.status_code == 409, str(dup.status_code))

        conflict = client.post(
            "/api/v1/admin/model-endpoints", headers=ADMIN,
            json=endpoint("chat-two", "chat", "https://api.moonshot.cn/v1", "kimi-k3", enabled=True),
        )
        check("同时启用两个对话端点被 409 拒绝", conflict.status_code == 409, str(conflict.status_code))


if __name__ == "__main__":
    run()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        raise SystemExit(1)
    print("端到端测试全部通过。")
