"""问答链路：检索 → 拼上下文 → 调模型 → 落库引用。

这个文件是三条已确认决定的落地处，而不只是"把问题转给模型"：

1. **分层（先引导，作答后给完整解答）**
   `mode=guide` 只给思路与追问；`mode=full` 要求该学生在这道题上已有作答记录，
   否则 403。这条靠接口拒绝保证，不靠提示词里写"请勿给出答案"——
   提示词是可以被套话绕过的，接口不行。

2. **只传必要片段**
   上下文有字符上限（`RAG_MAX_CHARS`，默认 6000），超出部分整段丢弃而不是截半句；
   每次外传的字符数写进 `model_call`，事后能查"某天到底传了多少教材原文出去"。

3. **教材版权**
   `copyright_status = 'internal-only'` 的教材，其原文片段不随外传端点发出。
   没有登记教材（`textbook` 表里没有对应行）时检索结果为空，直接拒答，
   而不是让模型自由发挥——这是"宁可说不知道"的具体实现。

   语料有两种来源（`textbook.source_kind`）：
   - `pdf`：教材原件切片，引用标页码；
   - `node`：知识点正文切块（见 qa_ingest.py），没有精确页，引用标知识点路径。
   两者共用这张表、这套引用、这条拒答规则；将来教材切片进来，pdf 那批自动
   有页码，node 那批继续兜底，问答代码不用再改。

检索退路：本机没有 pgvector，先用关键词匹配。上线应换成 embedding + 混合检索
（见《服务器部署与迁移指南》§3.2）。`retrieve()` 的签名保持稳定，换实现不动调用方。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db, llm, visibility
from .auth import Actor, require, require_login

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])

MAX_CONTEXT_CHARS = int(os.environ.get("RAG_MAX_CHARS", "6000"))
TOP_K = int(os.environ.get("RAG_TOP_K", "6"))
MIN_EXCERPT_CHARS = 120          # 剩余预算不足这么多字就不放这一段，免得只塞半句
PROMPT_VERSION = "qa-v1"

# 演示模式：没有 API Key 时也能把「检索 → 引用 → 落库」整条链路点通。
# 它明确标注自己不是真回答，并且在 APP_ENV=prod 下由 main.py 拒绝启动。
DEV_FAKE_MODEL = os.environ.get("DEV_FAKE_MODEL") == "1"


def fake_answer(mode: str, citations: list[dict[str, Any]]) -> str:
    first = citations[0] if citations else {}
    excerpt = (first.get("excerpt") or "")[:200]
    # 演示回答不猜内容，只把"引到的是哪一段"摆出来。以前这里写死了
    # 「先看定义里 ε 与 N 的关系」，换到最小二乘、特征值那些课就变成答非所问。
    hint = "先看引到的这一段怎么定义、条件是什么，再回到自己的问题" if mode == "guide" \
        else "完整推导见引到的片段（演示）"
    page = first.get("page")
    where = f"第 {page} 页" if page else f"知识点「{first.get('node_title') or '课程材料'}」"
    return (
        "【演示模式，不是模型输出】\n\n"
        f"{hint}。课程材料（{where}）：\n\n> {excerpt}\n\n"
        "配置好模型端点与 API Key 后，这条消息会换成真实回答。"
    )

GUIDE_SYSTEM = """你是武汉大学数学与统计学院的教学助手，面向本科生。

规则（必须遵守）：
1. 不给出最终答案，也不给出完整解题过程。只提供：思路提示、关键定义、
   第一步该做什么、常见错误。
2. 用反问引导学生自己往下走，一次只推进一小步。
3. 如果学生明确要求"直接给答案"，回答：先写出你的尝试，我再帮你检查。
4. 只依据提供的教材片段回答。片段里没有的内容，说"教材这部分我暂时没有依据"，
   不要凭印象补充公式或结论。
5. 数学式子用 $LaTeX$，行间公式用 $$LaTeX$$。
"""

FULL_SYSTEM = """你是武汉大学数学与统计学院的教学助手，面向本科生。

学生已经先提交了自己的作答，现在需要完整解答来对照。规则：
1. 给出完整、逐步的推导，并对学生的作答指出：哪一步对了、哪一步错了、为什么。
2. 只依据提供的教材片段。片段没有覆盖的部分，明确标注"教材未涉及，以下为补充"。
3. 数学式子用 $LaTeX$，行间公式用 $$LaTeX$$。
"""


FALLBACK_SYSTEM = """你是武汉大学数学与统计学院的教学助手，面向本科生。

