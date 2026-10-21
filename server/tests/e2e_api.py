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
import re
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

    # 两道专门用来验证问题分类的题：
    #   ex-4 自拟题没有教材页码，不该被标成页码不精确；
    #   ex-5 出处里写了待核，应当单独归到页码待核，而不是笼统的内容待核。
    for exercise_id, source_raw, provenance, difficulty, status in [
        ("ex-4", "课程建设自拟训练题，无教材原题页码", "self-authored", 1, "machine-checked"),
        ("ex-5", "《数学分析》第一版，书页待核，PDF第12页", "textbook", 2, "draft"),
    ]:
        db.query(
            """INSERT INTO exercise (course_id, id, chapter_id, prompt_md, answer_md,
                                     difficulty, source_raw, provenance, review_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            ("e2e-course", exercise_id, "ch-1", f"{exercise_id} 题面", "解：……", difficulty,
             source_raw, provenance, status),
        )
        db.query(
            "INSERT INTO exercise_node (course_id, exercise_id, node_id) VALUES (%s,%s,%s)",
            ("e2e-course", exercise_id, "n-1"),
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

        by_id = {item["exercise_id"]: item for item in items}
        categories = {ex: {issue["category"] for issue in item["issues"]}
                      for ex, item in by_id.items()}
        check("自拟题不被标页码问题",
              "ex-4" in categories and "page-imprecise" not in categories["ex-4"],
              str(categories.get("ex-4")))
        check("有书页但标了待核的题只归到页码待核",
              categories.get("ex-5") == {"page-imprecise"},
              str(categories.get("ex-5")))
        pending_details = [issue["detail"] for issue in by_id.get("ex-5", {}).get("issues", [])
                           if issue["category"] == "page-imprecise"]
        check("页码待核的说明能看懂", any("待核" in text for text in pending_details),
              str(pending_details))

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

        print("\n修订差异：")

        before = client.get("/api/v1/review/exercises/e2e-course/ex-2/revisions")
        check("未修改时没有修订历史", before.status_code == 200 and before.json()["count"] == 0,
              str(before.json().get("count")) if before.status_code == 200 else before.text[:80])

        patched = client.patch(
            "/api/v1/review/exercises/e2e-course/ex-2", headers=ADMIN,
            json={"prompt_md": "改过的题干（第一版）", "change_note": "改页码"},
        )
        check("管理员可以改题干", patched.status_code == 200, patched.text[:100])
        client.patch(
            "/api/v1/review/exercises/e2e-course/ex-2", headers=ADMIN,
            json={"prompt_md": "改过的题干（第二版）", "difficulty": 4},
        )

        after = client.get("/api/v1/review/exercises/e2e-course/ex-2/revisions")
        payload = after.json() if after.status_code == 200 else {}
        check("两次修改产生两条快照", payload.get("count") == 2, str(payload.get("count")))
        check("当前版本在 exercise 表", payload.get("current", {}).get("prompt_md") == "改过的题干（第二版）",
              str(payload.get("current", {}).get("prompt_md")))
        latest = (payload.get("revisions") or [{}])[0]
        check("最近一条快照是上一版内容", latest.get("prompt_md") == "改过的题干（第一版）",
              str(latest.get("prompt_md")))
        check("快照记了改动人", bool(latest.get("changed_by")), str(latest.get("changed_by")))
        check("最近一条快照的说明来自那次修改", latest.get("change_note") == "api patch",
              str(latest.get("change_note")))
        oldest = (payload.get("revisions") or [{}, {}])[-1]
        check("最早一条快照记了手写的修改说明", oldest.get("change_note") == "改页码",
              str(oldest.get("change_note")))
        check("最早一条快照保留了原始题干", "第 2 题" in str(oldest.get("prompt_md")),
              str(oldest.get("prompt_md"))[:40])

        gone = client.get("/api/v1/review/exercises/e2e-course/no-such/revisions")
        check("不存在的题目查修订返回 404", gone.status_code == 404, str(gone.status_code))

        # 改完之后，审核队列里读到的应该是新题干
        refreshed = client.get("/api/v1/review/queue?limit=100")
        target = next((item for item in refreshed.json()["items"]
                       if item["exercise_id"] == "ex-2"), None)
        check("队列读到的是修改后的题干", target and target["prompt_md"] == "改过的题干（第二版）",
              str(target["prompt_md"]) if target else "未找到 ex-2")

        print("\n教材上传与生成：")

        # 一份最小但结构真实的 PDF：2 页、有字体对象、有文字绘制指令。
        # 探测走的是不依赖库的字节启发式，所以这里不需要 pypdf。
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Count 2 /Kids [3 0 R 6 0 R] >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
            b"4 0 obj\n<< /Length 44 >>\nstream\nBT /F1 12 Tf 72 720 Td (Chapter 1) Tj ET\nendstream\nendobj\n"
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
            b"6 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
            b"7 0 obj\n<< /Length 44 >>\nstream\nBT /F1 12 Tf 72 720 Td (Chapter 1) Tj ET\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        )
        uploaded = client.post(
            "/api/v1/textbooks", headers=ADMIN, params={
                "title": "端到端测试教材", "course_id": "e2e-course", "authors": "测试作者",
                "edition": "第 1 版", "page_offset": 0,
            }, content=pdf_bytes,
        )
        check("上传教材 PDF", uploaded.status_code == 201, uploaded.text[:140])
        book = uploaded.json() if uploaded.status_code == 201 else {}
        check("探测到页数", book.get("page_count") == 2, str(book.get("page_count")))
        check("识别出文字层", book.get("has_text_layer") is True, str(book.get("has_text_layer")))
        check("探测不依赖 PDF 库", book.get("probe_engine") == "heuristic",
              str(book.get("probe_engine")))

        refused = client.post(
            "/api/v1/textbooks", headers=REVIEWER,
            params={"title": "x", "course_id": "e2e-course"}, content=pdf_bytes,
        )
        check("审核员不能上传教材", refused.status_code == 403, str(refused.status_code))

        # 抽取这一步在装了 pypdf 的机器上由 scripts/extract_pdf.py 完成；
        # 测试环境没有该库，所以直接放一份抽取结果，模拟"已经抽好"。
        import tempfile

        text_dir = Path(tempfile.mkdtemp(prefix="e2e-gen-"))
        text_path = text_dir / "chapter.md"
        text_path.write_text(
            "<!-- page 1 -->\n第1章 实数\n实数系有上确界性质。\n\n"
            "<!-- page 2 -->\n1.1 上确界\n设 $S$ 非空有上界，则存在最小上界。\n",
            encoding="utf-8",
        )
        db.query("UPDATE textbook SET text_path = %s WHERE id = %s",
                 (str(text_path), book["textbook_id"]))

        started = client.post(
            f"/api/v1/textbooks/{book['textbook_id']}/generate", headers=ADMIN,
            json={"page_from": 1, "page_to": 2, "chapter_hint": "实数"},
        )
        check("生成任务入队", started.status_code == 202, started.text[:140])
        job_id = (started.json() or {}).get("job", {}).get("id") if started.status_code == 202 else None
        queued = client.get(f"/api/v1/jobs/{job_id}", headers=REVIEWER)
        check("审核员能看到任务", queued.status_code == 200 and queued.json()["status"] == "queued",
              queued.text[:100])

        from app import generation  # noqa: E402  - 放在这里，避免影响文件头的导入顺序

        # 端到端测试不联网：强制走演示模型。真实模型调用另有单元测试覆盖
        # （test_generation 调模型返回值与 JSON 提取），这里要验的是队列与落库。
        generation.DEV_FAKE_MODEL = True
        done = generation.process_once()
        generation.DEV_FAKE_MODEL = False
        check("worker 领到任务并执行", done is not None and done["id"] == job_id,
              f"{done['id'] if done else None} vs {job_id}")
        check("生成任务成功", done is not None and done["status"] == "succeeded",
              str(done.get("error") if done else ""))

        result = (done or {}).get("result") or {}
        check("生成了知识点", result.get("nodes", 0) >= 1, str(result.get("nodes")))
        check("生成了习题", result.get("exercises", 0) >= 1, str(result.get("exercises")))
        check("记录了所用提示词版本", result.get("prompt_version") == generation.PROMPT_VERSION,
              str(result.get("prompt_version")))
        check("演示模型不会假装成真模型", result.get("model") == "demo", str(result.get("model")))

        pack_dir = Path(result.get("pack_dir") or "")
        exercises_md = pack_dir / "习题.md"
        check("资源包落在磁盘上", exercises_md.exists(), str(pack_dir))
        check("资源包里带可导入的题目标记",
              exercises_md.exists() and "<!-- source-exercise:" in exercises_md.read_text(encoding="utf-8"))

        # 生成出来的题必须进审核队列，而不是直接给学生看
        rebuilt = client.post("/api/v1/review/queue/build", headers=REVIEWER,
                              params={"course_id": "e2e-course"})
        check("生成后可以建审核队列", rebuilt.status_code == 200, rebuilt.text[:100])
        items = client.get("/api/v1/review/queue?limit=100", headers=REVIEWER).json()["items"]
        generated = [item for item in items if item["exercise_id"].startswith("gen-")]
        check("生成的题进了审核队列", bool(generated), f"{len(generated)} 道")
        if generated:
            first = generated[0]
            check("生成的题是 draft 待审", first["state"] == "pending", str(first["state"]))
            check("生成的题标了溯源", first["provenance"] in ("textbook", "self-authored", "ai-generated"),
                  str(first["provenance"]))
            check("生成的题带精确页码", first["source_page"] is not None, str(first["source_page"]))
            check("生成的题挂在知识点上", bool(first["node_ids"]), str(first["node_ids"]))

        # 重跑同一个任务不能产生重复题
        before = len(generated)
        generation.run_job(job_id)
        items = client.get("/api/v1/review/queue?limit=100", headers=REVIEWER).json()["items"]
        after = len([item for item in items if item["exercise_id"].startswith("gen-")])
        check("重跑任务不重复出题", after == before, f"{before} → {after}")
        print("\n整本生成（自动切章 + 断点续跑）：")

        # 换一份字节不同的 PDF，才能拿到另一个 textbook_id（id 由 course+sha 决定）
        book_pdf = pdf_bytes.replace(b"Chapter 1", b"Chapter 2", 1) + b"% book\n"
        book_up = client.post(
            "/api/v1/textbooks", headers=ADMIN,
            params={"title": "整本测试教材", "course_id": "e2e-course", "authors": "测试作者"},
            content=book_pdf,
        )
        check("上传整本教材", book_up.status_code == 201, book_up.text[:120])
        book_id = book_up.json()["textbook_id"] if book_up.status_code == 201 else ""

        book_text = text_dir / "book.md"
        book_text.write_text(
            "<!-- page 1 -->\n第1章 实数\n实数系有上确界性质。\n\n"
            "<!-- page 2 -->\n第1章 实数\n1.1 上确界定义。\n\n"
            "<!-- page 3 -->\n第2章 极限\n2.1 数列极限。\n\n"
            "<!-- page 4 -->\n第2章 极限\n2.2 收敛数列性质。\n",
            encoding="utf-8",
        )
        db.query("UPDATE textbook SET text_path = %s WHERE id = %s", (str(book_text), book_id))

        preview = client.get(f"/api/v1/textbooks/{book_id}/chapters", headers=ADMIN)
        check("能预览切章结果", preview.status_code == 200, preview.text[:120])
        plan = preview.json() if preview.status_code == 200 else {}
        check("识别出两章", len(plan.get("items") or []) == 2,
              str([i["key"] for i in (plan.get("items") or [])]))
        check("切章依据是标题而不是页窗", plan.get("source") == "heading", str(plan.get("source")))
        check("章页码范围对得上",
              [i["page_from"] for i in (plan.get("items") or [])] == [1, 3],
              str([(i["page_from"], i["page_to"]) for i in (plan.get("items") or [])]))

        book_started = client.post(f"/api/v1/textbooks/{book_id}/generate-book",
                                   headers=ADMIN, json={})
        check("整本任务入队", book_started.status_code == 202, book_started.text[:140])
        book_job = (book_started.json() or {}).get("job", {}) if book_started.status_code == 202 else {}
        book_job_id = book_job.get("id")
        check("整本任务的类型是 generate-book", book_job.get("kind") == "generate-book",
              str(book_job.get("kind")))

        # 一次 process_once 展开成子任务，之后每跑一次处理一章
        generation.DEV_FAKE_MODEL = True
        steps = 0
        while steps < 12:
            done = generation.process_once()
            steps += 1
            if done is None:
                break

        parent = client.get(f"/api/v1/jobs/{book_job_id}", headers=REVIEWER).json()
        check("整本任务最终成功", parent["status"] == "succeeded", str(parent["status"]))
        check("整本任务进度到 100", parent["progress"] == 100, str(parent["progress"]))
        check("整本任务记录了章数", (parent.get("result") or {}).get("chapters") == 2,
              str((parent.get("result") or {}).get("chapters")))

        children = client.get(f"/api/v1/jobs/{book_job_id}/children", headers=REVIEWER).json()["items"]
        check("两章各一个子任务", len(children) == 2, str(len(children)))
        check("子任务都成功了", all(c["status"] == "succeeded" for c in children),
              str([c["status"] for c in children]))
        check("子任务挂着父任务", all(c["parent_id"] == book_job_id for c in children),
              str([c.get("parent_id") for c in children]))
        check("子任务带章号与标题",
              all(c["chapter_key"] and c["chapter_title"] for c in children),
              str([(c.get("chapter_key"), c.get("chapter_title")) for c in children]))
        check("每章都产出了题", all((c.get("result") or {}).get("exercises", 0) >= 1 for c in children),
              str([(c.get("result") or {}).get("exercises") for c in children]))

        # 同名两章不能互相覆盖：每章的资源包里都得是自己的题，且题号不为空
        pack_ids_seen = set()
        empty_ids = []
        for child in children:
            pack_path = Path((child.get("result") or {}).get("pack_dir") or "") / "习题.md"
            if not pack_path.exists():
                continue
            content = pack_path.read_text(encoding="utf-8")
            if "source-exercise:None" in content:
                empty_ids.append(child["chapter_key"])
            found = re.findall(r"source-exercise:([^\s-]+(?:-[^\s-]+)*) -->", content)
            pack_ids_seen.update(found)
        check("资源包里的题号已填好", not empty_ids, str(empty_ids))
        check("两章的题号不重复", len(pack_ids_seen) >= 2, str(sorted(pack_ids_seen)))
        generation.DEV_FAKE_MODEL = False

        # 重跑整本：不新建子任务，也不重复出题
        before_gen = len(client.get("/api/v1/review/queue?limit=200", headers=REVIEWER)
                         .json()["items"])
        generation.DEV_FAKE_MODEL = True
        generation.run_job(book_job_id)
        generation.DEV_FAKE_MODEL = False
        children_again = client.get(f"/api/v1/jobs/{book_job_id}/children",
                                    headers=REVIEWER).json()["items"]
        after_gen = len(client.get("/api/v1/review/queue?limit=200", headers=REVIEWER)
                        .json()["items"])
        check("重跑整本不新建子任务", len(children_again) == 2, str(len(children_again)))
        check("重跑整本不重复出题", after_gen == before_gen, f"{before_gen} → {after_gen}")

        # 断点续跑：把一章标成失败，重试后只补那一章
        first_child = children[0]["id"]
        db.query("UPDATE pipeline_job SET status = 'failed', error = '模拟失败' WHERE id = %s",
                 (first_child,))
        db.query("UPDATE pipeline_job SET status = 'failed' WHERE id = %s", (book_job_id,))
        retried = client.post(f"/api/v1/jobs/{book_job_id}/retry", headers=ADMIN)
        check("整本任务可以重试", retried.status_code == 202, retried.text[:120])
        requeued = client.get(f"/api/v1/jobs/{first_child}", headers=REVIEWER).json()
        check("失败的章重新排队", requeued["status"] == "queued", str(requeued["status"]))
        other = client.get(f"/api/v1/jobs/{children[1]['id']}", headers=REVIEWER).json()
        check("成功的章不会被重跑", other["status"] == "succeeded", str(other["status"]))

        generation.DEV_FAKE_MODEL = True
        while generation.process_once() is not None:
            pass
        generation.DEV_FAKE_MODEL = False
        healed = client.get(f"/api/v1/jobs/{book_job_id}", headers=REVIEWER).json()
        check("补跑后整本任务转为成功", healed["status"] == "succeeded", str(healed["status"]))

        print("\\n课程内容：导入 / 导出 / 整体更新：")

        def content_pack(exercise_ids):
            """造一个最小的课程资源包：一章、两个知识点、若干题。"""
            nodes = [
                {"id": "course-e2e", "title": "端到端课程", "level": 1,
                 "path": ["端到端课程"], "content_md": ""},
                {"id": "ch-01", "title": "第一章 实数", "level": 2, "parent_id": "course-e2e",
                 "path": ["端到端课程", "第一章 实数"], "content_md": ""},
                {"id": "kp-01", "title": "确界原理", "level": 3, "parent_id": "ch-01",
                 "path": ["端到端课程", "第一章 实数", "确界原理"],
                 "content_md": "非空有上界的数集必有上确界。", "tags": ["定理", "确界"]},
                {"id": "kp-02", "title": "阿基米德性", "level": 3, "parent_id": "ch-01",
                 "path": ["端到端课程", "第一章 实数", "阿基米德性"],
                 "content_md": "对任意正实数都有自然数大于它。", "tags": ["定理"]},
            ]
            exercises = [
                {"id": ex_id, "prompt_md": f"{ex_id}：叙述并证明。",
                 "answer_md": "证明略。", "difficulty": 3,
                 "source_raw": "教材《数学分析》上册，书页12，教材习题1.1-1",
                 "node_ids": ["kp-01"]}
                for ex_id in exercise_ids
            ]
            return {"course": {"title": "端到端课程", "description": "导入测试", "subject": "数学"},
                    "nodes": nodes, "exercises": exercises, "mode": "replace"}

        first = client.post("/api/v1/courses/e2e-content/content/import", headers=ADMIN,
                            json=content_pack(["ex-a", "ex-b", "ex-c"]))
        check("导入资源包成功", first.status_code == 200, first.text[:160])
        result = first.json()
        check("导入报告知识点数", result.get("nodes_upserted") == 4, str(result.get("nodes_upserted")))
        check("导入报告题目数", result.get("exercises_upserted") == 3, str(result.get("exercises_upserted")))
        check("首次导入没有退休任何行",
              result.get("nodes_retired") == 0 and result.get("exercises_retired") == 0,
              f"{result.get('nodes_retired')}/{result.get('exercises_retired')}")
        check("导入给了批次号", bool(result.get("batch")), str(result.get("batch")))

        read_back = client.get("/api/v1/courses/e2e-content/content", headers=REVIEWER)
        check("读回课程内容", read_back.status_code == 200, read_back.text[:120])
        content = read_back.json()
        check("读回知识点", len(content["nodes"]) == 4, str(len(content["nodes"])))
        check("读回题目", len(content["exercises"]) == 3, str(len(content["exercises"])))
        kept = {n["id"]: n for n in content["nodes"]}
        check("标签存下来了", kept["kp-01"]["tags"] == ["定理", "确界"], str(kept["kp-01"]["tags"]))
        check("章节推出来了", kept["kp-01"]["chapter_id"] == "ch-01", str(kept["kp-01"]["chapter_id"]))
        body = {e["id"]: e for e in content["exercises"]}["ex-a"]
        check("题目的知识点关联存下来了", body["node_ids"] == ["kp-01"], str(body["node_ids"]))
        check("溯源按规则解析（书页）", body["source_page"] == 12, str(body["source_page"]))
        check("溯源判为教材原题", body["provenance"] == "textbook", body["provenance"])

        exported = client.get("/api/v1/courses/e2e-content/content/export", headers=ADMIN)
        check("导出接口可用", exported.status_code == 200, exported.text[:120])
        bundle = exported.json()
        check("导出带知识点.md", '<a id="kp-01"></a>' in bundle["knowledge_md"], bundle["knowledge_md"][:40])
        check("导出带习题.md", "<!-- source-exercise:ex-a -->" in bundle["exercise_md"],
              bundle["exercise_md"][:40])
        check("导出的题目可再导入（含难度出处行）",
              "**难度：3/5；出处：**" in bundle["exercise_md"])
        node_ids = [node["id"] for node in bundle["nodes"]]
        check("导出的知识点.md 覆盖全部节点锚点",
              all(f'<a id="{node_id}"></a>' in bundle["knowledge_md"] for node_id in node_ids),
              str(node_ids))
        check("导出的习题.md 覆盖全部节点锚点",
              all(f'<a id="{node_id}"></a>' in bundle["exercise_md"] for node_id in node_ids))
        ex_ids = [item["id"] for item in bundle["exercises"]]
        check("导出的习题.md 覆盖全部题目 ID",
              all(f"<!-- source-exercise:{ex_id} -->" in bundle["exercise_md"] for ex_id in ex_ids),
              str(ex_ids))

        # 老师导出 Excel 改完再导入：这次少了 ex-c，它必须被标记 retired 而不是删掉
        second = client.post("/api/v1/courses/e2e-content/content/import", headers=ADMIN,
                             json=content_pack(["ex-a", "ex-b"]))
        check("第二次导入成功", second.status_code == 200, second.text[:160])
        changed = second.json()
        check("整体更新退休了文件里缺的题", changed.get("exercises_retired") == 1,
              str(changed.get("exercises_retired")))
        after = client.get("/api/v1/courses/e2e-content/content", headers=REVIEWER).json()
        check("审核视图里看不到退休的题",
              sorted(e["id"] for e in after["exercises"]) == ["ex-a", "ex-b"],
              str(sorted(e["id"] for e in after["exercises"])))
        with_retired = client.get("/api/v1/courses/e2e-content/content?include_retired=true",
                                  headers=REVIEWER).json()
        retired = {e["id"]: e for e in with_retired["exercises"]}
        check("退休的题还在库里（不物理删除）", retired.get("ex-c", {}).get("review_status") == "retired",
              str(retired.get("ex-c", {}).get("review_status")))

        # 在线改知识点
        patched = client.patch("/api/v1/courses/e2e-content/nodes/kp-02", headers=ADMIN,
                               json={"content_md": "改写后的正文", "tags": ["引理"]})
        check("在线改知识点成功", patched.status_code == 200, patched.text[:140])
        check("改完正文生效", patched.json()["content_md"] == "改写后的正文", str(patched.json().get("content_md")))
        check("改完标签生效", patched.json()["tags"] == ["引理"], str(patched.json().get("tags")))

        created_ex = client.post("/api/v1/courses/e2e-content/exercises", headers=ADMIN,
                                  json={"prompt_md": "新加的一道题", "answer_md": "略",
                                        "difficulty": 2, "node_ids": ["kp-02"]})
        check("在线加题成功", created_ex.status_code == 201, created_ex.text[:140])
        new_id = created_ex.json()["id"]
        check("新题自动编号", bool(new_id) and new_id.startswith("ex-"), str(new_id))

        retired_node = client.post("/api/v1/courses/e2e-content/nodes/kp-02/retire", headers=ADMIN)
        check("知识点可停用", retired_node.status_code == 200, retired_node.text[:120])
        hide = client.get("/api/v1/courses/e2e-content/content", headers=REVIEWER).json()
        check("停用后不在正常视图里", "kp-02" not in {n["id"] for n in hide["nodes"]},
              str(sorted(n["id"] for n in hide["nodes"])))

        student_import = client.post("/api/v1/courses/e2e-content/content/import",
                                     headers=STUDENT, json=content_pack(["ex-a"]))
        check("学生不能导入资源包", student_import.status_code == 403, str(student_import.status_code))

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
