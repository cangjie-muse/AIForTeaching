"""把结构化的页面计划渲染成 PPTX。

排版归代码，内容归模型。模型只产出 JSON（标题、要点、公式文本、来源），
字号、分栏、页脚、分页、溢出处理全部在这里决定——排版错一次会出现在每一页上，
而模型每生成一次都可能错；代码错一次改一次就永远对。

一处刻意的取舍：幻灯片上的公式是 **Unicode 纯文本**（如 ω = dθ），不是 LaTeX，
也不是 Office Math 对象。原因是 OMML 需要手工拼 XML，写错了会在 WPS 里显示成
空白或乱码（V1—V4 踩过），而"宁可显示成文字、也不要显示成乱码"。latex 源式
仍然逐条留档（equations.json / slide_plan），需要原生公式时再按它补 OMML。
"""

from __future__ import annotations

from io import BytesIO

from lxml.etree import XMLSyntaxError
from pptx import Presentation
from pptx.oxml import parse_xml
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

from . import omml

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN_X = Inches(0.9)
BODY_TOP = Inches(1.75)
BODY_H = Inches(4.9)

NAVY = RGBColor(0x14, 0x2A, 0x4C)
ACCENT = RGBColor(0x2F, 0x6F, 0xD0)
GREY = RGBColor(0x64, 0x74, 0x8B)
LIGHT = RGBColor(0xF4, 0xF8, 0xFF)

FONT = "Source Han Sans SC"
MONO_MATH = "Cambria Math"


def _blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


