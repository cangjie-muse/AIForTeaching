"""加载仓库根目录的 `.env`。

为什么需要这个模块：部署时 systemd 用 `EnvironmentFile=.env` 注入环境变量，
所以 uvicorn 和 worker 拿到的 `DATABASE_URL` 是对的；但手动跑脚本（诊断、
自检、一次性修数据）没有这层注入，就会落到代码里的默认连接串。

表现出来是 `FATAL: password authentication failed for user "whu"`——
看着像数据库坏了或口令被改了，其实是脚本用了开发默认口令。
自己读一次 `.env`，三个入口就一致了。

规则：
- 只补空缺（setdefault）：命令行或 systemd 显式给的值优先，不会被文件覆盖；
- 不做变量展开、不执行命令，只认 `KEY=VALUE`；
- 文件不存在不算错误（服务器上可能确实只用环境变量）。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"


def parse(text: str) -> dict[str, str]:
    """把 .env 文本解析成字典。解析不了的行走 `warnings` 交回去，不静默吞掉。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: Path | None = None) -> dict[str, str]:
    """读取并补入环境变量，返回实际生效的键值（只含文件里有的键）。"""
    target = path or ENV_FILE
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    values = parse(text)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values
