"""模型密钥文件：读写 `secrets/<name>.key`。

为什么单独一个模块：密钥在两个地方被用到——配置页写入（admin.py）、
真实调用时读取（llm.py）。两处各写一份路径拼接，迟早会出现
"写进了 A 目录、读的是 B 目录"这种只在下班前才发现的 bug。

安全约定：
  · 文件名只允许 `[a-z0-9._-]`，防止 `../` 跑出目录；
  · 解析后必须仍在 SECRETS_DIR 内（双保险）；
  · 先写临时文件再 rename，避免断电留下半截密钥；
  · 权限 0600（Windows 上 chmod 语义有限，失败不算致命）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 必须绝对：Web 服务（WorkingDirectory=server）与 worker（=仓库根）是两个进程，
# 用相对路径时两边解析到不同目录——页面显示"密钥已配置"，worker 却报 MissingKey。
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR") or (ROOT / "secrets")).resolve()
# 历史版本按进程 cwd 落盘，Web 服务写到了 server/secrets/。只读兜底，避免升级后密钥"消失"。
LEGACY_DIRS = (ROOT / "server" / "secrets",)
NAME_RE = re.compile(r"^[a-z0-9._-]+$")


def _check_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise ValueError("端点名称只能包含小写字母、数字、点、下划线、短横线")


def key_path(name: str) -> Path:
    _check_name(name)
    path = (SECRETS_DIR / f"{name}.key").resolve()
    if SECRETS_DIR not in path.parents:
        raise ValueError("非法的密钥路径")
    return path


def display_path(name: str) -> str:
    """写进数据库的路径：仓库相对，人一眼能看懂，preflight 也能直接拼。"""
    _check_name(name)
    return f"secrets/{name}.key"


def read_key(name: str) -> str | None:
    """读不到就返回 None（没配密钥是正常状态，不是错误）。

    先读规范位置，再读历史位置：升级那一刻已有的密钥不该失效。
    """
    try:
        candidates = [key_path(name)] + [folder / f"{name}.key" for folder in LEGACY_DIRS]
    except ValueError:
        return None
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def write_key(name: str, value: str) -> Path:
    path = key_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def clear_key(name: str) -> bool:
    try:
        path = key_path(name)
    except ValueError:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
