"""作业拍照批改：学生传图 → 转写卷面 → 比对参考答案判分 → 教师复核。

为什么分两步，而不是「把图片丢给模型，让它打个分」：

  1. **可检查**。转写结果是人能核对的（学生自己就能看出「这题它识别错了」），
     而模型直接给的分不能核对。所以第一阶段的产物必须落库
     （submission_page.transcript），有争议时能回看当初识别成了什么。
  2. **可归因**。分错的两种原因要分开：认错了字，还是判错了理。一步到底时
     这两种错在日志里长得一模一样。
  3. **判分要有依据**。能匹配到题库题时，判分基准是题库里的 answer_md，
     模型自己的推导只在没有匹配项时兜底，且必须标 needs_review。

硬规则（与 submission_item 的注释一致）：AI 只写 auto_score / auto_verdict，
正式成绩只认 teacher_score。这条规则在整个系统里只允许有一处实现，就是这里。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import db, llm
from .auth import Actor, require, require_login

router = APIRouter(prefix="/api/v1/homework", tags=["homework"])

ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads")) / "homework"

MAX_PAGE_MB = int(os.environ.get("MAX_HOMEWORK_MB", "10"))
MAX_PAGES = 20               # 与 submission_page.page_no 的 CHECK 对齐
MAX_ITEMS_PER_PAGE = 12      # 一页超过这么多题，多半是整本卷子，拆开传更准
CANDIDATE_LIMIT = 60         # 送进判分提示词的候选题上限
ITEM_FULL_SCORE = 10.0       # 每题满分；assignment 有 points 时用 points

# 图片格式白名单：只认这几种。不接受 PDF 与 SVG——PDF 要渲染、SVG 能带脚本，
# 都不该出现在「学生上传」这条路上。
_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]


def _mime_of(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

TRANSCRIBE_SYSTEM = """你是数学作业的转写员，只做转写，不做批改。

规则：
1. 逐题抄出卷面上的题目与学生的完整作答，包括推导步骤和草稿。
2. 不要解题，不要判断对错，不要补全学生没写的内容，不要纠正笔误。
3. 公式用 LaTeX，行内用 $...$。看不清的字写 [无法辨认]，不要猜。
4. 题号照抄卷面（如 "3"、"3(2)"、"证明题一"）；没有题号就按从上到下顺序编 "1"。
5. 学生只写了答案没写过程时，student_answer 只放答案；完全空白写 ""。

只输出 JSON，不要解释，不要 markdown 代码块：
{"items":[{"question_no":"1","question":"...","student_answer":"..."}]}"""

JUDGE_SYSTEM = """你是大学数学作业的判分员。给你两样东西：学生卷面的转写结果，以及题库里可能对应的题目与参考答案。

对每一道学生作答的题，按下面的顺序处理：

1. 先判断它对应题库里的哪一道（看题干内容，不要只看题号）。对不上就 match_id 填 null。
2. 匹配到题库题：以题库的参考答案为准判分。不要自己另算一遍再否定参考答案；
   若你认为参考答案本身有误，verdict 填 "unjudged"，并在 comment 里写清楚哪一步对不上。
3. 没匹配到题库题：你可以自己解题，但必须在 comment 里写出关键步骤，并把
   needs_review 标为 true——没有参考答案时，你的结论只能是建议。
4. 判分看过程，不只看结果：过程对结果错给 partial；过程错但结论对也要给 partial。
5. comment 用中文，一句话指出**第一处**问题（或肯定其对的地方），不要复述整题。
   学生写得对就写"思路与结论均正确"这类短句，不要凑字数。

verdict 取值：correct（正确）、partial（部分正确）、wrong（错误）、unjudged（无法判定）。
score 是 0 到 {full} 的数；partial 时给中间值。confidence 是你对这次判断的把握，
0 到 1；低于 0.6 会在界面上被标出来让教师先看。

