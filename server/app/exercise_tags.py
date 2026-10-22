"""习题的三维标签：难度档、来源、可信度。

口径是 2026-10-22 定的——「三个维度都要，标签是组合」。一道题同时落在三个轴上：

  难度档   基础（1–2）/ 中等（3）/ 挑战（4–5）      ← exercise.difficulty
  来源     教材原题 / 同事导出 / 平台自拟 / 模型生成 / 网上搜集 / 来源未标
                                                  ← exercise.provenance
  可信度   双证已核 / 人工已核 / Lean 已验 / 未核    ← v_exercise_verification.tier

**不建标签表、不落库**，因为三个维度本来就是已有字段的派生值：存一份标签，
字段一改标签就过期，然后没人知道该信哪份。组合只在这里算一次，接口把
`tags.labels`（给人看的三枚）和 `tags.codes`（给筛选用的代码）一起发出去。

以后如果需要**自由打标**（比如"含参讨论""易错""考过三次"），那是另一回事：
加 `exercise.tags text[]` 列并和这三枚徽标并列显示，不要混进这个函数——
这个函数必须保持"纯派生、可重算、不会和字段打架"。
"""

from __future__ import annotations

from typing import Any

# 难度 1–5 → 三档。分界线放在 2/3 和 3/4 之间：1–2 是套定义就能做的，
# 3 要转一个弯，4–5 是要自己找路子的（多步证明、构造反例）。
DIFFICULTY_BANDS: dict[str, str] = {
    "basic": "基础",
    "medium": "中等",
    "challenge": "挑战",
    "unknown": "难度未标",
}

ORIGINS: dict[str, str] = {
    "textbook": "教材原题",
    "colleague-export": "同事导出",
    "self-authored": "平台自拟",
    "ai-generated": "模型生成",
    "web": "网上搜集",
    "unknown": "来源未标",
}

TRUST: dict[str, str] = {
    "strongest": "双证已核",
    "human": "人工已核",
    "lean": "Lean 已验",
    "none": "未核",
}


def difficulty_band(difficulty: Any) -> str:
    """1–5 → basic / medium / challenge / unknown。非法值一律当未标。"""
    try:
        value = int(difficulty)
    except (TypeError, ValueError):
        return "unknown"
    # 3.7 这种非整数的难度是坏数据（前端滑块只出整数），不当 3 处理——
    # 悄悄取整会让"难度 4 的题"被算进难度 3 的档里，而页面上写着 4/5。
    try:
        if float(difficulty) != value:
            return "unknown"
    except (TypeError, ValueError):
        pass
    if value <= 0 or value > 5:
        return "unknown"
    if value <= 2:
        return "basic"
    if value == 3:
        return "medium"
    return "challenge"


def tags_for(exercise: dict[str, Any]) -> dict[str, Any]:
    """一道题的三维标签。

    `labels` 是给人看的三枚徽标（顺序固定：难度 → 来源 → 可信度），
    `codes` 给前端做筛选，`text` 用于搜索框里直接搜"人工已核"这类词。
    """
    band = difficulty_band(exercise.get("difficulty"))
    origin = exercise.get("provenance") if exercise.get("provenance") in ORIGINS else "unknown"
    trust = exercise.get("tier")
    # codes 里永远是字符串：前端拿它和下拉框的 value 比，None 会让比较永远不成立。
    trust_key = trust if isinstance(trust, str) and trust in TRUST else "none"
    labels = [DIFFICULTY_BANDS[band], ORIGINS[origin], TRUST[trust_key]]
    return {
        "labels": labels,
        "codes": {"difficulty": band, "origin": origin, "trust": trust_key},
        "text": " · ".join(labels),
    }
