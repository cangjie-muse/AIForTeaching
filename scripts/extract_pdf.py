#!/usr/bin/env python3
"""把教材 PDF 抽成带页标记的 Markdown（生成流水线的第 1 步）。

为什么单独一个命令行工具：服务器上不一定装得上 PDF 库，而抽取是"一次就好"的活。
抽好的文本存成 `<!-- page N -->` 标记的 Markdown，之后每次生成都复用它，
不必为每一章重新解析整本 PDF。

用法：
    python scripts/extract_pdf.py 教材.pdf                       # 输出到 uploads/<文件名>.md
    python scripts/extract_pdf.py 教材.pdf --out uploads/book.md
    python scripts/extract_pdf.py 教材.pdf --from 20 --to 60     # 只抽一段
    python scripts/extract_pdf.py 教材.pdf --probe               # 只看页数与文字层

抽不出来（扫描件）会明确告诉你要 OCR，而不是输出一份空文件——空文件会让后面的
生成任务报"这一章没有抽到文字"，那时就分不清是漏抽还是扫描件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app import pdf as pdf_mod  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="抽取 PDF 文本")
    parser.add_argument("path")
    parser.add_argument("--out", default=None)
    parser.add_argument("--from", dest="first", type=int, default=1)
    parser.add_argument("--to", dest="last", type=int, default=None)
    parser.add_argument("--probe", action="store_true", help="只探测，不抽取")
    args = parser.parse_args()

    source = Path(args.path)
    if not source.exists():
        print(f"找不到文件：{source}", file=sys.stderr)
        return 1

    data = source.read_bytes()
    report = pdf_mod.probe_bytes(data)
    print(f"文件：{source.name}（{len(data) / 1024 / 1024:.1f} MB）")
    print(f"页数：{report['page_count'] or '未知'}")
    print(f"文字层：{'有' if report['has_text_layer'] else '没有'}"
          + (f"（{report['note']}）" if report.get("note") else ""))
    engines = pdf_mod.available_engines()
    print(f"可用解析引擎：{'、'.join(engines) or '无'}")
    if args.probe:
        return 0

    if not report["has_text_layer"] and not engines:
        print("\n这份 PDF 没有文字层，也没有可用的解析引擎。", file=sys.stderr)
        print("先装一个引擎：pip install pypdf；扫描件还需要 OCR。", file=sys.stderr)
        return 1

    try:
        pages, engine = pdf_mod.extract_pages(source, args.first, args.last)
    except pdf_mod.PdfEngineMissing as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    texts = [text for text in pages if text.strip()]
    if not texts:
        print("\n抽出来是空的：这份 PDF 很可能是扫描件，需要先 OCR。", file=sys.stderr)
        return 1

    markdown = pdf_mod.to_markdown(pages, args.first)
    target = Path(args.out) if args.out else ROOT / "uploads" / f"{source.stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")

    print(f"\n用 {engine} 抽取 {len(pages)} 页，其中 {len(texts)} 页有文字。")
    print(f"已写入：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
