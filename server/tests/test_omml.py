"""LaTeX→OMML 转换器的单元测试。

钉住的规则：

1. 课堂幻灯片上真会出现的构造必须转成对的 OMML 元素（分式/根式/上下标/积分求和）；
2. 不认识的命令必须抛 Unsupported，让调用方降级成文字——静默产出半个公式比报错更糟；
3. 每条公式都带 `mc:Fallback` 文字后备，且后备里不能再有 LaTeX 反斜杠
   （后备的用途就是"渲染器不认识公式对象时人还能看懂"）；
4. 产出的 XML 必须是合法 XML，否则整页 pptx 都打不开。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import xml.etree.ElementTree as etree  # noqa: E402

from app import omml  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


print("常见构造：")
cases = [
    ("\\frac{a}{b}", ["<m:f>", "<m:num>", "<m:den>"]),
    ("\\dfrac{1}{2}", ["<m:f>"]),
    ("\\sqrt{x}", ["<m:rad>", 'm:degHide m:val="1"']),
    ("\\sqrt[3]{x}", ["<m:rad>", "<m:deg>", 'm:degHide m:val="0"']),
    ("x^{2}", ["<m:sSup>", "<m:sup>"]),
    ("a_{n}", ["<m:sSub>", "<m:sub>"]),
    ("a_{n}^{2}", ["<m:sSubSup>"]),
    ("\\int_a^b f(x)\\,dx", ["<m:nary>", 'm:chr m:val="∫"', 'm:limLoc m:val="subSup"']),
    ("\\sum_{i=1}^{n} i", ["<m:nary>", 'm:chr m:val="∑"', 'm:limLoc m:val="undOvr"']),
    ("\\omega = d\\theta", ["ω", "θ"]),
    ("\\alpha \\le \\beta", ["α", "≤", "β"]),
    ("\\sin x", ["sin"]),
    ("\\text{当 } x > 0", ["当"]),
]
for latex, expected in cases:
    try:
        xml = omml.omath(latex)
        ok = all(fragment in xml for fragment in expected)
        detail = "" if ok else xml[:120]
    except omml.Unsupported as exc:
        ok, detail = False, f"不支持：{exc}"
    check(latex, ok, detail)

print("\n非法输入必须报错，不能静默出半个公式：")
for latex, label in [
    ("\\unknowncmd{x}", "不认识的命令"),
    ("\\frac{a}", "分式缺分母"),
    ("\\frac{a}{b", "花括号不配对"),
    ("x^", "上标没有内容"),
    ("", "空式子"),
]:
    try:
        omml.omath(latex)
        check(label, False, "居然转换成功了")
    except omml.Unsupported as exc:
        check(label, True, str(exc)[:30])

check("supports() 对合法式子返回真", omml.supports("\\frac{1}{2}"))
check("supports() 对非法式子返回假", not omml.supports("\\unknowncmd"))

print("\n后备文字与 XML 合法性：")
xml = omml.alternate_content("\\int_a^b f(x)\\,dx", "∫_a^b f(x)dx")
check("有 mc:Choice 与 a14:m", "<mc:Choice" in xml and "<a14:m>" in xml)
check("有 mc:Fallback", "<mc:Fallback>" in xml)
check("后备是纯文字，没有反斜杠", "\\" not in xml.split("<mc:Fallback>")[1], "")
check("后备写的是可读式子", "∫_a^b f(x)dx" in xml)

tree = etree.fromstring(xml.encode("utf-8"))
check("是合法 XML", tree is not None, tree.tag.split("}")[-1])
maths = tree.findall(".//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath")
check("OMML 用 math 命名空间", len(maths) == 1, str(len(maths)))

escaped = omml.alternate_content("x < y", "x < y")
check("文本里的 < 被转义", "&lt;" in escaped, "")
etree.fromstring(escaped.encode("utf-8"))
check("转义后仍是合法 XML", True)

print("")
if failures:
    print(f"OMML 测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("OMML 测试全部通过。")
