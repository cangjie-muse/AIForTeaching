"""知识点切块的单元测试（不依赖数据库、不依赖网络）。

切块错了不会报错，只会让检索"看起来有结果但缺前提"：一个定理的条件留在上一块、
结论落在下一块，模型据此作答就会编。所以这里按数学教材的真实形态测：
一段一个定义、`$$...$$` 行间公式成段、条件与证明跨段、以及只写了一个标题的空正文。

另外两条是这份代码的边界：
  - 空正文不能产出空块（否则检索命中一个空片段，引用显示"知识点：无"）；
  - 尾块过短要并进上一块（"证毕。"单独成块只会污染检索）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.qa_ingest import MAX_CHUNK_CHARS, split_content  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{'  — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def main() -> int:
    print("切块：基本形态")

    check("空正文不产出片段", split_content("") == [])
    check("只有空白也不产出片段", split_content("   \n\n\t ") == [])

    single = "**知识内容。** 数列上极限 $\\limsup a_n$ 定义为上确界的极限。"
    chunks = split_content(single)
    check("短正文原样一块", chunks == [single], str(chunks))

    two = "定义 1.1 设 $\\{a_n\\}$ 为实数列。\n\n定理 1.2 单调有界数列必收敛。"
    chunks = split_content(two)
    check("两个短段并成一块（不按段落硬切）", len(chunks) == 1, f"{len(chunks)} 块")
    check("段间保留空行", "\n\n" in chunks[0], repr(chunks[0][:40]))

    print("\n切块：长度上限")
    long_paragraph = "这是一句用来凑长度的话。" * 80      # 约 1040 字，单段超上限
    chunks = split_content(long_paragraph)
    check("超长单段被切开", len(chunks) > 1, f"{len(chunks)} 块")
    check("每块不超过上限", all(len(chunk) <= MAX_CHUNK_CHARS for chunk in chunks),
          str([len(chunk) for chunk in chunks][:5]))
    check("切开后不丢字", "".join(chunks).replace("\n", "") == long_paragraph,
          f"{len(''.join(chunks))} vs {len(long_paragraph)}")

    print("\n切块：行间公式不拆散")
    formula = "$$" + "f(x)=\\int_0^x g(t)\\,dt" + "$$"
    body = "由微积分基本定理，" + formula + "，于是 $f$ 连续。"
    chunks = split_content(body)
    check("公式与上下文留在同一块", chunks == [body], str(chunks))
    check("公式原文未被改动", formula in chunks[0])

    print("\n切块：多段长文")
    paragraph = "**证明。** " + "由定义与归纳假设可得，" * 12 + "故结论成立。"
    many = "\n\n".join([paragraph] * 12)      # 约 1900 字，必然超过单块上限
    chunks = split_content(many)
    check("多段长文切成多块", len(chunks) >= 2, f"{len(chunks)} 块")
    check("每块都带内容", all(chunk.strip() for chunk in chunks))
    check("所有块长度受控", all(len(chunk) <= MAX_CHUNK_CHARS for chunk in chunks),
          str(max(len(chunk) for chunk in chunks)))

    print("\n切块：尾块合并")
    text = ("甲" * 600) + "\n\n" + "证毕。"
    chunks = split_content(text)
    check("过短的尾块并进上一块", chunks[-1].endswith("证毕。") and len(chunks) == 1,
          f"{len(chunks)} 块，尾块 {len(chunks[-1])} 字")

    print("\n切块：幂等")
    again = split_content("\n\n".join(chunks))
    check("再切一次结果相同（重建不会越切越碎）", again == chunks,
          f"{len(again)} vs {len(chunks)}")

    print("")
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("知识点切块测试全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
