#!/usr/bin/env python
"""服务器首次建号的口令规则单测（不连库）。

`create_admin.py` 里真正容易出错的不是 INSERT，是"什么算合格的口令"：
写宽了等于允许 admin123，写窄了把运维卡住。这里只测那个纯函数。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("create_admin", root / "scripts" / "create_admin.py")
create_admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(create_admin)

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


print("合格口令通过")
for password in ["Whu-math-2026", "shuxue#2026#whu", "a1b2c3d4e5f6g7"]:
    check(password, create_admin.validate_password(password, "admin") is None,
          str(create_admin.validate_password(password, "admin")))

print("\n太短被拒")
check("11 个字符不够", create_admin.validate_password("abc12345678", "admin") is not None)
check("提示里说清要几个字符",
      "至少 12" in (create_admin.validate_password("abc12345678") or ""))

print("\n仓库里出现过的默认口令被拒")
for weak in ["whu-admin-2026", "whu-teacher-2026", "whu-student-2026"]:
    check(weak, create_admin.validate_password(weak, "admin") is not None,
          str(create_admin.validate_password(weak, "admin")))

print("\n包含登录名被拒（admin / whu-admin-2026 这类）")
check("口令含登录名", create_admin.validate_password("AdminMath2026x", "admin") is not None,
      str(create_admin.validate_password("AdminMath2026x", "admin")))

print("\n纯字母被拒，要有数字")
check("没有数字", create_admin.validate_password("abcdefghijkl", "admin") is not None,
      str(create_admin.validate_password("abcdefghijkl", "admin")))

print("\n重复字符太多被拒")
check("只有两种字符", create_admin.validate_password("abababababab1", "admin") is not None,
      str(create_admin.validate_password("abababababab1", "admin")))

print("\n空口令被拒")
check("空串", create_admin.validate_password("", "admin") is not None)

print("\n落库语句：不连库，把 db 换成记录器，看它到底要写什么")
calls: list[tuple[str, str, tuple]] = []


class FakeDb:
    def __init__(self, existing=None):
        self.existing = existing
        self.opened = False

    def query_one(self, sql, params=()):
        calls.append(("query_one", sql, params))
        return self.existing

    def execute(self, sql, params=()):
        calls.append(("execute", sql, params))
        return 1

    class pool:
        @staticmethod
        def open():
            pass

        @staticmethod
        def close():
            pass


fake = FakeDb(existing=None)
create_admin.db = fake
account_id = create_admin.create_admin("Boss", "教务", "Whu-math-2026x")
insert = [c for c in calls if c[0] == "execute"]
check("新建时只发一条 INSERT", len(insert) == 1, str(len(insert)))
sql, params = insert[0][1], insert[0][2]
check("角色写死 admin", "'admin'" in sql, sql.split("VALUES")[0][-40:])
check("首次登录强制改密", "must_change_password" in sql and "true" in sql.split("VALUES")[1],
      sql.split("VALUES")[1][:60])
check("登录名转小写", params[1] == "boss", str(params[1]))
check("口令存的是哈希不是明文", params[3] != "Whu-math-2026x" and len(params[3]) > 40,
      str(params[3])[:24] + "…")
check("返回的 id 是 acc- 前缀", account_id.startswith("acc-"), account_id)

calls.clear()
create_admin.db = FakeDb(existing={"id": "acc-old"})
try:
    create_admin.create_admin("boss", "", "Whu-math-2026x")
    check("已存在账号时中止", False, "没有抛错")
except SystemExit as error:
    check("已存在账号时中止", True, str(error).splitlines()[0])
check("已存在时不发 INSERT", not [c for c in calls if c[0] == "execute"], "")

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"建号口令规则测试全部通过（{checks} 项）。")