只输出 JSON，不要解释，不要 markdown 代码块：
{{"items":[{{"question_no":"1","match_id":null,"verdict":"correct","score":{full},"comment":"...","confidence":0.9,"needs_review":false}}]}}"""


def transcribe_prompt(page_no: int) -> list[dict]:
    return [
        {"role": "system", "content": TRANSCRIBE_SYSTEM},
        {"role": "user", "content": [
            {"type": "text",
             "text": f"这是作业的第 {page_no} 页，请转写。"},
        ]},
    ]


def judge_messages(items: list[dict], candidates: list[dict]) -> list[dict]:
    """判分提示词。

    候选题只给题号、题干和答案，不给解析：解析一长，模型就会去"参考"解析的
    写法，而不是独立核对学生那一步。
    """
    payload = {
        "students_items": [
            {"question_no": item["question_no"], "question": item["question"],
             "student_answer": item["student_answer"]}
            for item in items
        ],
        "bank_candidates": [
            {"id": row["id"], "no": row["no"], "question": row["prompt"],
             "answer": row["answer"]}
            for row in candidates
        ],
    }
    return [
        {"role": "system", "content": JUDGE_SYSTEM.format(full=int(ITEM_FULL_SCORE))},
        {"role": "user",
         "content": "数据如下（JSON）：\n"
                    + json.dumps(payload, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------------------
# 模型输出的解析（这一层必须容错：模型偶尔会加代码块或前后废话）
# ---------------------------------------------------------------------------

def _json_payload(text: str) -> dict:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fenced:
        raw = fenced.group(1).strip()
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"模型没有返回 JSON：{raw[:200]}")
    # raw_decode 从指定位置解一个值，忽略后面的废话；比"截断到最后一个 }"
    # 稳（点评里可能带花括号），也比逐个长度重试快得多。
    try:
        data, _ = json.JSONDecoder().raw_decode(raw, start)
    except json.JSONDecodeError as error:
        raise ValueError(f"模型返回的不是合法 JSON：{raw[:200]}") from error
    if isinstance(data, list):
        return {"items": data}
    if isinstance(data, dict):
        return data
    raise ValueError(f"模型返回的 JSON 既不是对象也不是数组：{raw[:200]}")


def parse_items(text: str) -> list[dict]:
    data = _json_payload(text)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("转写结果里没有 items")
    cleaned: list[dict] = []
    for index, item in enumerate(items[:MAX_ITEMS_PER_PAGE], start=1):
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "question_no": str(item.get("question_no") or index)[:40],
            "question": str(item.get("question") or "").strip()[:4000],
            "student_answer": str(item.get("student_answer") or "").strip()[:6000],
        })
    if not cleaned:
        raise ValueError("转写结果里没有可用的题目")
    return cleaned


def parse_judgements(text: str, count: int) -> list[dict]:
    data = _json_payload(text)
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("判分结果里没有 items")
    by_index = {index + 1: row for index, row in enumerate(rows) if isinstance(row, dict)}
    result: list[dict] = []
    for index in range(1, count + 1):
        row = by_index.get(index, {})
        verdict = str(row.get("verdict") or "unjudged").strip()
        if verdict not in ("correct", "partial", "wrong", "unjudged"):
            verdict = "unjudged"
        score = row.get("score")
        try:
            score = None if score is None else max(0.0, min(ITEM_FULL_SCORE, float(score)))
        except (TypeError, ValueError):
            score = None
        if verdict == "unjudged":
            score = None
        confidence = row.get("confidence")
        try:
            confidence = None if confidence is None else max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        result.append({
            "match_id": (str(row.get("match_id")) if row.get("match_id") else None),
            "verdict": verdict,
            "score": score,
            "comment": str(row.get("comment") or "").strip()[:800],
            "confidence": confidence,
            "needs_review": bool(row.get("needs_review")) or verdict == "unjudged",
        })
    return result


# ---------------------------------------------------------------------------
# 题库候选
# ---------------------------------------------------------------------------

def bank_candidates(course_id: str, limit: int = CANDIDATE_LIMIT) -> list[dict]:
    """候选＝已发布、有参考答案的题。

    不送 draft 题：那些题连教师都还没确认，拿它当判分基准等于用一个错答案
    去批学生。没有参考答案的题也不送——它在判分里只能靠模型自己算。
    """
    rows = db.query(
        """SELECT id, coalesce(chapter_id, '') AS chapter_id, prompt_md, answer_md
             FROM exercise
            WHERE course_id = %s AND review_status = 'published'
              AND coalesce(answer_md, '') <> ''
            ORDER BY chapter_id, id
            LIMIT %s""",
        (course_id, limit),
    )
    return [{"id": row["id"], "no": row.get("chapter_id") or "",
             "prompt": (row["prompt_md"] or "")[:1200],
             "answer": (row["answer_md"] or "")[:1200]} for row in rows]


# ---------------------------------------------------------------------------
# 批改主流程（由 worker 调用）
# ---------------------------------------------------------------------------

def _call_vision(page_no: int, data: bytes, mime: str) -> str:
    """单独一层，测试里替换它就能不碰网络。"""
    messages = transcribe_prompt(page_no)
    messages[1]["content"].append(llm.image_part(data, mime))
    return llm.chat_vision(messages, temperature=0.0)["content"]


def _call_text(messages: list[dict]) -> str:
    return llm.chat(messages, temperature=0.0)["content"]


def grade_submission(submission_id: str) -> dict:
    """跑一次完整批改。抛异常由 worker 记到 job.error，同时写进 submission.error。"""
    submission = db.query_one("SELECT * FROM submission WHERE id = %s", (submission_id,))
    if not submission:
        raise ValueError(f"作业不存在：{submission_id}")
    pages = db.query(
        """SELECT page_no, sha256, mime FROM submission_page
            WHERE submission_id = %s ORDER BY page_no""",
        (submission_id,),
    )
    if not pages:
        raise ValueError("这次作业没有上传任何页面")
    if not submission.get("course_id"):
        raise ValueError("作业没有课程，无法从题库取参考答案")

    db.execute("UPDATE submission SET status = 'grading', error = NULL WHERE id = %s",
               (submission_id,))

    items: list[dict] = []
    model_used = ""
    for page in pages:
        path = _image_path(submission_id, page["sha256"], page["mime"])
        data = path.read_bytes()
        content = _call_vision(page["page_no"], data, page["mime"])
        model_used = llm.vision_endpoint().model
        db.execute(
            "UPDATE submission_page SET transcript = %s::jsonb WHERE submission_id = %s AND page_no = %s",
            (json.dumps({"raw": content}, ensure_ascii=False), submission_id, page["page_no"]),
        )
        for item in parse_items(content):
            item["page_no"] = page["page_no"]
            items.append(item)

    if not items:
        raise ValueError("没有识别出任何题目：检查照片是否清晰、是否拍到了题目")

    candidates = bank_candidates(submission["course_id"])
    judged = parse_judgements(
        _call_text(judge_messages(items, candidates)), len(items))
    text_model = llm.chat_endpoint().model

    total = 0.0
    graded = 0
    with db.transaction() as cur:
        cur.execute("DELETE FROM submission_item WHERE submission_id = %s", (submission_id,))
        for index, (item, verdict) in enumerate(zip(items, judged), start=1):
            score = verdict["score"]
            if score is not None:
                total += score
                graded += 1
            cur.execute(
                """INSERT INTO submission_item
                     (submission_id, ord, exercise_id, page_no, question_no, question_md,
                      student_answer_md, auto_verdict, auto_score, auto_comment,
                      confidence, auto_evidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                (submission_id, index, verdict["match_id"], item["page_no"],
                 item["question_no"], item["question"], item["student_answer"],
                 verdict["verdict"], score, verdict["comment"], verdict["confidence"],
                 json.dumps({"needs_review": verdict["needs_review"],
                             "matched": bool(verdict["match_id"])},
                            ensure_ascii=False)),
            )
        cur.execute(
            """UPDATE submission
                  SET status = 'ai-graded', model = %s, page_count = %s,
                      error = NULL
                WHERE id = %s""",
            (f"{model_used} + {text_model}", len(pages), submission_id),
        )
    return {
        "submission_id": submission_id,
        "items": len(items),
        "graded": graded,
        "score": round(total / (len(items) * ITEM_FULL_SCORE) * 100, 1) if items else None,
        "needs_review": sum(1 for v in judged if v["needs_review"]),
    }


