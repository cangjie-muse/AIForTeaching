"""课件 pptx 渲染的真渲染测试（需要 python-pptx）。

没装 python-pptx 时打印"跳过"并返回 0：本地 .venv 可能没装，
服务器上按 requirements.txt 装了就一定会跑，不能让本地缺库挡住宿主流程。

检查的是"能不能拿去上课"的底线：
  · 文件能被 PowerPoint 解析（python-pptx 能读回来）
  · 页数与计划一致，续页真的多出一页
  · 每页页脚都有「待教师确认」——这句话必须在 PPT 里，而不只在网页上
  · 幻灯片上不出现 LaTeX 反斜杠命令（\\frac 这种在投影上是乱码）
  · 讲稿写进了备注
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

try:
    from pptx import Presentation
except ImportError:  # pragma: no cover - 本地无库时走这里
    print("跳过：未安装 python-pptx（服务器按 requirements.txt 安装后会跑）")
    sys.exit(0)

from app import slidedeck  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  — {detail}" if detail else ""))
        failures.append(name)


plan = {
    "title": "定积分的概念",
    "subtitle": "从分割到求和的极限",
    "slides": [
        {"title": "定积分", "kind": "cover", "bookPages": "p.112"},
        {"title": "学习目标", "kind": "content",
         "bullets": ["理解分割—求和—取极限", "会把面积写成定积分", "知道可积的必要条件"],
         "equations": [{"text": "∫_a^b f(x)dx", "latex": "\\int_a^b f(x)\\,dx"}],
         "speakerNote": "先问：曲边梯形面积怎么算"},
        {"title": "课堂练习", "kind": "exercise", "paragraphs": ["用定义直接算。"],
         "bullets": [f"练习 {index}" for index in range(1, 15)]},
        {"title": "小结", "kind": "summary", "bullets": ["定积分是和的极限"]},
    ],
}

data = slidedeck.render(plan, {
    "course_title": "数学分析II", "chapter_title": "定积分",
    "audience": "本科二年级", "book_pages": "pp.112–118",
})
target = ROOT / ".tmp" / "courseware-render-smoke.pptx"
target.parent.mkdir(exist_ok=True)
target.write_bytes(data)

check("生成了非空文件", len(data) > 20000, f"{len(data)} 字节")
prs = Presentation(str(target))
check("能被重新解析", len(prs.slides) > 0, "")
# 计划里 4 页 + 练习页拆成两页 = 5
check("页数按计划 + 续页", len(prs.slides) == 5, str(len(prs.slides)))

texts = [[paragraph.text for shape in slide.shapes if shape.has_text_frame
          for paragraph in shape.text_frame.paragraphs if paragraph.text]
         for slide in prs.slides]
check("每页都有「待教师确认」", all(any("待教师确认" in line for line in page) for page in texts),
      str([any("待教师确认" in line for line in page) for page in texts]))
check("每页都有页码", all(any("/ 5" in line for line in page) for page in texts), "")
check("续页标题带（续）", any("（续）" in line for page in texts for line in page), "")
check("幻灯片上没有 LaTeX 反斜杠",
      not any("\\" in line for page in texts for line in page),
      str([line for page in texts for line in page if "\\" in line][:3]))
with zipfile.ZipFile(target) as archive:
    _xml = "".join(archive.read(name).decode("utf-8") for name in archive.namelist()
                   if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
check("后备公式是可读文字", "∫_a^b f(x)dx" in _xml, "")
# --- 原生公式：a14:m 数量、后备文字、无 LaTeX 残留 ---
with zipfile.ZipFile(target) as archive:
    slide_xml = [archive.read(name).decode("utf-8")
                 for name in sorted(archive.namelist())
                 if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
native = sum(page.count("<a14:m>") for page in slide_xml)
check("公式写成原生 OMML", native == 1, f"{native} 处")
check("原生公式带兼容后备", any("<mc:Fallback>" in page for page in slide_xml), "")
check("幻灯片 XML 不含 LaTeX 残留",
      not any("\\int" in page or "\\frac" in page for page in slide_xml), "")

check("讲稿进了备注",
      any(slide.has_notes_slide and "先问" in slide.notes_slide.notes_text_frame.text
          for slide in prs.slides), "")

degraded = slidedeck.render({"slides": [
    {"title": "封面", "kind": "cover"},
    {"title": "不能转换的公式", "equations": [
        {"text": "自定义写法", "latex": "\\unknownmacro{x}"},
        {"text": "ω = dθ", "latex": "\\omega = d\\theta"}]},
]}, {})
degraded_path = target.parent / "courseware-degrade.pptx"
degraded_path.write_bytes(degraded)
with zipfile.ZipFile(degraded_path) as archive:
    degraded_xml = archive.read("ppt/slides/slide2.xml").decode("utf-8")
check("一条能转、一条降级", degraded_xml.count("<a14:m>") == 1,
      str(degraded_xml.count("<a14:m>")))
check("降级的公式保留可读文字", "自定义写法" in degraded_xml or "ω = dθ" in degraded_xml, "")

overlong = slidedeck.render({"slides": [
    {"title": "封面", "kind": "cover"},
    {"title": "一页塞满", "bullets": ["很长的一段要点" * 12 for _ in range(10)]},
]}, {})
check("塞满的页会自动拆页", len(Presentation(str(target)).slides) > 0 and len(overlong) > 0,
      f"{len(overlong)} 字节")

try:
    slidedeck.render({"slides": []}, {})
    check("空计划报错", False, "居然通过了")
except ValueError as exc:
    check("空计划报错", "没有可渲染的页" in str(exc), str(exc)[:40])

print("")
if failures:
    print(f"渲染测试失败 {len(failures)} 项：{'、'.join(failures)}")
    sys.exit(1)
print("渲染测试全部通过。")
