"""可见范围的作用域解析与图谱档位（纯函数，不连数据库）。

测三件容易静默出错的事：
1. 课程有覆盖 → 用课程；没有 → 跟随全局。写反了，一门试点课的宽松口径会污染整校。
2. 课程只覆盖了部分字段时，其余字段仍跟随全局（合并，不是整行替换）。
3. 图谱只有 all / human 两档，未知值必须拒绝。
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


GLOBAL = {"exercise_tier": "all", "qa_tier": "all", "graph_tier": "all", "note": "全校默认"}
COURSE_STRICT = {"exercise_tier": "human", "qa_tier": "human", "graph_tier": "human",
                 "note": "数学分析要严"}
COURSE_PARTIAL = {"exercise_tier": "verified", "qa_tier": None, "graph_tier": None}


print("档位的包含关系：")
human = set(visibility.allowed("human"))
verified = set(visibility.allowed("verified"))
everything = set(visibility.allowed("all"))
check("仅人工审核 = 人工 + 双重", human == {"human", "strongest"}, str(sorted(human)))
check("Lean 档包含仅人工档", human <= verified, str(sorted(verified)))
check("全部档包含 Lean 档", verified <= everything, str(sorted(everything)))
check("三档严格递增", len(human) < len(verified) < len(everything),
      f"{len(human)} < {len(verified)} < {len(everything)}")

print("\n未知档位不能静默降级：")
check("未知值抛 400", raises_400("whatever"))
check("空字符串抛 400", raises_400(""))
check("大小写不一致也抛 400（不猜意图）", raises_400("HUMAN"))

print("\n作用域解析：")
global_only = visibility.resolve(GLOBAL, None)
check("没有课程覆盖时跟随全局", global_only["exercise_tier"] == "all"
      and global_only["source"] == "global", str(global_only["source"]))
strict = visibility.resolve(GLOBAL, COURSE_STRICT)
check("课程有覆盖时用课程", strict["exercise_tier"] == "human", strict["exercise_tier"])
check("课程覆盖标记来源", strict["source"] == "course", strict["source"])
check("课程覆盖也带上了自己的修改信息", strict["note"] == "数学分析要严", str(strict["note"]))

partial = visibility.resolve(GLOBAL, COURSE_PARTIAL)
check("只覆盖习题时，问答仍跟随全局", partial["qa_tier"] == "all", partial["qa_tier"])
check("只覆盖习题时，图谱仍跟随全局", partial["graph_tier"] == "all", partial["graph_tier"])
check("被覆盖的字段用课程值", partial["exercise_tier"] == "verified", partial["exercise_tier"])

nothing = visibility.resolve(None, None)
check("库是空的也能给出默认口径", nothing == {**visibility.DEFAULT_POLICY, "source": "default",
                                             "note": None, "updated_at": None, "updated_by": None},
      str(nothing))
check("默认口径是全部（升级不改变行为）",
      nothing["exercise_tier"] == "all" and nothing["qa_tier"] == "all"
      and nothing["graph_tier"] == "all")

print("\n图谱档位：")
check("all 不限", visibility.graph_allowed("all") is True)
check("human 只放人工审核过的", visibility.graph_allowed("human") is False)
try:
    visibility.graph_allowed("lean")
    check("未知图谱档位抛 400", False, "没有拒绝 lean")
except HTTPException as exc:
    check("未知图谱档位抛 400", exc.status_code == 400, str(exc.status_code))

print("\nSQL 片段：")
fragment, params = visibility.exercise_filter(setting="human")
check("片段按验证等级过滤", fragment == "v.tier = ANY(%s::text[])", fragment)
check("参数是列表（psycopg 需要数组）", isinstance(params[0], list), str(type(params[0])))
check("参数与档位一致", params[0] == ["human", "strongest"], str(params[0]))
check("每次调用返回新列表（调用方改不坏策略）",
      visibility.exercise_filter(setting="human")[1][0] is not params[0])

node_sql, node_params = visibility.node_filter(setting="all")
check("图谱全放时片段恒真（SQL 可统一拼接）", node_sql == "TRUE" and node_params == (),
      f"{node_sql} {node_params}")
node_sql, node_params = visibility.node_filter(setting="human")
check("图谱严格时只放人工审核过的节点",
      node_sql == "n.human_verified_at IS NOT NULL" and node_params == (), node_sql)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("可见范围单元测试全部通过。")
