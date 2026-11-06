"""学生卷、答案卷、量规卷的分离渲染与落盘（§13.1、§14.1、§10.10）。

三份产物不是"同一页加一个 CSS class"，而是三条独立的数据路径：

  * **学生卷**：输入只能由 `build_student_projection` 从白名单键组装成
    `StudentPaperProjection`。答案/解析/量规/answer_test/隐藏批注在**组装之前**
    就被排除（不是模板不读，也不是 CSS 隐藏）。落盘前再跑一次
    `assert_student_safe`，并用 `assert_student_output_safe` 按内容扫描渲染结果，
    防止模板出错时把答案带进 PDF/HTML。
  * **答案卷**：题干 + 权威答案（`exercise.answer_md`）+ 解析（如果
    `display_config.solution` 有值）；`contains_solution=true`。
  * **量规卷**：题干 + 答案要点 + 分值 + 题库里已录入的步骤级量规
    （`display_config.rubric`）。题库没有步骤分时**明确写出"未录入"**，不编造。

输出格式：本仓库没有 PDF 生成依赖（`server/app/pdf.py` 只做解析：pypdf/pdftotext），
所以默认 renderer 产出可打印 HTML，并在文件里显式写
`data-pdf-implemented="false"` 与 `PDF_NOTICE`。`PaperRenderService` 的 renderer
是注入的，将来接 WeasyPrint 只需换一个 callable，服务/权限/落盘逻辑不变。

持久化：
  * 文件 → `ObjectStore`（生产为 `LocalObjectStore`，内容寻址 + 原子替换）；
  * 文件凭证 → `agent.artifact` / `agent.artifact_version`；
  * 组卷关系 → `assessment.paper_render`；
  * 三者写在同一 `db.transaction(assessment_service=True)` 里（paper_render 是
    FORCE RLS），重试靠 `(output_hash, media_kind, created_by)` 与
    `(paper_version_id, render_kind, output_hash)` 两个唯一键去重。

worker：`run_render_job` 只做渲染，不装配模型网关，也不能审核或发布。job 的
`requested_by` 是文本账号 id，经 `ref_uuid("account", ...)` 映射成 assessment 的
uuid；job 领取/心跳/失败重试沿用 `generation.py` 的 pipeline_job，不另起队列。
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping as AbcMapping
from collections.abc import Sequence as AbcSequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Callable, ContextManager, Iterator, Protocol

from pydantic import ValidationError

from . import db
from .artifacts import ArtifactRepository, ArtifactService, LocalObjectStore, ObjectStore
from .blueprints import READ_ACTION as PAPER_READ_ACTION
from .blueprints import CourseScopeAuthorizer, ref_uuid
from .contracts import (
    ArtifactMetadata,
    ArtifactVersion,
    PaperRenderRecord,
    RenderKind,
    StudentPaperProjection,
    StudentProjectionLeakError,
    StudentSlotProjection,
    assert_student_safe,
    canonical_hash,
    canonical_json,
)

# ---------------------------------------------------------------------------
# 版本与格式常量
# ---------------------------------------------------------------------------

TEMPLATE_VERSION = "paper-v1"
RENDERER_VERSION = "paper-html-v1"
LANGUAGE_CODE = "zh-CN"
MEDIA_KIND = "paper_render"
MIME_TYPE = "text/html; charset=utf-8"

# 没有 PDF 生成依赖时不要假装有。这个字符串会写进 worker result 与 API 响应，
# **不写进卷面**：学生卷和答案卷是要发给真人打印的，页脚出现"本仓库未安装
# WeasyPrint"这种开发说明既不该给学生看，也会泄露出实现细节。
PDF_IMPLEMENTED = False
PDF_NOTICE = (
    "渲染格式：可打印 HTML。PDF 渲染未实现"
    "（本仓库的 pdf.py 只做解析，未安装 WeasyPrint/reportlab 等生成库）。"
)

# 卷面页脚只放打印指引；机器可读的事实放在 <meta name="pdf-status"> 与
# data-pdf-implemented 属性里，接口响应另有 pdf_implemented 字段。
PRINT_NOTE = "本卷为可打印 HTML：请用 A4 纵向、100% 比例打印，勿缩放到一页。"

# font_manifest_hash 是 0027 的 NOT NULL 列。HTML 不嵌字体，字体由浏览器打印引擎
# 解析；这里固定"建议字体清单"的哈希，版式换字体时换 renderer_version/清单即可。
FONT_MANIFEST = (
    "Noto Serif CJK SC",
    "Noto Sans CJK SC",
    "Latin Modern Math",
    "SimSun",
)
FONT_MANIFEST_HASH = sha256(canonical_json(list(FONT_MANIFEST)).encode("utf-8")).hexdigest()

RENDER_KINDS: tuple[RenderKind, ...] = ("student", "answer", "rubric")
RENDER_ACTIONS = {kind: f"paper.render.{kind}" for kind in RENDER_KINDS}

# 学生投影的唯一字段白名单。顺序与计划书 §2 / 任务 6 的 STUDENT_KEYS 一致。
STUDENT_KEYS = (
    "slot_id",
    "question_no",
    "statement_ast",
    "public_assets",
    "response_kind",
    "answer_space_lines",
)

# 渲染输入里允许出现的公开资产 URL：只允许 https 与站内相对路径。
_SAFE_URL_RE = re.compile(r"^(https://|/(?!/)|\./)")


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class RenderError(Exception):
    """渲染域错误基类。"""


class PaperRenderNotFoundError(RenderError, LookupError):
    """paper_version 或 render 行不存在（HTTP 404）。"""


class PaperRenderWithdrawnError(RenderError, LookupError):
    """撤回的卷子不再允许渲染/下载（HTTP 410）。"""


class PaperRenderInputError(RenderError, ValueError):
    """输入不足或格式非法（HTTP 422）。"""


class PaperRenderIncompleteError(PaperRenderInputError):
    """卷面还有未选题的槽位：打印出来会是一道空白题。"""


class PaperRenderIntegrityError(RenderError):
    """落盘字节与登记的 output_hash 不一致。"""


# ---------------------------------------------------------------------------
# 领域输入（服务间冻结 dataclass；不是学生投影 DTO）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderSlot:
    slot_id: Any
    code: str
    slot_no: int
    question_no: str
    statement_md: str
    answer_md: str | None
    score: Decimal
    source_no: str | None = None
    difficulty: int | None = None
    display_config: AbcMapping[str, object] = field(default_factory=dict)
    empty: bool = False


@dataclass(frozen=True)
class RenderSection:
    code: str
    title: str
    instruction: str
    sort_order: int
    slots: tuple[RenderSlot, ...]


@dataclass(frozen=True)
class RenderSubject:
    paper_version_id: Any
    paper_id: Any
    course_id: Any
    course_ref: str
    offering_id: Any
    offering_ref: str | None
    release_context_id: Any
    course_title: str
    version_short_code: str
    status: str
    manifest_hash: str
    paper_kind: str
    total_score: Decimal
    duration_seconds: int
    instructions: str
    sections: tuple[RenderSection, ...]

    def slots(self) -> tuple[RenderSlot, ...]:
        return tuple(slot for section in self.sections for slot in section.slots)


@dataclass(frozen=True)
class PaperRenderOutcome:
    """一次渲染的结果：artifact 凭证 + paper_render 行 + 实际字节。"""

    artifact: ArtifactVersion
    render: PaperRenderRecord
    body: bytes


# ---------------------------------------------------------------------------
# 学生投影：显式白名单，不是"整行减字段"
# ---------------------------------------------------------------------------

def build_student_projection(
    paper_version_id: Any,
    short_code: str,
    course_title: str,
    instructions: str,
    rows: list[AbcMapping[str, object]],
) -> StudentPaperProjection:
    """把题目行组装成学生卷 DTO。

    只从 `STUDENT_KEYS` 取键：行里即使带 `answer` / `solution` / `rubric` /
    `answer_test` / `hidden_note`，也永远不会进入投影。取完后再由
    `StudentPaperProjection` 的 `extra="forbid"` 与递归扫描做两道复核。
    """
    slots: list[StudentSlotProjection] = []
    for index, row in enumerate(rows):
        missing = [key for key in STUDENT_KEYS if key not in row]
        if missing:
            raise PaperRenderInputError(
                f"第 {index + 1} 行缺少学生投影字段：{'、'.join(missing)}"
            )
        payload = {key: row[key] for key in STUDENT_KEYS}
        assets = payload.get("public_assets") or ()
        if isinstance(assets, (str, bytes)) or not isinstance(assets, AbcSequence):
            raise PaperRenderInputError("public_assets 必须是数组")
        payload["public_assets"] = tuple(assets)
        # 先扫自由字典（statement_ast / public_assets），再交给模型锁字段：
        # Pydantic 会把 model_validator 里的 ValueError 包成 ValidationError，
        # 直接扫 payload 才能让调用方拿到明确的 StudentProjectionLeakError。
        assert_student_safe(payload)
        try:
            slots.append(StudentSlotProjection(**payload))  # type: ignore[arg-type]
        except StudentProjectionLeakError:
            raise
        except (ValidationError, TypeError, ValueError) as exc:
            raise PaperRenderInputError(
                f"第 {index + 1} 行学生投影不合法：{exc}"
            ) from exc
    projection = StudentPaperProjection(
        paper_version_id=paper_version_id,
        version_short_code=short_code,
        course_title=course_title,
        instructions=instructions,
        slots=tuple(slots),
    )
    # 第三次复核：模型构造时的 validator 已经跑过，这里在"即将交给模板"的边界
    # 再显式调用一次，避免有人绕过 validator 直接构造 payload。
    assert_student_safe(projection.model_dump(mode="python"))
    return projection


def student_rows(subject: RenderSubject) -> list[dict[str, object]]:
    """从领域输入生成白名单行；这是唯一进入学生投影的数据路径。"""
    rows: list[dict[str, object]] = []
    for section in subject.sections:
        for slot in section.slots:
            config = slot.display_config or {}
            assets = config.get("public_assets") or ()
            if isinstance(assets, (str, bytes)) or not isinstance(assets, AbcSequence):
                raise PaperRenderInputError(
                    f"槽位 {slot.question_no} 的 public_assets 不是数组"
                )
            response_kind = config.get("response_kind")
            if not isinstance(response_kind, str) or not response_kind.strip():
                response_kind = "written"
            lines = config.get("answer_space_lines")
            if isinstance(lines, bool) or not isinstance(lines, int):
                lines = _default_answer_space(slot.score)
            rows.append({
                "slot_id": slot.slot_id,
                "question_no": slot.question_no,
                "statement_ast": {
                    "format": "markdown",
                    "text": slot.statement_md,
                },
                "public_assets": tuple(assets),
                "response_kind": response_kind,
                "answer_space_lines": max(0, min(200, lines)),
            })
    return rows


def _default_answer_space(score: Decimal) -> int:
    """按分值给作答区行数：只是版式，不是评分标准。"""
    value = Decimal(score)
    if value <= 5:
        return 6
    if value <= 10:
        return 12
    return 16


# ---------------------------------------------------------------------------
# 渲染上下文（答案/量规；学生上下文由投影生成）
# ---------------------------------------------------------------------------

def _presentation(subject: RenderSubject, kind: RenderKind) -> dict[str, object]:
    return {
        "schema_version": "paper-render/v1",
        "render_kind": kind,
        "paper_version_id": str(subject.paper_version_id),
        "version_short_code": subject.version_short_code,
        "course_title": subject.course_title,
        "paper_kind": subject.paper_kind,
        "manifest_hash": subject.manifest_hash,
        "language_code": LANGUAGE_CODE,
        "pdf_implemented": PDF_IMPLEMENTED,
        "pdf_notice": PDF_NOTICE,
        "template_version": TEMPLATE_VERSION,
        "renderer_version": RENDERER_VERSION,
    }


def _answer_context(subject: RenderSubject) -> dict[str, object]:
    sections = []
    for section in subject.sections:
        slots = []
        for slot in section.slots:
            config = slot.display_config or {}
            solution = config.get("solution")
            solution_md = solution.strip() if isinstance(solution, str) else ""
            slots.append({
                "question_no": slot.question_no,
                "statement_md": slot.statement_md,
                "answer_md": (slot.answer_md or "").strip(),
                "has_answer": bool((slot.answer_md or "").strip()),
                "solution_md": solution_md,
                "has_solution": bool(solution_md),
                "score": _fmt_score(slot.score),
                "source_no": slot.source_no,
                "difficulty": slot.difficulty,
            })
        sections.append({
            "code": section.code,
            "title": section.title,
            "instruction": section.instruction,
            "slots": slots,
        })
    return {
        **_presentation(subject, "answer"),
        "total_score": _fmt_score(subject.total_score),
        "duration_seconds": subject.duration_seconds,
        "sections": sections,
    }


def _rubric_context(subject: RenderSubject) -> dict[str, object]:
    sections = []
    for section in subject.sections:
        slots = []
        for slot in section.slots:
            config = slot.display_config or {}
            rubric = config.get("rubric")
            items = _rubric_items(rubric)
            slots.append({
                "question_no": slot.question_no,
                "statement_md": slot.statement_md,
                "answer_md": (slot.answer_md or "").strip(),
                "has_answer": bool((slot.answer_md or "").strip()),
                "rubric": rubric,
                "rubric_items": items,
                "has_rubric": bool(items),
                "score": _fmt_score(slot.score),
                "source_no": slot.source_no,
            })
        sections.append({
            "code": section.code,
            "title": section.title,
            "instruction": section.instruction,
            "slots": slots,
        })
    return {
        **_presentation(subject, "rubric"),
        "total_score": _fmt_score(subject.total_score),
        "duration_seconds": subject.duration_seconds,
        "sections": sections,
    }


def _rubric_items(value: object) -> list[dict[str, object]]:
    """把 `display_config.rubric` 归一成可打印的评分点列表。

    题库没有统一的量规 schema（规格 §10.12 说量规复用 exercise 领域，但本仓库
    只有 `assignment.rubric` 这个历史列，没有 per-exercise rubric 表）。所以这里
    接受三种现实写法：`{"steps": [...]}`、纯数组、或任意 dict；不认识的形状转成
    键值文本，**不补造步骤分**。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [{"criterion": text, "score": None}] if text else []
    if isinstance(value, AbcMapping):
        steps = value.get("steps")
        if isinstance(steps, AbcSequence) and not isinstance(steps, (str, bytes)):
            return _rubric_items(steps)
        if "criterion" in value or "desc" in value or "text" in value:
            return [{
                "criterion": str(value.get("criterion") or value.get("desc")
                                 or value.get("text") or ""),
                "score": value.get("score"),
            }]
        items = []
        for key, item in value.items():
            if isinstance(item, (AbcMapping, AbcSequence)) and not isinstance(item, (str, bytes)):
                text = json.dumps(item, ensure_ascii=False, default=str)
            else:
                text = str(item)
            items.append({"criterion": f"{key}：{text}", "score": None})
        return items
    if isinstance(value, AbcSequence) and not isinstance(value, (str, bytes)):
        items = []
        for item in value:
            if isinstance(item, AbcMapping):
                items.append({
                    "criterion": str(item.get("criterion") or item.get("desc")
                                     or item.get("text") or ""),
                    "score": item.get("score"),
                })
            else:
                items.append({"criterion": str(item), "score": None})
        return items
    return [{"criterion": str(value), "score": None}]