def run_grading_job(job: dict) -> dict:
    """pipeline_job 的分支：失败也要让 submission 显示失败，而不是一直"批改中"。"""
    from .generation import _finish, _step

    payload = job.get("payload") or {}
    submission_id = payload.get("submission_id")
    if not submission_id:
        _finish(job["id"], "failed", error="任务缺 submission_id")
        return {"id": job["id"], "status": "failed"}
    try:
        _step(job["id"], 20)
        result = grade_submission(submission_id)
        _step(job["id"], 95)
        _finish(job["id"], "succeeded", result=result)
        return {"id": job["id"], "status": "succeeded", **result}
    except Exception as error:  # noqa: BLE001 —— worker 不能因为一次批改失败就退出
        message = f"{type(error).__name__}: {error}"
        db.execute("UPDATE submission SET status = 'failed', error = %s WHERE id = %s",
                   (message[:500], submission_id))
        _finish(job["id"], "failed", error=message)
        return {"id": job["id"], "status": "failed", "error": message}


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def _image_path(submission_id: str, sha: str, mime: str) -> Path:
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".img")
    return UPLOAD_DIR / f"{submission_id}-{sha[:12]}{ext}"


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------

def _item_view(row: dict) -> dict:
    return {
        "ord": row["ord"],
        "page_no": row.get("page_no"),
        "question_no": row.get("question_no"),
        "question": row.get("question_md"),
        "student_answer": row.get("student_answer_md"),
        "exercise_id": row.get("exercise_id"),
        "verdict": row.get("auto_verdict"),
        "score": float(row["auto_score"]) if row.get("auto_score") is not None else None,
        "full_score": ITEM_FULL_SCORE,
        "comment": row.get("auto_comment"),
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
        "needs_review": bool((row.get("auto_evidence") or {}).get("needs_review")),
        "teacher_score": float(row["teacher_score"]) if row.get("teacher_score") is not None else None,
        "teacher_comment": row.get("teacher_comment"),
        "graded_by": row.get("graded_by"),
    }


