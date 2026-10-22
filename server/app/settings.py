"""平台开关：存在 `app_setting` 表里，管理员在前端改。

为什么不全放 .env：
  · 改 .env 要登服务器 + 重启，一个"要不要让学生看到通用知识回答"的决定
    不该需要运维介入；
  · 网页上点一下能留痕（audit_log 里有谁改的、改成什么）。

为什么不全都搬过来：密钥、出校白名单这类"改错就把数据发出去"的配置
仍然只认 .env——一个网页按钮不该有这种权限。
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import db

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
