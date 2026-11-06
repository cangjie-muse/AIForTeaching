"""canonical JSON + SHA-256：内容哈希与依赖清单的唯一算法。

为什么必须统一：manifest_hash 一旦在两次计算里出现差异（键顺序、Decimal 标度、
UUID 大小写、时间时区），审核失效判断就会把"内容没变"误判成"内容变了"，或者
反过来放过真的改动。所以这里把归一化规则写死成一处：

  * dict 键按字符串排序；list/tuple 统一成 list；
  * Decimal 归一成两位小数字符串（40 与 40.00 同哈希），超出两位时保留原样；
  * UUID → 小写字符串；datetime/date → ISO 8601；
  * Enum → 其 value；Pydantic 模型 → model_dump(mode="python")；dataclass → asdict；
  * UTF-8、ensure_ascii=False、无多余空白。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel

SCORE_PLACES = Decimal("0.01")


def normalize(value: Any) -> Any:
    """把任意受支持的值归一成可稳定 JSON 化的结构。"""
    if isinstance(value, BaseModel):
        return normalize(value.model_dump(mode="python"))
    # is_dataclass() 对 dataclass 类本身也返回 True，这里只要实例。
    if is_dataclass(value) and not isinstance(value, type):
        return normalize(asdict(value))  # type: ignore[arg-type]
    if isinstance(value, Enum):
        return normalize(value.value)
    if isinstance(value, Decimal):
        try:
            quantized = value.quantize(SCORE_PLACES)
        except InvalidOperation:
            return format(value, "f")
        if value == quantized:
            return f"{quantized:.2f}"
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    """返回 64 位小写十六进制 SHA-256（对应数据库 char(64)）。"""
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()