def _view(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "course_id": row.get("course_id"),
        "assignment_id": row.get("assignment_id"),
        "student_id": row["student_id"],
        "title": row.get("title"),
        "status": row["status"],
        "page_count": row.get("page_count") or 0,
        "model": row.get("model"),
        "error": row.get("error"),
        "submitted_at": row.get("submitted_at"),
    }


def _load(submission_id: str) -> dict:
    row = db.query_one("SELECT * FROM submission WHERE id = %s", (submission_id,))
    if not row:
        raise HTTPException(404, f"作业不存在：{submission_id}")
    return row


def _guard(actor: Actor, row: dict, *, teacher_only: bool = False) -> None:
    """学生只能看自己的；教师能看全部。

    v1 不做"教师只能看自己班的"：班级关系表在，但一次作业常常跨班，
    先按"教师可见全校作业"放开，并把这条写在这里，等真的需要收窄时只改这一处。
    """
    if actor.has("teacher", "admin"):
        return
    if teacher_only:
        raise HTTPException(403, "这个操作只有教师可以")
    if row["student_id"] != actor.id:
        raise HTTPException(403, "只能查看自己的作业")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

class HomeworkIn(BaseModel):
    course_id: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=80)


@router.post("", status_code=201)
def create_homework(body: HomeworkIn, actor: Actor = Depends(require_login)) -> dict:
    course = db.query_one("SELECT title FROM course WHERE id = %s", (body.course_id,))
    if not course:
        raise HTTPException(404, f"课程不存在：{body.course_id}")
    row = db.query_one(
        """INSERT INTO submission (course_id, student_id, title, status)
           VALUES (%s, %s, %s, 'submitted') RETURNING *""",
        (body.course_id, actor.id,
         body.title or f"{course['title']} 作业"),
    )
    return _view(row)


