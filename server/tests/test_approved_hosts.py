#!/usr/bin/env python
"""出校域名：管理员在前端加，立刻生效，且不能绕过策略（不连网）。

这条链路以前必须登服务器改 .env 再重启。搬进页面之后，风险从"操作麻烦"
变成"点错了就把课程内容发给一家没审过的公司"，所以测试要盯住三件事：
输入只能是一个域名、校内地址不许加、执行过的事都留了痕。

不连库：db.query / db.query_one 用假实现顶掉，看它们收到的 SQL 与参数。
"""

from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "server"))

from app import db, egress, llm, settings  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


print("域名规范化：人粘进来的东西五花八门")
for raw, want in (
    ("api.example.com", "api.example.com"),
    ("  API.Example.COM  ", "api.example.com"),
    ("https://api.example.com/v1/chat/completions", "api.example.com"),
    ("https://api.example.com:8443/v1", "api.example.com"),
    ("api.example.com/path?x=1", "api.example.com"),
    ("api.example.com.", "api.example.com"),
):
    got = egress.normalize_host(raw)
    check(f"{raw!r} → {want}", got == want, got)

print("\n拒绝的输入（都要给出人话原因）")
for raw, keyword in (
    ("", "为空"),
    ("localhost", "校内"),
    ("127.0.0.1", "域名"),
    ("10.0.0.5", "域名"),
    ("math.whu.edu.cn", "校内"),
    ("*.example.com", "通配符"),
    ("api@example.com", "密钥"),
    ("不合法", "域名"),
    ("example", "域名"),
):
    host, problem = egress.validate_host(raw)
    check(f"{raw!r} 被拒绝（含「{keyword}」）", not host and keyword in problem, problem)

print("\n通配符被拒是有意的")
# 放开通配符等于把"逐家审核"变回"谁都行"，所以宁可让人多填几行
_, problem = egress.validate_host("*.volces.com")
check("提示里说清楚为什么", "通配符" in problem and "空话" in problem, problem)

print("\n库里的域名与 .env 取并集")
saved_query = db.query
saved_one = db.query_one
try:
    added = {"api.example.com"}
    db.query = lambda sql, params=None: (
        [{"host": "api.example.com", "note": "测试", "added_by": "admin", "created_at": None}]
        if "FROM approved_host" in sql else []
    )
    check("页面上加的域名被批准",
          llm.classify("https://api.example.com/v1") == "approved")
    check("子域也批准", llm.classify("https://eu.api.example.com/v1") == "approved")
    check("仿冒域名不批准", llm.classify("https://evil-api.example.com/v1") == "unapproved")
    check(".env 基线仍然有效", llm.classify("https://api.deepseek.com") == "approved")

    # 库不可用时不能把整条链路拖垮：应当安静回落成"只有 .env 基线"
    def boom(*args, **kwargs):
        raise RuntimeError("database down")
    db.query = boom
    check("库挂了不影响 .env 那条路", llm.classify("https://api.deepseek.com") == "approved")
    check("库挂了时未收录域名按未批准处理",
          llm.classify("https://api.example.com/v1") == "unapproved")

    print("\n写入与审计")
    seen: list[tuple[str, tuple | None]] = []
    db.query = lambda sql, params=None: seen.append((sql, params)) or []
    db.query_one = lambda sql, params=None: None
    settings.add_host("https://API.example.org/v2", "换了个供应商", "admin-1")
    inserts = [sql for sql, _ in seen]
    check("先插 approved_host",
          any("INSERT INTO approved_host" in sql for sql in inserts), str(len(inserts)))
    check("再写 audit_log",
          any("audit_log" in sql and "host.allow" in sql for sql in inserts))
    host_param = next(params[0] for sql, params in seen if "INSERT INTO approved_host" in sql)
    check("存进去的是规范化后的裸域名", host_param == "api.example.org", str(host_param))

    print("\n重复添加走 409 而不是静默成功")
    db.query = lambda sql, params=None: []
    db.query_one = lambda sql, params=None: {"host": "api.example.com", "note": ""}
    check("能查出现有记录", settings.get_host("api.example.com") is not None)

    print("\n撤销")
    seen.clear()
    db.query = lambda sql, params=None: seen.append((sql, params)) or []
    db.query_one = lambda sql, params=None: {"host": "api.example.com"}
    removed = settings.remove_host("api.example.com", "admin-1")
    check("返回 True", removed is True)
    check("执行了 DELETE",
          any("DELETE FROM approved_host" in sql for sql, _ in seen))
    check("撤销也留痕",
          any("host.revoke" in sql for sql, _ in seen))

    db.query_one = lambda sql, params=None: None
    check("撤不存在的返回 False，前端才能说实话",
          settings.remove_host("gone.example.com", "admin-1") is False)

    print("\ninternal-only 是硬锁：页面加了也不生效")
    saved_mode = egress.EGRESS_MODE
    egress.EGRESS_MODE = "internal-only"
    state = settings.mode()
    check("can_edit 为 false", state["can_edit"] is False)
    check("说明了原因", "internal-only" in state["reason"])
    try:
        settings.add_host("api.blocked.com", "试试", "admin-1")
        check("internal-only 下拒绝新增", False, "居然加进去了")
    except ValueError as error:
        check("internal-only 下拒绝新增", "internal-only" in str(error), str(error))
    egress.EGRESS_MODE = saved_mode
