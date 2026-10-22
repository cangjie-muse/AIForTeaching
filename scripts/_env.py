"""脚本的运行时检查：确认自己在项目虚拟环境里跑。

为什么需要：`scripts/` 下的运维脚本会 import `server/app/` 里的模块，那些模块依赖
fastapi、psycopg 等。用系统 `python3` 跑会得到一个只有一行 traceback 的
`ModuleNotFoundError: No module named 'fastapi'`——对不熟 Python 环境的人来说，
这行错误既不说明"该用什么解释器"，也不说明"为什么"。

所以这里把话说清楚：用哪个解释器、为什么、以及它现在缺什么。
"""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED = ("fastapi", "psycopg", "pydantic")


def venv_python(root: Path) -> Path:
    for relative in (".venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = root / relative
        if candidate.exists():
            return candidate
    return root / ".venv/bin/python"


def ensure_runtime(script: str, root: Path) -> None:
    """缺依赖就带着修复方法退出，而不是抛一行 traceback。"""
    missing = []
    for name in REQUIRED:
        try:
            __import__(name)
        except ModuleNotFoundError:
            missing.append(name)
    if not missing:
        return

    python = venv_python(root)
    shown = python if python.exists() else root / ".venv/bin/python"
    sys.exit(
        "缺少依赖：{}\n"
        "这些脚本必须用项目的虚拟环境解释器运行，系统自带的 python3 里没有装依赖。\n\n"
        "改这样跑：\n"
        "  {} scripts/{}\n"
        "\n"
        "（Windows 上是 .venv\\Scripts\\python.exe）\n"
        "如果 {} 不存在，先建虚拟环境：\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt\n"
        .format("、".join(missing), shown, script, shown)
    )
