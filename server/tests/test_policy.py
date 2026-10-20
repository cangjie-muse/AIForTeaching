#!/usr/bin/env python3
"""数据出校与身份的纯逻辑测试（不依赖数据库、不依赖网络）。

运行：
    python server/tests/test_policy.py

这两块是"错了不会报错、只会悄悄出事"的地方，所以单独测：
  · 出校判断错了 → 教材内容被发到校外，没有任何提示
  · 角色判断错了 → 学生能调审核接口，删改题库
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓ 会直接抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, llm  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{' — ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


print("出校判断（is_internal_url）：")
internal = [
    "http://localhost:8000/v1",
    "http://127.0.0.1:8000/v1",
    "http://10.10.2.31:8001/v1",
    "http://172.16.5.9:8001/v1",
    "http://192.168.1.20:8001/v1",
    "https://llm.whu.edu.cn/v1",
    "http://llm.internal/v1",
]
external = [
    "https://api.deepseek.com",
    "https://api.moonshot.cn/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://api.openai.com/v1",
    "http://8.8.8.8/v1",
    "https://evil.example.com/v1",
    "",           # 空值必须拒绝
    "not-a-url",  # 无法解析必须拒绝
]
for url in internal:
    check(f"校内：{url or '(空)'}", llm.is_internal_url(url) is True)
for url in external:
    check(f"校外：{url or '(空)'}", llm.is_internal_url(url) is False)

print("\n出校拦截（assert_egress_allowed）：")
campus = llm.Endpoint("campus-gateway", "http://10.0.0.8:8001/v1", "qwen2.5-14b", "chat")
deepseek = llm.Endpoint("deepseek", "https://api.deepseek.com", "deepseek-flash", "chat")

llm.ALLOW_EXTERNAL = False
try:
    llm.assert_egress_allowed(campus)
    ok = True
except llm.EgressViolation:
    ok = False
check("校内端点放行", ok)

try:
    llm.assert_egress_allowed(deepseek)
    blocked = False
except llm.EgressViolation as exc:
    blocked = "数据不出校" in str(exc)
check("校外端点被拦截且提示原因", blocked)

# 显式放行只在开发和测试时用
llm.ALLOW_EXTERNAL = True
try:
    llm.assert_egress_allowed(deepseek)
    allowed = True
except llm.EgressViolation:
    allowed = False
check("显式 ALLOW_EXTERNAL 时放行", allowed)
llm.ALLOW_EXTERNAL = False

print("\n请求构造：")
req = llm.build_chat_request(campus, [{"role": "user", "content": "什么是收敛"}])
check("OpenAI 兼容格式", req["model"] == "qwen2.5-14b" and req["messages"][0]["role"] == "user")
check("默认不开流式（后端先跑通再改）", req["stream"] is False)

print("\n身份解析（开发模式）：")
auth.AUTH_MODE = "dev"
actor = auth.resolve_actor("teacher-01", "teacher,reviewer")
check("解析 id", actor.id == "teacher-01")
check("解析多角色", actor.roles == frozenset({"teacher", "reviewer"}))
check("未知角色被丢弃", auth.resolve_actor("x", "superuser").roles == frozenset())
check("缺省为 anonymous", auth.resolve_actor(None, None).id == "anonymous")
check("admin 拥有一切权限", auth.Actor("root", frozenset({"admin"})).has("teacher", "reviewer"))
check("无权限者被拒", auth.Actor("s1", frozenset({"student"})).has("teacher") is False)

dep = auth.require("teacher", "reviewer")
try:
    dep(auth.Actor("s1", frozenset({"student"})))
    denied = False
except auth.HTTPException as exc:
    denied = exc.status_code == 403
check("require() 对学生返回 403", denied)

got = dep(auth.Actor("t1", frozenset({"teacher"})))
check("require() 对教师放行", got.id == "t1")

print("\n认证未接入时必须拒绝，而不是放行：")
auth.AUTH_MODE = "cas"
try:
    auth.resolve_actor("teacher-01", "teacher")
    refused = False
except auth.HTTPException as exc:
    refused = exc.status_code == 501
check("AUTH_MODE=cas 时返回 501（不静默放行）", refused)
auth.AUTH_MODE = "dev"

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    raise SystemExit(1)
print("全部通过。")