这次课程材料里没有检索到能支撑回答的片段。规则：
1. 用你自己的通用数学知识回答，但你要清楚：这不是本课程材料的结论。
2. 不要编造教材章节、页码、定理编号，也不要写"书中第 X 页"这类出处；不确定就说不确定。
3. 有些记号/定义在不同教材里约定不同，遇到这种地方要指出来。
4. 学生要思路时别直接给完整答案；「完整解答」模式下可以给推导。
5. 数学式子用 $LaTeX$，行间公式用 $$LaTeX$$。
6. 不要在回答开头重复免责声明，系统会单独标注这一段没有教材依据。
"""

# 机器加的标注，不依赖模型是否照做。放在正文最前面，复制走也带着。
UNVERIFIED_BANNER = (
    "> ⚠️ **未使用课程材料**：课程知识库里没有找到相关依据，"
    "以下内容由模型自身的通用知识生成，不是本课程教材或大纲的结论，"
    "也没有经过教师审核，可能有误，请对照教材核对。\n\n"
)


def fallback_enabled() -> bool:
    """知识库没有依据时，是否允许改用模型的通用知识回答。

    默认开着：一个连"什么是勒贝格积分"都答不了的教学助手，学生会直接不用它。
    但必须在回答上标注，且 `QA_FALLBACK=off` 能一键回到严格模式（论文/答辩场合）。
    """
    return os.environ.get("QA_FALLBACK", "model").strip().lower() != "off"


def mark_unverified(text: str) -> str:
    """给没依据的回答加标注。幂等：模型自己写了也不重复加。"""
    body = (text or "").strip()
    if body.startswith("> ⚠️"):
        return body
    return UNVERIFIED_BANNER + body


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    course_id: str | None = None
    exercise_id: str | None = None
    mode: str = Field(default="guide", pattern="^(guide|full)$")
    conversation_id: str | None = None


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_WORD = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{1,}")


def _terms(text: str, limit: int = 10) -> list[str]:
    """把问题拆成检索词。

    中文没有空格，靠滑窗：
      · 2 字窗 —— 覆盖"极限、幂法、导数、上确界"这类术语的核心，
        步长 1 而不是 2，否则"上极限"里的"上极"会被漏掉；
      · 4 字窗 —— 长句里较具体的组合，命中它比命中两个字更能说明相关。
    排序上把长词放前面（截断时优先留长词），因为长词更具体。
    """
    terms: list[str] = []
    for word in _WORD.findall(text):
        if len(word) >= 2:
            terms.append(word.lower())
    for run in _CJK_RUN.findall(text):
        terms.extend(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) >= 4:
            terms.extend(run[i:i + 4] for i in range(len(run) - 3))
    # 不能先按长度排序再截断：一句 16 字的话能拆出十几个 4 字窗，
    # 排前面就把"消息""传递"这些 2 字词全挤掉了，于是只命中 1 个词、被证据门槛拒答。
    # 顺序保持"先 2 字窗后 4 字窗"，长度只用在 SQL 打分（长词权重更高）。
    seen: list[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return seen[:limit]


def _min_hits(terms: list[str]) -> int:
    """证据门槛：问题拆出 3 个以上词时，只命中一个词不算依据。"""
    return 2 if len(terms) >= 3 else 1


def retrieve(question: str, course_id: str | None, allow_internal: bool) -> list[dict[str, Any]]:
    """关键词检索课程片段。

    allow_internal=False 时排除 copyright_status='internal-only' 的教材——
    版权未确认的原文不随着外传端点发出去。

    排序两条规则（见模块顶部"检索排序"）：
      · 知识点（`node_kind='knowledge'`）排在章节容器之前——
        章节正文是子标题清单，命中词最多但答不了问题；
      · 同一个知识点只保留最相关的一块——否则一个切了四块的知识点
        会把整个 top-k 占满，等于只检索到一个知识点。
    """
    terms = _terms(question)
    if not terms:
        return []
    rows = db.query(
        """
        WITH scored AS (
            SELECT c.id AS chunk_id, c.page_from, c.page_to, c.section_path, c.content_md,
                   c.seq,
                   t.id AS textbook_id, t.title AS textbook_title, t.copyright_status,
                   t.source_kind,
                   k.id AS node_id, k.kind AS node_kind,
                   (SELECT count(*) FROM unnest(%s::text[]) AS term
                     WHERE c.content_md ILIKE '%%' || term || '%%') AS hits,
                   (SELECT coalesce(sum(length(term)), 0)
                      FROM unnest(%s::text[]) AS term
                     WHERE c.content_md ILIKE '%%' || term || '%%') AS weight
            FROM textbook_chunk c
            JOIN textbook t ON t.id = c.textbook_id
            LEFT JOIN node_chunk nc ON nc.chunk_id = c.id
            LEFT JOIN knowledge_node k
                   ON k.course_id = nc.course_id AND k.id = nc.node_id
            WHERE (%s::text IS NULL OR t.course_id = %s)
              AND (%s OR t.copyright_status <> 'internal-only')
              AND EXISTS (SELECT 1 FROM unnest(%s::text[]) AS term
                           WHERE c.content_md ILIKE '%%' || term || '%%')
        ), picked AS (
            SELECT DISTINCT ON (COALESCE(node_id, 'chunk-' || chunk_id::text)) *
            FROM scored
            ORDER BY COALESCE(node_id, 'chunk-' || chunk_id::text),
                     (node_kind = 'knowledge') DESC, weight DESC, hits DESC, chunk_id
        )
        SELECT * FROM picked
        WHERE hits >= %s
        ORDER BY (node_kind = 'knowledge') DESC, weight DESC, hits DESC, seq
        LIMIT %s
        """,
        (terms, terms, course_id, course_id, allow_internal, terms, _min_hits(terms), TOP_K),
    )
    return rows


def _where(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """片段的位置描述，以及要写进引用的字段。

    知识点语料**不编页码**：节点溯源里只有"书页197起"这种章节起页，把它当
    知识点所在页，等于给每条回答盖一个错章。宁可说"知识点：上极限"。
    """
    path = " / ".join(row.get("section_path") or [])
    if (row.get("source_kind") or "pdf") == "node":
        title = (row.get("section_path") or ["知识点"])[-1]
        where = f"知识点「{title}」" + (f"（{path}）" if path else "")
        return where, {"page": None, "node_id": row.get("node_id"), "node_title": title}

    if row["page_from"] is None:
        where = " ".join(part for part in (row.get("textbook_title"), path) if part)
        return where or "教材片段", {"page": None, "node_id": row.get("node_id"), "node_title": None}

    pages = (f"第 {row['page_from']} 页" if row["page_from"] == row["page_to"]
             else f"第 {row['page_from']}–{row['page_to']} 页")
    where = " ".join(part for part in (row.get("textbook_title"), path, pages) if part)
    return where, {"page": row["page_from"], "node_id": row.get("node_id"), "node_title": None}


def compose_context(rows: list[dict[str, Any]], budget: int = MAX_CONTEXT_CHARS) -> tuple[str, int, list[dict[str, Any]]]:
    """拼上下文，返回 (文本, 实际字符数, 引用列表)。

    预算是硬上限，宁可少给一段，也不把一段截成半句——半句公式比没有更误导。
    """
    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    used = 0
    for index, row in enumerate(rows, start=1):
        where, extra = _where(row)
        block = f"[{index}] {where}\n{row['content_md']}"
        remain = budget - used
        if len(block) > remain:
            if remain < MIN_EXCERPT_CHARS:
                break
            block = block[:remain]
        blocks.append(block)
        used += len(block)
        citation = {
            "ord": index,
            "chunk_id": row["chunk_id"],
            "section_path": row.get("section_path") or [],
            "textbook": row.get("textbook_title"),
            "kind": row.get("source_kind") or "pdf",
            "excerpt": (row["content_md"] or "")[:160],
        }
        citation.update(extra)
        citations.append(citation)
    return "\n\n".join(blocks), used, citations


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


def _conversation_id(raw: str | None, actor: Actor, course_id: str | None, question: str) -> str:
    if raw:
        try:
            parsed = uuid.UUID(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"conversation_id 不是合法 UUID：{raw}") from exc
        exists = db.query_one("SELECT id FROM ai_conversation WHERE id = %s", (str(parsed),))
        if not exists:
            raise HTTPException(status_code=404, detail=f"会话不存在：{raw}")
        return str(parsed)
    row = db.query_one(
        """INSERT INTO ai_conversation (course_id, user_ref, title)
           VALUES (%s, %s, %s) RETURNING id""",
        (course_id, actor.id, question[:60]),
    )
    return str(row["id"])


def _save_turn(conversation_id: str, question: str, answer: str,
               refused: bool, citations: list[dict[str, Any]],
               basis: str = "course") -> int:
    db.query(
        "INSERT INTO ai_message (conversation_id, role, content_md) VALUES (%s, 'user', %s)",
        (conversation_id, question),
    )
    message = db.query_one(
        """INSERT INTO ai_message (conversation_id, role, content_md, refused, basis)
           VALUES (%s, 'assistant', %s, %s, %s) RETURNING id""",
        (conversation_id, answer, refused, basis),
    )
    message_id = int(message["id"])
    for citation in citations:
        db.query(
            """INSERT INTO ai_citation (message_id, ord, chunk_id, course_id, node_id, page, excerpt)
               VALUES (%s, %s, %s, NULL, %s, %s, %s)""",
            (message_id, citation["ord"], citation["chunk_id"], citation.get("node_id"),
             citation.get("page"), citation["excerpt"]),
        )
    return message_id


def _record_call(*, succeeded: bool, provider: str, model: str, latency_ms: int,
                 error: str | None = None, context_chars: int = 0,
                 usage: dict[str, Any] | None = None) -> None:
    """每次外传都留一条记录：谁、多少字、成没成。决策 ③ 靠这个可审计。"""
    usage = usage or {}
    db.query(
        """INSERT INTO model_call (purpose, provider, model, prompt_tokens,
                                   completion_tokens, latency_ms, succeeded, error, prompt_version)
           VALUES ('chat', %s, %s, %s, %s, %s, %s, %s, %s)""",
        (provider, model, usage.get("prompt_tokens"), usage.get("completion_tokens"),
         latency_ms, succeeded, error, f"{PROMPT_VERSION}+{context_chars}chars"),
    )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.post("/ask")
def ask(body: AskIn, actor: Actor = Depends(require_login)) -> dict:
    if body.mode == "full":
        _require_attempt(actor, body)
        _require_tier(body)

    endpoint = _chat_endpoint_or_400()
    # 只有地址真的在校园网内，才允许把「仅校内使用」的教材原文放进上下文。
    # classify 的取值是 internal / approved / unapproved / invalid——
    # approved 表示"出校但供应商已批准"，仍然属于外传。
    allow_internal = llm.classify(endpoint.base_url) == "internal"

    rows = retrieve(body.question, body.course_id, allow_internal)
    conversation_id = _conversation_id(body.conversation_id, actor, body.course_id, body.question)

    if not rows:
        if not fallback_enabled():
            answer = (
                "这个问题我在课程材料里没有找到可以引用的依据，所以不猜。\n\n"
                "可能的原因：这门课的知识点还没有入库成问答语料（管理员可在"
                "「问答语料」里重建），或者问题里的说法与知识点标题差得太远。\n"
                "你可以换用课程里的术语再问一次，比如直接问某个知识点是什么。"
            )
            message_id = _save_turn(conversation_id, body.question, answer, True, [])
            return {
                "conversation_id": conversation_id, "message_id": message_id,
                "answer_md": answer, "refused": True, "mode": body.mode,
                "citations": [], "context_chars": 0, "grounded": True, "basis": "course",
            }

        # 兜底：课程材料里没有依据，改用模型自身的通用知识，并在回答正文与接口字段上双重标注。
        # 注意这里**不发任何课程内容**，只发问题——比正常检索路径外传得还少。
        if DEV_FAKE_MODEL:
            answer = mark_unverified(
                "【演示模式，不是模型输出】\n\n"
                "课程材料里没有找到可以引用的片段。配好模型端点后，这里会换成模型用通用知识给出的回答，"
                "并带着同样的标注。"
            )
            _record_call(succeeded=True, provider="demo", model="demo",
                         latency_ms=0, context_chars=0)
            message_id = _save_turn(conversation_id, body.question, answer, False, [], "model")
            return {
                "conversation_id": conversation_id, "message_id": message_id,
                "answer_md": answer, "refused": False, "mode": body.mode,
                "citations": [], "context_chars": 0, "demo": True,
                "grounded": False, "basis": "model",
            }

        result, answer, latency_ms = _call_model(
            [{"role": "system", "content": FALLBACK_SYSTEM},
             {"role": "user", "content": body.question}],
            endpoint, 0)
        answer = mark_unverified(answer)
        message_id = _save_turn(conversation_id, body.question, answer, False, [], "model")
        return {
            "conversation_id": conversation_id, "message_id": message_id,
            "answer_md": answer, "refused": False, "mode": body.mode,
            "citations": [], "context_chars": 0,
            "grounded": False, "basis": "model",
        }

    context, used, citations = compose_context(rows)
    system = GUIDE_SYSTEM if body.mode == "guide" else FULL_SYSTEM
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"教材片段：\n\n{context}\n\n学生的问题：{body.question}"},
    ]

    if DEV_FAKE_MODEL:
        answer = fake_answer(body.mode, citations)
        _record_call(succeeded=True, provider="demo", model="demo",
                     latency_ms=0, context_chars=used)
        message_id = _save_turn(conversation_id, body.question, answer, False, citations)
        return {
            "conversation_id": conversation_id, "message_id": message_id,
            "answer_md": answer, "refused": False, "mode": body.mode,
            "citations": citations, "context_chars": used, "demo": True,
            "grounded": True, "basis": "course",
        }

    result, answer, latency_ms = _call_model(messages, endpoint, used)
    message_id = _save_turn(conversation_id, body.question, answer, False, citations)
    return {
        "conversation_id": conversation_id, "message_id": message_id,
        "answer_md": answer, "refused": False, "mode": body.mode,
        "citations": citations, "context_chars": used,
        "grounded": True, "basis": "course",
    }


def _call_model(messages: list[dict[str, str]], endpoint: llm.Endpoint,
                context_chars: int) -> tuple[dict[str, Any], str, int]:
    """调用模型并把各类失败翻译成对用户有意义的 HTTP 错误。

    抽出来是因为"有依据"和"没依据"两条路都要走同一套错误处理，
    复制两份迟早会出现一条路漏了 MissingKey 的 400。
    """
    started = time.monotonic()
    try:
        result = llm.chat(messages)
    except llm.EgressViolation as exc:
        _record_call(succeeded=False, provider=endpoint.name, model=endpoint.model,
                     latency_ms=int((time.monotonic() - started) * 1000),
                     error=str(exc), context_chars=context_chars)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except llm.MissingKey as exc:
        # 配置问题（没配密钥）不是上游故障：回 400，别让人以为是模型服务挂了
        _record_call(succeeded=False, provider=endpoint.name, model=endpoint.model,
                     latency_ms=int((time.monotonic() - started) * 1000),
                     error=str(exc)[:400], context_chars=context_chars)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        _record_call(succeeded=False, provider=endpoint.name, model=endpoint.model,
                     latency_ms=int((time.monotonic() - started) * 1000),
                     error="timeout", context_chars=context_chars)
        raise HTTPException(status_code=504, detail="模型响应超时，请稍后重试") from exc
    except RuntimeError as exc:
        _record_call(succeeded=False, provider=endpoint.name, model=endpoint.model,
                     latency_ms=int((time.monotonic() - started) * 1000),
                     error=str(exc)[:400], context_chars=context_chars)
        raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    answer = (result.get("content") or "").strip()
    if not answer:
        _record_call(succeeded=False, provider=endpoint.name, model=endpoint.model,
                     latency_ms=latency_ms, error="empty-completion",
                     context_chars=context_chars)
        raise HTTPException(status_code=502, detail="模型返回了空内容")

    _record_call(succeeded=True, provider=result.get("provider", endpoint.name),
                 model=result.get("model", endpoint.model), latency_ms=latency_ms,
                 context_chars=context_chars, usage=result.get("usage"))
    return result, answer, latency_ms


def _require_tier(body: AskIn) -> None:
    """这道题得在"学生可见范围"允许的验证等级里，才谈得上完整解答。

    和作答解锁是两件事：作答解锁回答"这个学生有没有做过"，
    验证等级回答"这道题本身够不够可信"。后者是学校口径，前者是学生行为。
    """
    policy = visibility.get_policy()
    tiers = visibility.allowed(policy["qa_tier"])
    row = db.query_one(
        "SELECT tier FROM v_exercise_verification WHERE course_id = %s AND id = %s",
        (body.course_id, body.exercise_id),
    )
    if row is None:
        return
    if row["tier"] not in tiers:
        raise HTTPException(
            status_code=403,
            detail=(
                f"这道题目前的验证等级是「{row['tier']}」，不在学生可见范围"
                f"（{visibility.TIER_LABELS.get(policy['qa_tier'], policy['qa_tier'])}）内，"
                "所以不提供完整解答。可以先用引导模式问思路。"
            ),
        )


def _require_attempt(actor: Actor, body: AskIn) -> None:
    """完整解答的解锁条件：这道题上真的有作答记录。

    不是"打开过页面"，是"写过东西并提交"。这样批量刷题也绕不过去。
    """
    if not body.course_id or not body.exercise_id:
        raise HTTPException(status_code=400, detail="mode=full 需要同时给出 course_id 与 exercise_id")
    row = db.query_one(
        """SELECT 1 FROM exercise_attempt
           WHERE student_id = %s AND course_id = %s AND exercise_id = %s
             AND coalesce(length(answer_md), 0) > 0
           LIMIT 1""",
        (actor.id, body.course_id, body.exercise_id),
    )
    if not row:
        raise HTTPException(
            status_code=403,
            detail="先提交你的作答，再解锁完整解答。当前这道题还没有你的作答记录。",
        )


def _chat_endpoint_or_400() -> llm.Endpoint:
    try:
        return llm.chat_endpoint()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"对话端点未配置：{exc}") from exc
