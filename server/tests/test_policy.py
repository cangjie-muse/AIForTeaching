#!/usr/bin/env python3
"""出校策略与身份的纯逻辑测试（不依赖数据库、不依赖网络）。

运行：
    python server/tests/test_policy.py

这两块是"错了不会报错、只会悄悄出事"的地方，所以单独测：
  · 出校判断错了 → 内容被发到来路不明的中转站，没有任何提示
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


print("地址分类（classify）：")
internal = [
    "http://localhost:8000/v1",
    "http://127.0.0.1:8000/v1",
    "http://10.10.2.31:8001/v1",
    "http://192.168.1.20:8001/v1",
    "https://llm.whu.edu.cn/v1",
]
approved = [
    "https://api.deepseek.com",
    "https://api.deepseek.com/v1",
    "https://api.moonshot.cn/v1",
    "https://platform.kimi.com/api",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://open.bigmodel.cn/api/paas/v4",
]
unapproved = [
    "https://api.openai.com/v1",
    "https://api.anthropic.com",
    "http://8.8.8.8/v1",
    # 域名伪造：前缀像已批准域名，实际归属别人的子域
    "https://api.deepseek.com.attacker.com/v1",
    "https://deepseek.com.attacker.com/v1",
    "https://evil-proxy.example.com/v1",
]
invalid = ["", "not-a-url", "ftp://"]

for url in internal:
    check(f"校内 {url}", llm.classify(url) == "internal")
for url in approved:
    check(f"已批准 {url}", llm.classify(url) == "approved")
for url in unapproved:
    check(f"未批准 {url}", llm.classify(url) == "unapproved")
for url in invalid:
    check(f"无效 {url or '(空)'}", llm.classify(url) == "invalid")

print("\nallowlist 模式（默认）：")
llm.EGRESS_MODE = "allowlist"
ok_internal = llm.Endpoint("campus", "http://10.0.0.8:8001/v1", "qwen", "chat")
ok_approved = llm.Endpoint("deepseek", "https://api.deepseek.com", "deepseek-flash", "chat")
bad = llm.Endpoint("proxy", "https://evil-proxy.example.com/v1", "gpt", "chat")

for name, ep, want_raise in [
    ("校内端点放行", ok_internal, False),
    ("已批准供应商放行", ok_approved, False),
    ("未批准供应商拒绝", bad, True),
]:
    try:
        llm.assert_egress_allowed(ep)
        raised = False
    except llm.EgressViolation:
        raised = True
    check(name, raised is want_raise)

# 拒绝时必须说清楚怎么加白名单，否则运维只能翻代码
try:
    llm.assert_egress_allowed(bad)
except llm.EgressViolation as exc:
    msg = str(exc)
    check(
        "拒绝信息含加入白名单的方法",
        "MODEL_ALLOWED_HOSTS" in msg and "MODEL_EGRESS=any" in msg,
    )

print("\ninternal-only 模式（若将来要求数据不出校）：")
llm.EGRESS_MODE = "internal-only"
try:
    llm.assert_egress_allowed(ok_approved)
    blocked = False
except llm.EgressViolation:
    blocked = True
check("已批准供应商也被拒绝", blocked)
try:
    llm.assert_egress_allowed(ok_internal)
    still_ok = True
except llm.EgressViolation:
    still_ok = False
check("校内端点仍放行", still_ok)

print("\nany 模式（仅开发临时用）：")
llm.EGRESS_MODE = "any"
try:
    llm.assert_egress_allowed(bad)
    any_ok = True
except llm.EgressViolation:
    any_ok = False
check("未批准供应商临时放行", any_ok)
try:
    llm.assert_egress_allowed(llm.Endpoint("x", "", "m", "chat"))
    empty_ok = True
except llm.EgressViolation:
    empty_ok = False
check("空地址即便 any 模式也拒绝", empty_ok is False)

llm.EGRESS_MODE = "allowlist"

print("\n请求构造：")
req = llm.build_chat_request(ok_approved, [{"role": "user", "content": "什么是收敛"}])
check("OpenAI 兼容格式", req["model"] == "deepseek-flash" and req["messages"][0]["role"] == "user")
check("默认不开流式（后端先跑通再改）", req["stream"] is False)
check("默认端点指向 DeepSeek", llm.chat_endpoint().base_url.startswith("https://api.deepseek.com"))
check(
    "默认 embedding 为百炼 qwen 且 1024 维",
    llm.embedding_endpoint().dim == 1024
    and "aliyuncs.com" in llm.embedding_endpoint().base_url,
)

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
check("require() 对教师放行", dep(auth.Actor("t1", frozenset({"teacher"}))).id == "t1")

print("\n认证未接入时必须拒绝，而不是放行：")
auth.AUTH_MODE = "cas"
try:
    auth.resolve_actor("teacher-01", "teacher")
    refused = False
except auth.HTTPException as exc:
    refused = exc.status_code == 501
check("AUTH_MODE=cas 时返回 501（不静默放行）", refused)
auth.AUTH_MODE = "dev"

print("\n配置界面权限矩阵：")
auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset({"admin"})

default_actor = auth.resolve_actor("信息化专员", None)
check("开发期未传角色时默认 admin", default_actor.has("admin"))

explicit = auth.resolve_actor("某学生", "student")
check("显式传角色时不套用默认", explicit.has("admin") is False and explicit.has("student"))

write_dep = auth.require("admin")
try:
    write_dep(auth.Actor("审-1", frozenset({"reviewer"})))
    reviewer_blocked = False
except auth.HTTPException as exc:
    reviewer_blocked = exc.status_code == 403
check("审核员不能改配置（403）", reviewer_blocked)

check("管理员可改配置", write_dep(auth.Actor("管-1", frozenset({"admin"}))).id == "管-1")

read_dep = auth.require("reviewer", "teacher", "admin")
check("审核员可查看配置", read_dep(auth.Actor("审-1", frozenset({"reviewer"}))).id == "审-1")
check("教师可查看配置", read_dep(auth.Actor("师-1", frozenset({"teacher"}))).id == "师-1")
try:
    read_dep(auth.Actor("学-1", frozenset({"student"})))
    student_blocked = False
except auth.HTTPException:
    student_blocked = True
check("学生不能查看配置", student_blocked)

# 只读者即便看得到列表，也不能通过 admin 依赖写
try:
    write_dep(auth.Actor("审-1", frozenset({"reviewer", "teacher"})))
    escalate = False
except auth.HTTPException:
    escalate = True
check("多角色叠加也不能提权到 admin", escalate)

print("\n地址与数据范围一致性（admin._validate_scope 的纯逻辑）：")
# 该函数依赖 fastapi，未安装时跳过；用分类结果直接验证规则本身
cases = [
    ("https://api.deepseek.com", "external", True),
    ("http://10.0.0.8:8001/v1", "internal", True),
    ("https://api.deepseek.com", "internal", False),
    ("http://10.0.0.8:8001/v1", "external", False),
]
for url, scope, expect_ok in cases:
    actual = "internal" if llm.classify(url) == "internal" else "external"
    check(
        f"{url} 标为 {scope}（应{'接受' if expect_ok else '拒绝'}）",
        (actual == scope) is expect_ok,
    )

print()
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    raise SystemExit(1)
print("全部通过。")
