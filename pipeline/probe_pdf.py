#!/usr/bin/env python3
"""教材 PDF 入门体检：决定后续切片路线，不做切片本身。

用法：
    python pipeline/probe_pdf.py textbooks/analysis/chenjixiu-3e-vol1.pdf
    python pipeline/probe_pdf.py textbooks/analysis/*.pdf --json

输出：
    - 页数、是否电子版（有文字层）、每页平均字符数
    - 疑似公式密度（$ 出现次数、数学字体片段）
    - 结论：direct-extract / needs-ocr / mixed

依赖：pip install pymupdf
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.exit(
        "缺少 PyMuPDF。安装：\n"
        "    pip install pymupdf\n"
        "若装不上（离线环境），改用 pip download pymupdf 后本地安装。"
    )

# 判定阈值：这些数字是经验值，跑过几本真教材后应回来校准
SCANNED_PAGE_CHARS = 50      # 单页文字少于这个数，基本等于没有文字层
FORMULA_DENSITY = 0.002      # $ 数量 / 字符数


def probe(path: Path) -> dict:
    doc = fitz.open(path)
    pages = []
    total_chars = 0
    total_dollars = 0
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        chars = len(text.strip())
        dollars = text.count("$")
        total_chars += chars
        total_dollars += dollars
        pages.append(
            {
                "pdf_page": i,
                "chars": chars,
                "dollars": dollars,
                "images": len(page.get_images(full=True)),
            }
        )
    doc.close()

    empty = [p for p in pages if p["chars"] < SCANNED_PAGE_CHARS]
    ratio = len(empty) / max(len(pages), 1)
    if ratio > 0.9:
        verdict = "needs-ocr"          # 纯扫描件
    elif ratio > 0.2:
        verdict = "mixed"              # 部分页是图，需逐页判断
    else:
        verdict = "direct-extract"     # 电子版，可直接抽文字层

    density = total_dollars / total_chars if total_chars else 0.0

    return {
        "file": str(path),
        "pages": len(pages),
        "verdict": verdict,
        "pages_without_text": len(empty),
        "avg_chars_per_page": round(total_chars / max(len(pages), 1), 1),
        "formula_density": round(density, 5),
        "formula_hint": "high" if density >= FORMULA_DENSITY else "low",
        "sample_pages": pages[:3],
        "note": (
            "公式密度基于 $ 与文字层的粗略估计。数学教材若用 Unicode 数学字符"
            "或图片公式，该指标会偏低，需人工抽检 10 页确认。"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="教材 PDF 入门体检")
    ap.add_argument("pdf", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args()

    results = []
    for path in args.pdf:
        if not path.exists():
            print(f"跳过（不存在）：{path}", file=sys.stderr)
            continue
        results.append(probe(path))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    for r in results:
        print(f"\n{r['file']}")
        print(f"  页数            {r['pages']}")
        print(f"  结论            {r['verdict']}")
        print(f"  无文字层页数    {r['pages_without_text']}")
        print(f"  平均每页字符    {r['avg_chars_per_page']}")
        print(f"  公式密度        {r['formula_density']}（{r['formula_hint']}）")
    print("\n" + (results[0]["note"] if results else ""))


if __name__ == "__main__":
    main()
