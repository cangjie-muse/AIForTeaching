"""学生可见范围：按验证等级与作用域决定学生端能拿到什么。

三层设计，各自回答一个问题：

    visibility_policy      "哪一档可信度才允许出现"（口径）
    review_status          "工作流走到哪一步"（发布闸）
    course / exercise 数据 "这条内容是什么"

口径有两级：全局默认（`course_id IS NULL`）+ 课程覆盖（`course_id = 课程号`）。
查询时课程覆盖优先，没有覆盖就跟随全局——"数学分析严格、试点课全放"不需要两套代码。

三个维度各自独立：

    exercise_tier  human / verified / all   习题与测验
    qa_tier        human / verified / all   AI 问答的完整解答模式
    graph_tier     human / all              知识图谱（只有人工这一档）

为什么图谱只有人工：它不是定理，谈不上"形式化验证"；审的是"这条图谱挂得对不对"。
为什么教材片段不设审核：切片是原文转载，审的是作者而不是我们。

和发布闸的关系：review_status = 'published' 决定"能不能出现"，本策略决定
"哪一档可信度才允许出现"。两者是 AND，不是替代。
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

# Lean 那一层还没接产线：界面上要如实标出来，别让人以为选了就有 Lean 兜底。
TIER_HINTS: dict[str, str] = {
    "human": "",
    "verified": "Lean 验证尚未接入，当前效果与「仅人工审核」相同",
    "all": "",
}

GRAPH_TIERS: tuple[str, ...] = ("all", "human")

GRAPH_LABELS: dict[str, str] = {
    "all": "全部（含未审核节点）",
    "human": "仅人工审核通过",
}

# 每一档允许看到的验证等级。三档是包含关系（human ⊂ verified ⊂ all），
# 这条不变式由测试钉住：写错一个名字，学生端会静默少一批题或多一批题。
TIER_RULES: dict[str, tuple[str, ...]] = {
    "human": ("human", "strongest"),
    "verified": ("lean", "human", "strongest"),
    "all": ("none", "lean", "human", "strongest"),
}

DEFAULT_POLICY: dict[str, Any] = {
    "exercise_tier": "all",
    "qa_tier": "all",
    "graph_tier": "all",
}

_COLUMNS = "id, course_id, exercise_tier, qa_tier, graph_tier, note, updated_at, updated_by"


class PolicyIn(BaseModel):
    exercise_tier: str
    qa_tier: str
    graph_tier: str = "all"
    note: str | None = None


# ---------------------------------------------------------------------------
# 作用域解析
# ---------------------------------------------------------------------------


def allowed(setting: str) -> list[str]:
    """把设置值翻译成允许的验证等级列表。未知值直接 400，不静默降级。"""
    if setting not in TIER_RULES:
        raise HTTPException(
            status_code=400,
            detail=f"未知的可见范围：{setting}（可选：{'、'.join(TIERS)}）",
        )
    return list(TIER_RULES[setting])


def graph_allowed(setting: str) -> bool:
    """图谱这一档：True 表示不限（全放），False 表示只要人工审核过的节点。"""
    if setting not in GRAPH_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"未知的图谱可见范围：{setting}（可选：{'、'.join(GRAPH_TIERS)}）",
        )
    return setting == "all"


def _row(course_id: str | None) -> dict | None:
    """读某一作用域的行。course_id 为 None 时读全局行。"""
    if course_id is None:
        return db.query_one(
            f"SELECT {_COLUMNS} FROM visibility_policy WHERE course_id IS NULL")
    return db.query_one(
        f"SELECT {_COLUMNS} FROM visibility_policy WHERE course_id = %s", (course_id,))


def resolve(global_row: dict | None, course_row: dict | None) -> dict[str, Any]:
    """合并全局与课程覆盖，得到生效口径。

    纯函数：端到端测试与单元测试都直接喂行，不需要数据库。
    规则只有一条——课程有覆盖用课程，否则整行跟随全局。
    """
    chosen = course_row or global_row
    source = "course" if course_row else ("global" if global_row else "default")
    effective = dict(DEFAULT_POLICY)
    if chosen:
        for key in DEFAULT_POLICY:
            if chosen.get(key):
                effective[key] = chosen[key]
    return {
        "exercise_tier": effective["exercise_tier"],
        "qa_tier": effective["qa_tier"],
        "graph_tier": effective["graph_tier"],
        "source": source,
        "note": (chosen or {}).get("note"),
        "updated_at": (chosen or {}).get("updated_at"),
        "updated_by": (chosen or {}).get("updated_by"),
    }


def get_policy(course_id: str | None = None) -> dict[str, Any]:
    """生效口径。迁移未跑或没有行时退到默认值——读路径不该因为配置缺失而 500。"""
    try:
        global_row = _row(None)
        course_row = _row(course_id) if course_id else None
    except Exception:  # noqa: BLE001 - 迁移没跑时整站仍可用
        return dict(DEFAULT_POLICY, source="default")
    return resolve(global_row, course_row)


def exercise_filter(
    course_id: str | None = None, setting: str | None = None
) -> tuple[str, tuple[list[str], ...]]:
    """给目录/检索用的 SQL 片段与参数：`v.tier = ANY(%s::text[])`。

    setting 显式给出时不读库——测试要能直接验"human 档会拼出什么"，而不是先造一份配置。
    """
    tiers = allowed(setting or get_policy(course_id)["exercise_tier"])
    return "v.tier = ANY(%s::text[])", (tiers,)


def node_filter(course_id: str | None = None, setting: str | None = None) -> tuple[str, tuple]:
    """知识图谱可见性片段。全放时返回恒真，SQL 里仍能统一拼接。"""
    if graph_allowed(setting or get_policy(course_id)["graph_tier"]):
        return "TRUE", ()
    return "n.human_verified_at IS NOT NULL", ()


# ---------------------------------------------------------------------------
# 统计：先给后果，再让人点保存
# ---------------------------------------------------------------------------


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
    """这一档下，学生实际能看到的已发布题量。"""
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


def node_counts(course_id: str | None = None) -> dict[str, int]:
    """图谱节点：总数 / 已人工审核 / 未审核（retired 不算）。"""
    row = db.query_one(
        """
        SELECT count(*)::int AS total,
               count(*) FILTER (WHERE human_verified_at IS NOT NULL)::int AS verified
        FROM knowledge_node n
        WHERE n.review_status <> 'retired'
          AND (%s::text IS NULL OR n.course_id = %s)
        """,
        (course_id, course_id),
    ) or {"total": 0, "verified": 0}
    return {
        "total": int(row["total"]),
        "verified": int(row["verified"]),
        "pending": int(row["total"]) - int(row["verified"]),
    }


def courses_payload() -> list[dict[str, Any]]:
    """课程清单 + 各自是否有覆盖，供页面上切换作用域。"""
    rows = db.query(
        """
        SELECT c.id, c.title, p.exercise_tier, p.qa_tier, p.graph_tier
        FROM course c
        LEFT JOIN visibility_policy p ON p.course_id = c.id
        ORDER BY c.id
        """
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "has_override": row["exercise_tier"] is not None,
            "exercise_tier": row["exercise_tier"],
            "qa_tier": row["qa_tier"],
            "graph_tier": row["graph_tier"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.get("")
def read(course_id: str | None = None) -> dict:
    effective = get_policy(course_id)
    counts = tier_counts(course_id)
    nodes = node_counts(course_id)
    global_row = _row(None) or {}
    override = (_row(course_id) or None) if course_id else None
    return {
        "scope": "course" if course_id else "global",
        "course_id": course_id,
        # 生效口径（可能是课程覆盖，也可能是全局）
        "effective": {
            "exercise_tier": effective["exercise_tier"],
            "qa_tier": effective["qa_tier"],
            "graph_tier": effective["graph_tier"],
        },
        "source": effective["source"],
        "note": effective.get("note"),
        "updated_at": effective.get("updated_at"),
        "updated_by": effective.get("updated_by"),
        # 全局与课程覆盖都返回：页面要显示"跟随全局"还是"本课单独设置"
        "global": {
            "exercise_tier": global_row.get("exercise_tier", "all"),
            "qa_tier": global_row.get("qa_tier", "all"),
            "graph_tier": global_row.get("graph_tier", "all"),
        },
        "override": override and {
            "exercise_tier": override["exercise_tier"],
            "qa_tier": override["qa_tier"],
            "graph_tier": override["graph_tier"],
            "updated_at": override.get("updated_at"),
            "updated_by": override.get("updated_by"),
        },
        "labels": TIER_LABELS,
        "hints": TIER_HINTS,
        "options": list(TIERS),
        "graph_labels": GRAPH_LABELS,
        "graph_options": list(GRAPH_TIERS),
        "rules": {key: list(value) for key, value in TIER_RULES.items()},
        "tiers": counts,
        "total": sum(int(row["total"]) for row in counts),
        "published_total": sum(int(row["published"]) for row in counts),
        # 每一档各自会放出多少题：界面上先给后果，再让人点保存。
        "impact": {key: visible_published(key, course_id) for key in TIERS},
        "graph": {**nodes, "impact": {"all": nodes["total"], "human": nodes["verified"]}},
        "courses": courses_payload() if course_id is None else [],
    }


@router.put("")
def update(
    body: PolicyIn,
    course_id: str | None = None,
    actor: Actor = Depends(require("teacher", "reviewer", "admin")),
) -> dict:
    allowed(body.exercise_tier)
    allowed(body.qa_tier)
    graph_allowed(body.graph_tier)
    if course_id:
        known = db.query_one("SELECT 1 FROM course WHERE id = %s", (course_id,))
        if not known:
            raise HTTPException(status_code=404, detail=f"没有这门课：{course_id}")
        row_id = f"course:{course_id}"
    else:
        row_id = "default"
    # 全局行与课程覆盖都走 upsert，靠部分唯一索引保证一个作用域只有一行。
    db.execute(
        """
        INSERT INTO visibility_policy (id, course_id, exercise_tier, qa_tier, graph_tier,
                                       note, updated_at, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (id) DO UPDATE
        SET exercise_tier = EXCLUDED.exercise_tier,
            qa_tier = EXCLUDED.qa_tier,
            graph_tier = EXCLUDED.graph_tier,
            note = EXCLUDED.note,
            updated_at = now(),
            updated_by = EXCLUDED.updated_by
        """,
        (row_id, course_id or None, body.exercise_tier, body.qa_tier, body.graph_tier,
         body.note, actor.id),
    )
    # detail 是 jsonb，必须传 JSON 字符串并显式转型——直接传 "exercise=human" 会被
    # PG 当成非法 JSON 拒绝（端到端测试踩过一次）。
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'visibility.update', 'visibility_policy', %s, %s::jsonb)""",
        (actor.id, course_id or "default",
         json.dumps({"course_id": course_id, "exercise_tier": body.exercise_tier,
                     "qa_tier": body.qa_tier, "graph_tier": body.graph_tier,
                     "note": body.note}, ensure_ascii=False)),
    )
    result = read(course_id)
    result["updated_by"] = actor.id
    return result


@router.delete("")
def drop_override(
    course_id: str,
    actor: Actor = Depends(require("teacher", "reviewer", "admin")),
) -> dict:
    """取消某门课的单独设置，让它回到跟随全局。"""
    deleted = db.execute(
        "DELETE FROM visibility_policy WHERE course_id = %s", (course_id,))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"这门课没有单独设置：{course_id}")
    db.execute(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'visibility.reset', 'visibility_policy', %s, %s::jsonb)""",
        (actor.id, course_id, json.dumps({"course_id": course_id}, ensure_ascii=False)),
    )
    return read(course_id)
