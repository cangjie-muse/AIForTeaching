#!/usr/bin/env python
"""习题三维标签的单测：难度档 / 来源 / 可信度。

标签是**派生值**：接口每次都按字段重算。所以这里守的是两个性质：
  1. 三个轴各自映射对（分界线、未知值兜底）；
  2. `labels` / `codes` / `text` 三者永远同源，不会出现"徽标写基础、筛选给 challenge"。
不依赖数据库，直接调函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import exercise_tags as tags  # noqa: E402

checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


print("难度分档：1–2 基础 / 3 中等 / 4–5 挑战")
for value, expected in [(1, "basic"), (2, "basic"), (3, "medium"), (4, "challenge"), (5, "challenge")]:
    check(f"难度 {value} → {expected}", tags.difficulty_band(value) == expected)

print("\n非法难度一律按未标处理，不猜")
for value in [None, 0, 6, -3, "abc", 3.7]:
    check(f"{value!r} → unknown", tags.difficulty_band(value) == "unknown")

print("\n来源映射：按 exercise_provenance 的枚举一一对应")
for code, label in [("textbook", "教材原题"), ("colleague-export", "同事导出"),
                    ("self-authored", "平台自拟"), ("ai-generated", "模型生成"),
                    ("web", "网上搜集"), ("unknown", "来源未标")]:
    result = tags.tags_for({"difficulty": 3, "provenance": code, "tier": "human"})
    check(f"{code} → {label}", result["labels"][1] == label and result["codes"]["origin"] == code)

print("\n来源为空或乱码时兜底，不把 None 打到页面上")
for value in [None, "", "made-up"]:
    result = tags.tags_for({"difficulty": 3, "provenance": value, "tier": "human"})
    check(f"{value!r} → 来源未标", result["labels"][1] == "来源未标" and result["codes"]["origin"] == "unknown")

print("\n可信度映射：与 v_exercise_verification.tier 对齐")
for code, label in [("strongest", "双证已核"), ("human", "人工已核"),
                    ("lean", "Lean 已验"), ("none", "未核"), (None, "未核")]:
    result = tags.tags_for({"difficulty": 3, "provenance": "textbook", "tier": code})
    check(f"{code!r} → {label}", result["labels"][2] == label and result["codes"]["trust"] == (code or "none"))

print("\n三者同源：labels / codes / text 不能各说各话")
cases = [
    {"difficulty": 1, "provenance": "textbook", "tier": "human"},
    {"difficulty": 5, "provenance": "self-authored", "tier": "none"},
    {"difficulty": None, "provenance": None, "tier": None},
]
for case in cases:
    result = tags.tags_for(case)
    check(f"三枚标签数量固定：{len(result['labels'])}", len(result["labels"]) == 3)
    check("text 就是三枚标签用 · 连接",
          result["text"] == " · ".join(result["labels"]), result["text"])
    check("codes 的三个键都在",
          set(result["codes"]) == {"difficulty", "origin", "trust"}, str(result["codes"]))
    check("labels 与 codes 指向同一本字典",
          result["labels"][0] == tags.DIFFICULTY_BANDS[result["codes"]["difficulty"]]
          and result["labels"][1] == tags.ORIGINS[result["codes"]["origin"]]
          and result["labels"][2] == tags.TRUST[result["codes"]["trust"]])

print("\n真实取值抽查（复变 402 道题的三种典型形态）")
samples = [
    ({"difficulty": 4, "provenance": "colleague-export", "tier": "human"}, "挑战 · 同事导出 · 人工已核"),
    ({"difficulty": 3, "provenance": "self-authored", "tier": "human"}, "中等 · 平台自拟 · 人工已核"),
    ({"difficulty": 2, "provenance": "textbook", "tier": "none"}, "基础 · 教材原题 · 未核"),
]
for raw, expected in samples:
    result = tags.tags_for(raw)
    check(expected, result["text"] == expected, result["text"])

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"习题三维标签测试全部通过（{checks} 项）。")
