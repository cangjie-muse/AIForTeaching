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
        anonymous_who = client.get("/api/v1/admin/whoami").json()
        check("没有身份时不再默认 admin（必须登录）",
              anonymous_who["can_edit"] is False and anonymous_who["can_view"] is False,
              str(anonymous_who))
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

            # 通过必须同时改"题目自身的可信等级"，否则总表、内容页徽标、
            # 可见范围的统计全都不动，人会觉得按钮没生效。
            decided = approved.json().get("exercise") or {}
            check("通过返回题目状态", decided.get("review_status") == "teacher-reviewed",
                  str(decided))
            check("通过后可信等级变成人工档", decided.get("tier") == "human", str(decided))
            check("通过不等于发布：学生仍看不到",
                  decided.get("visible_to_students") is False, str(decided))

            target_id = items[0]["exercise_id"]
            board = client.get("/api/v1/review/ledger?course_id=e2e-course",
                               headers=REVIEWER).json()
            board_row = next((row for row in board["items"] if row["id"] == target_id), {})
            check("总表里这道题的等级跟着变", board_row.get("tier") == "human",
                  str(board_row.get("tier")))
            check("总表的等级计数跟着变",
                  any(row["tier"] == "human" and row["n"] >= 1 for row in board["counts"]),
                  str(board["counts"]))
            content_now = client.get("/api/v1/courses/e2e-course/content",
                                     headers=REVIEWER).json()
            content_row = next((row for row in content_now["exercises"]
                                if row["id"] == target_id), {})
            check("课程内容页的徽标跟着变",
                  content_row.get("tier") == "human"
                  and content_row.get("review_status") == "teacher-reviewed",
                  str({k: content_row.get(k) for k in ("tier", "review_status")}))

            reverted = client.post(
                f"/api/v1/review/tasks/{task_id}/decision", headers=REVIEWER,
                json={"action": "request-changes", "note": "答案要再核一遍"},
            )
            reverted_ex = (reverted.json() or {}).get("exercise") or {}
            check("退回会撤销人工审核标记", reverted_ex.get("human_verified") is False,
                  str(reverted_ex))
            check("退回后等级回到未核", reverted_ex.get("tier") == "none", str(reverted_ex))
            check("退回后工作流回到草稿", reverted_ex.get("review_status") == "draft",
                  str(reverted_ex))

            # 恢复成已通过，后面的可见范围统计按"有一道人工已审"的假设走
            client.post(f"/api/v1/review/tasks/{task_id}/decision", headers=REVIEWER,
                        json={"action": "approve", "note": "复核后通过"})

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

        before = client.get("/api/v1/review/exercises/e2e-course/ex-2/revisions", headers=REVIEWER)
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

        after = client.get("/api/v1/review/exercises/e2e-course/ex-2/revisions", headers=REVIEWER)
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

        gone = client.get("/api/v1/review/exercises/e2e-course/no-such/revisions", headers=REVIEWER)
        check("不存在的题目查修订返回 404", gone.status_code == 404, str(gone.status_code))

        # 改完之后，审核队列里读到的应该是新题干
        refreshed = client.get("/api/v1/review/queue?limit=100", headers=REVIEWER)
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

        print("\n发布出口（学生端只看得见已审核发布的内容）：")

        publish_pack = {
            "course": {"title": "发布测试课程", "description": "发布链路", "subject": "数学"},
            "nodes": [
                {"id": "course-e2e-pub", "title": "发布测试课程", "level": 1,
                 "path": ["发布测试课程"], "content_md": ""},
                {"id": "ch-pub", "title": "第一章 极限", "level": 2, "parent_id": "course-e2e-pub",
                 "path": ["发布测试课程", "第一章 极限"], "content_md": ""},
                {"id": "kp-pub", "title": "数列极限", "level": 3, "parent_id": "ch-pub",
                 "path": ["发布测试课程", "第一章 极限", "数列极限"],
                 "content_md": "极限的定义。", "tags": ["定义"]},
            ],
            "exercises": [
                {"id": "pub-a", "prompt_md": "证明收敛数列有界。", "answer_md": "略",
                 "difficulty": 2, "source_raw": "教材《数学分析》上册，书页30，教材习题2.1-1",
                 "node_ids": ["kp-pub"]},
                {"id": "pub-b", "prompt_md": "举例说明极限不唯一。", "answer_md": "略",
                 "difficulty": 3, "source_raw": "自拟", "node_ids": ["kp-pub"]},
            ],
            "mode": "replace",
        }
        imported = client.post("/api/v1/courses/e2e-publish/content/import", headers=ADMIN,
                               json=publish_pack)
        check("发布测试课程导入成功", imported.status_code == 200, imported.text[:120])

        status = client.get("/api/v1/courses/e2e-publish/publish/status", headers=REVIEWER)
        check("发布前能看到自检信息", status.status_code == 200, status.text[:120])
        check("自检：还没有题被发布", status.json()["published"] == 0, str(status.json().get("published")))
        check("自检：待审题数", status.json()["hidden"] == 2, str(status.json().get("hidden")))

        early = client.post("/api/v1/courses/e2e-publish/publish", headers=ADMIN, json={})
        check("没有已发布题时拒绝发布", early.status_code == 400, early.text[:120])
        check("拒绝原因指向审核队列", "审核" in early.text, early.text[:90])

        public = client.get("/api/v1/catalog")
        check("公开目录不含未发布课程",
              "e2e-publish" not in {item["id"] for item in public.json()["items"]})

        queue = client.post("/api/v1/review/queue/build?course_id=e2e-publish", headers=REVIEWER)
        check("生成审核任务", queue.status_code == 200, queue.text[:120])
        tasks = client.get("/api/v1/review/queue?course_id=e2e-publish&limit=50",
                           headers=REVIEWER).json()["items"]
        target = next((task for task in tasks if task["exercise_id"] == "pub-a"), None)
        check("队列里有 pub-a 的任务", target is not None,
              str(sorted(task["exercise_id"] for task in tasks)))
        decided = client.post(f"/api/v1/review/tasks/{target['task_id']}/decision",
                              headers=REVIEWER, json={"action": "publish"})
        check("审核通过并发布 pub-a", decided.status_code == 200, decided.text[:120])

        status2 = client.get("/api/v1/courses/e2e-publish/publish/status", headers=REVIEWER).json()
        check("自检：已发布 1 题", status2["published"] == 1, str(status2["published"]))

        published = client.post("/api/v1/courses/e2e-publish/publish", headers=ADMIN,
                                json={"note": "端到端发布"})
        check("课程发布成功", published.status_code == 200, published.text[:140])
        check("发布报告题目数", published.json().get("exercises") == 1,
              str(published.json().get("exercises")))
        check("发布报告藏起来的题数", published.json().get("drafts_hidden") == 1,
              str(published.json().get("drafts_hidden")))

        catalog_now = client.get("/api/v1/catalog").json()["items"]
        entry = next((item for item in catalog_now if item["id"] == "e2e-publish"), None)
        check("公开目录出现已发布课程", entry is not None, str([i["id"] for i in catalog_now]))
        check("目录里只报已发布题目数", bool(entry) and entry["exercises"] == 1, str(entry))

        as_student = client.get("/api/v1/catalog", headers=STUDENT)
        check("学生身份能读目录", as_student.status_code == 200, str(as_student.status_code))
        detail = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT)
        check("学生能读已发布课程", detail.status_code == 200, detail.text[:120])
        payload = detail.json()
        ids = {item["id"] for item in payload["exercises"]}
        check("课程里只有已发布的题", ids == {"pub-a"}, str(sorted(ids)))
        check("未发布的题没有泄露", "pub-b" not in ids)
        check("节点树完整",
              {"course-e2e-pub", "ch-pub", "kp-pub"} <= {node["id"] for node in payload["nodes"]},
              str(sorted(node["id"] for node in payload["nodes"])))
        check("前端需要的字段都在",
              {"schemaVersion", "relationships", "stats", "files"} <= set(payload),
              str(sorted({"schemaVersion", "relationships", "stats", "files"} - set(payload))))
        anchor_node = next(node for node in payload["nodes"] if node["id"] == "kp-pub")
        check("锚点可用", anchor_node["exerciseAnchor"] == "kp-pub", anchor_node["exerciseAnchor"])
        check("章节归属传下来了", anchor_node["chapterId"] == "ch-pub", str(anchor_node["chapterId"]))

        # ---- 学生可见范围（验证等级闸）----
        # pub-a 走过人工审核并发布（tier=human）；pub-b 模拟历史数据：
        # 已发布、但没人逐题确认过（tier=none）。这正是这个开关要处理的现实——
        # 库里总会有"逃过人工"的已发布内容（旧数据、批量导入、自动发布）。
        print("\n学生可见范围：")
        client.post("/api/v1/courses/e2e-publish/publish", headers=ADMIN, json={})
        db.query(
            """UPDATE exercise
                  SET review_status = 'published',
                      human_verified_at = NULL, human_verified_by = NULL
                WHERE course_id = 'e2e-publish' AND id = 'pub-b'"""
        )
        policy = client.get("/api/v1/visibility", headers=REVIEWER)
        check("可见范围接口可读", policy.status_code == 200, policy.text[:120])
        check("默认是全部（升级不改变行为）",
              policy.json()["effective"]["exercise_tier"] == "all"
              and policy.json()["effective"]["qa_tier"] == "all"
              and policy.json()["effective"]["graph_tier"] == "all",
              str(policy.json().get("effective")))

        bad = client.put("/api/v1/visibility", headers=REVIEWER,
                         json={"exercise_tier": "strict", "qa_tier": "all"})
        check("非法档位被 400 拒绝", bad.status_code == 400, str(bad.status_code))

        human_only = client.put("/api/v1/visibility", headers=REVIEWER,
                                json={"exercise_tier": "human", "qa_tier": "human"})
        check("切到仅人工审核成功", human_only.status_code == 200, human_only.text[:120])
        check("这一档只放出人工确认过的 1 道",
              human_only.json()["impact"]["human"] == 1,
              str(human_only.json().get("impact")))

        hidden = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT)
        hidden_ids = {item["id"] for item in hidden.json()["exercises"]}
        check("仅人工审核时学生只看到 pub-a", hidden_ids == {"pub-a"}, str(sorted(hidden_ids)))
        check("未人工确认的 pub-b 被挡掉", "pub-b" not in hidden_ids)
        check("课程统计按可见范围重新计数",
              hidden.json()["stats"]["exercises"] == 1, str(hidden.json()["stats"]))
        entry = next(item for item in client.get("/api/v1/catalog").json()["items"]
                     if item["id"] == "e2e-publish")
        check("公开目录计数也跟着变", entry["exercises"] == 1, str(entry["exercises"]))
        check("知识点不因等级闸被隐藏",
              {"kp-pub"} <= {node["id"] for node in hidden.json()["nodes"]},
              str([node["id"] for node in hidden.json()["nodes"]]))
        check("学生不能改可见范围",
              client.put("/api/v1/visibility", headers=STUDENT,
                         json={"exercise_tier": "all", "qa_tier": "all"}).status_code == 403,
              "学生身份改动了全局策略")

        restored = client.put("/api/v1/visibility", headers=REVIEWER,
                              json={"exercise_tier": "all", "qa_tier": "all"})
        check("切回全部成功", restored.status_code == 200, restored.text[:80])
        wide = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT)
        check("切回全部后两道都能看到",
              {item["id"] for item in wide.json()["exercises"]} == {"pub-a", "pub-b"},
              str(sorted(item["id"] for item in wide.json()["exercises"])))

        # ---- 口径按课程分别设置 ----
        # 全局收紧到 human，再给这一门课单独放宽到 all：两套口径并存，互不影响。
        print("\n按课程分别设置：")
        client.put("/api/v1/visibility", headers=REVIEWER,
                   json={"exercise_tier": "human", "qa_tier": "human", "graph_tier": "all"})
        check("全局收紧后这门课只见人工确认的题",
              {item["id"] for item in client.get("/api/v1/catalog/e2e-publish",
                                                 headers=STUDENT).json()["exercises"]} == {"pub-a"},
              "全局口径没有生效")

        override = client.put("/api/v1/visibility?course_id=e2e-publish", headers=REVIEWER,
                              json={"exercise_tier": "all", "qa_tier": "all", "graph_tier": "all",
                                    "note": "试点课全放"})
        check("为单门课单独设置成功", override.status_code == 200, override.text[:120])
        check("课程覆盖来源标记为 course", override.json()["source"] == "course",
              str(override.json().get("source")))
        relaxed = {item["id"] for item in client.get("/api/v1/catalog/e2e-publish",
                                                     headers=STUDENT).json()["exercises"]}
        check("这门课放宽后能看到未人工确认的题", relaxed == {"pub-a", "pub-b"}, str(sorted(relaxed)))

        global_view = client.get("/api/v1/visibility", headers=REVIEWER).json()
        check("全局口径没有被课程覆盖污染",
              global_view["effective"]["exercise_tier"] == "human"
              and global_view["scope"] == "global",
              str(global_view.get("effective")))
        courses_list = {row["id"]: row for row in global_view.get("courses", [])}
        check("课程清单标出谁有单独设置",
              courses_list.get("e2e-publish", {}).get("has_override") is True,
              str(courses_list.get("e2e-publish")))
        check("没设置的课程标为跟随全局",
              courses_list.get("e2e-publish", {}).get("has_override") is True
              and any(row["has_override"] is False for row in global_view.get("courses", [])),
              "没有跟随全局的课程可对照")

        dropped = client.delete("/api/v1/visibility?course_id=e2e-publish", headers=REVIEWER)
        check("取消课程覆盖成功", dropped.status_code == 200, dropped.text[:120])
        back = {item["id"] for item in client.get("/api/v1/catalog/e2e-publish",
                                                 headers=STUDENT).json()["exercises"]}
        check("取消后回到跟随全局（只见 pub-a）", back == {"pub-a"}, str(sorted(back)))
        check("重复取消返回 404",
              client.delete("/api/v1/visibility?course_id=e2e-publish",
                            headers=REVIEWER).status_code == 404,
              "第二次取消没有报 404")
        check("学生不能给学生端口径开小灶",
              client.put("/api/v1/visibility?course_id=e2e-publish", headers=STUDENT,
                         json={"exercise_tier": "all", "qa_tier": "all",
                               "graph_tier": "all"}).status_code == 403,
              "学生身份改了课程口径")

        # ---- 知识图谱人工审核 ----
        # 教材切片正文不审；审的是"图谱挂得对不对"。只有人工一档。
        print("\n知识图谱人工审核：")
        before = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT).json()["stats"]["nodes"]
        check("图谱默认全放（与引入审核前一致）", before == 3, str(before))

        client.put("/api/v1/visibility", headers=REVIEWER,
                   json={"exercise_tier": "all", "qa_tier": "all", "graph_tier": "human"})
        strict_graph = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT).json()
        check("图谱切到仅人工审核后未审核节点被挡掉",
              strict_graph["stats"]["nodes"] == 0, str(strict_graph["stats"]))
        check("题目不因图谱口径被隐藏",
              {item["id"] for item in strict_graph["exercises"]} == {"pub-a", "pub-b"},
              str(sorted(item["id"] for item in strict_graph["exercises"])))

        graph_list = client.get("/api/v1/review/graph?course_id=e2e-publish", headers=REVIEWER)
        check("图谱审核列表可读", graph_list.status_code == 200, graph_list.text[:120])
        chapter_ids = {row["chapter_id"] for row in graph_list.json()["chapters"]}
        check("按章分组", {"ch-pub"} <= chapter_ids or "course-e2e-pub" in chapter_ids,
              str(sorted(chapter_ids)))
        graph_body = graph_list.json()
        node_rows = graph_body.get("nodes") or []
        check("同时给出每个节点（界面按章节树渲染）", len(node_rows) >= 2,
              str(len(node_rows)))
        check("课程根节点单列成 root（学生端的树要从根长出来）",
              (graph_body.get("root") or {}).get("id") == "course-e2e-pub",
              str(graph_body.get("root")))
        check("root 不出现在章节或节点清单里（避免一张同名卡片）",
              all(row["id"] != "course-e2e-pub" for row in node_rows)
              and all(row["chapter_id"] != "course-e2e-pub" for row in graph_body["chapters"]))
        node_by_id = {row["id"]: row for row in node_rows}
        check("节点带父子关系（能连成树）",
              all("parent_id" in row and "chapter_id" in row for row in node_rows))
        check("节点带章节归属",
              node_by_id.get("kp-pub", {}).get("chapter_id") == "ch-pub",
              str(node_by_id.get("kp-pub", {}).get("chapter_id")))
        check("节点带人工审核状态",
              all(isinstance(row["verified"], bool) for row in node_rows))
        check("不指定课程时不给节点明细（四千多个节点不白吐）",
              client.get("/api/v1/review/graph", headers=REVIEWER).json().get("nodes") == [])

        # 审核时发现"标题不对"要能就地改：退回管理员再改一轮，审核就断了
        renamed = client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub",
                               headers=REVIEWER, json={"title": "改过的知识点"})
        check("教师能就地改知识点标题", renamed.status_code == 200, renamed.text[:120])
        check("改标题返回新标题", renamed.json().get("title") == "改过的知识点",
              str(renamed.json().get("title")))
        check("改标题写了审计",
              "node.patch" in {row["action"] for row in db.query(
                  "SELECT action FROM audit_log ORDER BY id DESC LIMIT 20")})
        check("学生不能改知识点",
              client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub",
                           headers=STUDENT, json={"title": "学生改的"}).status_code == 403,
              "学生身份改了知识点")
        client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub",
                     headers=REVIEWER, json={"title": "知识点"})

        verified = client.post("/api/v1/review/graph/verify", headers=REVIEWER,
                               json={"course_id": "e2e-publish", "chapter_id": "ch-pub",
                                     "action": "verify"})
        check("整章人工通过成功", verified.status_code == 200, verified.text[:120])
        check("报告通过节点数", verified.json()["changed"] >= 2, str(verified.json().get("changed")))
        partial = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT).json()
        check("通过一章后该章节点可见",
              {"ch-pub", "kp-pub"} <= {node["id"] for node in partial["nodes"]},
              str([node["id"] for node in partial["nodes"]]))
        check("没审过的课程根节点仍被挡掉",
              "course-e2e-pub" not in {node["id"] for node in partial["nodes"]},
              str([node["id"] for node in partial["nodes"]]))
        check("统计跟着变", partial["stats"]["nodes"] == 2, str(partial["stats"]["nodes"]))

        whole = client.post("/api/v1/review/graph/verify", headers=REVIEWER,
                            json={"course_id": "e2e-publish",
                                  "node_ids": ["course-e2e-pub"], "action": "verify"})
        check("可以逐节点通过", whole.status_code == 200 and whole.json()["changed"] == 1,
              whole.text[:120])
        full = client.get("/api/v1/catalog/e2e-publish", headers=STUDENT).json()
        check("全部通过后整棵树可见",
              {node["id"] for node in full["nodes"]} == {"course-e2e-pub", "ch-pub", "kp-pub"},
              str([node["id"] for node in full["nodes"]]))
        check("关系边跟着节点一起出现",
              len(full["relationships"]) == 2, str(len(full["relationships"])))

        undone = client.post("/api/v1/review/graph/verify", headers=REVIEWER,
                             json={"course_id": "e2e-publish", "chapter_id": "ch-pub",
                                   "action": "unverify"})
        check("撤销整章通过", undone.status_code == 200 and undone.json()["changed"] >= 2,
              undone.text[:120])
        check("撤销后该章节点重新隐藏",
              "kp-pub" not in {node["id"] for node in client.get(
                  "/api/v1/catalog/e2e-publish", headers=STUDENT).json()["nodes"]},
              "撤销没有生效")
        check("学生不能审核知识图谱",
              client.post("/api/v1/review/graph/verify", headers=STUDENT,
                          json={"course_id": "e2e-publish", "chapter_id": "ch-pub",
                                "action": "verify"}).status_code == 403,
              "学生身份做了图谱审核")

        # ---- 知识点管理：挂题、解绑、删节点 ----
        # 这一页要能改树，而不是只看树。四个动作都会让挂着的题退回未核。
        print("\n知识点挂载与删除：")
        detail = client.get("/api/v1/review/nodes/e2e-publish/ch-pub", headers=REVIEWER)
        check("节点详情可读", detail.status_code == 200, detail.text[:120])
        detail_body = detail.json()
        check("详情给出子节点与挂题数",
              detail_body["counts"]["children"] == 1
              and isinstance(detail_body["counts"]["exercises"], int),
              str(detail_body.get("counts")))
        check("详情带父级与章", detail_body.get("chapter", {}).get("id") == "ch-pub",
              str(detail_body.get("chapter")))

        options = client.get("/api/v1/review/node-options?course_id=e2e-publish&q=知识",
                             headers=REVIEWER).json()
        check("可联想的知识点清单能按标题过滤", len(options.get("items", [])) >= 1,
              str(options.get("count")))
        ex_options = client.get("/api/v1/review/exercise-options?course_id=e2e-publish",
                                headers=REVIEWER).json()
        check("可挂的题目清单带挂载信息",
              {row["id"] for row in ex_options["items"]} == {"pub-a", "pub-b"},
              str([row["id"] for row in ex_options["items"]]))

        # 先把 pub-a 标成人工已审，才能看出"挂载一变就撤销"这条
        client.patch("/api/v1/review/exercises/e2e-publish/pub-a", headers=REVIEWER,
                     json={"human_verified": True})
        before_mount = client.get("/api/v1/review/exercises/e2e-publish/pub-a/verification",
                                  headers=REVIEWER).json()
        check("挂载前 pub-a 是人工已审", before_mount["tier"] == "human", str(before_mount["tier"]))

        temp = client.post("/api/v1/courses/e2e-publish/nodes", headers=ADMIN,
                           json={"id": "tmp-kp", "title": "临时知识点", "parent_id": "ch-pub"})
        check("建一个临时知识点", temp.status_code in (200, 201), temp.text[:120])
        mounted = client.post("/api/v1/review/nodes/e2e-publish/tmp-kp/exercises",
                              headers=REVIEWER, json={"exercise_ids": ["pub-a"]})
        check("可以按知识点挂题", mounted.status_code == 200
              and mounted.json()["attached"] == ["pub-a"], mounted.text[:140])
        check("挂题顺带撤销这道题的人工审核",
              mounted.json()["reset"]["cleared"] == 1, str(mounted.json().get("reset")))
        after_mount = client.get("/api/v1/review/nodes/e2e-publish/tmp-kp", headers=REVIEWER).json()
        row = after_mount["exercises"][0]
        check("挂上后等级回到未核", row["tier"] == "none", str(row["tier"]))
        check("已发布的题仍保持发布（学生不断供）", row["review_status"] == "published",
              str(row["review_status"]))

        detached = client.delete("/api/v1/review/nodes/e2e-publish/tmp-kp/exercises/pub-a",
                                 headers=REVIEWER)
        check("可以解绑", detached.status_code == 200 and detached.json()["detached"],
              detached.text[:120])
        check("解绑后这个节点上没有题",
              client.get("/api/v1/review/nodes/e2e-publish/tmp-kp", headers=REVIEWER)
              .json()["counts"]["exercises"] == 0)

        blocked = client.delete("/api/v1/review/nodes/e2e-publish/ch-pub", headers=REVIEWER)
        check("有子节点时先 409 报连带范围", blocked.status_code == 409, blocked.text[:120])
        check("409 里带节点数与题数",
              isinstance(blocked.json().get("detail"), dict)
              and blocked.json()["detail"].get("nodes", 0) >= 2,
              blocked.text[:160])
        check("没确认就没删（节点还在）",
              "ch-pub" in {row["id"] for row in client.get(
                  "/api/v1/review/graph?course_id=e2e-publish", headers=REVIEWER)
                  .json()["nodes"]})

        gone = client.delete("/api/v1/review/nodes/e2e-publish/tmp-kp?cascade=true",
                             headers=REVIEWER)
        check("确认后能真删", gone.status_code == 200 and gone.json()["deleted"] == 1,
              gone.text[:120])
        check("删掉的节点不再出现",
              "tmp-kp" not in {row["id"] for row in client.get(
                  "/api/v1/review/graph?course_id=e2e-publish", headers=REVIEWER)
                  .json()["nodes"]})

        check("学生不能看节点详情",
              client.get("/api/v1/review/nodes/e2e-publish/ch-pub",
                         headers=STUDENT).status_code == 403)
        check("学生不能删节点",
              client.delete("/api/v1/review/nodes/e2e-publish/kp-pub",
                            headers=STUDENT).status_code == 403)
        check("学生不能挂题",
              client.post("/api/v1/review/nodes/e2e-publish/kp-pub/exercises",
                          headers=STUDENT, json={"exercise_ids": ["pub-a"]}).status_code == 403)

        # 习题侧换挂载：整份 node_ids 覆盖，同样触发重置
        client.patch("/api/v1/review/exercises/e2e-publish/pub-a", headers=REVIEWER,
                     json={"node_ids": ["kp-pub"], "human_verified": True})
        switched = client.patch("/api/v1/review/exercises/e2e-publish/pub-a",
                                headers=REVIEWER, json={"node_ids": ["ch-pub"]})
        check("习题侧可以换挂载", switched.status_code == 200, switched.text[:120])
        check("换挂载也撤销人工审核", switched.json().get("reset", {}).get("cleared") == 1,
              str(switched.json().get("reset")))
        chapter_after = client.get("/api/v1/review/nodes/e2e-publish/ch-pub",
                                   headers=REVIEWER).json()
        check("换挂载后关系真的落在新节点上",
              [row["id"] for row in chapter_after["exercises"]] == ["pub-a"],
              str([row["id"] for row in chapter_after["exercises"]]))

        # 收尾：把 pub-a 挂回知识点并重新标人工已审，后面的用例按这个状态断言
        client.patch("/api/v1/review/exercises/e2e-publish/pub-a", headers=REVIEWER,
                     json={"node_ids": ["kp-pub"], "human_verified": True})

        # ---- 改父节点：把知识点挪到别的章，以及"改标题要重新审" ----
        # 建一个"节"当容器：create 接口只会建知识点（kind='knowledge'），
        # 真实导入数据里有章→节→知识点三层，所以这里直接落一条 fixture。
        db.execute(
            """INSERT INTO knowledge_node
                 (course_id, id, parent_id, title, level, kind, path, content_md)
               VALUES ('e2e-publish', 'sec-pub', 'ch-pub', '第一节', 3, 'container',
                       ARRAY['测试课程', '测试章', '第一节'], '')"""
        )
        print("\n节点移动：")
        ch_detail = client.get("/api/v1/review/nodes/e2e-publish/ch-pub", headers=REVIEWER).json()
        targets = [row["id"] for row in ch_detail.get("move_targets", [])]
        check("详情给出可移动目标", "sec-pub" not in targets and targets == ["course-e2e-pub"],
              str(targets))
        check("可移动目标里不含自己这棵子树",
              "ch-pub" not in targets and "kp-pub" not in targets, str(targets))

        client.post("/api/v1/review/graph/verify", headers=REVIEWER,
                    json={"course_id": "e2e-publish", "node_ids": ["kp-pub"], "action": "verify"})
        moved = client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub", headers=REVIEWER,
                             json={"parent_id": "sec-pub"})
        check("能把知识点挪到别的节下面", moved.status_code == 200, moved.text[:160])
        check("移动把层级算对了", moved.json().get("level") == 4, str(moved.json().get("level")))
        check("移动重算了 path",
              moved.json().get("path") == ["测试课程", "测试章", "第一节", "知识点"],
              str(moved.json().get("path")))
        check("移动会撤销这个节点的审核（位置变了）",
              moved.json().get("move", {}).get("nodes") == 1
              and db.query_one(
                  "SELECT human_verified_at FROM knowledge_node "
                  "WHERE course_id = 'e2e-publish' AND id = 'kp-pub'")["human_verified_at"] is None,
              "移完还是已通过")
        check("移动写了审计",
              "node.move" in {row["action"] for row in db.query(
                  "SELECT action FROM audit_log ORDER BY id DESC LIMIT 10")})

        cycle = client.patch("/api/v1/courses/e2e-publish/nodes/ch-pub", headers=REVIEWER,
                             json={"parent_id": "sec-pub"})
        check("移到自己的子树里被 400 拦住（不能成环）", cycle.status_code == 400,
              str(cycle.status_code))
        into_leaf = client.patch("/api/v1/courses/e2e-publish/nodes/ch-pub", headers=REVIEWER,
                                 json={"parent_id": "kp-pub"})
        check("不能挂到知识点底下", into_leaf.status_code == 400, str(into_leaf.status_code))
        ghost_parent = client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub",
                                    headers=REVIEWER, json={"parent_id": "nope"})
        check("目标不存在时 404", ghost_parent.status_code == 404, str(ghost_parent.status_code))
        blank = client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub", headers=REVIEWER,
                             json={"parent_id": "  "})
        check("parent_id 传空串被 400 拒绝（想不动就别传）", blank.status_code == 400,
              str(blank.status_code))

        client.post("/api/v1/review/graph/verify", headers=REVIEWER,
                    json={"course_id": "e2e-publish", "node_ids": ["kp-pub"], "action": "verify"})
        renamed_free = client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub",
                                    headers=REVIEWER, json={"title": "知识点（改名）"})
        check("改标题会连这个节点的审核一起撤销",
              renamed_free.json().get("title_reverified_reset") is True
              and db.query_one(
                  "SELECT human_verified_at FROM knowledge_node "
                  "WHERE course_id = 'e2e-publish' AND id = 'kp-pub'")["human_verified_at"] is None,
              "改完标题还显示已通过")
        check("只改 review_status 不清审核（停用/启用不算内容变化）",
              client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub", headers=REVIEWER,
                           json={"review_status": "draft"}).status_code == 200)

        # 收尾：结构搬回去，后面的用例按原来的树断言。
        # 搬回来本身也是一次移动，会把挂在 kp-pub 上的 pub-a 退回未核（这正是要的行为），
        # 所以这里按"收尾"的规矩把 pub-a 重新标成人工已审，后面单题记录的断言才有基准。
        client.patch("/api/v1/courses/e2e-publish/nodes/kp-pub", headers=REVIEWER,
                     json={"parent_id": "ch-pub", "title": "知识点"})
        client.patch("/api/v1/review/exercises/e2e-publish/pub-a", headers=REVIEWER,
                     json={"human_verified": True})
        db.execute("DELETE FROM knowledge_node WHERE course_id = 'e2e-publish' "
                   "AND id = 'sec-pub'")
        restored = client.get("/api/v1/review/nodes/e2e-publish/kp-pub", headers=REVIEWER).json()
        check("搬回原处后层级恢复",
              restored["node"]["level"] == 3, str(restored["node"]["level"]))

        # ---- 单道题"标注过什么" ----
        # 已发布的题不在审核队列里，所以要能按题号直接查：等级、谁审的、审的哪版、之后改没改。
        print("\n单题审核记录：")
        record = client.get("/api/v1/review/exercises/e2e-publish/pub-a/verification",
                            headers=REVIEWER)
        check("能查一道已发布题的审核记录", record.status_code == 200, record.text[:140])
        body = record.json()
        check("记录里带可信等级", body["tier"] in {"human", "strongest"}, str(body.get("tier")))
        check("记录里带人工审核时间与审核人",
              bool(body["human_verified_at"]) and bool(body["human_verified_by"]),
              f"{body.get('human_verified_at')} / {body.get('human_verified_by')}")
        check("记录里带内容版本", body["content_version"] >= 1, str(body.get("content_version")))
        check("刚审完不该报「审核后被改过」", body["edited_after_review"] is False,
              str(body.get("edited_after_review")))
        check("记录里带最近一次审核任务状态",
              (body.get("task") or {}).get("state") == "published",
              str(body.get("task")))
        check("Lean 未接入要如实标出", body["lean_available"] is False,
              str(body.get("lean_available")))
        check("没有 Lean 证明时列表为空", body["proofs"] == [], str(body.get("proofs")))

        unverified = client.get("/api/v1/review/exercises/e2e-publish/pub-b/verification",
                                headers=REVIEWER).json()
        check("未人工确认的题等级为 none", unverified["tier"] == "none", str(unverified["tier"]))
        check("未确认时人工字段为空",
              unverified["human_verified_at"] is None and unverified["human_verified_by"] is None,
              str(unverified.get("human_verified_at")))
        check("未确认时不误报「审核后被改过」", unverified["edited_after_review"] is False,
              str(unverified.get("edited_after_review")))

        # 改一次内容，再查：应当提示"审核通过后又被改过"
        client.patch("/api/v1/review/exercises/e2e-publish/pub-a", headers=REVIEWER,
                     json={"difficulty": 4, "change_note": "审后调整难度"})
        edited = client.get("/api/v1/review/exercises/e2e-publish/pub-a/verification",
                            headers=REVIEWER).json()
        check("审核后改过内容会被标出来", edited["edited_after_review"] is True,
              str(edited.get("edited_after_review")))
        check("改过之后内容版本加一", edited["content_version"] > body["content_version"],
              f"{body['content_version']} → {edited['content_version']}")

        check("学生不能读审核记录",
              client.get("/api/v1/review/exercises/e2e-publish/pub-a/verification",
                         headers=STUDENT).status_code == 403,
              "学生看到了审核记录")
        check("不存在的题返回 404",
              client.get("/api/v1/review/exercises/e2e-publish/nope/verification",
                         headers=REVIEWER).status_code == 404,
              "不存在的题没有报 404")

        content_view = client.get("/api/v1/courses/e2e-publish/content", headers=REVIEWER)
        check("内容接口带上题目的可信等级",
              all("tier" in row for row in content_view.json()["exercises"]),
              str([row.get("tier") for row in content_view.json()["exercises"]]))
        check("内容接口带上知识点的人工审核状态",
              all("human_verified_at" in row for row in content_view.json()["nodes"]),
              "节点缺少 human_verified_at")

        # ---- 课程审核总表（一门课全部题目，不只待审的） ----
        print("\n课程审核总表：")
        board = client.get("/api/v1/review/ledger?course_id=e2e-publish", headers=REVIEWER)
        check("能列出这门课全部题目", board.status_code == 200, board.text[:140])
        table = board.json()
        check("总表包含已发布与未审核的题", table["total"] >= 2, str(table["total"]))
        ids = {row["id"] for row in table["items"]}
        check("总表里有已发布的题（不在队列里也要出现）", "pub-a" in ids, str(sorted(ids)))
        check("总表里仍有人工确认的题", "pub-b" in ids, str(sorted(ids)))
        check("未审核的题排在人工确认之前",
              [row["tier"] for row in table["items"]][:2] == ["none", "human"]
              or [row["tier"] for row in table["items"]][0] == "none",
              str([row["tier"] for row in table["items"]]))
        check("总表带每档数量", {row["tier"] for row in table["counts"]} >= {"none", "human"},
              str(table["counts"]))
        check("总表带题干摘要", all("prompt" in row for row in table["items"]), "缺 prompt")

        only_human = client.get("/api/v1/review/ledger?course_id=e2e-publish&tier=human",
                                headers=REVIEWER).json()
        check("按等级筛选只留人工确认的题",
              only_human["items"] and all(row["tier"] == "human" for row in only_human["items"]),
              str([row["tier"] for row in only_human["items"]]))
        check("筛选后数量只算这一类", only_human["total"] == len(only_human["items"]),
              f"{only_human['total']} vs {len(only_human['items'])}")
        check("筛选不影响各档总计数", only_human["counts"] == table["counts"],
              str(only_human["counts"]))

        search = client.get("/api/v1/review/ledger?course_id=e2e-publish&question=pub-a",
                            headers=REVIEWER).json()
        check("按题号搜索命中一条", [row["id"] for row in search["items"]] == ["pub-a"],
              str([row["id"] for row in search["items"]]))
        check("搜索不到时返回空表", client.get(
            "/api/v1/review/ledger?course_id=e2e-publish&question=zzz", headers=REVIEWER
        ).json()["total"] == 0, "没搜到却还有条目")
        check("课程不存在时总表为空", client.get(
            "/api/v1/review/ledger?course_id=nope", headers=REVIEWER).json()["total"] == 0,
            "不存在的课程还有题")
        check("非法等级 400", client.get(
            "/api/v1/review/ledger?tier=weird", headers=REVIEWER).status_code == 400, "没有拒绝")
        check("学生不能看审核总表", client.get(
            "/api/v1/review/ledger?course_id=e2e-publish", headers=STUDENT).status_code == 403,
            "学生看到了总表")
        check("分页限制生效", len(client.get(
            "/api/v1/review/ledger?course_id=e2e-publish&limit=1", headers=REVIEWER
        ).json()["items"]) == 1, "limit 没生效")

        # 收尾：把口径恢复成全放，后面的撤回发布测试按默认口径走
        client.put("/api/v1/visibility", headers=REVIEWER,
                   json={"exercise_tier": "all", "qa_tier": "all", "graph_tier": "all"})

        unpub = client.post("/api/v1/courses/e2e-publish/unpublish", headers=ADMIN, json={})
        check("撤回发布成功", unpub.status_code == 200, unpub.text[:120])
        check("撤回后学生读不到",
              client.get("/api/v1/catalog/e2e-publish", headers=STUDENT).status_code == 404)
        check("撤回后目录里消失",
              "e2e-publish" not in {item["id"] for item in client.get("/api/v1/catalog").json()["items"]})

        student_publish = client.post("/api/v1/courses/e2e-publish/publish", headers=STUDENT, json={})
        check("学生不能发布课程", student_publish.status_code == 403, str(student_publish.status_code))

        # ------------------------------------------------------------------
        # 登录与会话（账号密码），以及三个角色的权限边界
        #
        # 全部用同一个 client：cookie 会一直带着，所以每换一个身份就
        # 先清 cookie（或退出），否则后面的请求会以"上一个人"的身份发出去。
        # ------------------------------------------------------------------
        print("\n账号密码登录：")
        client.cookies.clear()
        me0 = client.get("/api/v1/auth/me")
        check("未登录时 me 返回 authenticated=false",
              me0.status_code == 200 and me0.json()["authenticated"] is False, me0.text[:120])
        providers = client.get("/api/v1/auth/providers").json()
        check("登录方式里账号密码可用", providers["password"]["available"] is True)
        check("统一认证明确标为未接入", providers["cas"]["available"] is False,
              str(providers["cas"].get("hint"))[:60])
        check("登录方式带会话时长", providers["session_hours"] >= 1, str(providers["session_hours"]))

        wrong = client.post("/api/v1/auth/login",
                            json={"username": "nobody", "password": "whatever123"})
        check("不存在的账号 401", wrong.status_code == 401, wrong.text[:100])
        check("不泄露账号是否存在", "账号或密码" in wrong.json()["detail"], wrong.json()["detail"])

        created = client.post("/api/v1/auth/accounts", headers=ADMIN, json={
            "username": "e2e-teacher", "display_name": "端到端教师",
            "role": "teacher", "password": "e2e-teacher-2026", "must_change_password": False,
        })
        check("管理员能建教师账号", created.status_code == 200, created.text[:160])
        check("新账号默认有平台密码", created.json()["has_password"] is True)
        check("重名账号 409",
              client.post("/api/v1/auth/accounts", headers=ADMIN, json={
                  "username": "e2e-teacher", "display_name": "重复", "role": "teacher",
                  "password": "e2e-teacher-2026",
              }).status_code == 409, "重名没有拒绝")
        check("弱密码 400",
              client.post("/api/v1/auth/accounts", headers=ADMIN, json={
                  "username": "e2e-weak", "display_name": "弱口令", "role": "student",
                  "password": "123",
              }).status_code == 400, "弱密码被接受")
        check("非法角色 400",
              client.post("/api/v1/auth/accounts", headers=ADMIN, json={
                  "username": "e2e-role", "display_name": "错角色", "role": "superuser",
                  "password": "e2e-role-2026",
              }).status_code == 400, "非法角色被接受")

        # 教师：用 cookie 登录后不再发任何身份请求头
        client.cookies.clear()
        signed = client.post("/api/v1/auth/login",
                             json={"username": "e2e-teacher", "password": "e2e-teacher-2026"})
        check("正确密码能登录", signed.status_code == 200, signed.text[:160])
        check("登录响应带角色", signed.json()["actor"]["role"] == "teacher",
              str(signed.json()["actor"]))
        check("会话 cookie 是 httpOnly", "httponly" in signed.headers.get("set-cookie", "").lower(),
              signed.headers.get("set-cookie", "")[:80])
        me1 = client.get("/api/v1/auth/me").json()
        check("登录后 me 认得这个人",
              me1["authenticated"] and me1["actor"]["display_name"] == "端到端教师",
              str(me1.get("actor")))
        check("教师带 cookie 就能读审核总表（无需请求头）",
              client.get("/api/v1/review/ledger?course_id=e2e-publish").status_code == 200,
              "cookie 没生效")
        check("教师能审核（reviewer 权限由 teacher 覆盖）",
              client.get("/api/v1/review/graph").status_code == 200, "教师审不了图谱")
        check("教师不能建账号 403",
              client.post("/api/v1/auth/accounts", json={
                  "username": "e2e-x", "display_name": "x", "role": "student",
                  "password": "e2e-x-2026",
              }).status_code == 403, "教师也能建账号")
        check("教师不能把别人提成管理员 403",
              client.patch("/api/v1/auth/accounts/" + created.json()["id"],
                           json={"role": "admin"}).status_code == 403, "教师也能提权")

        # 学生：能看课程，不能审内容，也不能改口径
        client.post("/api/v1/auth/logout")
        client.cookies.clear()
        client.post("/api/v1/auth/accounts", headers=ADMIN, json={
            "username": "e2e-student", "display_name": "端到端学生",
            "role": "student", "password": "e2e-student-2026",
        })
        check("学生登录成功",
              client.post("/api/v1/auth/login", json={
                  "username": "e2e-student", "password": "e2e-student-2026",
              }).status_code == 200, "学生登录失败")
        check("学生能看课程目录",
              client.get("/api/v1/catalog").status_code == 200, "目录都看不了")
        check("学生不能读审核队列 403",
              client.get("/api/v1/review/queue").status_code == 403, "学生看到了审核队列")
        check("学生不能改可见范围 403",
              client.put("/api/v1/visibility",
                         json={"exercise_tier": "all"}).status_code == 403, "学生改了可见范围")
        check("学生不能读账号列表 403",
              client.get("/api/v1/auth/accounts").status_code == 403, "学生看到了账号列表")

        # 退出：会话失效，再访问受保护接口是 401（不是 403）
        check("退出成功", client.post("/api/v1/auth/logout").status_code == 200)
        check("退出后 me 变匿名",
              client.get("/api/v1/auth/me").json()["authenticated"] is False)
        check("退出后受保护接口 401（可据此跳登录）",
              client.get("/api/v1/review/ledger").status_code == 401,
              "退出后还是 403，前端分不清该不该弹登录")
        client.cookies.clear()

        # 改密：旧密码失效、新密码可登录
        client.post("/api/v1/auth/login",
                    json={"username": "e2e-teacher", "password": "e2e-teacher-2026"})
        check("改密前旧密码可登录",
              client.get("/api/v1/auth/me").json()["authenticated"] is True)
        check("当前密码错时拒绝改密",
              client.post("/api/v1/auth/password", json={
                  "current_password": "wrong-password", "new_password": "e2e-teacher-2027",
              }).status_code == 400, "错的当前密码被接受")
        check("新密码太短被拒",
              client.post("/api/v1/auth/password", json={
                  "current_password": "e2e-teacher-2026", "new_password": "short",
              }).status_code in {400, 422}, "弱新密码被接受")
        check("改密成功",
              client.post("/api/v1/auth/password", json={
                  "current_password": "e2e-teacher-2026", "new_password": "e2e-teacher-2027",
              }).status_code == 200, "改密失败")
        check("改密后当前会话仍可用",
              client.get("/api/v1/auth/me").json()["authenticated"] is True)
        client.post("/api/v1/auth/logout")
        client.cookies.clear()
        check("旧密码不能再登录",
              client.post("/api/v1/auth/login", json={
                  "username": "e2e-teacher", "password": "e2e-teacher-2026",
              }).status_code == 401, "旧密码还能登录")
        check("新密码能登录",
              client.post("/api/v1/auth/login", json={
                  "username": "e2e-teacher", "password": "e2e-teacher-2027",
              }).status_code == 200, "新密码登录失败")
        client.post("/api/v1/auth/logout")
        client.cookies.clear()

        # 连续失败锁定
        client.post("/api/v1/auth/accounts", headers=ADMIN, json={
            "username": "e2e-lock", "display_name": "锁定测试", "role": "student",
            "password": "e2e-lock-2026",
        })
        codes = []
        for _ in range(5):
            codes.append(client.post("/api/v1/auth/login", json={
                "username": "e2e-lock", "password": "definitely-wrong",
            }).status_code)
        check("连续失败计数", codes[:4] == [401, 401, 401, 401], str(codes))
        check("第五次失败后锁定", codes[4] == 423, str(codes))
        check("锁定期间正确密码也进不去",
              client.post("/api/v1/auth/login", json={
                  "username": "e2e-lock", "password": "e2e-lock-2026",
              }).status_code == 423, "锁定了还能登录")
        client.cookies.clear()

        # 管理员账号管理
        listing = client.get("/api/v1/auth/accounts", headers=ADMIN).json()
        usernames = [row["username"] for row in listing["items"]]
        check("账号列表含刚建的三个", {"e2e-teacher", "e2e-student", "e2e-lock"} <= set(usernames),
              str(usernames[:6]))
        check("账号列表带角色统计", listing["counts"].get("teacher", 0) >= 1,
              str(listing["counts"]))
        check("账号列表不返回密码哈希",
              all("password_hash" not in row for row in listing["items"]), "泄漏了哈希")
        check("按角色筛选",
              all(row["role"] == "student" for row in
                  client.get("/api/v1/auth/accounts?role=student",
                             headers=ADMIN).json()["items"]),
              "筛角色没生效")
        lock_id = [row["id"] for row in listing["items"] if row["username"] == "e2e-lock"][0]
        check("停用账号成功",
              client.patch("/api/v1/auth/accounts/" + lock_id, headers=ADMIN,
                           json={"disabled": True}).status_code == 200, "停用失败")
        check("停用后连锁定中的账号也进不来",
              client.post("/api/v1/auth/login", json={
                  "username": "e2e-lock", "password": "e2e-lock-2026",
              }).status_code in {403, 423}, "停用了还能登录")
        # 库里的管理员账号是 0 个（上面的 admin 身份来自请求头），
        # 先建一个真的，才能验证"最后一个管理员不能被降级"。
        made_admin = client.post("/api/v1/auth/accounts", headers=ADMIN, json={
            "username": "e2e-admin", "display_name": "端到端管理员",
            "role": "admin", "password": "e2e-admin-2026",
        })
        check("管理员能建管理员账号", made_admin.status_code == 200, made_admin.text[:160])
        admin_items = client.get("/api/v1/auth/accounts?role=admin", headers=ADMIN).json()["items"]
        check("管理员账号能被列出来", len(admin_items) == 1, str(len(admin_items)))
        check("最后一个管理员不能被降级",
              client.patch("/api/v1/auth/accounts/" + admin_items[0]["id"], headers=ADMIN,
                           json={"role": "teacher"}).status_code == 400,
              "把最后一个管理员降级了")
        check("最后一个管理员不能被停用",
              client.patch("/api/v1/auth/accounts/" + admin_items[0]["id"], headers=ADMIN,
                           json={"disabled": True}).status_code == 400,
              "把最后一个管理员停用了")

        # 统一认证：留了位置，但明确说没接
        check("CAS 登录入口明确报未接入",
              client.get("/api/v1/auth/cas/login", follow_redirects=False).status_code == 501,
              "CAS 入口没有明确拒绝")
        check("CAS 回调明确报未接入",
              client.get("/api/v1/auth/cas/callback?ticket=ST-1").status_code == 501,
              "回调没有拒绝")

        # ------------------------------------------------------------------
        # 班级与选课：管理员发账号，教师建班加人，学生没有这些权限
        # ------------------------------------------------------------------
        print("\n班级与选课：")
        student_row = [row for row in listing["items"] if row["username"] == "e2e-student"][0]
        teacher_row = [row for row in listing["items"] if row["username"] == "e2e-teacher"][0]
        teacher_headers = {"X-Actor": teacher_row["id"], "X-Roles": "teacher"}
        inbox = {"course_id": "e2e-course", "name": "E2E-01班",
                 "academic_year": "2026-2027", "term_kind": "秋"}

        check("学生不能建班", client.post("/api/v1/classes", headers=STUDENT, json=inbox)
              .status_code == 403, "学生把班建起来了")
        check("学生不能看班级列表",
              client.get("/api/v1/classes", headers=STUDENT).status_code == 403, "泄漏了班级")
        check("学生不能看学生清单",
              client.get("/api/v1/students", headers=STUDENT).status_code == 403, "泄漏了名单")

        made = client.post("/api/v1/classes", headers=teacher_headers, json=inbox)
        check("教师能建班", made.status_code == 200, made.text[:200])
        made_class = made.json() if made.status_code == 200 else {}
        check("建班返回课程名", made_class.get("course_title") == "端到端测试课",
              str(made_class.get("course_title")))
        check("学期按学校口径显示", made_class.get("term") == "2026年秋季学期",
              str(made_class.get("term")))
        check("学期同时返回结构字段",
              made_class.get("academic_year") == "2026-2027"
              and made_class.get("term_kind") == "秋",
              str((made_class.get("academic_year"), made_class.get("term_kind"))))
        check("任课教师默认是建班的人", made_class.get("teacher_name") == "端到端教师",
              str(made_class.get("teacher_name")))
        check("新班人数为 0", made_class.get("student_count") == 0)

        dup_class = client.post("/api/v1/classes", headers=teacher_headers, json=inbox)
        check("同课程同学期重名被 409 拒绝", dup_class.status_code == 409, str(dup_class.status_code))
        bad_year = client.post("/api/v1/classes", headers=teacher_headers,
                               json={**inbox, "name": "E2E-02班", "academic_year": "2026-2028"})
        check("学年两个年份不连续被 400 拒绝", bad_year.status_code == 400, str(bad_year.status_code))
        bad_kind = client.post("/api/v1/classes", headers=teacher_headers,
                               json={**inbox, "name": "E2E-02班", "term_kind": "第一学期"})
        check("学期不在秋/春/夏里被 400 拒绝", bad_kind.status_code == 400, str(bad_kind.status_code))
        free_text = client.post("/api/v1/classes", headers=teacher_headers,
                                json={**inbox, "name": "E2E-02班",
                                      "academic_year": "2026年", "term_kind": "秋"})
        check("把 2026年 当学年传被 400 拒绝", free_text.status_code == 400,
              str(free_text.status_code))
        no_course = client.post("/api/v1/classes", headers=teacher_headers,
                                json={**inbox, "course_id": "nope", "name": "E2E-03班"})
        check("课程不存在被 404 拒绝", no_course.status_code == 404, str(no_course.status_code))

        class_id = made_class.get("id", "")
        pupils = client.get("/api/v1/students", headers=teacher_headers).json()["items"]
        check("学生清单只有学生", all(row["id"] != teacher_row["id"] for row in pupils),
              "把教师当学生返回了")
        check("学生清单能按学号搜到人",
              any(row["username"] == "e2e-student" for row in pupils), str(len(pupils)))

        added = client.post(f"/api/v1/classes/{class_id}/students", headers=teacher_headers,
                            json={"student_ids": [student_row["id"]]})
        check("教师能把学生加进班", added.status_code == 200 and added.json()["added"] == 1,
              added.text[:160])
        again = client.post(f"/api/v1/classes/{class_id}/students", headers=teacher_headers,
                            json={"student_ids": [student_row["id"]]})
        check("重复加入不报错、只记一次",
              again.status_code == 200 and again.json()["added"] == 0
              and again.json()["skipped"] == 1 and again.json()["total"] == 1,
              again.text[:160])
        wrong_role = client.post(f"/api/v1/classes/{class_id}/students", headers=teacher_headers,
                                 json={"student_ids": [teacher_row["id"]]})
        check("教师账号不能塞进学生名单", wrong_role.status_code == 400,
              str(wrong_role.status_code))
        ghost = client.post(f"/api/v1/classes/{class_id}/students", headers=teacher_headers,
                            json={"student_ids": ["acc-does-not-exist"]})
        check("不存在的账号被 400 拒绝", ghost.status_code == 400, str(ghost.status_code))

        roster = client.get(f"/api/v1/classes/{class_id}/students", headers=teacher_headers).json()
        check("名单里能看到学生", roster["total"] == 1
              and roster["items"][0]["username"] == "e2e-student", str(roster["total"]))
        check("名单记下是谁加进来的",
              roster["items"][0]["added_by"] == "端到端教师",
              str(roster["items"][0]["added_by"]))

        spring = client.post("/api/v1/classes", headers=teacher_headers,
                             json={**inbox, "name": "E2E-春季班", "term_kind": "春"})
        check("同一学年能另建春季班", spring.status_code == 200, spring.text[:160])
        check("春季班显示成次年春季学期",
              spring.status_code == 200 and spring.json()["term"] == "2027年春季学期",
              str(spring.json().get("term") if spring.status_code == 200 else ""))
        only_autumn = client.get("/api/v1/classes", headers=teacher_headers,
                                 params={"course_id": "e2e-course", "term_kind": "秋"}).json()
        check("能按学期筛班级", only_autumn["total"] == 1
              and only_autumn["items"][0]["term_kind"] == "秋", str(only_autumn["total"]))
        if spring.status_code == 200:
            client.delete("/api/v1/classes/" + spring.json()["id"], headers=teacher_headers)

        listed = client.get("/api/v1/classes?course_id=e2e-course",
                            headers=teacher_headers).json()
        check("按课程筛选能查到这门课", listed["total"] == 1
              and listed["items"][0]["student_count"] == 1, str(listed["total"]))
        check("班级列表统计选课人次", listed["students"] == 1, str(listed["students"]))

        renamed = client.patch(f"/api/v1/classes/{class_id}", headers=teacher_headers,
                               json={"name": "E2E-01班（改）"})
        check("班级能改名", renamed.status_code == 200
              and renamed.json()["name"] == "E2E-01班（改）", renamed.text[:160])

        removed = client.delete(f"/api/v1/classes/{class_id}/students/{student_row['id']}",
                                headers=teacher_headers)
        check("教师能把学生移出名单", removed.status_code == 200
              and removed.json()["total"] == 0, removed.text[:160])
        check("重复移出被 404 拒绝",
              client.delete(f"/api/v1/classes/{class_id}/students/{student_row['id']}",
                            headers=teacher_headers).status_code == 404, "重复移出没报错")

        dropped = client.delete(f"/api/v1/classes/{class_id}", headers=teacher_headers)
        check("班级能删除", dropped.status_code == 200, dropped.text[:160])
        check("删班后列表为空",
              client.get("/api/v1/classes", headers=teacher_headers).json()["total"] == 0,
              "删了还在")

        actions = {row["action"] for row in db.query(
            "SELECT action FROM audit_log WHERE entity = 'teaching_class'")}
        check("建班/加人/移出/删班都写了审计",
              {"class.create", "class.update", "enrollment.add",
               "enrollment.remove", "class.delete"} <= actions, str(sorted(actions)))
        check("班级不影响学生看到的课程（本阶段）",
              client.get("/api/v1/catalog", headers=STUDENT).status_code == 200,
              "目录被班级连带改动了")

        # ---- 批量导入：账号 / 班级 / 名单 ----
        # Excel 由前端解析成 JSON 行；这里直接打接口，验的是校验规则与写库后果。
        print("\n批量导入：")
        account_rows = [
            {"line": 2, "cells": {"username": "imp-2023001", "display_name": "导入生一",
                                  "role": "学生", "password": ""}},
            {"line": 3, "cells": {"username": "imp-2023002", "display_name": "导入生二",
                                  "role": "学生", "password": "Imp2026pass"}},
            {"line": 4, "cells": {"username": "e2e-teacher", "display_name": "重复账号",
                                  "role": "教师"}},
            {"line": 5, "cells": {"username": "bad name!", "display_name": "非法账号",
                                  "role": "学生"}},
            {"line": 6, "cells": {"username": "imp-2023001", "display_name": "文件内重复",
                                  "role": "学生"}},
        ]
        denied = client.post("/api/v1/accounts/import", headers=teacher_headers,
                             json={"rows": account_rows, "dry_run": True})
        check("教师不能批量建账号（账号是管理员的事）", denied.status_code == 403,
              str(denied.status_code))
        forbidden = client.post("/api/v1/accounts/import", headers=STUDENT,
                                json={"rows": account_rows, "dry_run": True})
        check("学生更不能批量建账号", forbidden.status_code == 403, str(forbidden.status_code))

        preview = client.post("/api/v1/accounts/import", headers=ADMIN,
                              json={"rows": account_rows, "dry_run": True})
        check("账号导入预览可读", preview.status_code == 200, preview.text[:160])
        preview_body = preview.json()
        check("预览逐行给结论（5 行都在）", len(preview_body["results"]) == 5,
              str(len(preview_body["results"])))
        check("预览统计可导入 2 / 跳过 1 / 有问题 2",
              preview_body["summary"]["ok"] == 2
              and preview_body["summary"]["skip"] == 1
              and preview_body["summary"]["error"] == 2,
              str(preview_body["summary"]))
        reasons = {row["line"]: row["reason"] for row in preview_body["results"]}
        check("已有的账号算跳过，并说明原因", "已存在" in reasons.get(4, ""), reasons.get(4, ""))
        check("非法账号名报错", "只能用字母" in reasons.get(5, ""), reasons.get(5, ""))
        check("文件内重复报错并指出第几行", "第 2 行" in reasons.get(6, ""), reasons.get(6, ""))
        check("预览没写库",
              db.query_one("SELECT count(*)::int AS n FROM app_account "
                           "WHERE username ~ '^imp-'")["n"] == 0, "预览就把账号建了")

        imp = client.post("/api/v1/accounts/import", headers=ADMIN,
                          json={"rows": account_rows, "dry_run": False})
        check("确认后真导入", imp.status_code == 200, imp.text[:160])
        imp_body = imp.json()
        check("新建 2 个账号", imp_body["summary"]["created"] == 2,
              str(imp_body["summary"]))
        passwords = {row["username"]: row for row in imp_body["passwords"]}
        check("新账号各给一个初始密码",
              len(passwords) == 2 and all(row["password"] for row in passwords.values()),
              str(sorted(passwords)))
        check("没填密码就由系统生成（并标记 generated）",
              passwords.get("imp-2023001", {}).get("generated") is True,
              str(passwords.get("imp-2023001", {}).get("generated")))
        check("填了密码就用填的（不改写）",
              passwords.get("imp-2023002", {}).get("password") == "Imp2026pass"
              and passwords.get("imp-2023002", {}).get("generated") is False,
              str(passwords.get("imp-2023002")))
        check("生成的账号要求首次登录改密",
              db.query_one("SELECT must_change_password FROM app_account "
                           "WHERE username = 'imp-2023001'")["must_change_password"] is True,
              "首次登录不要求改密")
        check("手填密码的账号不强制改密",
              db.query_one("SELECT must_change_password FROM app_account "
                           "WHERE username = 'imp-2023002'")["must_change_password"] is False)
        check("重复的那行没把已有账号的姓名改掉",
              db.query_one("SELECT display_name FROM app_account "
                           "WHERE username = 'e2e-teacher'")["display_name"] == "端到端教师",
              "批量导入顺手改了已有账号")
        check("账号导入写了审计",
              "account.import" in {row["action"] for row in db.query(
                  "SELECT action FROM audit_log ORDER BY id DESC LIMIT 20")})

        class_rows = [
            {"line": 2, "cells": {"course": "端到端测试课", "name": "导入-01班",
                                  "academic_year": "2026-2027", "term_kind": "秋季学期",
                                  "teacher": "", "note": "Excel 导入"}},
            {"line": 3, "cells": {"course": "e2e-course", "name": "导入-02班",
                                  "academic_year": "2026-2027", "term_kind": "春",
                                  "teacher": "e2e-teacher"}},
            {"line": 4, "cells": {"course": "E2E-01班（改）", "name": "导入-03班",
                                  "academic_year": "2026-2027", "term_kind": "秋"}},
            {"line": 5, "cells": {"course": "端到端测试课", "name": "导入-01班",
                                  "academic_year": "2027-2028", "term_kind": "秋"}},
        ]
        class_preview = client.post("/api/v1/classes/import", headers=teacher_headers,
                                    json={"rows": class_rows, "dry_run": True})
        check("班级导入预览可读", class_preview.status_code == 200,
              class_preview.text[:160])
        class_summary = class_preview.json()["summary"]
        check("班级预览：3 行可建、1 行有问题（课程名对不上）",
              class_summary["ok"] == 3 and class_summary["error"] == 1,
              str(class_summary))
        class_reasons = {row["line"]: row["reason"]
                         for row in class_preview.json()["results"]}
        check("课程名对不上时报错并提示填 id 或全名",
              "没有这门课" in class_reasons.get(4, ""), class_reasons.get(4, ""))
        check("学年跨学年写错时报错",
              "2026-2027" in class_reasons.get(4, "") or class_reasons.get(5, "") != "",
              f"{class_reasons.get(4, '')} / {class_reasons.get(5, '')}")
        bad_year_rows = [{"line": 9, "cells": {"course": "端到端测试课", "name": "导入-坏学年",
                                                "academic_year": "2026-2028",
                                                "term_kind": "秋"}}]
        bad_year = client.post("/api/v1/classes/import", headers=teacher_headers,
                               json={"rows": bad_year_rows, "dry_run": True})
        check("学年两个年份不连续会被拦下",
              bad_year.json()["summary"]["error"] == 1
              and "2026-2027" in bad_year.json()["results"][0]["reason"],
              str(bad_year.json()["results"][0]["reason"]))
        check("学生不能批量建班",
              client.post("/api/v1/classes/import", headers=STUDENT,
                          json={"rows": class_rows, "dry_run": True}).status_code == 403)

        class_imp = client.post("/api/v1/classes/import", headers=teacher_headers,
                                json={"rows": class_rows, "dry_run": False})
        check("确认后真建班", class_imp.status_code == 200
              and class_imp.json()["summary"]["created"] == 3, class_imp.text[:160])
        made_classes = class_imp.json()["classes"]
        listed_after = client.get("/api/v1/classes?course_id=e2e-course",
                                  headers=teacher_headers).json()
        names_after = {row["name"] for row in listed_after["items"]}
        check("建出来的班能查到",
              {"导入-01班", "导入-02班"} <= names_after, str(sorted(names_after)))
        default_teacher = db.query(
            "SELECT t.username FROM teaching_class c "
            "JOIN app_account t ON t.id = c.teacher_id "
            "WHERE c.name = '导入-01班' AND c.academic_year = '2026-2027'")
        check("任课教师留空就是自己",
              bool(default_teacher) and default_teacher[0]["username"] == "e2e-teacher",
              str(default_teacher))
        again_class = client.post("/api/v1/classes/import", headers=teacher_headers,
                                  json={"rows": class_rows[:1], "dry_run": False})
        check("同样的班再导一次算跳过，不重复建",
              again_class.json()["summary"]["skip"] == 1
              and again_class.json()["summary"]["created"] == 0,
              str(again_class.json()["summary"]))

        roster_class = made_classes[0]["class_id"] if made_classes else ""
        roster_rows = [
            {"line": 2, "cells": {"username": "e2e-student", "display_name": ""}},
            {"line": 3, "cells": {"username": "imp-2023100", "display_name": "名单新同学",
                                  "password": ""}},
            {"line": 4, "cells": {"username": "imp-2023101", "display_name": "",
                                  "password": ""}},
            {"line": 5, "cells": {"username": "e2e-teacher", "display_name": "教师",
                                  "password": ""}},
        ]
        roster_preview = client.post(
            f"/api/v1/classes/{roster_class}/students/import", headers=teacher_headers,
            json={"rows": roster_rows, "dry_run": True})
        check("名单导入预览可读", roster_preview.status_code == 200,
              roster_preview.text[:160])
        roster_summary = roster_preview.json()["summary"]
        check("名单预览：2 行可加、2 行有问题",
              roster_summary["ok"] == 2 and roster_summary["error"] == 2,
              str(roster_summary))
        will_create = [row for row in roster_preview.json()["results"]
                       if row.get("will_create_account")]
        check("库里没有的学号会标出「将新建账号」", len(will_create) == 1,
              str(len(will_create)))
        roster_reasons = {row["line"]: row["reason"]
                          for row in roster_preview.json()["results"]}
        check("没姓名又不存在的学号建不了号",
              "没填姓名" in roster_reasons.get(4, ""), roster_reasons.get(4, ""))
        check("教师账号不能当学生拉进班",
              "不是学生" in roster_reasons.get(5, ""), roster_reasons.get(5, ""))

        roster_imp = client.post(
            f"/api/v1/classes/{roster_class}/students/import", headers=teacher_headers,
            json={"rows": roster_rows, "dry_run": False})
        check("确认后真加进班", roster_imp.status_code == 200
              and roster_imp.json()["added"] == 2, roster_imp.text[:160])
        check("顺便建了一个学生账号",
              roster_imp.json()["created_accounts"] == 1,
              str(roster_imp.json().get("created_accounts")))
        made_student = (roster_imp.json().get("passwords") or [{}])[0]
        check("建号时给的初始密码只出现这一次", bool(made_student.get("password")),
              str(made_student))
        check("名单里能看到这两个人",
              client.get(f"/api/v1/classes/{roster_class}/students",
                         headers=teacher_headers).json()["total"] == 2,
              "名单没落库")
        roster_again = client.post(
            f"/api/v1/classes/{roster_class}/students/import", headers=teacher_headers,
            json={"rows": roster_rows[:2], "dry_run": False})
        check("同一份名单再导一次：一个跳过、一个已在班里",
              roster_again.json()["added"] == 0 and roster_again.json()["summary"]["skip"] == 2,
              str(roster_again.json()["summary"]))
        check("学生不能导名单",
              client.post(f"/api/v1/classes/{roster_class}/students/import",
                          headers=STUDENT,
                          json={"rows": roster_rows, "dry_run": True}).status_code == 403)
        check("名单导入写了审计",
              "enrollment.import" in {row["action"] for row in db.query(
                  "SELECT action FROM audit_log ORDER BY id DESC LIMIT 30")})
        check("空表被 400 拒绝",
              client.post(f"/api/v1/classes/{roster_class}/students/import",
                          headers=teacher_headers,
                          json={"rows": [{"line": 1, "cells": {"username": ""}}]})
              .status_code == 400, "空行当成了有效数据")

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
