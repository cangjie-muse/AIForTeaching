"""把一本教材的文本切成章。

为什么需要这一步：老师只上传一个 PDF，平台要能自己决定"这一章是第几页到第几页"。
切错了，整章的页码溯源都会偏，所以这里的规则要能解释、能复核：

1. 先找章标题行（`第X章`、`Chapter N`、`§N`），只认每页前若干行里的独立短行。
   为什么只看页首：正文里"见第 1 章"这种句子到处都是，把它当章标题会把书切成碎片。
2. 同一个章号再次出现不再计数。教材页眉通常每页都印"第 1 章 实数"，那不是新章。
3. 找不到两个以上标题就退回"固定页窗"切分，并带上 warning —— 宁可切得粗，
   也不要静默产出一个"整本书一章"的巨型任务。
4. 章数有上限。超过上限时保留前面的章，最后一章延伸到书末，并说明被合并了。
"""

from __future__ import annotations

import re
from typing import Any

# 章标题：第X章 / Chapter N / §N。数字支持中文数字与阿拉伯数字。
HEADING_RE = re.compile(
    r"^(?:第\s*([一二三四五六七八九十百零〇两0-9]{1,4})\s*章"
    r"|chapter\s+([0-9]{1,3})"
    r"|§\s*([0-9]{1,3}))\s*[:：、.．]?\s*(.*)$",
    re.IGNORECASE,
)

MAX_TITLE_LEN = 40
HEAD_LINES = 12          # 每页只看前 12 个非空行
DEFAULT_WINDOW = 15      # 兜底：每 15 页一章
MAX_CHAPTERS = 40        # 一本书最多切这么多章，剩下的并进最后一章

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """把「十二」「二十三」「1」这类章号转成整数。只处理常见写法。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    total, section = 0, 0
    for char in text:
        if char in CN_DIGITS:
            section = CN_DIGITS[char]
        elif char == "十":
            section = (section or 1) * 10
            total += section
            section = 0
        elif char == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


def _clean(line: str) -> str:
    return line.strip().lstrip("#").strip().strip("*_").strip()


def _looks_like_heading(line: str) -> re.Match[str] | None:
    if not line or len(line) > MAX_TITLE_LEN:
        return None
    if line[-1] in "。；;，,":
        return None
    return HEADING_RE.match(line)


def _chapter_key(match: re.Match[str]) -> str:
    cn, en, section = match.group(1), match.group(2), match.group(3)
    number = _cn_number(cn) if cn else int(en or section)
    return f"ch-{number}" if number is not None else "ch-?"


def _scan_headings(pages: dict[int, str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in sorted(pages):
        lines = [_clean(line) for line in pages[page].splitlines()]
        lines = [line for line in lines if line]
        for line in lines[:HEAD_LINES]:
            match = _looks_like_heading(line)
            if not match:
                continue
            key = _chapter_key(match)
            if key in seen:          # 页眉重复，不是新章
                break
            seen.add(key)
            found.append({"key": key, "title": line, "page": page})
            break
    return found


def _windows(pages: dict[int, str], window: int = DEFAULT_WINDOW) -> list[dict[str, Any]]:
    numbers = sorted(pages)
    if not numbers:
        return []
    found = []
    for index, start in enumerate(range(numbers[0], numbers[-1] + 1, window), start=1):
        end = min(start + window - 1, numbers[-1])
        found.append({"key": f"part-{index}", "title": f"第 {start}–{end} 页（自动切分）",
                      "page": start, "page_to": end})
    return found


def split_chapters(pages: dict[int, str], *, window: int = DEFAULT_WINDOW,
                   max_chapters: int = MAX_CHAPTERS) -> dict[str, Any]:
    """返回 {source, items, warning}。

    每项：key / title / page_from / page_to / chars。
    source 是 'heading' 或 'window'——页面上要能看出"这是按标题切的还是猜的"。
    """
    if not pages:
        return {"source": "empty", "items": [], "warning": "没有可切分的文本"}

    numbers = sorted(pages)
    last_page = numbers[-1]
    warning = ""

    found = _scan_headings(pages)
    source = "heading"
    if len(found) < 2:
        found = _windows(pages, window)
        source = "window"
        warning = ("没有识别到两个以上的章标题，已按每 %d 页自动切分；"
                   "建议先用「单章」模式校准页码范围。" % window)

    if len(found) > max_chapters:
        warning = (warning + " " if warning else "") + \
            f"识别到 {len(found)} 章，只处理前 {max_chapters} 章，其余并入最后一章。"
        found = found[:max_chapters]

    items = []
    for index, entry in enumerate(found):
        start = entry["page"]
        end = entry.get("page_to") or (found[index + 1]["page"] - 1 if index + 1 < len(found) else last_page)
        end = max(start, min(end, last_page))
        text = "".join(pages.get(page, "") for page in range(start, end + 1))
        items.append({
            "key": entry["key"],
            "title": entry["title"],
            "page_from": start,
            "page_to": end,
            "chars": len(text.strip()),
        })
    return {"source": source, "items": items, "warning": warning}
