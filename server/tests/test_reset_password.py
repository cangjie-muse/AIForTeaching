#!/usr/bin/env python
"""重置口令 / 解锁账号的行为（不连库，用假 db）。

真正容易漏的不是 UPDATE 本身，而是它必须同时做三件事：
写新哈希、标记下次改密、清掉失败计数与锁定。少最后一件，账号还锁着，
运维会以为"重置没生效"，然后开始怀疑密码哈希算法。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("reset_password", root / "scripts" / "reset_password.py")
reset_password = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_password)

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


class FakeDb:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, str, tuple]] = []

    def query_one(self, sql, params=()):
        self.calls.append(("query_one", sql, params))
        return self.row

    def execute(self, sql, params=()):
        self.calls.append(("execute", sql, params))
        return 1

    class pool:
        @staticmethod
        def open():
            pass

        @staticmethod
        def close():
            pass


print("重置口令：一条 UPDATE 要同时改五样东西")
fake = FakeDb({"id": "acc-1", "username": "zhangsan", "role": "student", "disabled_at": None})
reset_password.db = fake
result = reset_password.reset("ZhangSan", "Whu-math-2026")
sql, params = [c for c in fake.calls if c[0] == "execute"][0][1], [c for c in fake.calls if c[0] == "execute"][0][2]
check("返回 password_reset=true", result["password_reset"] is True)
check("登录名转小写后再查库", fake.calls[0][2] == ("zhangsan",), str(fake.calls[0][2]))
check("写入新哈希", "password_hash = %s" in sql)
check("清除失败计数", "failed_attempts = 0" in sql)
check("清除锁定", "locked_until = NULL" in sql)
check("标记下次必须改密", "must_change_password = true" in sql)
check("存的是哈希不是明文", params[0] != "Whu-math-2026" and params[0].startswith("pbkdf2_sha256$"),
      str(params[0])[:28] + "…")
check("按 id 更新，不按登录名", params[1] == "acc-1")

print("\n只解锁：不碰口令")
fake2 = FakeDb({"id": "acc-2", "username": "lisi", "role": "teacher", "disabled_at": None})
reset_password.db = fake2
r2 = reset_password.reset("lisi", None)
sql2 = [c for c in fake2.calls if c[0] == "execute"][0][1]
check("返回 password_reset=false", r2["password_reset"] is False)
check("SQL 里没有 password_hash", "password_hash" not in sql2)

print("\n异常路径")
reset_password.db = FakeDb(None)
try:
    reset_password.reset("nobody", "Whu-math-2026")
    check("账号不存在时报错", False, "没有抛错")
except SystemExit as error:
    check("账号不存在时报错", "没有这个账号" in str(error))

reset_password.db = FakeDb({"id": "acc-3", "username": "wangwu", "role": "student", "disabled_at": "2026-01-01"})
try:
    reset_password.reset("wangwu", "Whu-math-2026")
    check("已停用账号拒绝重置", False, "没有抛错")
except SystemExit as error:
    check("已停用账号拒绝重置", "已停用" in str(error))

fake3 = FakeDb({"id": "acc-4", "username": "admin", "role": "admin", "disabled_at": None})
reset_password.db = fake3
try:
    reset_password.reset("admin", "admin123456")
    check("弱口令被拒", False, "没有抛错")
except SystemExit as error:
    check("弱口令被拒", "口令不合格" in str(error))
check("口令不合格时不发 UPDATE", not [c for c in fake3.calls if c[0] == "execute"])

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"重置口令测试全部通过（{checks} 项）。")
