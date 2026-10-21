"""学生可见范围：按验证等级决定学生端能拿到什么。

为什么需要这一层：审核不是二元的。一道题的可信程度至少有四档
（数据在 `v_exercise_verification`，不是某个页面算出来的）：

    none       导入进来、没人看过
    lean       Lean 证明编译通过，且形式化忠实性经人工确认（lean_proof.faithful_status）
    human      人工逐题审核通过（exercise.human_verified_at）
    strongest  两者都有

学校在不同阶段要的口径不一样——开发期只能全放，正式上课要"只放人工审核过的"。
这个开关就是那句口径，落在库里，而不是每个页面各写一个 if。页面上写着"仅人工审核"、
接口却把未审核的题也发出去，是最难被发现的一类不一致。

与发布闸的关系（两层不是替代关系）：
    review_status = 'published'   决定这道题"能不能出现"
    visibility_policy            决定"哪一档可信度才允许出现"
两者是 AND：先发布，再按等级过滤。

默认 all：与引入本表之前的行为完全一致，升级不会让学生端突然空掉。
"""

from __future__ import annotations

import json

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import Actor, require

router = APIRouter(prefix="/api/v1/visibility", tags=["visibility"])

# 从严到宽。列表顺序同时用于页面上选项的展示顺序。
TIERS: tuple[str, ...] = ("human", "verified", "all")

TIER_LABELS: dict[str, str] = {
    "human": "仅人工审核通过",
    "verified": "Lean 验证 + 人工审核",
    "all": "全部（含未验证）",
}

# 每一档允许看到的验证等级。三档是包含关系（human ⊂ verified ⊂ all），
# 这条不变式由测试钉住：写错一个名字，学生端会静默少一批题或多一批题。
TIER_RULES: dict[str, tuple[str, ...]] = {
    "human": ("human", "strongest"),
    "verified": ("lean", "human", "strongest"),
    "all": ("none", "lean", "human", "strongest"),
}

DEFAULT_POLICY: dict[str, Any] = {"exercise_tier": "all", "qa_tier": "all"}


class PolicyIn(BaseModel):
    exercise_tier: str
    qa_tier: str
    note: str | None = None


def allowed(setting: str) -> list[str]:
    """把设置值翻译成允许的验证等级列表。未知值直接 400，不静默降级。"""
    if setting not in TIER_RULES:
        raise HTTPException(
            status_code=400,
            detail=f"未知的可见范围：{setting}（可选：{'、'.join(TIERS)}）",
        )
    return list(TIER_RULES[setting])


def get_policy(course_id: str | None = None) -> dict[str, Any]:
    """读当前策略。表不存在或没有行时返回默认值——读路径不该因为配置缺失而 500。"""
    try:
        row = db.query_one(
            """SELECT exercise_tier, qa_tier, note, updated_at, updated_by
               FROM visibility_policy WHERE id = 'default'"""
        )
    except Exception:  # noqa: BLE001 - 迁移未跑时退到默认，避免整站不可用
        return dict(DEFAULT_POLICY)
    return row or dict(DEFAULT_POLICY)


def exercise_filter(setting: str | None = None) -> tuple[str, tuple[list[str], ...]]:
    """给目录/检索用的 SQL 片段与参数：`v.tier = ANY(%s::text[])`。"""
    tiers = allowed(setting or get_policy()["exercise_tier"])
    return "v.tier = ANY(%s::text[])", (tiers,)


def tier_counts(course_id: str | None = None) -> list[dict[str, Any]]:
    """各验证等级的题量，以及其中已发布（真的会出现在学生端）的数量。"""
    rows = db.query(
        """
        SELECT v.tier,
               count(*)::int AS total,
               count(*) FILTER (WHERE e.review_status = 'published')::int AS published
        FROM v_exercise_verification v
        JOIN exercise e ON e.course_id = v.course_id AND e.id = v.id
        WHERE (%s::text IS NULL OR e.course_id = %s)
        GROUP BY v.tier
        """,
        (course_id, course_id),
    )
    order = {"none": 0, "lean": 1, "human": 2, "strongest": 3}
    return sorted(rows, key=lambda row: order.get(row["tier"], 9))


def visible_published(setting: str, course_id: str | None = None) -> int:
    """这一档下，学生实际能看到的已发布题量。用于页面上"切换前先看后果"。"""
    tiers = allowed(setting)
    row = db.query_one(
        """
        SELECT count(*)::int AS n
        FROM v_exercise_verification v
        JOIN exercise e ON e.course_id = v.course_id AND e.id = v.id
        WHERE e.review_status = 'published'
          AND v.tier = ANY(%s::text[])
          AND (%s::text IS NULL OR e.course_id = %s)
        """,
        (tiers, course_id, course_id),
    )
    return int(row["n"]) if row else 0


@router.get("")
def read(course_id: str | None = None) -> dict:
    policy = get_policy()
    counts = tier_counts(course_id)
    published_total = sum(int(row["published"]) for row in counts)
    return {
        "exercise_tier": policy["exercise_tier"],
        "qa_tier": policy["qa_tier"],
        "note": policy.get("note"),
        "updated_at": policy.get("updated_at"),
        "updated_by": policy.get("updated_by"),
        "labels": TIER_LABELS,
        "options": list(TIERS),
        "rules": {key: list(value) for key, value in TIER_RULES.items()},
        "tiers": counts,
        "total": sum(int(row["total"]) for row in counts),
        "published_total": published_total,
        # 每一档各自会放出多少题：界面上先给后果，再让人点保存。
        "impact": {key: visible_published(key, course_id) for key in TIERS},
    }


@router.put("")
def update(body: PolicyIn, actor: Actor = Depends(require("teacher", "reviewer", "admin"))) -> dict:
    allowed(body.exercise_tier)
    allowed(body.qa_tier)
    db.execute(
        """UPDATE visibility_policy
           SET exercise_tier = %s, qa_tier = %s, note = %s,
               updated_at = now(), updated_by = %s
           WHERE id = 'default'""",
        (body.exercise_tier, body.qa_tier, body.note, actor.id),
    )
    # detail 是 jsonb，必须传 JSON 字符串并显式转型——直接传 "exercise=human" 会被
    # PG 当成非法 JSON 拒绝（端到端测试踩过一次）。
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'visibility.update', 'visibility_policy', 'default', %s::jsonb)""",
        (actor.id,
         json.dumps({"exercise_tier": body.exercise_tier, "qa_tier": body.qa_tier,
                     "note": body.note}, ensure_ascii=False)),
    )
    result = read()
    result["updated_by"] = actor.id
    return result