def _collect_rubric_text(value: object, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, AbcMapping):
        for item in value.values():
            _collect_rubric_text(item, out)
    elif isinstance(value, AbcSequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _collect_rubric_text(item, out)


# ---------------------------------------------------------------------------
# 学生卷产物内容级守卫
# ---------------------------------------------------------------------------

def _normalise_for_scan(value: str) -> str:
    """去标签、反转义、去空白：用于比较"意思相同但排版不同"的泄露。"""
    text = html.unescape(value)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", "", text)


def assert_student_output_safe(body: bytes, subject: RenderSubject) -> None:
    """按内容（不是字段名）确认学生卷产物没有答案/解析/量规文本。

    这是 `assert_student_safe` 的补充：后者只能扫描投影里的键，模板如果从别处
    （比如全局变量、错误的 context）拿到答案，键名是对的、内容却泄了。这里把
    每个槽位的答案、解析、量规文本归一化后到产物里找；与题干相同的文本跳过，
    避免"写出定义"这类题被误报。
    """
    text = body.decode("utf-8", "replace")
    haystack = _normalise_for_scan(text)
    for slot in subject.slots():
        statement = _normalise_for_scan(slot.statement_md or "")
        secrets: list[str] = [slot.answer_md or ""]
        config = slot.display_config or {}
        _collect_rubric_text(config.get("rubric"), secrets)
        _collect_rubric_text(config.get("solution"), secrets)
        for secret in secrets:
            needle = _normalise_for_scan(secret)
            if len(needle) < 6:
                # 太短（"1/3"、"同上"）做子串匹配只会误报，交给投影白名单兜底
                continue
            if needle in statement:
                continue
            if needle in haystack:
                raise StudentProjectionLeakError(
                    f"学生卷产物包含答案/量规文本：槽位 {slot.question_no}"
                )


# ---------------------------------------------------------------------------
# HTML 渲染器（默认实现；可被测试/未来 PDF renderer 替换）
# ---------------------------------------------------------------------------

class PaperRenderer(Protocol):
    def __call__(self, *, template: str, kind: RenderKind,
                 context: AbcMapping[str, object]) -> bytes: ...


_CSS = """
:root { color-scheme: light; }
@page {
  size: A4;
  margin: 18mm 16mm 20mm;
  @bottom-center { content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
                   font-size: 9pt; color: #555; }
}
* { box-sizing: border-box; }
html, body { background: #fff; }
body.paper {
  font-family: "Noto Serif CJK SC", "Songti SC", "SimSun", serif;
  color: #111; line-height: 1.65; max-width: 190mm; margin: 0 auto;
  padding: 10mm 6mm; font-size: 11pt;
}
h1, h2, h3, .paper-head { font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
.paper-head { border-bottom: 2.5px solid #111; padding-bottom: 6px; margin-bottom: 10px; }
.paper-head .course { font-size: 12pt; letter-spacing: .08em; }
.paper-head .title { font-size: 20pt; font-weight: 700; margin: 2px 0; }
.paper-head .meta { font-size: 9.5pt; color: #333; }
.instructions { background: #f3f3f3; border-left: 4px solid #555; padding: 6px 10px; }
.section { margin-top: 14px; }
.section > h2 { font-size: 13pt; border-bottom: 1px solid #999; padding-bottom: 2px; }
.section-instruction { color: #444; font-size: 10pt; }
.slot { margin: 10px 0 16px; page-break-inside: avoid; }
.slot-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.qno { font-weight: 700; font-size: 12pt; }
.score, .response { color: #444; font-size: 9.5pt; }
.statement p { margin: 4px 0; }
.answer-space {
  border: 1px dashed #999; margin: 8px 0 0;
  background-image: repeating-linear-gradient(to bottom, transparent 0 5.6mm, #e6e6e6 5.6mm 5.8mm);
}
.asset { margin: 8px 0; text-align: center; }
.asset img { max-width: 100%; max-height: 90mm; }
.asset figcaption { color: #555; font-size: 9pt; }
.answer-block, .solution-block, .rubric-block {
  border: 1px solid #bbb; border-radius: 3px; padding: 8px 10px; margin: 8px 0;
  page-break-inside: avoid;
}
.answer-block { background: #f7f9fc; }
.solution-block { background: #fbf7f0; }
.rubric-block { background: #f6f6f6; }
.answer-block h3, .solution-block h3, .rubric-block h3 { margin: 0 0 4px; font-size: 11pt; }
.source { color: #555; font-size: 9pt; }
table.rubric-table { width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 10.5pt; }
table.rubric-table th, table.rubric-table td { border: 1px solid #999; padding: 4px 6px; vertical-align: top; }
table.rubric-table th { background: #e9e9e9; text-align: left; }
.sensitive-banner {
  background: #7a0b0b; color: #fff; font-weight: 700; letter-spacing: .12em;
  padding: 6px 10px; margin: 0 0 10px;
}
.render-note {
  margin-top: 18px; font-size: 8.8pt; color: #555;
  border-top: 1px solid #ccc; padding-top: 6px;
}
.paper-foot { margin-top: 18px; font-size: 9pt; color: #444; }
.empty { color: #888; }
@media print { body.paper { padding: 0; } .no-print { display: none; } }
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_score(value: object) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:  # noqa: BLE001 — 显示层不因脏数据抛错
        return _esc(value)


def _minutes(seconds: object) -> int:
    try:
        return max(1, round(int(seconds) / 60))
    except Exception:  # noqa: BLE001
        return 1


def _paragraphs(value: object, *, empty_text: str = "（空）") -> str:
    raw = "" if value is None else str(value)
    stripped = raw.strip()
    if not stripped:
        return f'<p class="empty">{_esc(empty_text)}</p>'
    escaped = html.escape(stripped, quote=False)
    chunks = [chunk for chunk in re.split(r"\n\s*\n", escaped) if chunk.strip()]
    return "".join(
        "<p>" + chunk.replace("\n", "<br>") + "</p>" for chunk in chunks
    )


def _statement_text(statement_ast: object) -> str:
    if not isinstance(statement_ast, AbcMapping):
        raise PaperRenderInputError("statement_ast 必须是对象")
    text = statement_ast.get("text")
    if not isinstance(text, str):
        raise PaperRenderInputError("statement_ast 缺少字符串 text")
    return text


def _safe_asset_url(url: object) -> str:
    text = "" if url is None else str(url).strip()
    if not text or not _SAFE_URL_RE.match(text):
        raise PaperRenderInputError(f"公开资产 URL 只允许 https 或站内相对路径：{text[:80]!r}")
    return text


def _asset_html(asset: object) -> str:
    if not isinstance(asset, AbcMapping):
        raise PaperRenderInputError("公开资产必须是对象")
    url = _safe_asset_url(asset.get("url"))
    alt = asset.get("alt_text") or ""
    caption = f"<figcaption>{_esc(alt)}</figcaption>" if alt else ""
    return (
        f'<figure class="asset"><img src="{_esc(url)}" '
        f'alt="{_esc(alt)}" loading="lazy">{caption}</figure>'
    )


_RESPONSE_LABELS = {
    "proof": "证明",
    "calculation": "计算",
    "written": "书面作答",
    "short_answer": "简答",
    "choice": "选择",
}


def _response_label(value: object) -> str:
    key = str(value or "written")
    return _RESPONSE_LABELS.get(key, key)


def _document(*, title: str, kind: RenderKind, context: AbcMapping[str, object],
              body: str) -> bytes:
    manifest = _esc(context.get("manifest_hash") or "")
    short = _esc(context.get("version_short_code") or "")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f'<meta name="paper-render-kind" content="{_esc(kind)}">\n'
        f'<meta name="paper-version-short-code" content="{short}">\n'
        f'<meta name="paper-manifest-hash" content="{manifest}">\n'
        f'<meta name="template-version" content="{TEMPLATE_VERSION}">\n'
        f'<meta name="renderer-version" content="{RENDERER_VERSION}">\n'
        f'<meta name="font-manifest-hash" content="{FONT_MANIFEST_HASH}">\n'
        '<meta name="pdf-status" content="not-implemented">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        f'<body class="paper paper-{_esc(kind)}" data-pdf-implemented="false">\n'
        f"{body}\n"
        "</body>\n</html>\n"
    ).encode("utf-8")


def _header(context: AbcMapping[str, object], label: str, *, sensitive: bool) -> str:
    course = _esc(context.get("course_title"))
    short = _esc(context.get("version_short_code"))
    bits = [f"版本 {short}"]
    if context.get("total_score") not in (None, ""):
        bits.append(f"满分 {_fmt_score(context.get('total_score'))} 分")
    if context.get("duration_seconds"):
        bits.append(f"时长 {_minutes(context.get('duration_seconds'))} 分钟")
    banner = (
        '<div class="sensitive-banner">保密卷 · 含答案/评分标准 · 仅授权教师与复核人可见</div>'
        if sensitive else ""
    )
    return (
        f"{banner}"
        '<header class="paper-head">'
        f'<div class="course">{course}</div>'
        f'<div class="title">{_esc(label)}</div>'
        f'<div class="meta">{" · ".join(bits)}</div>'
        "</header>\n"
    )


def _footer(kind: RenderKind) -> str:
    if kind == "student":
        note = "学生卷 · 仅授权教师考前导出；学生在线可见不等于可下载。"
    elif kind == "answer":
        note = "答案卷 · 独立保密权限；导出会留下访问审计。"
    else:
        note = "量规卷 · 教师/复核教师权限；含答案与评分标准。"
    return (
        f'<footer class="paper-foot"><p>{_esc(note)}</p>'
        f'<p class="render-note" data-pdf-implemented="false">{_esc(PRINT_NOTE)}</p>'
        "</footer>"
    )


def _render_student_html(context: AbcMapping[str, object]) -> bytes:
    slots = context.get("slots") or []
    if not isinstance(slots, AbcSequence):
        raise PaperRenderInputError("学生卷 slots 必须是数组")
    parts: list[str] = []
    for slot in slots:
        if not isinstance(slot, AbcMapping):
            raise PaperRenderInputError("学生卷槽位必须是对象")
        statement = _statement_text(slot.get("statement_ast"))
        assets = slot.get("public_assets") or ()
        if not isinstance(assets, AbcSequence):
            raise PaperRenderInputError("public_assets 必须是数组")
        lines = slot.get("answer_space_lines")
        if isinstance(lines, bool) or not isinstance(lines, int):
            lines = 8
        lines = max(0, min(200, lines))
        asset_html = "".join(_asset_html(asset) for asset in assets)
        parts.append(
            f'<article class="slot" data-slot-id="{_esc(slot.get("slot_id"))}">'
            '<div class="slot-head">'
            f'<span class="qno">{_esc(slot.get("question_no"))}</span>'
            f'<span class="response">作答方式：{_esc(_response_label(slot.get("response_kind")))}</span>'
            "</div>"
            f'<div class="statement">{_paragraphs(statement)}</div>'
            f"{asset_html}"
            f'<div class="answer-space" style="height:{lines * 6.2:.1f}mm" '
            f'aria-label="第 {_esc(slot.get("question_no"))} 题作答区"></div>'
            "</article>"
        )
    body = (
        _header(context, "学生卷", sensitive=False)
        + f'<p class="instructions">{_paragraphs(context.get("instructions"), empty_text="按题号作答。")}</p>'
        + '<main class="slots">' + "".join(parts) + "</main>"
        + _footer("student")
    )
    return _document(
        title=f"{context.get('course_title') or '试卷'} · 学生卷 · {context.get('version_short_code') or ''}",
        kind="student", context=context, body=body,
    )


def _render_answer_html(context: AbcMapping[str, object]) -> bytes:
    sections = context.get("sections") or []
    if not isinstance(sections, AbcSequence):
        raise PaperRenderInputError("答案卷 sections 必须是数组")
    parts: list[str] = []
    for section in sections:
        if not isinstance(section, AbcMapping):
            raise PaperRenderInputError("答案卷分节必须是对象")
        slots_html: list[str] = []
        for slot in section.get("slots") or []:
            if not isinstance(slot, AbcMapping):
                raise PaperRenderInputError("答案卷槽位必须是对象")
            source = (
                f'<p class="source">题源：{_esc(slot.get("source_no"))}'
                + (f" · 难度 {_esc(slot.get('difficulty'))}/5" if slot.get("difficulty") else "")
                + "</p>"
                if slot.get("source_no") else ""
            )
            solution = (
                '<div class="solution-block"><h3>解析</h3>'
                f'{_paragraphs(slot.get("solution_md"), empty_text="（题库未提供单独解析）")}'
                "</div>"
                if slot.get("has_solution") else ""
            )
            slots_html.append(
                '<article class="slot">'
                '<div class="slot-head">'
                f'<span class="qno">{_esc(slot.get("question_no"))}</span>'
                f'<span class="score">（{_esc(slot.get("score"))} 分）</span>'
                "</div>"
                f'<div class="statement">{_paragraphs(slot.get("statement_md"))}</div>'
                '<div class="answer-block"><h3>答案</h3>'
                f'{_paragraphs(slot.get("answer_md"), empty_text="（题库未提供答案）")}</div>'
                f"{solution}{source}</article>"
            )
        parts.append(
            '<section class="section">'
            f'<h2>{_esc(section.get("code"))} {_esc(section.get("title"))}</h2>'
            f'<p class="section-instruction">{_esc(section.get("instruction"))}</p>'
            + "".join(slots_html) + "</section>"
        )
    body = (
        _header(context, "答案卷（保密）", sensitive=True)
        + '<main class="sections">' + "".join(parts) + "</main>"
        + _footer("answer")
    )
    return _document(
        title=f"{context.get('course_title') or '试卷'} · 答案卷 · {context.get('version_short_code') or ''}",
        kind="answer", context=context, body=body,
    )


def _render_rubric_html(context: AbcMapping[str, object]) -> bytes:
    sections = context.get("sections") or []
    if not isinstance(sections, AbcSequence):
        raise PaperRenderInputError("量规卷 sections 必须是数组")
    summary_rows: list[str] = []
    parts: list[str] = []
    for section in sections:
        if not isinstance(section, AbcMapping):
            raise PaperRenderInputError("量规卷分节必须是对象")
        slots_html: list[str] = []
        for slot in section.get("slots") or []:
            if not isinstance(slot, AbcMapping):
                raise PaperRenderInputError("量规卷槽位必须是对象")
            summary_rows.append(
                "<tr>"
                f'<td>{_esc(slot.get("question_no"))}</td>'
                f'<td>{_esc(slot.get("score"))}</td>'
                "</tr>"
            )
            items = slot.get("rubric_items") or []
            if items:
                rubric_html = (
                    '<div class="rubric-block"><h3>步骤分 / 评分标准</h3>'
                    '<table class="rubric-table"><thead><tr><th>评分点</th><th>分值</th></tr></thead><tbody>'
                    + "".join(
                        "<tr>"
                        f'<td>{_esc(item.get("criterion"))}</td>'
                        f'<td>{_esc(item.get("score") if item.get("score") is not None else "—")}</td>'
                        "</tr>"
                        for item in items if isinstance(item, AbcMapping)
                    )
                    + "</tbody></table></div>"
                )
            else:
                rubric_html = (
                    '<div class="rubric-block"><h3>步骤分 / 评分标准</h3>'
                    f'<p class="empty">本题未录入步骤级量规；当前只有分值 '
                    f'{_esc(slot.get("score"))} 分。不要据此伪造步骤分，需教师补齐。</p>'
                    "</div>"
                )
            slots_html.append(
                '<article class="slot">'
                '<div class="slot-head">'
                f'<span class="qno">{_esc(slot.get("question_no"))}</span>'
                f'<span class="score">（{_esc(slot.get("score"))} 分）</span>'
                "</div>"
                f'<div class="statement">{_paragraphs(slot.get("statement_md"))}</div>'
                '<div class="answer-block"><h3>答案要点</h3>'
                f'{_paragraphs(slot.get("answer_md"), empty_text="（题库未提供答案要点）")}</div>'
                f"{rubric_html}</article>"
            )
        parts.append(
            '<section class="section">'
            f'<h2>{_esc(section.get("code"))} {_esc(section.get("title"))}</h2>'
            + "".join(slots_html) + "</section>"
        )
    summary = (
        '<table class="rubric-table"><thead><tr><th>题号</th><th>分值</th></tr></thead>'
        "<tbody>" + "".join(summary_rows) + "</tbody></table>"
        f'<p class="score">合计：{_esc(context.get("total_score"))} 分</p>'
    )
    body = (
        _header(context, "量规卷（保密）", sensitive=True)
        + summary
        + '<main class="sections">' + "".join(parts) + "</main>"
        + _footer("rubric")
    )
    return _document(
        title=f"{context.get('course_title') or '试卷'} · 量规卷 · {context.get('version_short_code') or ''}",
        kind="rubric", context=context, body=body,
    )


def render_paper_html(*, template: str, kind: RenderKind,
                      context: AbcMapping[str, object]) -> bytes:
    """默认 renderer：可打印 HTML。

    不包含时间戳/随机 id：同一输入必须得到同一份字节，否则内容寻址与 worker
    重试去重都会失效（每次重试都生成新 artifact）。
    """
    if template != TEMPLATE_VERSION:
        raise PaperRenderInputError(f"未知模板版本：{template!r}")
    if kind == "student":
        return _render_student_html(context)
    if kind == "answer":
        return _render_answer_html(context)
    if kind == "rubric":
        return _render_rubric_html(context)
    raise PaperRenderInputError(f"未知 render_kind：{kind!r}")


# ---------------------------------------------------------------------------
# 授权端口
# ---------------------------------------------------------------------------

class PaperRenderAuthorizer:
    """HTTP 路径：教师必须在课程范围内；学生/答案/量规是三个独立动作。

    三个 `paper.render.*` 动作名分开，是为了让更细的 PDP（例如"答案卷只给命题组"）
    可以单独拒绝，而不用改服务层。当前生产实现都映射到既有
    `paper_blueprint.read`（课程范围教师/管理员），不复制第二套"谁教这门课"。
    """

    _ARTIFACT_ACTIONS = frozenset({"artifact.write", "artifact.read"})

    def __init__(self, actor: Any) -> None:
        self.actor = actor
        self._inner = CourseScopeAuthorizer(actor)

    def require(self, actor_id: Any, action: str, **scope: object) -> None:
        if action in self._ARTIFACT_ACTIONS:
            if self.actor.anonymous or not self.actor.has("teacher"):
                raise PermissionError("需要教师或管理员角色")
            return
        if action not in RENDER_ACTIONS.values():
            raise PermissionError(action)
        self._inner.require(actor_id, PAPER_READ_ACTION, **scope)

    def is_human(self, actor_id: Any) -> bool:
        return True


class ServiceRenderAuthorizer:
    """worker 服务账号：只允许 artifact 读写与三种 render 动作。

    worker 不能审核（`paper.review.*`）、不能发布（`assignment.publish`）、不能
    调用模型网关——这里直接把允许动作写成闭集，任何越界动作当场 PermissionError。
    """

    _ALLOWED = frozenset(
        {"artifact.write", "artifact.read", *RENDER_ACTIONS.values()}
    )

    def require(self, actor_id: Any, action: str, **scope: object) -> None:
        if action not in self._ALLOWED:
            raise PermissionError(action)

    def is_human(self, actor_id: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class PaperRenderUnitOfWork(Protocol):
    def load_subject(self, paper_version_id: Any) -> RenderSubject | None: ...
    def artifact_repository(self) -> ArtifactRepository: ...
    def add_render(self, record: PaperRenderRecord) -> PaperRenderRecord: ...
    def get_render(self, render_id: Any) -> PaperRenderRecord | None: ...
    def list_renders(self, paper_version_id: Any) -> tuple[PaperRenderRecord, ...]: ...


class PaperRenderRepository(Protocol):
    def unit_of_work(self) -> ContextManager[PaperRenderUnitOfWork]: ...


class InMemoryPaperRenderRepository:
    """测试仓储：与 Postgres 版同语义（含两个唯一键的去重）。"""

    def __init__(self, subject: RenderSubject | None = None) -> None:
        self.subject = subject
        self.renders: list[PaperRenderRecord] = []
        self._artifact_repository = InMemoryArtifactRepositoryAdapter()

    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryPaperRenderRepository"]:
        yield self

    def load_subject(self, paper_version_id: Any) -> RenderSubject | None:
        if self.subject is None:
            return None
        return self.subject if self.subject.paper_version_id == paper_version_id else None

    def artifact_repository(self) -> ArtifactRepository:
        return self._artifact_repository

    def add_render(self, record: PaperRenderRecord) -> PaperRenderRecord:
        for existing in self.renders:
            if (
                existing.paper_version_id == record.paper_version_id
                and existing.render_kind == record.render_kind
                and existing.output_hash == record.output_hash
            ):
                return existing
        self.renders.append(record)
        return record

    def get_render(self, render_id: Any) -> PaperRenderRecord | None:
        return next((row for row in self.renders if row.id == render_id), None)

    def list_renders(self, paper_version_id: Any) -> tuple[PaperRenderRecord, ...]:
        return tuple(
            row for row in self.renders if row.paper_version_id == paper_version_id
        )


class InMemoryArtifactRepositoryAdapter:
    """薄适配：复用 artifacts.InMemoryArtifactRepository，避免两份内存语义。"""

    def __init__(self) -> None:
        from .artifacts import InMemoryArtifactRepository

        self._inner = InMemoryArtifactRepository()

    @property
    def versions(self):  # type: ignore[no-untyped-def]
        return self._inner.versions

    @property
    def access_events(self):  # type: ignore[no-untyped-def]
        return self._inner.access_events

    def find_version(self, output_hash: str, media_kind: str, created_by: Any):
        return self._inner.find_version(output_hash, media_kind, created_by)

    def get_version(self, artifact_version_id: Any):
        return self._inner.get_version(artifact_version_id)

    def save_version(self, version: Any) -> None:
        self._inner.save_version(version)

    def record_access(self, event: Any) -> None:
        self._inner.record_access(event)


class PostgresPaperRenderRepository:
    """生产仓储：一次渲染的读、artifact 写、paper_render 写共用同一个事务。"""

    @contextmanager
    def unit_of_work(self) -> Iterator["_PostgresPaperRenderUnitOfWork"]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresPaperRenderUnitOfWork(cur)


_RENDER_COLUMNS = """id, paper_version_id, render_kind, paper_manifest_hash,
    template_version, language_code, artifact_version_id, renderer_version,
    font_manifest_hash, security_manifest_hash, output_hash, status,
    created_by, created_at"""

_ARTIFACT_VERSION_COLUMNS = """id, artifact_id, version_no, release_context_id,
    visibility, contains_solution, contains_answer_test, media_kind, mime_type,
    storage_uri, byte_size, output_hash, created_by, created_at"""


class _PostgresPaperRenderUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur
        self._artifact_repository = PostgresArtifactRepository(cur)

    # -- 读 ---------------------------------------------------------------
    def load_subject(self, paper_version_id: Any) -> RenderSubject | None:
        self.cur.execute(
            """SELECT pv.id, pv.paper_id, pv.version_no, pv.course_id, pv.offering_id,
                      pv.release_context_id, pv.paper_kind, pv.status, pv.manifest_hash,
                      pv.total_score, pv.duration_seconds,
                      rc.course_ref, rc.offering_ref,
                      coalesce(c.title, ir.ref, rc.course_ref, '未命名课程') AS course_title
                 FROM assessment.paper_version pv
                 LEFT JOIN assessment.release_context rc
                        ON rc.id = pv.release_context_id
                 LEFT JOIN assessment.id_ref ir
                        ON ir.subject = pv.course_id AND ir.kind = 'course'
                 LEFT JOIN public.course c ON c.id = rc.course_ref
                WHERE pv.id = %s""",
            (paper_version_id,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        course_ref = row.get("course_ref")
        if not course_ref:
            raise PaperRenderInputError(
                "卷面缺少可用的 Release Context 课程范围，不能渲染"
            )
        self.cur.execute(
            """SELECT id, code, title, instruction, sort_order
                 FROM assessment.paper_section
                WHERE paper_version_id = %s
                ORDER BY sort_order""",
            (paper_version_id,),
        )
        section_rows = self.cur.fetchall()
        if not section_rows:
            raise PaperRenderInputError("卷面没有任何分节，不能渲染")
        section_ids = [section["id"] for section in section_rows]
        self.cur.execute(
            """SELECT ps.id, ps.section_id, ps.code, ps.slot_no, ps.exercise_version_id,
                      ps.score, ps.display_config,
                      e.prompt_md, e.answer_md, e.source_no, e.difficulty
                 FROM assessment.paper_slot ps
                 LEFT JOIN assessment.release_item ri
                        ON ri.release_context_id = ps.release_context_id
                       AND ri.exercise_version_id = ps.exercise_version_id
                 LEFT JOIN public.exercise e
                        ON e.course_id = ri.course_ref AND e.id = ri.exercise_ref
                WHERE ps.section_id = ANY(%s)
                ORDER BY ps.slot_no""",
            (section_ids,),
        )
        slot_rows = self.cur.fetchall()
        slots_by_section: dict[Any, list[RenderSlot]] = {
            section_id: [] for section_id in section_ids
        }
        for slot_row in slot_rows:
            empty = slot_row.get("exercise_version_id") is None
            statement = (slot_row.get("prompt_md") or "").strip()
            if not empty and not statement:
                raise PaperRenderInputError(
                    f"槽位 {slot_row.get('code')} 找不到固定的题目内容"
                    "（release_item / exercise 可能已失效）"
                )
            config = _as_dict(slot_row.get("display_config"))
            slots_by_section[slot_row["section_id"]].append(RenderSlot(
                slot_id=slot_row["id"],
                code=slot_row["code"],
                slot_no=slot_row["slot_no"],
                question_no="",  # 下面按分节顺序统一编号
                statement_md=statement,
                answer_md=slot_row.get("answer_md"),
                score=Decimal(slot_row["score"]),
                source_no=slot_row.get("source_no"),
                difficulty=slot_row.get("difficulty"),
                display_config=config,
                empty=empty,
            ))
        manifest_hash = str(row["manifest_hash"])
        duration_seconds = int(row["duration_seconds"])
        total_score = Decimal(row["total_score"])
        instructions = (
            f"考试时长 {max(1, round(duration_seconds / 60))} 分钟，"
            f"满分 {total_score:.2f} 分。请按题号在答题区作答。"
        )
        sections: list[RenderSection] = []
        for index, section in enumerate(section_rows, start=1):
            numbered: list[RenderSlot] = []
            for slot in slots_by_section[section["id"]]:
                numbered.append(RenderSlot(
                    slot_id=slot.slot_id,
                    code=slot.code,
                    slot_no=slot.slot_no,
                    question_no=f"{index}.{slot.slot_no}",
                    statement_md=slot.statement_md,
                    answer_md=slot.answer_md,
                    score=slot.score,
                    source_no=slot.source_no,
                    difficulty=slot.difficulty,
                    display_config=slot.display_config,
                    empty=slot.empty,
                ))
            sections.append(RenderSection(
                code=section["code"],
                title=section["title"],
                instruction=section["instruction"],
                sort_order=section["sort_order"],
                slots=tuple(numbered),
            ))
        return RenderSubject(
            paper_version_id=row["id"],
            paper_id=row["paper_id"],
            course_id=row["course_id"],
            course_ref=str(course_ref),
            offering_id=row.get("offering_id"),
            offering_ref=row.get("offering_ref"),
            release_context_id=row["release_context_id"],
            course_title=row.get("course_title") or str(course_ref),
            version_short_code=manifest_hash[:8],
            status=row["status"],
            manifest_hash=manifest_hash,
            paper_kind=row["paper_kind"],
            total_score=total_score,
            duration_seconds=duration_seconds,
            instructions=instructions,
            sections=tuple(sections),
        )

    # -- 写 ---------------------------------------------------------------
    def artifact_repository(self) -> ArtifactRepository:
        return self._artifact_repository

    def add_render(self, record: PaperRenderRecord) -> PaperRenderRecord:
        self.cur.execute(
            f"""INSERT INTO assessment.paper_render ({_RENDER_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (paper_version_id, render_kind, output_hash) DO NOTHING
                RETURNING {_RENDER_COLUMNS}""",
            (
                record.id, record.paper_version_id, record.render_kind,
                record.paper_manifest_hash, record.template_version,
                record.language_code, record.artifact_version_id,
                record.renderer_version, record.font_manifest_hash,
                record.security_manifest_hash, record.output_hash, record.status,
                record.created_by, record.created_at,
            ),
        )
        row = self.cur.fetchone()
        if row:
            return _render_from_db(row)
        self.cur.execute(
            f"""SELECT {_RENDER_COLUMNS}
                  FROM assessment.paper_render
                 WHERE paper_version_id = %s AND render_kind = %s AND output_hash = %s""",
            (record.paper_version_id, record.render_kind, record.output_hash),
        )
        existing = self.cur.fetchone()
        if not existing:
            raise PaperRenderIntegrityError("paper_render 写入后读不回同一行")
        return _render_from_db(existing)

    def get_render(self, render_id: Any) -> PaperRenderRecord | None:
        self.cur.execute(
            f"SELECT {_RENDER_COLUMNS} FROM assessment.paper_render WHERE id = %s",
            (render_id,),
        )
        row = self.cur.fetchone()
        return _render_from_db(row) if row else None

    def list_renders(self, paper_version_id: Any) -> tuple[PaperRenderRecord, ...]:
        self.cur.execute(
            f"""SELECT {_RENDER_COLUMNS}
                  FROM assessment.paper_render
                 WHERE paper_version_id = %s
                 ORDER BY render_kind, created_at DESC""",
            (paper_version_id,),
        )
        return tuple(_render_from_db(row) for row in self.cur.fetchall())


class PostgresArtifactRepository:
    """`agent.artifact*` 的 Postgres 仓储，绑定渲染事务的同一个 cursor。"""

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def find_version(self, output_hash: str, media_kind: str,
                     created_by: Any) -> ArtifactVersion | None:
        self.cur.execute(
            f"""SELECT {_ARTIFACT_VERSION_COLUMNS}
                  FROM agent.artifact_version
                 WHERE output_hash = %s AND media_kind = %s AND created_by = %s""",
            (output_hash, media_kind, created_by),
        )
        row = self.cur.fetchone()
        return _artifact_from_db(row) if row else None

    def get_version(self, artifact_version_id: Any) -> ArtifactVersion:
        self.cur.execute(
            f"""SELECT {_ARTIFACT_VERSION_COLUMNS}
                  FROM agent.artifact_version WHERE id = %s""",
            (artifact_version_id,),
        )
        row = self.cur.fetchone()
        if not row:
            raise KeyError(f"artifact version not found: {artifact_version_id}")
        return _artifact_from_db(row)

    def save_version(self, version: ArtifactVersion) -> None:
        # artifact 行先建：artifact_version.artifact_id 是外键。
        # artifact_kind 用 media_kind（当前只有 paper_render），不新造枚举。
        self.cur.execute(
            """INSERT INTO agent.artifact (id, artifact_kind, created_by, created_at)
               VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (version.artifact_id, version.media_kind, version.created_by,
             version.created_at),
        )
        self.cur.execute(
            """INSERT INTO agent.artifact_version
                 (id, artifact_id, version_no, release_context_id, visibility,
                  contains_solution, contains_answer_test, media_kind, mime_type,
                  storage_uri, byte_size, output_hash, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (output_hash, media_kind, created_by) DO NOTHING""",
            (
                version.id, version.artifact_id, version.version_no,
                version.release_context_id, version.visibility,
                version.contains_solution, version.contains_answer_test,
                version.media_kind, version.mime_type, version.storage_uri,
                version.byte_size, version.output_hash, version.created_by,
                version.created_at,
            ),
        )

    def record_access(self, event: Any) -> None:
        self.cur.execute(
            """INSERT INTO agent.artifact_access_event
                 (id, artifact_version_id, actor_id, action, occurred_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                event.id, event.artifact_version_id, event.actor_id,
                event.action, event.occurred_at,
            ),
        )


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, AbcMapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _artifact_from_db(row: AbcMapping[str, Any]) -> ArtifactVersion:
    return ArtifactVersion(
        id=row["id"],
        artifact_id=row["artifact_id"],
        version_no=row["version_no"],
        release_context_id=row["release_context_id"],
        visibility=row["visibility"],
        contains_solution=row["contains_solution"],
        contains_answer_test=row["contains_answer_test"],
        media_kind=row["media_kind"],
        mime_type=row["mime_type"],
        storage_uri=row["storage_uri"],
        byte_size=row["byte_size"],
        output_hash=row["output_hash"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _render_from_db(row: AbcMapping[str, Any]) -> PaperRenderRecord:
    return PaperRenderRecord(
        id=row["id"],
        paper_version_id=row["paper_version_id"],
        render_kind=row["render_kind"],
        paper_manifest_hash=row["paper_manifest_hash"],
        template_version=row["template_version"],
        language_code=row["language_code"],
        artifact_version_id=row["artifact_version_id"],
        renderer_version=row["renderer_version"],
        font_manifest_hash=row["font_manifest_hash"],
        security_manifest_hash=row["security_manifest_hash"],
        output_hash=row["output_hash"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperRenderService:
    """一次 render 的完整编排：授权 → 白名单/上下文 → 渲染 → 守卫 → 落盘 → 登记。"""

    def __init__(
        self,
        repository: PaperRenderRepository,
        authorizer: Any,
        object_store: ObjectStore,
        renderer: PaperRenderer | None = None,
        id_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self.object_store = object_store
        self.renderer: PaperRenderer = renderer or render_paper_html
        self._new_id = id_factory or _uuid4
        self._clock = clock

    @staticmethod
    def object_key(output_hash: str) -> str:
        return ArtifactService.object_key(MEDIA_KIND, output_hash)

    # -- 授权/读取 -------------------------------------------------------
    def authorize(self, actor_id: Any, paper_version_id: Any,
                  render_kind: RenderKind) -> RenderSubject:
        with self.repository.unit_of_work() as uow:
            return self._load_and_authorize(
                uow, actor_id, paper_version_id, render_kind
            )

    def list_current_renders(
        self, actor_id: Any, paper_version_id: Any
    ) -> tuple[RenderSubject, tuple[PaperRenderRecord, ...]]:
        with self.repository.unit_of_work() as uow:
            subject = self._load_subject(uow, paper_version_id)
            allowed = self._allowed_kinds(actor_id, subject)
            records = tuple(
                record for record in uow.list_renders(paper_version_id)
                if record.render_kind in allowed
                and record.paper_manifest_hash == subject.manifest_hash
                and record.status == "ready"
            )
            return subject, records

    # -- 渲染 ------------------------------------------------------------
    def render(self, actor_id: Any, paper_version_id: Any,
               render_kind: RenderKind) -> PaperRenderOutcome:
        if render_kind not in RENDER_KINDS:
            raise PaperRenderInputError(f"未知 render_kind：{render_kind!r}")
        with self.repository.unit_of_work() as uow:
            subject = self._load_and_authorize(
                uow, actor_id, paper_version_id, render_kind
            )
            if any(slot.empty for slot in subject.slots()):
                raise PaperRenderIncompleteError(
                    "卷面还有未选题的槽位：先补齐题目再渲染，不能打印空白题"
                )
            context, projection, security_hash = self._context_for(subject, render_kind)
            body = self.renderer(
                template=TEMPLATE_VERSION, kind=render_kind, context=context
            )
            if isinstance(body, bytearray):
                body = bytes(body)
            if not isinstance(body, bytes):
                raise PaperRenderInputError(
                    f"renderer 必须返回 bytes，收到 {type(body).__name__}"
                )

            # 落盘前的最后两道锁：键级白名单 + 内容级扫描。
            if render_kind == "student":
                if projection is None:
                    raise PaperRenderIntegrityError("学生卷缺少投影")
                assert_student_safe(projection.model_dump(mode="python"))
                assert_student_output_safe(body, subject)

            artifacts = ArtifactService(
                self.object_store,
                self.authorizer,
                uow.artifact_repository(),
                id_factory=self._new_id,
                clock=self._clock,
            )
            artifact = artifacts.register(
                actor_id,
                body,
                ArtifactMetadata(
                    release_context_id=subject.release_context_id,
                    # 三类卷都只对"授权教师本人/服务"可见：学生在线可见不等于可下载，
                    # 答案卷与量规卷更不能因为同班就自动共享。
                    visibility="private",
                    contains_solution=render_kind != "student",
                    contains_answer_test=False,
                    mime_type=MIME_TYPE,
                    media_kind=MEDIA_KIND,
                ),
            )
            record = PaperRenderRecord(
                id=self._new_id(),
                paper_version_id=subject.paper_version_id,
                render_kind=render_kind,
                paper_manifest_hash=subject.manifest_hash,
                template_version=TEMPLATE_VERSION,
                language_code=LANGUAGE_CODE,
                artifact_version_id=artifact.id,
                renderer_version=RENDERER_VERSION,
                font_manifest_hash=FONT_MANIFEST_HASH,
                security_manifest_hash=security_hash,
                output_hash=artifact.output_hash,
                status="ready",
                created_by=actor_id,
                created_at=self._clock(),
            )
            stored = uow.add_render(record)
        return PaperRenderOutcome(artifact=artifact, render=stored, body=body)

    # -- 下载 ------------------------------------------------------------
    def download(self, actor_id: Any, render_id: Any) -> tuple[bytes, PaperRenderRecord, RenderSubject]:
        with self.repository.unit_of_work() as uow:
            record = uow.get_render(render_id)
            if record is None:
                raise PaperRenderNotFoundError(f"渲染不存在：{render_id}")
            subject = self._load_subject(uow, record.paper_version_id)
            self._require(actor_id, subject, record.render_kind)
            artifacts = ArtifactService(
                self.object_store,
                self.authorizer,
                uow.artifact_repository(),
                id_factory=self._new_id,
                clock=self._clock,
            )
            body = artifacts.open(actor_id, record.artifact_version_id)
            if sha256(body).hexdigest() != record.output_hash:
                raise PaperRenderIntegrityError(
                    "落盘字节与 paper_render.output_hash 不一致，拒绝下载"
                )
            return body, record, subject

    # -- 内部 ------------------------------------------------------------
    def _load_subject(self, uow: PaperRenderUnitOfWork,
                      paper_version_id: Any) -> RenderSubject:
        subject = uow.load_subject(paper_version_id)
        if subject is None:
            raise PaperRenderNotFoundError(f"纸卷版本不存在：{paper_version_id}")
        if subject.status == "withdrawn":
            raise PaperRenderWithdrawnError("卷子已撤回：不再允许渲染或下载")
        return subject

    def _load_and_authorize(self, uow: PaperRenderUnitOfWork, actor_id: Any,
                            paper_version_id: Any,
                            render_kind: RenderKind) -> RenderSubject:
        if render_kind not in RENDER_KINDS:
            raise PaperRenderInputError(f"未知 render_kind：{render_kind!r}")
        subject = self._load_subject(uow, paper_version_id)
        self._require(actor_id, subject, render_kind)
        return subject

    def _require(self, actor_id: Any, subject: RenderSubject,
                 render_kind: RenderKind) -> None:
        self.authorizer.require(
            actor_id,
            RENDER_ACTIONS[render_kind],
            course_id=subject.course_id,
            course_ref=subject.course_ref,
            offering_id=subject.offering_id,
            offering_ref=subject.offering_ref,
        )

    def _allowed_kinds(self, actor_id: Any,
                       subject: RenderSubject) -> set[RenderKind]:
        allowed: set[RenderKind] = set()
        for kind in RENDER_KINDS:
            try:
                self._require(actor_id, subject, kind)
            except PermissionError:
                continue
            allowed.add(kind)
        if not allowed:
            raise PermissionError("没有任何卷种的导出权限")
        return allowed

    def _context_for(
        self, subject: RenderSubject, render_kind: RenderKind
    ) -> tuple[dict[str, object], StudentPaperProjection | None, str]:
        if render_kind == "student":
            projection = build_student_projection(
                subject.paper_version_id,
                subject.version_short_code,
                subject.course_title,
                subject.instructions,
                student_rows(subject),
            )
            context = {
                "schema_version": "paper-render/v1",
                "render_kind": "student",
                "paper_version_id": str(subject.paper_version_id),
                "version_short_code": subject.version_short_code,
                "course_title": subject.course_title,
                "instructions": subject.instructions,
                "manifest_hash": subject.manifest_hash,
                "language_code": LANGUAGE_CODE,
                "pdf_implemented": PDF_IMPLEMENTED,
                "pdf_notice": PDF_NOTICE,
                "template_version": TEMPLATE_VERSION,
                "renderer_version": RENDERER_VERSION,
                "slots": [
                    slot.model_dump(mode="json") for slot in projection.slots
                ],
            }
            security_hash = canonical_hash({
                "kind": "student",
                "projection": projection.model_dump(mode="json"),
            })
            return context, projection, security_hash
        if render_kind == "answer":
            context = _answer_context(subject)
        elif render_kind == "rubric":
            context = _rubric_context(subject)
        else:  # pragma: no cover - 上游已校验
            raise PaperRenderInputError(f"未知 render_kind：{render_kind!r}")
        security_hash = canonical_hash({"kind": render_kind, "context": context})
        return context, None, security_hash


def _uuid4() -> Any:
    from uuid import uuid4

    return uuid4()


# ---------------------------------------------------------------------------
# worker 装配与 job 执行
# ---------------------------------------------------------------------------

def build_render_service(object_store: ObjectStore | None = None) -> PaperRenderService:
    """worker 的唯一装配点：本地内容仓储 + Artifact + HTML renderer。

    刻意不装配模型网关、review 服务或 assignment 服务：worker 只能渲染。
    """
    return PaperRenderService(
        repository=PostgresPaperRenderRepository(),
        authorizer=ServiceRenderAuthorizer(),
        object_store=object_store or LocalObjectStore(),
    )


def run_render_job(job: dict) -> dict:
    """执行一个 `paper-render` pipeline_job。

    成功/失败都写回 job（失败不抛，worker 继续下一个）——与 generation.run_job
    的约定一致。重试时同一输入得到同一 output_hash，Artifact 与 paper_render
    都按唯一键复用，不会产生第二份文件。
    """
    job_id = int(job["id"])
    payload = job.get("payload") or {}
    try:
        try:
            paper_version_id = _uuid_from(payload.get("paper_version_id"))
        except (TypeError, ValueError) as exc:
            raise PaperRenderInputError("job 缺少合法的 paper_version_id") from exc
        render_kind = str(payload.get("render_kind") or "")
        if render_kind not in RENDER_KINDS:
            raise PaperRenderInputError(f"job 的 render_kind 非法：{render_kind!r}")
        requested_by = str(job.get("requested_by") or "").strip()
        if not requested_by:
            raise PaperRenderInputError("job 缺少 requested_by，无法确定导出操作者")
        actor_id = ref_uuid("account", requested_by)

        outcome = build_render_service().render(
            actor_id, paper_version_id, render_kind  # type: ignore[arg-type]
        )
        result = {
            "paper_version_id": str(paper_version_id),
            "render_kind": render_kind,
            "paper_render_id": str(outcome.render.id),
            "artifact_version_id": str(outcome.artifact.id),
            "output_hash": outcome.artifact.output_hash,
            "byte_size": outcome.artifact.byte_size,
            "template_version": TEMPLATE_VERSION,
            "renderer_version": RENDERER_VERSION,
            "media_type": MIME_TYPE,
            "pdf_implemented": PDF_IMPLEMENTED,
            "notice": PDF_NOTICE,
        }
        _finish_job(job_id, "succeeded", result=result)
        return {"id": job_id, "status": "succeeded", "result": result, "error": None}
    except Exception as error:  # noqa: BLE001 — 任何失败都要落进任务记录
        message = f"{type(error).__name__}: {error}"
        print(f"[任务 {job_id}] 渲染失败：{message}", flush=True)
        _finish_job(job_id, "failed", error=message)
        return {"id": job_id, "status": "failed", "result": None, "error": message}


def _uuid_from(value: object):
    from uuid import UUID

    if value is None or value == "":
        raise ValueError("missing uuid")
    return UUID(str(value))


def _finish_job(job_id: int, status: str, *, result: dict | None = None,
                error: str | None = None) -> None:
    db.execute(
        """UPDATE pipeline_job
              SET status = %s, progress = %s, finished_at = now(),
                  result = %s::jsonb, error = %s
            WHERE id = %s""",
        (
            status,
            100 if status == "succeeded" else 0,
            json.dumps(result, ensure_ascii=False, default=str) if result else None,
            error,
            job_id,
        ),
    )
