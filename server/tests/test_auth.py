#!/usr/bin/env python3
"""认证的纯逻辑测试：不连数据库，只验哈希、令牌、角色蕴含与匿名判定。

这些是"错了就静默出事"的地方：
  - 密码哈希比较写成 ==，会被计时攻击；
  - teacher 不覆盖 reviewer，审核接口会突然对教师 403；
  - 匿名 Actor 被当成"有角色"，等于没登录也能改内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from app import auth  # noqa: E402

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


print("密码哈希：")
hashed = auth.hash_password("whu-teacher-2026")
check("哈希格式带算法与迭代次数", hashed.startswith("pbkdf2_sha256$"), hashed.split("$")[0])
parts = hashed.split("$")
check("哈希串四段", len(parts) == 4, str(len(parts)))
check("盐是随机的（两次不同）", auth.hash_password("x" * 8) != auth.hash_password("x" * 8))
check("正确密码通过", auth.verify_password("whu-teacher-2026", hashed))
check("错误密码不通过", not auth.verify_password("whu-teacher-2027", hashed))
check("空密码不通过", not auth.verify_password("", hashed))
check("没有哈希（只能走统一认证）不通过", not auth.verify_password("x" * 8, None))
check("损坏的哈希不抛异常", not auth.verify_password("x" * 8, "garbage"))
check("哈希里不含明文", "whu-teacher-2026" not in hashed)

print("\n密码规则：")
check("太短被拒", auth.password_problem("1234567") is not None)
check("八位通过", auth.password_problem("12345678") is None)
check("首尾空格被拒", auth.password_problem(" 12345678 ") is not None)
check("提示里说了位数", "8" in (auth.password_problem("123") or ""))

print("\n会话令牌：")
token, digest = auth.new_session_token()
check("令牌够长", len(token) >= 40, str(len(token)))
check("入库的是哈希不是令牌", digest != token and len(digest) == 64, str(len(digest)))
check("同一令牌哈希稳定", auth.token_hash(token) == digest)
check("不同令牌哈希不同", auth.token_hash(auth.new_session_token()[0]) != digest)

print("\n角色：")
admin = auth.Actor(id="a1", roles=frozenset({"admin"}))
teacher = auth.Actor(id="t1", roles=frozenset({"teacher"}))
student = auth.Actor(id="s1", roles=frozenset({"student"}))
anonymous = auth.ANONYMOUS
check("管理员拥有一切", admin.has("teacher", "student", "admin"))
check("教师含审核权限（老接口写的是 reviewer）", teacher.has("reviewer"),
      "require('reviewer') 必须对教师放行")
check("教师没有管理员权限", not teacher.has("admin"))
check("学生不能审题", not student.has("reviewer"))
check("学生不能改可见范围", not student.has("teacher"))
check("匿名什么都不能", not anonymous.has("student") and anonymous.anonymous)
check("匿名 id 为空", anonymous.id == "")
check("登录用户不是匿名", not student.anonymous)
check("role 取最高权限的那个", admin.as_dict()["role"] == "admin")
check("角色中文名", admin.as_dict()["role_label"] == "管理员")
check("学生角色中文名", student.as_dict()["role_label"] == "学生")

print("\n请求头身份（仅 dev）：")
legacy = auth.resolve_header_actor("张老师", "teacher")
check("中文姓名被解码", legacy.display_name == "张老师", legacy.display_name)
check("只接受三个合法角色",
      auth.resolve_header_actor("x", "admin,superuser").roles == frozenset({"admin"}),
      str(sorted(auth.resolve_header_actor("x", "admin,superuser").roles)))
check("没头没角色就是匿名", auth.resolve_header_actor(None, None).anonymous)
check("有名字没角色仍算登录（无权限）",
      not auth.resolve_header_actor("x", None).anonymous
      and not auth.resolve_header_actor("x", None).has("student"))

print("\n统一认证的位置：")
check("CAS 标记为未接入", auth.CAS_AVAILABLE is False)
try:
    auth.cas_login_url("/catalog")
    check("cas_login_url 应报未接入", False)
except auth.HTTPException as error:  # type: ignore[attr-defined]
    check("cas_login_url 明确报未接入", error.status_code == 501)
    check("错误信息指向要改的位置", "cas_login_url" in error.detail, error.detail[:40])
try:
    auth.cas_verify_ticket("ST-123", "https://x")
    check("cas_verify_ticket 应报未接入", False)
except auth.HTTPException as error:  # type: ignore[attr-defined]
    check("cas_verify_ticket 明确报未接入", error.status_code == 501)

print(f"\n通过 {passed} 项", f"，失败 {failed} 项" if failed else "")
sys.exit(1 if failed else 0)