finally:
    db.query = saved_query
    db.query_one = saved_one

print("\n屏蔽：连 .env 基线也能拿掉（前端唯一能撤掉内置域名的方式）")
saved_query = db.query
saved_one = db.query_one
try:
    # 黑名单优先于白名单，且子域一起挡——屏蔽 sophnet.com 却放过 www.sophnet.com
    # 等于没屏蔽，而人不会记得两个都填
    blocked = {"sophnet.com"}
    db.query = lambda sql, params=None: (
        [{"host": "sophnet.com", "note": "合规", "blocked_by": "admin", "created_at": None}]
        if "FROM blocked_host" in sql else []
    )
    db.query_one = lambda sql, params=None: None
    check("基线域名被屏蔽后为 blocked", llm.classify("https://sophnet.com/api") == "blocked")
    check("子域一起挡", llm.classify("https://www.sophnet.com/api") == "blocked")
    check("没屏蔽的基线照常", llm.classify("https://api.deepseek.com") == "approved")

    # 屏蔽优先于"页面上加的"，否则加一遍再屏蔽一遍就成了拉锯
    db.query = lambda sql, params=None: (
        [{"host": "sophnet.com", "note": "", "blocked_by": "admin", "created_at": None}]
        if "FROM blocked_host" in sql
        else [{"host": "sophnet.com", "note": "", "added_by": "admin", "created_at": None}]
        if "FROM approved_host" in sql
        else []
    )
    check("屏蔽压过白名单", llm.classify("https://sophnet.com/api") == "blocked")

    print("\n屏蔽的写入、审计与恢复")
    seen: list[tuple[str, tuple | None]] = []
    db.query = lambda sql, params=None: seen.append((sql, params)) or []
    settings.block_host("https://WWW.Sophnet.com/x", "该厂商出了合规问题", "admin-1")
    check("写进 blocked_host",
          any("INSERT INTO blocked_host" in sql for sql, _ in seen))
    check("写审计 host.block",
          any("host.block" in sql for sql, _ in seen))
    host_param = next(params[0] for sql, params in seen if "INSERT INTO blocked_host" in sql)
    check("存规范化域名", host_param == "www.sophnet.com", str(host_param))

    seen.clear()
    db.query_one = lambda sql, params=None: {"host": "www.sophnet.com"}
    check("恢复返回 True", settings.unblock_host("www.sophnet.com", "admin-1") is True)
    check("恢复留痕 host.unblock", any("host.unblock" in sql for sql, _ in seen))

    db.query_one = lambda sql, params=None: None
    check("恢复不存在的返回 False",
          settings.unblock_host("gone.example.com", "admin-1") is False)

    print("\n屏蔽不受 internal-only 限制（它只会少发数据）")
    saved_mode = egress.EGRESS_MODE
    egress.EGRESS_MODE = "internal-only"
    seen.clear()
    db.query = lambda sql, params=None: seen.append((sql, params)) or []
    db.query_one = lambda sql, params=None: None
    settings.block_host("api.deepseek.com", "临时全禁", "admin-1")
    check("internal-only 下仍可屏蔽",
          any("INSERT INTO blocked_host" in sql for sql, _ in seen))
    egress.EGRESS_MODE = saved_mode
finally:
    db.query = saved_query
    db.query_one = saved_one

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"出校域名测试全部通过（{checks} 项）。")
