"""LaTeX → OMML（Office Math）子集转换。

为什么需要它：`python-pptx` 没有公式 API，PowerPoint/WPS 的原生公式是 `a14:m`
里的 OMML。教师拿到 PPTX 后要双击改公式，所以公式必须是对象，不能是文字或图片。

为什么只做子集：完整的 LaTeX 是图灵完备的排版语言，没人能在一次生成里覆盖。
这里只做**课堂幻灯片上真会出现的**那几种：分式、根式、上下标、积分求和、希腊字母、
运算符、括号、文本。遇到不认识的命令一律抛 `Unsupported`，调用方退回纯文本——
宁可这一处公式是文字，也不能整页失败，更不能显示成乱码。

兼容策略（沿用演示课件 V5 的做法）：每个公式都写成

    <mc:AlternateContent>
      <mc:Choice Requires="a14"><a14:m>…OMML…</a14:m></mc:Choice>
      <mc:Fallback><a:r><a:t>ω = dθ</a:t></a:r></mc:Fallback>
    </mc:AlternateContent>

支持 `a14` 的 PowerPoint/WPS 显示原生公式，不支持的渲染器显示后备文字。
两条路都指向同一个数学含义，不存在"某处空白"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
A14_NS = "http://schemas.microsoft.com/office/drawing/2010/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


class Unsupported(ValueError):
    """这个式子里有转换器不认识的构造。调用方应退回纯文本。"""


GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

OPERATORS = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "to": "→", "rightarrow": "→", "Rightarrow": "⇒", "mapsto": "↦",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "emptyset": "∅",
    "infty": "∞", "partial": "∂", "nabla": "∇", "circ": "∘", "star": "⋆",
    "ldots": "…", "cdots": "⋯", "dots": "…",
    "lvert": "|", "rvert": "|", "mid": "|",
    "langle": "⟨", "rangle": "⟩",
    "lbrace": "{", "rbrace": "}", "backslash": "\\",
}

FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "log", "ln", "lg", "exp", "lim", "max", "min",
    "sup", "inf", "det", "dim", "ker", "deg", "gcd",
}

NARY = {"int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
        "sum": "∑", "prod": "∏", "bigcup": "⋃", "bigcap": "⋂"}

# 只影响间距/字体的命令：直接丢掉，不影响数学含义
IGNORED = {"displaystyle", "textstyle", "limits", "nolimits", "quad", "qquad",
           "left", "right", "big", "Big", "bigg", "Bigg", "!", ",", ";", ":", " "}


@dataclass
class Token:
    kind: str          # 'char' | 'cmd' | 'group' | 'sup' | 'sub' | 'brace'
    text: str = ""
    body: list | None = None


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\":
            match = re.match(r"\\([A-Za-z]+|.)", source[index:])
            if not match:
                raise Unsupported("反斜杠后面没有命令")
            tokens.append(Token("cmd", match.group(1)))
            index += len(match.group(0))
            continue
        if char == "{":
            depth, cursor = 1, index + 1
            while cursor < len(source) and depth:
                if source[cursor] == "{":
                    depth += 1
                elif source[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth:
                raise Unsupported("花括号没有配对")
            tokens.append(Token("group", body=tokenize(source[index + 1:cursor - 1])))
            index = cursor
            continue
        if char == "^":
            tokens.append(Token("sup"))
            index += 1
            continue
        if char == "_":
            tokens.append(Token("sub"))
            index += 1
            continue
        if char == "}":
            raise Unsupported("多余的 }")
        if char in "$":
            index += 1
            continue
        if char.isspace():
            index += 1
            continue
        tokens.append(Token("char", char))
        index += 1
    return tokens


class Converter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.cursor = 0

    # ---------------------------------------------------------------- 基础
    def peek(self) -> Token | None:
        return self.tokens[self.cursor] if self.cursor < len(self.tokens) else None

    def next(self) -> Token:
        if self.cursor >= len(self.tokens):
            raise Unsupported("式子意外结束")
        token = self.tokens[self.cursor]
        self.cursor += 1
        return token

    @staticmethod
    def run(text: str, *, style: str = "") -> str:
        props = f'<m:rPr><m:sty m:val="{style}"/></m:rPr>' if style else ""
        return f"<m:r>{props}<m:t>{_escape(text)}</m:t></m:r>"

    # ---------------------------------------------------------------- 解析
    def convert(self) -> str:
        parts = self.sequence(top=True)
        if self.cursor < len(self.tokens):
            raise Unsupported("还有没解析完的内容")
        return "".join(parts)

    def sequence(self, *, top: bool = False) -> list[str]:
        parts: list[str] = []
        while True:
            token = self.peek()
            if token is None:
                if not top:
                    raise Unsupported("分组意外结束")
                return parts
            if token.kind == "sub" or token.kind == "sup":
                # 没有底数的上下标：当作空底数，交给 base 处理
                parts.append(self.script(""))
                continue
            if token.kind == "cmd" and token.text in ("right",):
                raise Unsupported("\\right 没有配对的 \\left")
            parts.append(self.atom())

    def atom(self) -> str:
        token = self.next()
        if token.kind == "group":
            inner = Converter.__new__(Converter)
            inner.__dict__.update(self.__dict__)
            inner.tokens, inner.cursor = token.body, 0
            content = inner.sequence(top=True)
            if inner.cursor < len(inner.tokens):
                raise Unsupported("分组里有多余内容")
            base = "".join(content) or self.run("")
            return self.script_with_base(base)
        if token.kind == "cmd":
            return self.command(token.text)
        if token.kind == "char":
            return self.script_with_base(self.run(token.text))
        raise Unsupported(f"无法处理的记号：{token.kind}")

    def script_with_base(self, base: str) -> str:
        token = self.peek()
        if token is None or token.kind not in ("sub", "sup"):
            return base
        sub = sup = None
        while self.cursor < len(self.tokens):
            marker = self.peek()
            if marker is None or marker.kind not in ("sub", "sup"):
                break
            self.cursor += 1
            argument = self.script_argument()
            if marker.kind == "sub":
                sub = argument
            else:
                sup = argument
        if sub is not None and sup is not None:
            return (f"<m:sSubSup><m:e>{base}</m:e><m:sub>{sub}</m:sub>"
                    f"<m:sup>{sup}</m:sup></m:sSubSup>")
        if sub is not None:
            return f"<m:sSub><m:e>{base}</m:e><m:sub>{sub}</m:sub></m:sSub>"
        return f"<m:sSup><m:e>{base}</m:e><m:sup>{sup}</m:sup></m:sSup>"

    def script(self, base: str) -> str:
        return self.script_with_base(base)

    def script_argument(self) -> str:
        token = self.peek()
        if token is None:
            raise Unsupported("上下标后面没有内容")
        if token.kind == "group":
            self.cursor += 1
            inner = Converter.__new__(Converter)
            inner.__dict__.update(self.__dict__)
            inner.tokens, inner.cursor = token.body, 0
            content = inner.sequence(top=True)
            return "".join(content) or self.run("")
        return self.atom()

    def command(self, name: str) -> str:
        if name in IGNORED:
            return ""
        if name in ("frac", "dfrac", "tfrac"):
            numerator = self.script_argument()
            denominator = self.script_argument()
            return f"<m:f><m:num>{numerator}</m:num><m:den>{denominator}</m:den></m:f>"
        if name == "sqrt":
            degree = None
            if self.peek() and self.peek().kind == "char" and self.peek().text == "[":
                self.cursor += 1
                degree_tokens: list[Token] = []
                while self.peek() and not (self.peek().kind == "char" and self.peek().text == "]"):
                    degree_tokens.append(self.next())
                if not self.peek():
                    raise Unsupported("根指数缺 ]")
                self.cursor += 1
                inner = Converter.__new__(Converter)
                inner.__dict__.update(self.__dict__)
                inner.tokens, inner.cursor = degree_tokens, 0
                degree = "".join(inner.sequence(top=True))
            radicand = self.script_argument()
            if degree:
                return (f"<m:rad><m:radPr><m:degHide m:val=\"0\"/></m:radPr>"
                        f"<m:deg>{degree}</m:deg><m:e>{radicand}</m:e></m:rad>")
            return (f"<m:rad><m:radPr><m:degHide m:val=\"1\"/></m:radPr>"
                    f"<m:deg/><m:e>{radicand}</m:e></m:rad>")
        if name in NARY:
            sub = sup = None
            while self.peek() and self.peek().kind in ("sub", "sup"):
                marker = self.next()
                argument = self.script_argument()
                if marker.kind == "sub":
                    sub = argument
                else:
                    sup = argument
            if name.startswith("int") or name == "oint":
                limits = '<m:limLoc m:val="subSup"/>'
            else:
                limits = '<m:limLoc m:val="undOvr"/>'
            props = (f"<m:naryPr><m:chr m:val=\"{NARY[name]}\"/>{limits}"
                     f"<m:subHide m:val=\"{0 if sub else 1}\"/>"
                     f"<m:supHide m:val=\"{0 if sup else 1}\"/></m:naryPr>")
            return (f"<m:nary>{props}<m:sub>{sub or ''}</m:sub>"
                    f"<m:sup>{sup or ''}</m:sup><m:e>{self.script_argument()}</m:e></m:nary>")
        if name == "text":
            argument = self.next()
            if argument.kind != "group":
                raise Unsupported("\\text 后面要跟花括号")
            raw = _render_plain(argument.body)
            return self.run(raw, style="p")
        if name in GREEK:
            return self.script_with_base(self.run(GREEK[name]))
        if name in OPERATORS:
            return self.script_with_base(self.run(OPERATORS[name]))
        if name in FUNCTIONS:
            # 函数名用正体（upright），与变量斜体区分——这是数学排版惯例
            return self.run(name, style="p")
        if name in ("{", "}", "%", "&", "#", "_", "$"):
            return self.run(name)
        raise Unsupported(f"不认识的命令：\\{name}")


def _render_plain(tokens: list[Token]) -> str:
    """\\text{...} 里的内容按字面拼接，不再转公式结构。"""
    parts = []
    for token in tokens:
        if token.kind == "char":
            parts.append(token.text)
        elif token.kind == "cmd" and token.text in GREEK:
            parts.append(GREEK[token.text])
        elif token.kind == "cmd" and token.text in IGNORED:
            continue
        elif token.kind == "group":
            parts.append(_render_plain(token.body or []))
        else:
            raise Unsupported("\\text 里有无法按字面显示的内容")
    return "".join(parts)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def omath(latex: str, *, center: bool = True) -> str:
    """返回 `<a14:m>` 里的 oMathPara 片段（不含外层 AlternateContent）。"""
    converter = Converter(latex)
    body = converter.convert()
    if not body:
        raise Unsupported("式子是空的")
    alignment = '<m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>' if center else ""
    return (f'<m:oMathPara xmlns:m="{M_NS}">{alignment}'
            f"<m:oMath>{body}</m:oMath></m:oMathPara>")


def alternate_content(latex: str, fallback_text: str) -> str:
    """公式的完整 XML：原生 OMML 优先，后备是可编辑文字。

    fallback_text 必须是真的能读的数学写法（如 `ω = dθ`），不能是 LaTeX——
    后备的用途就是"渲染器不认识公式对象时，人还能看懂"。
    """
    try:
        math = omath(latex)
    except Unsupported:
        raise
    return (
        f'<mc:AlternateContent xmlns:mc="{MC_NS}" xmlns:a="{A_NS}" '
        f'xmlns:a14="{A14_NS}" xmlns:m="{M_NS}">'
        f'<mc:Choice Requires="a14"><a14:m>{math}</a14:m></mc:Choice>'
        f"<mc:Fallback><a:r><a:rPr lang=\"zh-CN\" i=\"1\"/>"
        f"<a:t>{_escape(fallback_text)}</a:t></a:r></mc:Fallback>"
        f"</mc:AlternateContent>"
    )


def supports(latex: str) -> bool:
    try:
        omath(latex)
        return True
    except Unsupported:
        return False
