"""PDF 探查与取页文字。

为什么单独一层：服务器不一定装得上 PDF 库（内网离线环境很常见），
而"能不能解析"必须显式暴露出来——否则任务会在半路抛一个 ImportError，
审核员看到的是"生成失败"，真实原因是缺一个包。

所以这里分两档能力：
- `probe_bytes`：不依赖任何库，只扫原始字节，回答"多少页、有没有文字层"。
  够用来决定"这份是扫描件还是文本 PDF"，也够前端展示。
- `extract_pages`：需要 `pypdf`（`pip install pypdf`）或系统里的 `pdftotext`。
  两者都没有就抛 `PdfEngineMissing`，由调用方给出"先跑 scripts/extract_pdf.py"的指引。

文本格式固定为 Markdown + `<!-- page N -->` 页标记，N 是 **PDF 页**（从 1 起）。
书页码与 PDF 页的换算由 `textbook.page_offset` 负责，不在这里猜。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

PAGE_MARK = "<!-- page {n} -->"
PAGE_MARK_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")

# 探测文字层时只看前 2 MB：足够判断，且不会为了一个布尔值把 100 MB 文件读进内存
PROBE_BYTES = 2 * 1024 * 1024


class PdfEngineMissing(RuntimeError):
    """没有任何可用的 PDF 解析引擎。"""


def _page_count_from_bytes(head: bytes, tail: bytes) -> int:
    """从原始字节里数页数。

    优先用 `/Count N`（页面树里的总数），拿不到再数 `/Type /Page`。
    这是启发式，只用于展示与判断，不用于定位内容——定位一律以抽取出的页标记为准。
    """
    for pattern in (rb"/Count\s+(\d+)", rb"/Type\s*/Pages[^>]*/Count\s+(\d+)"):
        found = re.findall(pattern, tail + head)
        if found:
            numbers = [int(n) for n in found]
            if numbers:
                return max(numbers)
    return (head + tail).count(b"/Type /Page") - (head + tail).count(b"/Type /Pages")


def probe_bytes(data: bytes) -> dict:
    """不依赖库的探测。返回页数、是否有文字层，以及判断依据。"""
    head = data[:PROBE_BYTES]
    tail = data[-PROBE_BYTES:] if len(data) > PROBE_BYTES else b""
    if not head.startswith(b"%PDF"):
        return {"is_pdf": False, "page_count": None, "has_text_layer": None,
                "engine": "heuristic", "note": "文件头不是 %PDF"}

    page_count = _page_count_from_bytes(head, tail)
    # 有字体对象说明至少排过版；纯扫描件通常只有 /XObject 图像
    has_font = b"/Font" in head or b"/Font" in tail
    has_text_ops = b"BT" in head and b"Tj" in head
    has_text_layer = bool(has_font and has_text_ops)
    note = "有字体对象且有文字绘制指令" if has_text_layer else (
        "没有发现文字绘制指令，可能是扫描件（需要 OCR）" if not has_text_layer else ""
    )
    return {
        "is_pdf": True,
        "page_count": page_count if page_count > 0 else None,
        "has_text_layer": has_text_layer,
        "engine": "heuristic",
        "note": note,
    }


def available_engines() -> list[str]:
    engines = []
    try:
        import pypdf  # noqa: F401
        engines.append("pypdf")
    except Exception:
        pass
    if shutil.which("pdftotext"):
        engines.append("pdftotext")
    return engines


def extract_pages(path: str | Path, first: int = 1, last: int | None = None,
                  engine: str | None = None) -> tuple[list[str], str]:
    """抽取 [first, last] 范围内的页文字（PDF 页号，从 1 起）。

    返回 `(逐页文本, 使用的引擎)`。空文本保留为空串——页数不能因为抽取失败而错位，
    页码错位比缺字危险得多。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    engines = available_engines()
    if not engines:
        raise PdfEngineMissing(
            "服务器上没有 PDF 解析引擎。装一个即可：pip install pypdf "
            "（或安装 poppler 后使用 pdftotext）。装好后重跑 scripts/extract_pdf.py。"
        )
    chosen = engine or engines[0]
    if chosen not in engines:
        raise PdfEngineMissing(f"没有可用的 {chosen} 引擎，当前可用：{', '.join(engines)}")

    if chosen == "pypdf":
        return _extract_with_pypdf(path, first, last)
    return _extract_with_pdftotext(path, first, last)


def _extract_with_pypdf(path: Path, first: int, last: int | None) -> tuple[list[str], str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    total = len(reader.pages)
    stop = min(last or total, total)
    pages = []
    for index in range(first - 1, stop):
        try:
            pages.append(reader.pages[index].extract_text() or "")
        except Exception:
            # 单页抽不出来不该让整本失败；留空，页码继续对齐
            pages.append("")
    return pages, "pypdf"


def _extract_with_pdftotext(path: Path, first: int, last: int | None) -> tuple[list[str], str]:
    args = ["pdftotext", "-layout", "-f", str(first)]
    if last:
        args += ["-l", str(last)]
    args += [str(path), "-"]
    result = subprocess.run(args, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext 失败：{result.stderr.decode('utf-8', 'replace')[:300]}")
    pages = result.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages, "pdftotext"


def to_markdown(pages_text: list[str], first: int = 1) -> str:
    """把逐页文本拼成带页标记的 Markdown。"""
    blocks = []
    for offset, text in enumerate(pages_text):
        blocks.append(PAGE_MARK.format(n=first + offset))
        blocks.append(text.strip("\n"))
    return "\n\n".join(blocks).strip() + "\n"


def parse_markdown(text: str) -> dict[int, str]:
    """把带页标记的 Markdown 解析回 {pdf_page: text}。"""
    pages: dict[int, str] = {}
    current = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = PAGE_MARK_RE.search(line)
        if match:
            if current is not None:
                pages[current] = "\n".join(buffer).strip()
            current = int(match.group(1))
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        pages[current] = "\n".join(buffer).strip()
    return pages
