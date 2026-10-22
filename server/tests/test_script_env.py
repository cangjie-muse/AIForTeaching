#!/usr/bin/env python
"""运维脚本的运行时检查（不连库）。

这类检查的价值全在"用户看到的到底是什么"：一句
`ModuleNotFoundError: No module named 'fastapi'` 会让人以为项目缺依赖、
去 pip install 一堆东西，而实际答案只是"用错了解释器"。所以这里正面测提示文案。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("script_env", root / "scripts" / "_env.py")
script_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script_env)

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


print("找到虚拟环境的解释器")
with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp)
    (fake / ".venv" / "bin").mkdir(parents=True)
    (fake / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    found = script_env.venv_python(fake)
    check("Linux/macOS: .venv/bin/python", found == fake / ".venv" / "bin" / "python", str(found))

with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp)
    (fake / ".venv" / "Scripts").mkdir(parents=True)
    (fake / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    found = script_env.venv_python(fake)
    check("Windows: .venv/Scripts/python.exe", found.name == "python.exe", str(found))

with tempfile.TemporaryDirectory() as tmp:
    found = script_env.venv_python(Path(tmp))
    check("都没有时给出预期路径（用于打印修复命令）",
          found == Path(tmp) / ".venv" / "bin" / "python", str(found))

print("\n依赖齐全时什么都不做")
real_required = script_env.REQUIRED
script_env.REQUIRED = ("json", "sys")   # 一定存在
try:
    script_env.ensure_runtime("publish_courses.py", root)
    check("不抛错", True)
except SystemExit:
    check("不抛错", False, "不该退出的场景退出了")

print("\n缺依赖时的提示要说人话")
script_env.REQUIRED = ("definitely_not_a_real_module_xyz",)
try:
    script_env.ensure_runtime("publish_courses.py", root)
    check("应该退出", False, "没有 SystemExit")
    message = ""
except SystemExit as error:
    message = str(error)
    check("退出了", True)
check("指出缺哪个模块", "definitely_not_a_real_module_xyz" in message)
check("给出可直接复制的命令", ".venv" in message and "scripts/publish_courses.py" in message)
check("解释原因（系统 python3 没装依赖）", "虚拟环境" in message)
check("不是一行 traceback", "Traceback" not in message)
check("提到 Windows 的解释器路径", "Scripts" in message)

script_env.REQUIRED = real_required

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"运行时检查测试全部通过（{checks} 项）。")
