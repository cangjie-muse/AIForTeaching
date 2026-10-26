"""作业拍照批改的单元测试（假库 + 假模型，不连数据库、不发网络请求）。

钉住的规则：

1. 转写与判分是两件事，模型输出必须能被容错解析（代码块、前后废话、字段缺失）；
2. 判分只认题库参考答案；没有参考答案时必须标 needs_review，且不给分；
3. AI 分只写 auto_score，正式成绩只认 teacher_score——这条不能有第二个实现；
4. 批改失败要让作业显示"失败"，而不是永远停在"批改中"；
5. 学生只能看自己的作业，教师能看全部。

真库上的 e2e 在 e2e_api.py 里另跑。
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import homework  # noqa: E402
from app.auth import Actor  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


class FakeCursor:
    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    def execute(self, sql, params=None):
        self.db.execute(sql, params)


class FakeDB:
    def __init__(self, submission: dict | None = None, pages: list[dict] | None = None,
                 exercises: list[dict] | None = None) -> None:
        self.submission = submission if submission is not None else {
            "id": "sub-1", "course_id": "analysis-ii", "student_id": "acc-s",
            "title": "今天的作业", "status": "submitted", "page_count": 1,
            "assignment_id": None, "model": None, "error": None,
            "submitted_at": "2026-10-26",
        }
        self.pages = pages if pages is not None else [
            {"page_no": 1, "sha256": "a" * 64, "mime": "image/jpeg"}]
        self.exercises = exercises if exercises is not None else []
        self.items: list[dict] = []
        self.statements: list[tuple[str, tuple | None]] = []

    def query(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM submission_page" in sql:
            return [dict(page) for page in self.pages]
        if "FROM exercise" in sql:
            return [dict(row) for row in self.exercises]
        if "FROM submission_item" in sql:
            return [dict(item) for item in self.items]
        return []

    def query_one(self, sql, params=None):
        self.statements.append((sql, params))
        if "count(*) AS n FROM submission_page" in sql:
            return {"n": len(self.pages)}
        if "FROM submission WHERE id" in sql:
            return dict(self.submission) if self.submission else None
        if "FROM submission_item" in sql:
            ord_ = params[1]
            return next((dict(i) for i in self.items if i["ord"] == ord_), None)
        return None

    @staticmethod
    def _assignments(sql: str, params) -> dict:
        """把 `SET a = %s, b = now()` 拆成 {列名: 值}。

        不能按"逗号切完按下标取参数"——`now()` 那种占位符以外的写法会让
        下标整体错位，测试就会在错误的地方通过。
        """
        import re
        match = re.search(r"SET (.*?) WHERE", sql, re.S)
        if not match:
            return {}
        values = iter(params or ())
        result = {}
        for part in match.group(1).split(","):
            name, _, value = part.strip().partition(" = ")
            value = value.strip()
            if value == "%s":
                result[name] = next(values, None)
            elif value.startswith("'") and value.endswith("'"):
                result[name] = value[1:-1]          # 字面量，如 status = 'ai-graded'
            elif value.upper() == "NULL":
                result[name] = None
        return result

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        # 真实 SQL 是多行缩进的（UPDATE 与 SET 之间有换行），比对前缀前先压成一行，
        # 否则多行写法的语句会被当成"没匹配上"静默跳过。
        text = " ".join(sql.split())

        if text.startswith("INSERT INTO submission_item"):
            (submission_id, ord_, exercise_id, page_no, question_no, question_md,
             answer, verdict, score, comment, confidence, evidence) = params
            self.items.append({
                "submission_id": submission_id, "ord": ord_, "exercise_id": exercise_id,
                "page_no": page_no, "question_no": question_no, "question_md": question_md,
                "student_answer_md": answer, "auto_verdict": verdict,
                "auto_score": score, "auto_comment": comment,
                "confidence": confidence, "teacher_score": None,
                "teacher_comment": None, "graded_by": None,
                "auto_evidence": json.loads(evidence),
            })
            return 1

        if text.startswith("UPDATE submission SET"):
            for key, value in self._assignments(text, params).items():
                if key in self.submission:
                    self.submission[key] = value
            return 1

        if text.startswith("UPDATE submission_item SET"):
            changes = self._assignments(text, params)
            if "teacher_score = auto_score" in text:
                for item in self.items:
                    if item["teacher_score"] is None and item["auto_score"] is not None:
                        item["teacher_score"] = item["auto_score"]
                return 1
            ord_ = params[-1]
            for item in self.items:
                if item["ord"] == ord_:
                    item.update(changes)
            return 1

        if text.startswith("DELETE FROM submission_item"):
            self.items = []
            return 1
        return 1

    @contextmanager
    def transaction(self):
        yield FakeCursor(self)


def student() -> Actor:
    return Actor(id="acc-s", roles=frozenset({"student"}), display_name="李同学")


def teacher() -> Actor:
    return Actor(id="acc-t", roles=frozenset({"teacher"}), display_name="张老师")


print("图片格式：")
check("认 JPEG", homework._mime_of(b"\xff\xd8\xff\xe0rest") == "image/jpeg")
check("认 PNG", homework._mime_of(b"\x89PNG\r\n\x1a\nrest") == "image/png")
check("认 WebP", homework._mime_of(b"RIFF" + b"\x00" * 4 + b"WEBPVP8 ") == "image/webp")
check("不认识的格式返回 None", homework._mime_of(b"GIF89a") is None)

print("\n转写结果解析：")
items = homework.parse_items('```json\n{"items":[{"question_no":"1","question":"求 $x^2$ 的导数",'
                             '"student_answer":"$2x$"}]}\n```')
check("能剥掉 markdown 代码块", len(items) == 1 and items[0]["question_no"] == "1", str(items))
items = homework.parse_items('好的，结果如下：\n{"items":[{"question_no":"2","question":"证明极限存在",'
                             '"student_answer":""}]}\n希望有帮助。')
check("前后有废话也能解析", items[0]["question_no"] == "2", str(items))
check("接受数组形式的顶层", homework.parse_items('[{"question_no":"1","question":"q","student_answer":"a"}]')[0]["question_no"] == "1")
try:
    homework.parse_items("抱歉，我看不清这张图片。")
    check("没有 JSON 时报错", False, "居然解析成功了")
except ValueError as exc:
    check("没有 JSON 时报错", "JSON" in str(exc), str(exc)[:40])

print("\n判分结果解析：")
judged = homework.parse_judgements('{"items":[{"verdict":"correct","score":10,"comment":"对",'
                                   '"confidence":0.9,"match_id":"ex-1"}]}', 1)
check("正常判分", judged[0]["verdict"] == "correct" and judged[0]["score"] == 10.0, str(judged[0]))
check("recognized match_id", judged[0]["match_id"] == "ex-1")

judged = homework.parse_judgements('{"items":[{"verdict":"unjudged","score":8,"comment":"存疑"}]}', 1)
check("unjudged 一律不给分", judged[0]["score"] is None and judged[0]["needs_review"], str(judged[0]))

judged = homework.parse_judgements('{"items":[{"verdict":"partial","score":99}]}', 1)
check("分数被夹到满分内", judged[0]["score"] == homework.ITEM_FULL_SCORE, str(judged[0]["score"]))

judged = homework.parse_judgements('{"items":[]}', 2)
check("模型漏题时补成 unjudged 且待复核",
      len(judged) == 2 and judged[0]["verdict"] == "unjudged" and judged[0]["needs_review"], "")

judged = homework.parse_judgements('{"items":[{"verdict":"胡说","score":3}]}', 1)
check("不认识的 verdict 归为 unjudged", judged[0]["verdict"] == "unjudged", str(judged[0]))

print("\n批改主流程：")
original_db = homework.db
original_vision = homework._call_vision
original_text = homework._call_text
original_dir = homework.UPLOAD_DIR
import shutil
import tempfile

# 建在系统临时目录里并保证删掉：跑测试不该在仓库根目录留下 hw-xxxx 目录。
tmp = Path(tempfile.mkdtemp(prefix="hw-", dir=tempfile.gettempdir()))
homework.UPLOAD_DIR = tmp

try:
    db = FakeDB(
        pages=[{"page_no": 1, "sha256": "a" * 64, "mime": "image/jpeg"}],
        exercises=[{"id": "ex-1", "chapter_id": "ch-1",
                    "prompt_md": "求 $x^2$ 的导数", "answer_md": "$2x$"}],
    )
    homework.db = db
    (tmp / f"sub-1-{'a' * 12}.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    homework._call_vision = lambda page_no, data, mime: json.dumps({
        "items": [
            {"question_no": "1", "question": "求 $x^2$ 的导数", "student_answer": "$2x$"},
            {"question_no": "2", "question": "求 $\\sin x$ 的积分", "student_answer": "$-\\cos x+C$"},
            {"question_no": "3", "question": "证明闭区间上连续函数有界", "student_answer": "[无法辨认]"},
        ]}, ensure_ascii=False)
    homework._call_text = lambda messages: json.dumps({"items": [
        {"question_no": "1", "match_id": "ex-1", "verdict": "correct", "score": 10,
         "comment": "思路与结论均正确", "confidence": 0.95, "needs_review": False},
        {"question_no": "2", "match_id": None, "verdict": "correct", "score": 10,
         "comment": "无参考答案，按自己推导判断", "confidence": 0.7, "needs_review": True},
        {"question_no": "3", "match_id": None, "verdict": "unjudged", "score": None,
         "comment": "卷面无法辨认", "confidence": 0.2, "needs_review": True},
    ]}, ensure_ascii=False)

    result = homework.grade_submission("sub-1")
    check("三道题都入库", len(db.items) == 3, str(len(db.items)))
    check("总分按满分 10 折成百分制", abs(result["score"] - 66.7) < 0.2, str(result["score"]))
    check("报告待复核题数", result["needs_review"] == 2, str(result["needs_review"]))
    check("作业状态变成已初评", db.submission["status"] == "ai-graded", db.submission["status"])
    check("写入了模型名", "+" in (db.submission["model"] or ""), str(db.submission["model"]))
    check("页面转写结果落库",
          any("UPDATE submission_page SET transcript" in sql for sql, _ in db.statements), "")
    check("匹配到的题库题记在条目上", db.items[0]["exercise_id"] == "ex-1", str(db.items[0]["exercise_id"]))
    check("没匹配上的题标了待复核", db.items[1]["auto_evidence"]["needs_review"] is True, "")
    check("AI 分不写教师分", all(item["teacher_score"] is None for item in db.items), "")

    print("\n失败要显示失败：")
    db = FakeDB(pages=[], exercises=[])
    homework.db = db
    try:
        homework.grade_submission("sub-1")
        check("没有页面时批改报错", False, "居然跑完了")
    except ValueError as exc:
        check("没有页面时批改报错", "没有上传" in str(exc), str(exc))

    class JobDB(FakeDB):
        def query_one(self, sql, params=None):
            if "FROM pipeline_job" in sql:
                return {"id": 7, "status": "failed", "kind": "grade-homework",
                        "payload": {"submission_id": "sub-1"}, "result": None,
                        "progress": 0, "attempts": 1}
            return super().query_one(sql, params)

    db = JobDB()
    homework.db = db
    # _finish / _step 在 generation 里，改的是 generation.db：批改任务复用了
    # 那套状态机，测试里必须一起换成假库，否则会连真库。
    from app import generation
    generation_db_before = generation.db
    generation.db = db
    homework._call_vision = lambda *args: (_ for _ in ()).throw(RuntimeError("模型 401"))
    job = {"id": 7, "kind": "grade-homework", "payload": {"submission_id": "sub-1"}}
    out = homework.run_grading_job(job)
    check("任务失败", out["status"] == "failed", str(out.get("error"))[:40])
    check("作业状态写成失败", db.submission["status"] == "failed", db.submission["status"])
    check("失败原因写进作业", "模型 401" in (db.submission["error"] or ""), str(db.submission["error"])[:40])
    homework._call_vision = original_vision
    generation.db = generation_db_before

    print("\n教师复核：")
    homework._call_vision = original_vision
    homework._call_text = original_text
    db = FakeDB()
    homework.db = db
    db.items = [{"submission_id": "sub-1", "ord": 1, "auto_score": 6.0, "auto_verdict": "partial",
                 "teacher_score": None, "teacher_comment": None, "graded_by": None},
                {"submission_id": "sub-1", "ord": 2, "auto_score": None, "auto_verdict": "unjudged",
                 "teacher_score": None, "teacher_comment": None, "graded_by": None}]
    homework.review_item("sub-1", 1, homework.ReviewIn(teacher_score=8.0, teacher_comment="第二步跳步"),
                         teacher())
    check("教师改分写进 teacher_score", db.items[0]["teacher_score"] == 8.0, str(db.items[0]["teacher_score"]))
    check("教师评语入库", db.items[0]["teacher_comment"] == "第二步跳步", "")
    check("复核写审计", any("homework.review" in sql for sql, _ in db.statements), "")
    check("AI 分没有被覆盖", db.items[0]["auto_score"] == 6.0, str(db.items[0]["auto_score"]))

    homework.confirm("sub-1", teacher())
    check("一键确认只补空缺", db.items[1]["teacher_score"] is None, "unjudged 没有 auto_score，不该被填")
    check("确认后状态为教师已确认", db.submission["status"] == "teacher-confirmed", db.submission["status"])

    print("\n总分口径：")
    rows = [
        {"auto_score": 10, "teacher_score": 8, "auto_evidence": {}},
        {"auto_score": 0, "teacher_score": None, "auto_evidence": {"needs_review": True}},
    ]
    score = homework._score(rows, {"status": "ai-graded"})
    check("AI 分与教师分分开算", score["auto"] == 50.0 and score["teacher"] == 40.0, str(score))
    check("只统计还没被教师复核的待复核题", score["needs_review"] == 1, str(score["needs_review"]))

    print("\n权限：")
    try:
        homework._guard(student(), {"student_id": "acc-other"})
        check("学生看不了别人的作业", False, "居然放行了")
    except HTTPException as exc:
        check("学生看不了别人的作业", exc.status_code == 403, str(exc.status_code))
    homework._guard(teacher(), {"student_id": "acc-other"})
    check("教师能看学生的作业", True)
    try:
        homework._guard(student(), {"student_id": "acc-s"}, teacher_only=True)
        check("学生不能复核", False, "居然放行了")
    except HTTPException as exc:
        check("学生不能复核", exc.status_code == 403, str(exc.status_code))

    print("\n路由与列名：")
    source = homework.__dict__["create_homework"].__code__.co_consts
    check("路由带 /api/v1 前缀",
          "/api/v1/homework" in homework.__dict__.get("router").__dict__.get("prefix", ""),
          str(getattr(homework.router, "prefix", "")))
    check("任务类型登记进 worker",
          "grade-homework" in Path("server/app/generation.py").read_text(encoding="utf-8"), "")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    homework.db = original_db
    homework._call_vision = original_vision
    homework._call_text = original_text
    homework.UPLOAD_DIR = original_dir

print("")
if failures:
    print(f"作业批改测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("作业批改测试全部通过。")
