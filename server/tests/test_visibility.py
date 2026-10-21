"""学生可见范围（验证等级闸）的单元测试（不连数据库）。

测的是三件容易出错、且出错后很难被发现的事：

1. **档位是包含关系**：human ⊂ verified ⊂ all。写错一个等级名字，学生端会静默少题
   或多题——没有报错，只有"怎么少了几道题"这种事后才发现的问题。
2. **未知档位直接拒绝**：不能静默降级成"全部"，那等于把没审的题发给学生。
3. **默认值与引入本表之前一致**：all/all。升级不能让在读的学生突然看不到题。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import HTTPException  # noqa: E402

from app import visibility  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  ✗ {name}  — {detail}")


def raises_400(setting: str) -> bool:
    try:
        visibility.allowed(setting)
    except HTTPException as exc:
        return exc.status_code == 400
    return False


print("档位的包含关系：")
human = set(visibility.allowed("human"))
verified = set(visibility.allowed("verified"))
everything = set(visibility.allowed("all"))
check("仅人工审核 = 人工 + 双重", human == {"human", "strongest"}, str(sorted(human)))
check("Lean 档包含仅人工档", human <= verified, str(sorted(verified)))
check("全部档包含 Lean 档", verified <= everything, str(sorted(everything)))
check("三档严格递增", len(human) < len(verified) < len(everything),
      f"{len(human)} < {len(verified)} < {len(everything)}")
check("全部档覆盖四种验证等级",
      everything == {"none", "lean", "human", "strongest"}, str(sorted(everything)))

print("\n未知档位不能静默降级：")
check("未知值抛 400", raises_400("whatever"))
check("空字符串抛 400", raises_400(""))
check("大小写不一致也抛 400（不猜意图）", raises_400("HUMAN"))

print("\n标签与选项：")
check("每一档都有中文标签", all(key in visibility.TIER_LABELS for key in visibility.TIERS),
      str(list(visibility.TIER_LABELS)))
check("标签顺序与档位一致", list(visibility.TIERS) == ["human", "verified", "all"],
      str(list(visibility.TIERS)))
check("页面上按从严到宽展示", visibility.TIERS[0] == "human")

print("\n默认策略：")
check("习题默认全部", visibility.DEFAULT_POLICY["exercise_tier"] == "all")
check("问答默认全部", visibility.DEFAULT_POLICY["qa_tier"] == "all")
check("默认值本身合法", not raises_400(visibility.DEFAULT_POLICY["exercise_tier"]))

print("\nSQL 片段：")
fragment, params = visibility.exercise_filter("human")
check("片段按验证等级过滤", fragment == "v.tier = ANY(%s::text[])", fragment)
check("参数是列表（psycopg 需要数组）", isinstance(params[0], list), str(type(params[0])))
check("参数与档位一致", params[0] == ["human", "strongest"], str(params[0]))
check("每次调用返回新列表（调用方改不坏策略）",
      visibility.exercise_filter("human")[1][0] is not params[0])

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("可见范围单元测试全部通过。")
