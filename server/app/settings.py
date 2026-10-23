"""平台开关：存在 `app_setting` 表里，管理员在前端改。

为什么不全放 .env：
  · 改 .env 要登服务器 + 重启，一个"要不要让学生看到通用知识回答"的决定
    不该需要运维介入；
  · 网页上点一下能留痕（audit_log 里有谁改的、改成什么）。

为什么不全都搬过来：密钥仍然只认 .env 或 secrets/ 下的文件——一个网页按钮
不该有发数据的权限。

出校域名是例外：它是"追加"，不是"放开"。`.env` 的基线一直在，页面只能往上加，
而且 `MODEL_EGRESS=internal-only` 依然一票否决；每次追加都留审计。
不加这个入口的代价很具体——换一个模型供应商就要登服务器、改文件、重启，
运维不在的时候整件事就卡住。
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import db, egress

# 开关名 → 默认值。新增开关在这里登记，前端就知道该问哪些。
DEFAULTS: dict[str, Any] = {
    "qa_fallback": True,   # 知识库没依据时，是否允许改用模型通用知识（会标注）
}


def get(key: str, default: Any = None) -> Any:
    """读一个开关。表不存在/库不可用时返回默认值，不让页面因为读配置而报错。"""
    fallback = DEFAULTS.get(key, default)
    try:
        row = db.query_one("SELECT value FROM app_setting WHERE key = %s", (key,))
    except Exception:
        return fallback
    if not row:
        return fallback
    value = row["value"]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def set_value(key: str, value: Any, actor_id: str, note: str = "") -> None:
    """写一个开关，并记审计。调用方负责权限检查。"""
    db.query(
        """INSERT INTO app_setting (key, value, updated_at, updated_by)
                VALUES (%s, %s::jsonb, now(), %s)
           ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value,
                       updated_at = now(),
                       updated_by = EXCLUDED.updated_by""",
        (key, json.dumps(value, ensure_ascii=False), actor_id),
    )
    db.query(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'setting.update', 'app_setting', %s,
                   jsonb_build_object('value', %s::jsonb, 'note', %s::text))""",
        (actor_id, key, json.dumps(value, ensure_ascii=False), note),
    )


def qa_fallback() -> dict[str, Any]:
    """兜底回答的总开关，含"为什么现在是这个状态"，好让前端说清楚。

    优先级：`.env` 的 QA_FALLBACK=off 一票否决 > 数据库开关 > 默认开。

    一票否决是给运维留的：学校临时要求"一律不许让模型自由发挥"时，
    不该依赖有人在网页上点对按钮。
    """
    if os.environ.get("QA_FALLBACK", "").strip().lower() == "off":
        return {"enabled": False, "source": "env", "forced": True}
    try:
        row = db.query_one("SELECT value FROM app_setting WHERE key = 'qa_fallback'")
    except Exception:
        row = None
    if not row:
        return {"enabled": bool(DEFAULTS["qa_fallback"]), "source": "default", "forced": False}
    value = row["value"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            # 存进去的东西坏了，别把 False 当成"管理员关掉了"——那是猜的
            return {"enabled": bool(DEFAULTS["qa_fallback"]), "source": "default",
                    "forced": False}
    return {"enabled": bool(value), "source": "db", "forced": False}


# ---------------------------------------------------------------------------
# 出校域名（管理员可在前端追加）
# ---------------------------------------------------------------------------

def approved_hosts() -> list[dict[str, Any]]:
    """页面上加过的域名。库不可用时返回空列表，页面照常显示 .env 的基线。"""
    try:
        rows = db.query(
            """SELECT host, note, added_by, created_at
                 FROM approved_host ORDER BY created_at DESC, host"""
        )
    except Exception:
        return []
    return [dict(row) for row in rows or []]


def get_host(host: str) -> dict[str, Any] | None:
    try:
        row = db.query_one("SELECT host, note FROM approved_host WHERE host = %s", (host,))
    except Exception:
        return None
    return dict(row) if row else None


def approved_host_set() -> frozenset[str]:
    """只要域名本身，给 egress 判定用。"""
    return frozenset(row["host"] for row in approved_hosts())


def mode() -> dict[str, Any]:
    """当前出校模式，以及前端能不能改。"""
    locked = egress.EGRESS_MODE == "internal-only"
    return {
        "mode": egress.EGRESS_MODE,
        "can_edit": not locked,
        "reason": (
            "服务器处于 internal-only 模式：一切外部模型调用都被禁止，"
            "这个开关加了也不起作用。要放开请改 .env 的 MODEL_EGRESS 并重启。"
            if locked else ""
        ),
    }


def add_host(raw: str, note: str, actor_id: str) -> dict[str, Any]:
    """批准一个域名。校验在这里做，因为它同时决定"什么算合法输入"。"""
    if egress.EGRESS_MODE == "internal-only":
        raise ValueError(
            "服务器处于 internal-only 模式，禁止新增出校域名。"
            "要放开请改 .env 的 MODEL_EGRESS 并重启服务。"
        )
    host, problem = egress.validate_host(raw)
    if problem:
        raise ValueError(problem)
    if host in egress.APPROVED_HOSTS:
        raise ValueError(f"{host} 已经在内置白名单里了，不用再加")
    db.query(
        """INSERT INTO approved_host (host, note, added_by)
                VALUES (%s, %s, %s)
           ON CONFLICT (host) DO NOTHING""",
        (host, (note or "").strip(), actor_id),
    )
    db.query(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'host.allow', 'approved_host', %s,
                   jsonb_build_object('note', %s::text))""",
        (actor_id, host, (note or "").strip()),
    )
    return {"host": host, "note": (note or "").strip(), "added_by": actor_id}


def remove_host(host: str, actor_id: str) -> bool:
    """撤销一个域名。返回是否真的删掉了（删不存在的要能让前端说实话）。"""
    if egress.EGRESS_MODE == "internal-only":
        raise ValueError("服务器处于 internal-only 模式，不能改出校域名")
    row = db.query_one("SELECT host FROM approved_host WHERE host = %s", (host,))
    if not row:
        return False
    db.query("DELETE FROM approved_host WHERE host = %s", (host,))
    db.query(
        """INSERT INTO audit_log (actor, action, entity, entity_key, detail)
           VALUES (%s, 'host.revoke', 'approved_host', %s, '{}'::jsonb)""",
        (actor_id, host),
    )
    return True