@router.put("/{submission_id}/pages/{page_no}")
async def upload_page(submission_id: str, page_no: int, request: Request,
                      actor: Actor = Depends(require_login)) -> dict:
    """上传一页图片。用原始字节而不是 multipart：前端 fetch(file) 直接传。"""
    row = _load(submission_id)
    _guard(actor, row)
    if row["status"] in ("ai-graded", "teacher-confirmed"):
        raise HTTPException(409, "这次作业已经批改过；要重做请新建一次提交")
    if not 1 <= page_no <= MAX_PAGES:
        raise HTTPException(400, f"页码要在 1 到 {MAX_PAGES} 之间")

    data = await request.body()
    if not data:
        raise HTTPException(400, "请求体为空：请把图片内容作为 body 发过来")
    if len(data) > MAX_PAGE_MB * 1024 * 1024:
        raise HTTPException(413, f"单页超过 {MAX_PAGE_MB} MB，拍小一点或压缩后再传")
    mime = _mime_of(data)
    if not mime:
        raise HTTPException(400, "只收 JPEG / PNG / WebP 图片")

    sha = hashlib.sha256(data).hexdigest()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _image_path(submission_id, sha, mime).write_bytes(data)
    db.execute(
        """INSERT INTO submission_page (submission_id, page_no, sha256, bytes, mime)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (submission_id, page_no) DO UPDATE
             SET sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes, mime = EXCLUDED.mime""",
        (submission_id, page_no, sha, len(data), mime),
    )
    count = db.query_one(
        "SELECT count(*) AS n FROM submission_page WHERE submission_id = %s",
        (submission_id,))
    db.execute("UPDATE submission SET page_count = %s WHERE id = %s",
               (count["n"], submission_id))
    return {"page_no": page_no, "bytes": len(data), "mime": mime,
            "page_count": count["n"], "sha256": sha[:12]}


@router.get("/{submission_id}/pages/{page_no}/image")
def get_page_image(submission_id: str, page_no: int,
                   actor: Actor = Depends(require_login)) -> Response:
    row = _load(submission_id)
    _guard(actor, row)
    page = db.query_one(
        """SELECT sha256, mime FROM submission_page
            WHERE submission_id = %s AND page_no = %s""",
        (submission_id, page_no))
    if not page:
        raise HTTPException(404, "这一页没有图片")
    path = _image_path(submission_id, page["sha256"], page["mime"])
    if not path.exists():
        raise HTTPException(410, "图片文件已不在服务器上")
    return Response(content=path.read_bytes(), media_type=page["mime"])


@router.post("/{submission_id}/grade", status_code=202)
def start_grading(submission_id: str, actor: Actor = Depends(require_login)) -> dict:
    row = _load(submission_id)
    _guard(actor, row)
    pages = db.query_one(
        "SELECT count(*) AS n FROM submission_page WHERE submission_id = %s",
        (submission_id,))
    if not pages or not pages["n"]:
        raise HTTPException(400, "先上传至少一页图片再提交批改")
    if row["status"] == "teacher-confirmed":
        raise HTTPException(409, "教师已确认过这次作业，不再重新初评")

    from . import generation
    job = generation.enqueue(
        {"submission_id": submission_id, "course_id": row.get("course_id")},
        actor.id, kind="grade-homework")
    db.execute("UPDATE submission SET job_id = %s, status = 'submitted', error = NULL WHERE id = %s",
               (job["id"], submission_id))
    return {"submission_id": submission_id, "job": job,
            "hint": "worker 会领取任务；用 GET /api/v1/jobs/{id} 看进度"}


@router.get("")
def list_homework(course_id: str | None = Query(default=None),
                  student_id: str | None = Query(default=None),
                  limit: int = Query(default=30, ge=1, le=100),
                  actor: Actor = Depends(require_login)) -> dict:
    teacher = actor.has("teacher", "admin")
    who = student_id if (teacher and student_id) else (None if teacher else actor.id)
    rows = db.query(
        """SELECT * FROM submission
            WHERE assignment_id IS NULL
              AND (%s::text IS NULL OR course_id = %s)
              AND (%s::text IS NULL OR student_id = %s)
            ORDER BY submitted_at DESC
            LIMIT %s""",
        (course_id, course_id, who, who, limit),
    )
    return {"items": [_view(row) for row in rows], "is_teacher": teacher}


@router.get("/{submission_id}")
def get_homework(submission_id: str, actor: Actor = Depends(require_login)) -> dict:
    row = _load(submission_id)
    _guard(actor, row)
    items = db.query(
        "SELECT * FROM submission_item WHERE submission_id = %s ORDER BY ord",
        (submission_id,))
    pages = db.query(
        """SELECT page_no, bytes, mime, (transcript IS NOT NULL) AS transcribed
             FROM submission_page WHERE submission_id = %s ORDER BY page_no""",
        (submission_id,))
    return {
        "submission": _view(row),
        "items": [_item_view(item) for item in items],
        "pages": pages,
        "score": _score(items, row),
    }


