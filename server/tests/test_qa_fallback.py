#!/usr/bin/env python
"""知识库没依据时的兜底回答（不连库、不连模型）。

这块的风险是"标注被悄悄弄丢"：模型忘记声明、前端漏渲染、接口字段没传，
任何一种都会让通用知识看起来像教材结论。所以四条都测：
开关、提示词、正文标注（幂等）、接口字段。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "server"))

from fastapi import HTTPException  # noqa: E402

from app import qa  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def ask_with(rows, answer="这是模型的通用回答。", env=None, fake_model=False):
    """把 ask() 的依赖全部换成假的，只看决策与标注。"""
    saved = {}
    for key in ("QA_FALLBACK", "DEV_FAKE_MODEL"):
        saved[key] = getattr(qa, key, None)

    qa.retrieve = lambda *args: rows
    qa._conversation_id = lambda *args: "11111111-1111-1111-1111-111111111111"
    qa._record_call = lambda **kwargs: None
    qa.DEV_FAKE_MODEL = fake_model
    if env is None:
        os.environ.pop("QA_FALLBACK", None)
    else:
        os.environ["QA_FALLBACK"] = env

    calls = []

    def fake_chat(messages, temperature=0.2):
        calls.append(messages)
        return {"content": answer, "provider": "fake", "model": "fake-1", "usage": {}}

    qa.llm.chat = fake_chat
    qa.llm.MissingKey = qa.llm.MissingKey  # 保持类型

    saved_turns = []
    real_save = qa._save_turn
    qa._save_turn = lambda *args, **kwargs: saved_turns.append((args, kwargs)) or 1
    try:
        body = qa.AskIn(question="什么是勒贝格积分？")
        result = qa.ask(body, actor=object())
        return result, calls, saved_turns
    finally:
        qa._save_turn = real_save
        for key, value in saved.items():
            if value is None:
                pass
        qa.DEV_FAKE_MODEL = saved["DEV_FAKE_MODEL"]


print("开关：默认允许兜底，QA_FALLBACK=off 关闭")
os.environ.pop("QA_FALLBACK", None)
check("默认开启", qa.fallback_enabled() is True)
os.environ["QA_FALLBACK"] = "off"
check("off 关闭", qa.fallback_enabled() is False)
os.environ["QA_FALLBACK"] = "OFF"
check("大小写不敏感", qa.fallback_enabled() is False)
os.environ["QA_FALLBACK"] = "model"
check("model 开启", qa.fallback_enabled() is True)
os.environ.pop("QA_FALLBACK", None)

print("\n正文标注：加在最前面，且不会重复加")
marked = qa.mark_unverified("正文")
check("开头是警示块", marked.startswith("> ⚠️"), marked[:12])
check("包含'未使用课程材料'", "未使用课程材料" in marked)
check("包含'可能有误'", "可能有误" in marked)
check("幂等：已标注的不重复加", qa.mark_unverified(marked) == marked)
check("空内容也能标", qa.mark_unverified("").startswith("> ⚠️"))

print("\n知识库没依据 + 兜底开启：调用模型、标注、字段 grounded=false")
result, calls, turns = ask_with([], answer="通用知识回答。")
check("调用了模型（而不是直接拒答）", len(calls) == 1)
check("接口字段 grounded=False", result["grounded"] is False, str(result.get("grounded")))
check("basis=model", result["basis"] == "model")
check("refused=False（我们答了，只是没依据）", result["refused"] is False)
check("回答带标注", result["answer_md"].startswith("> ⚠️"))
check("没有伪造引用", result["citations"] == [])
saved_basis = (turns[0][0][5] if len(turns[0][0]) > 5 else turns[0][1].get("basis")) if turns else None
check("落库记的是 basis=model", saved_basis == "model", str(saved_basis))
check("系统提示词要求不要编造页码", "不要编造教材章节" in calls[0][0]["content"])
check("只发问题，不发教材内容", "教材片段" not in calls[0][1]["content"])

print("\n知识库没依据 + 兜底关闭：拒答，且不调用模型")
result, calls, turns = ask_with([], env="off")
check("没有调用模型", calls == [])
check("refused=True", result["refused"] is True)
check("不再声称 grounded", result["grounded"] is True)
check("正文给的是拒答说明", "没有找到可以引用的依据" in result["answer_md"])

print("\n知识库有依据：正常路径，grounded=true")
row = {"chunk_id": 1, "content_md": "勒贝格积分是……", "page_from": 3, "page_to": 3,
       "node_id": None, "node_title": "测度论"}
result, calls, turns = ask_with([row], answer="依据教材的回答。")
check("调用了模型", len(calls) == 1)
check("grounded=True", result["grounded"] is True)
check("basis=course", result["basis"] == "course")
check("回答没被加警示块", not result["answer_md"].startswith("> ⚠️"))
check("上下文里有教材片段", "教材片段" in calls[0][1]["content"])

print("\n演示模式（没配模型）也要标注")
result, calls, turns = ask_with([], fake_model=True)
check("没有真的调用模型", calls == [])
check("回答仍带标注", result["answer_md"].startswith("> ⚠️"))
check("字段 grounded=False", result["grounded"] is False)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"兜底回答测试全部通过（{checks} 项）。")
