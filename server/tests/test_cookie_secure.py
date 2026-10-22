#!/usr/bin/env python
"""会话 cookie 的 Secure 标记解析（不连库）。

这条规则看起来琐碎，但它决定"能不能登录"：prod 默认带 Secure，而 http 下浏览器
不回传 Secure cookie，表现是登录成功、下一步又回到登录页。所以这里把四种取值都测掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "server"))

from app import auth  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


resolve = auth._resolve_cookie_secure

print("没有覆盖时：由 APP_ENV 决定")
check("prod → 带 Secure", resolve("prod", None) is True)
check("prod（空字符串）→ 带 Secure", resolve("prod", "") is True)
check("dev → 不带", resolve("dev", None) is False)
check("test → 不带", resolve("test", None) is False)
check("大小写不敏感：PROd", resolve("PROd", None) is True)

print("\n显式开启（https 环境，即使 APP_ENV=dev）")
for value in ["1", "true", "TRUE", "yes", "on", " true "]:
    check(repr(value), resolve("dev", value) is True)

print("\n显式关闭（域名与证书未到位时的过渡）")
for value in ["0", "false", "FALSE", "no", "off", " 0 "]:
    check(repr(value), resolve("prod", value) is False)

print("\n无法识别的值：退回按 APP_ENV 决定，不静默改行为")
for value in ["maybe", "2", "-1", "on "]:
    expected = resolve("prod", None)
    check(repr(value), resolve("prod", value) is expected)

print("\n真实环境变量能生效：改了要重启进程才读得到")
import importlib
import os

os.environ["APP_ENV"] = "prod"
os.environ.pop("COOKIE_SECURE", None)
check("prod 下默认 True", importlib.reload(auth).COOKIE_SECURE is True)
os.environ["COOKIE_SECURE"] = "0"
check("设 COOKIE_SECURE=0 后为 False", importlib.reload(auth).COOKIE_SECURE is False)
os.environ["COOKIE_SECURE"] = "1"
check("设 COOKIE_SECURE=1 后为 True", importlib.reload(auth).COOKIE_SECURE is True)
os.environ.pop("COOKIE_SECURE", None)
os.environ["APP_ENV"] = "dev"
check("回到 dev 后为 False", importlib.reload(auth).COOKIE_SECURE is False)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"会话 cookie 规则测试全部通过（{checks} 项）。")