def _score(items: list[dict], row: dict) -> dict:
    """总分红两份：AI 初评（只算 auto_score）与教师确认分（优先 teacher_score）。"""
    def total(rows, teacher):
        got = 0.0
        counted = 0
        for item in rows:
            value = item.get("teacher_score") if teacher else item.get("auto_score")
            if value is None:
                continue
            got += float(value)
            counted += 1
        if not counted:
            return None
        return round(got / (len(rows) * ITEM_FULL_SCORE) * 100, 1)

    return {
        "auto": total(items, False),
        "teacher": total(items, True),
        "confirmed": row["status"] == "teacher-confirmed",
        "needs_review": sum(
            1 for item in items
            if (item.get("auto_evidence") or {}).get("needs_review")
            and item.get("teacher_score") is None),
    }


class ReviewIn(BaseModel):
    teacher_score: float | None = Field(default=None, ge=0, le=ITEM_FULL_SCORE)
    teacher_comment: str | None = Field(default=None, max_length=800)
    verdict: str | None = Field(default=None, pattern="^(correct|partial|wrong|unjudged)$")


@router.patch("/{submission_id}/items/{ord}")
def review_item(submission_id: str, ord: int, body: ReviewIn,
                actor: Actor = Depends(require("teacher"))) -> dict:
    row = _load(submission_id)
    _guard(actor, row, teacher_only=True)
    item = db.query_one(
        "SELECT * FROM submission_item WHERE submission_id = %s AND ord = %s",
        (submission_id, ord))
    if not item:
        raise HTTPException(404, f"没有第 {ord} 题")

    fields: dict[str, object] = {}
    if body.teacher_score is not None:
        fields["teacher_score"] = body.teacher_score
    if body.teacher_comment is not None:
        fields["teacher_comment"] = body.teacher_comment
    if body.verdict is not None:
        fields["auto_verdict"] = body.verdict
    if not fields:
        raise HTTPException(400, "没有要改的内容")
    fields["graded_by"] = actor.id
    # graded_at 用库里的 now()，不传 Python 时间：两处时钟不一致时，
    # 审计里"谁在什么时候改的"会前后矛盾。
    sets = ", ".join(f"{name} = %s" for name in fields) + ", graded_at = now()"
    db.execute(
        f"UPDATE submission_item SET {sets} WHERE submission_id = %s AND ord = %s",
        (*fields.values(), submission_id, ord),
    )
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'homework.review', 'submission', %s, %s::jsonb)""",
        (actor.id, submission_id,
         json.dumps({"ord": ord, **{k: v for k, v in fields.items()
                                    if k in ("teacher_score", "teacher_comment", "auto_verdict")}},
                    ensure_ascii=False)),
    )
    return {"submission_id": submission_id, "ord": ord, "changed": sorted(fields)}


@router.post("/{submission_id}/confirm")
def confirm(submission_id: str, actor: Actor = Depends(require("teacher"))) -> dict:
    """一键确认：把还没有教师分的题按 AI 分记下，状态转为教师已确认。

    这不是"替教师打分"，而是"教师看过并接受 AI 的建议"——所以只补空缺，
    不覆盖教师已经改过的题。
    """
    row = _load(submission_id)
    _guard(actor, row, teacher_only=True)
    count = db.query_one(
        """UPDATE submission_item
              SET teacher_score = auto_score, graded_by = %s, graded_at = now()
            WHERE submission_id = %s AND teacher_score IS NULL AND auto_score IS NOT NULL""",
        (actor.id, submission_id))
    db.execute("UPDATE submission SET status = 'teacher-confirmed' WHERE id = %s",
               (submission_id,))
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'homework.confirm', 'submission', %s, %s::jsonb)""",
        (actor.id, submission_id, json.dumps({"confirmed_by": actor.id}, ensure_ascii=False)),
    )
    return {"submission_id": submission_id, "status": "teacher-confirmed"}


@router.delete("/{submission_id}")
def delete_homework(submission_id: str, actor: Actor = Depends(require_login)) -> dict:
    row = _load(submission_id)
    _guard(actor, row)
    if row["status"] == "teacher-confirmed" and not actor.has("admin"):
        raise HTTPException(409, "教师已确认过这次作业，只有管理员能删")
    db.execute("DELETE FROM submission WHERE id = %s", (submission_id,))
    return {"deleted": submission_id}
