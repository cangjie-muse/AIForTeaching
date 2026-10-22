#!/usr/bin/env python
"""平台开关（不连库）：默认值、优先级、审计、以及"改不动时要说清为什么"。

开关这种东西错了不会报错，只会静默地按错误的状态跑——学生看到的是
"这个助手怎么什么都不会"或"它怎么开始瞎编了"。所以优先级必须钉死。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "server"))

from fastapi import HTTPException  # noqa: E402

from app import admin, db, settings  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


class FakeDb:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or {}
        self.fail = fail
        self.writes = []

    def query_one(self, sql, params=()):
        if self.fail:
            raise RuntimeError("db down")
        # qa_fallback() 把 key 写在 SQL 字面量里（不带参数），这里两种情况都照顾到
        key = params[0] if params else "qa_fallback"
        return self.rows.get(key)

    def query(self, sql, params=()):
        self.writes.append((sql, params))
        return []


real = {"query_one": db.query_one, "query": db.query}

print("读默认值：没有记录时用默认，不让页面因为读配置而报错")
db.query_one = lambda sql, params=(): None
db.query = lambda sql, params=(): []
check("qa_fallback 默认开启", settings.get("qa_fallback") is True)
check("未知开关返回调用方给的默认值", settings.get("nope", "x") == "x")

print("\n库不可用：回落默认，不抛异常")
db.query_one = FakeDb(fail=True).query_one
check("库挂了也返回默认值", settings.get("qa_fallback") is True)
check("qa_fallback() 不抛", settings.qa_fallback()["enabled"] is True)

print("\n管理员关掉：库里 false 生效")
db.query_one = FakeDb({"qa_fallback": {"value": False}}).query_one
state = settings.qa_fallback()
check("enabled=False", state["enabled"] is False)
check("source=db", state["source"] == "db")
check("没被标记为强制", state["forced"] is False)

print("\njsonb 以字符串形式返回时也能解析（psycopg 两种都可能）")
db.query_one = FakeDb({"qa_fallback": {"value": "true"}}).query_one
check("字符串 'true' 解析成 True", settings.qa_fallback()["enabled"] is True)
db.query_one = FakeDb({"qa_fallback": {"value": "not-json"}}).query_one
check("坏 JSON 回落默认（开）", settings.qa_fallback()["enabled"] is True)

print("\n.env 一票否决：即使库里是开的也关")
os.environ["QA_FALLBACK"] = "off"
db.query_one = FakeDb({"qa_fallback": {"value": True}}).query_one
state = settings.qa_fallback()
check("enabled=False", state["enabled"] is False)
check("source=env", state["source"] == "env")
check("forced=True", state["forced"] is True)
os.environ["QA_FALLBACK"] = "OFF"
check("大小写不敏感", settings.qa_fallback()["forced"] is True)
os.environ.pop("QA_FALLBACK", None)

print("\n写开关：upsert + 审计")
fake = FakeDb()
db.query = fake.query
db.query_one = lambda sql, params=(): None
settings.set_value("qa_fallback", True, "acct-admin", note="测试")
check("写了两次（upsert + 审计）", len(fake.writes) == 2, str(len(fake.writes)))
check("第一条是 upsert 到 app_setting",
      "INSERT INTO app_setting" in fake.writes[0][0] and "ON CONFLICT" in fake.writes[0][0])
check("第二条是审计", "audit_log" in fake.writes[1][0])
check("审计动作是 setting.update", "setting.update" in fake.writes[1][0])
check("审计带上操作人", "acct-admin" in str(fake.writes[1][1]))
check("值以 JSON 写入", json.loads(fake.writes[0][1][1]) is True)

print("\n被强制关闭时，写接口要拒绝并说明原因（否则页面看起来像坏了）")
os.environ["QA_FALLBACK"] = "off"
db.query_one = lambda sql, params=(): None
try:
    admin.set_qa_fallback(admin.QaFallbackIn(enabled=True), actor=type("A", (), {"id": "x"})())
    check("应该拒绝", False, "居然写成功了")
    message = ""
except HTTPException as error:
    check("拒绝了", True)
    check("状态码 409（冲突，不是参数错）", error.status_code == 409, str(error.status_code))
    message = str(error.detail)
check("说明了改不动的原因", "QA_FALLBACK=off" in message and ".env" in message, message[:30])
os.environ.pop("QA_FALLBACK", None)

print("\n未被强制时可以正常写入")
fake = FakeDb()
db.query = fake.query
state = admin.set_qa_fallback(admin.QaFallbackIn(enabled=False),
                              actor=type("A", (), {"id": "acct-admin"})())
check("写入成功", len(fake.writes) == 2)
check("返回体里带 source", "source" in state)
check("返回体里带 note 字段（页面要显示）", "note" in state)

db.query_one = real["query_one"]
db.query = real["query"]

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"平台开关测试全部通过（{checks} 项）。")