def _text(slide, left, top, width, height, *, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.clear()
    return frame


def _run(paragraph, text, *, size, color=NAVY, bold=False, italic=False, font=FONT):
    run = paragraph.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    run.font.name = font
    return run


def _footer(slide, meta: dict, page: int, total: int) -> None:
    frame = _text(slide, MARGIN_X, SLIDE_H - Inches(0.65),
                  SLIDE_W - 2 * MARGIN_X, Inches(0.35))
    paragraph = frame.paragraphs[0]
    label = meta.get("chapter_title") or ""
    if meta.get("book_pages"):
        label = f"{label} · {meta['book_pages']}"
    _run(paragraph, label, size=10, color=GREY)
    _run(paragraph, f"    {page} / {total}", size=10, color=GREY)
    # 每一页都带这句话：PPT 会被转发、会被学生看到，"还没教师确认"必须写在页面上，
    # 不能只写在网页上。
    _run(paragraph, "    · 待教师确认", size=10, color=RGBColor(0xC0, 0x50, 0x2E))


def _cover(prs, slide_plan: dict, meta: dict) -> None:
    slide = _blank(prs)
    band = slide.shapes.add_shape(1, 0, 0, SLIDE_W, Inches(2.1))
    band.fill.solid()
    band.fill.fore_color.rgb = NAVY
    band.line.fill.background()

    frame = _text(slide, MARGIN_X, Inches(0.55), SLIDE_W - 2 * MARGIN_X, Inches(1.2))
    _run(frame.paragraphs[0], meta.get("course_title") or "课程课件",
         size=14, color=RGBColor(0xC7, 0xD8, 0xF2))

    frame = _text(slide, MARGIN_X, Inches(2.55), SLIDE_W - 2 * MARGIN_X, Inches(1.6))
    _run(frame.paragraphs[0], slide_plan.get("title") or meta.get("chapter_title") or "课件",
         size=34, color=NAVY, bold=True)

    subtitle = slide_plan.get("subtitle") or meta.get("chapter_title") or ""
    if subtitle:
        frame = _text(slide, MARGIN_X, Inches(4.15), SLIDE_W - 2 * MARGIN_X, Inches(0.8))
        _run(frame.paragraphs[0], subtitle, size=18, color=GREY)

    frame = _text(slide, MARGIN_X, Inches(5.35), SLIDE_W - 2 * MARGIN_X, Inches(1.0))
    _run(frame.paragraphs[0], f"AI 初稿 · {meta.get('audience') or '本科'} · "
                              f"教师审核后使用", size=12, color=ACCENT)


def _bullets(lines: list[str]) -> list[str]:
    return [line for line in lines if str(line).strip()]


def _content(prs, slide_plan: dict, meta: dict, page: int, total: int,
             bullets: list[str], start: int) -> int:
    slide = _blank(prs)
    frame = _text(slide, MARGIN_X, Inches(0.55), SLIDE_W - 2 * MARGIN_X, Inches(1.0))
    title_text = slide_plan.get("title") or ""
    if start:
        title_text = f"{title_text}（续）"
    _run(frame.paragraphs[0], title_text, size=24, color=NAVY, bold=True)

    rule = slide.shapes.add_shape(1, MARGIN_X, Inches(1.5), Inches(1.5), Inches(0.06))
    rule.fill.solid()
    rule.fill.fore_color.rgb = ACCENT
    rule.line.fill.background()

    paragraphs = [str(text).strip() for text in (slide_plan.get("paragraphs") or [])
                  if str(text).strip()]
    # 公式保留整个对象：latex 用来生成原生 OMML，text 用来做后备与降级
    equations = [item for item in (slide_plan.get("equations") or [])
                 if isinstance(item, dict) and (item.get("text") or item.get("latex"))]

    frame = _text(slide, MARGIN_X, BODY_TOP, SLIDE_W - 2 * MARGIN_X, BODY_H)
    first = True
    for text in paragraphs:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        paragraph.space_after = Pt(8)
        _run(paragraph, text, size=17)
    for line in bullets:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        paragraph.space_after = Pt(9)
        _run(paragraph, "· ", size=17, color=ACCENT, bold=True)
        _run(paragraph, line, size=17)
    for item in equations:
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        paragraph.space_after = Pt(10)
        paragraph.alignment = PP_ALIGN.CENTER
        if _append_math(paragraph, item):
            continue
        # 转换不了就退成文字：一行公式是文字，好过整页失败
        _run(paragraph, str(item.get("text") or ""), size=20, italic=True,
             font=MONO_MATH, color=NAVY)

    note = slide_plan.get("speakerNote")
    if note:
        slide.notes_slide.notes_text_frame.text = str(note)
    _footer(slide, meta, page, total)
    return len(bullets)



def _append_math(paragraph, item: dict) -> bool:
    """把一条公式写成原生 OMML（带文字后备）。成功返回 True。

    失败一律返回 False 让调用方降级成文字：公式转换是个"尽力而为"的环节，
    它不该让整份课件生成失败——教师更可能接受一处公式是文字，而不是一整版空白。
    """
    latex = str(item.get("latex") or "").strip()
    text = str(item.get("text") or "").strip()
    if not latex or not text:
        return False
    try:
        paragraph._p.append(parse_xml(omml.alternate_content(latex, text)))
        return True
    except (omml.Unsupported, XMLSyntaxError, ValueError):
        return False


def _estimate_lines(texts: list[str], chars_per_line: int) -> int:
    return sum(max(1, -(-len(str(text)) // chars_per_line)) for text in texts)


def _split_bullets(bullets: list[str], chars_per_line: int = 34,
                   capacity: int = 11) -> list[list[str]]:
    """要点太多就自动开一页，而不是压小字号硬塞——压到 12pt 的课件没人看。"""
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for text in bullets:
        need = max(1, -(-len(text) // chars_per_line))
        if current and used + need > capacity:
            pages.append(current)
            current, used = [], 0
        current.append(text)
        used += need
    if current:
        pages.append(current)
    return pages or [[]]


def render(plan: dict, meta: dict | None = None) -> bytes:
    """plan: {"slides":[{title, kind, paragraphs, bullets, equations, speakerNote}]}

    meta: {course_title, chapter_title, audience, book_pages}
    返回 pptx 字节。不写文件：调用方决定存哪里（版本不可变，路径里带版本号）。
    """
    meta = dict(meta or {})
    slides = [slide for slide in (plan or {}).get("slides") or [] if isinstance(slide, dict)]
    if not slides:
        raise ValueError("页面计划是空的：没有可渲染的页")

    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W)
    prs.slide_height = Emu(SLIDE_H)

    # 先把「续页」拆出来，才能算总页数——页脚要写 第 n / 总页数。
    layout: list[tuple[dict, list[str], int]] = []
    for index, slide in enumerate(slides):
        if index == 0 or slide.get("kind") == "cover":
            layout.append((slide, [], 0))
            continue
        bullets = _bullets(slide.get("bullets") or [])
        equations = [str(item.get("text") or "").strip()
                     for item in (slide.get("equations") or []) if item.get("text")]
        budget = max(4, 11 - 2 * len(equations) - len(slide.get("paragraphs") or []))
        for part_index, part in enumerate(_split_bullets(bullets, capacity=budget)):
            layout.append((slide, part, part_index))

    total = len(layout)
    for page, (slide_plan, bullets, start) in enumerate(layout, start=1):
        if page == 1 and slide_plan.get("kind") in (None, "cover"):
            _cover(prs, slide_plan, meta)
            _footer(prs.slides[-1], meta, page, total)
            if slide_plan.get("speakerNote"):
                prs.slides[-1].notes_slide.notes_text_frame.text = str(slide_plan["speakerNote"])
            continue
        _content(prs, slide_plan, meta, page, total, bullets, start)

    buffer = BytesIO()
    prs.save(buffer)
    return buffer.getvalue()
