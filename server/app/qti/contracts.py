"""QTI 3 导入导出（阶段 E 任务 1—3 的纯逻辑部分）。

三条边界（设计 §13.2）：

1. **导入只产生草稿，不继承外部审核。** QTI 文件里可能带 `reviewStatus: approved`
   之类的字段——那是**别人的审核**，不是本平台的。导入后一律 `draft`，必须重新
   走本平台的整卷审核。
2. **不认识的扩展不静默丢弃。** 每个被丢掉/降级的字段都进差异报告（带严重度），
   教师能看到"这份卷进来以后少了什么"。
3. **导出不绕过治理。** 有槽位缺题、缺来源 Release，或卷面未批准，导出直接拒绝——
   导出一份来自不明确来源的卷子，比导出失败危险得多。

本模块只做纯逻辑（解析、差异、导出结构），落库与 HTTP 由后续任务接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Literal, Sequence

from pydantic import Field

from ..contracts.common import ContractModel, Score

QtiSeverity = Literal["error", "warning", "info"]

SUPPORTED_INTERACTIONS = frozenset({"qti-text-entry", "qti-extended-text"})
# 这些字段代表"外部系统的审核/许可结论"，本平台不接受
FOREIGN_GOVERNANCE_KEYS = frozenset({
    "reviewStatus", "reviewedBy", "approvedBy", "license", "rights",
})


class QtiError(ValueError):
    code = "qti_error"


class QtiImportError(QtiError):
    code = "qti_import_invalid"


class QtiExportBlocked(QtiError):
    code = "qti_export_blocked"

    def __init__(self, message: str, *, reasons: Sequence[str]) -> None:
        super().__init__(message)
        self.reasons = tuple(reasons)


class Difference(ContractModel):
    """导入/导出的一条差异：**丢掉什么、降级什么、为什么**。"""

    code: str = Field(min_length=1, max_length=60)
    severity: QtiSeverity
    message: str = Field(min_length=1, max_length=500)
    item_ref: str | None = Field(default=None, max_length=200)


@dataclass(frozen=True)
class ImportedItem:
    """一份导入后的题目（草稿态）。"""

    ref: str
    prompt: str
    interaction: str
    max_score: Decimal
    answer: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class ImportReport:
    items: tuple[ImportedItem, ...]
    differences: tuple[Difference, ...]
    # 一律 draft：导入的卷子不继承任何外部审核结论
    status: str = "draft"

    @property
    def errors(self) -> tuple[Difference, ...]:
        return tuple(d for d in self.differences if d.severity == "error")

    @property
    def importable(self) -> bool:
        return bool(self.items) and not self.errors


def _text_of(value: Any) -> str:
    """从字符串或结构化文本节点取正文。

    QTI 的 itemBody 可能是 `{"prompt": "..."}`，也可能是规范的
    `{"value": "..."}`；两种都认，否则导入会静默得到空题干（第一次跑就撞上了）。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("prompt", "value", "text", "content"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
        return ""
    return ""


def import_qti(payload: dict[str, Any]) -> ImportReport:
    """解析一份 QTI 3 风格的结构（只取本平台用得到的部分）。

    结构（简化自 1EdTech QTI 3 AssessmentItem）：
        {"identifier": "...", "title": "...",
         "itemBody": {"prompt": "..."},
         "interaction": {"type": "qti-text-entry"},
         "maxScore": "10.00", "correctResponse": "..."}
    """
    raw_items = payload.get("items") or payload.get("assessmentItems") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise QtiImportError("qti_import_invalid: 文件里没有任何题目")

    items: list[ImportedItem] = []
    differences: list[Difference] = []

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            differences.append(Difference(
                code="item_not_object", severity="error",
                message=f"第 {index + 1} 个条目不是对象，已拒绝"))
            continue
        ref = str(raw.get("identifier") or raw.get("ref") or f"item-{index + 1}")
        prompt = _text_of(raw.get("itemBody") or raw.get("prompt"))
        interaction = str(
            (raw.get("interaction") or {}).get("type")
            if isinstance(raw.get("interaction"), dict)
            else raw.get("interaction") or "qti-text-entry"
        )
        if not prompt.strip():
            differences.append(Difference(
                code="missing_prompt", severity="error",
                message=f"{ref} 没有题干，已拒绝", item_ref=ref))
            continue
        if interaction not in SUPPORTED_INTERACTIONS:
            differences.append(Difference(
                code="unsupported_interaction", severity="warning",
                message=f"{ref} 的交互类型 {interaction} 不受支持，已降级为文本作答",
                item_ref=ref))
            interaction = "qti-text-entry"

        try:
            max_score = Decimal(str(raw.get("maxScore") or raw.get("score") or "0"))
        except Exception:  # noqa: BLE001
            differences.append(Difference(
                code="invalid_score", severity="error",
                message=f"{ref} 的分值不是数字，已拒绝", item_ref=ref))
            continue
        if max_score <= 0:
            differences.append(Difference(
                code="invalid_score", severity="error",
                message=f"{ref} 的分值必须为正，已拒绝", item_ref=ref))
            continue

        for key in (raw.get("metadata") or {}):
            if key in FOREIGN_GOVERNANCE_KEYS:
                differences.append(Difference(
                    code="foreign_governance_dropped", severity="warning",
                    message=f"{ref} 的外部审核/许可字段 {key} 已丢弃：导入不继承外部结论",
                    item_ref=ref))

        items.append(ImportedItem(
            ref=ref, prompt=prompt, interaction=interaction, max_score=max_score,
            answer=_text_of(raw.get("correctResponse")) or None,
            title=str(raw.get("title") or "") or None,
        ))

    return ImportReport(items=tuple(items), differences=tuple(differences))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportSlot:
    """卷面一个槽位。`source_release_id` / `exercise_version_id` 缺一不可。"""

    slot_no: int
    code: str
    score: Decimal
    prompt: str | None
    exercise_version_id: Any | None
    source_release_id: Any | None


def export_qti(*, slots: Sequence[ExportSlot], paper_status: str,
               paper_title: str) -> dict[str, Any]:
    """导出固定卷为 QTI 风格结构：**顺序与分值原样保留**，缺件直接拒绝。"""
    reasons: list[str] = []
    if paper_status != "approved":
        reasons.append(f"paper_status_{paper_status}")
    # 一个槽位可能同时缺题、缺来源、缺题干：三条都要报，而不是只报第一条。
    # 教师修完第一条再发现第二条，是最消耗耐心的失败方式。
    for slot in slots:
        if slot.exercise_version_id is None:
            reasons.append(f"slot_{slot.code}_missing_exercise")
        if slot.source_release_id is None:
            reasons.append(f"slot_{slot.code}_missing_release")
        if not (slot.prompt or "").strip():
            reasons.append(f"slot_{slot.code}_missing_prompt")
    if reasons:
        raise QtiExportBlocked(
            f"不能导出：{len(reasons)} 个原因（" + "、".join(reasons) + ")", reasons=reasons)

    return {
        "identifier": "mathagent-paper-export",
        "title": paper_title,
        "items": [
            {
                "identifier": slot.code,
                "title": f"第 {slot.slot_no} 题",
                "itemBody": {"prompt": slot.prompt},
                "interaction": {"type": "qti-text-entry"},
                "maxScore": f"{slot.score:.2f}",
                "order": slot.slot_no,
                "sourceReleaseId": str(slot.source_release_id),
            }
            for slot in sorted(slots, key=lambda item: item.slot_no)
        ],
        "totalScore": f"{sum((slot.score for slot in slots), Decimal('0')):.2f}",
    }


def diff_roundtrip(exported: dict[str, Any]) -> ImportReport:
    """把导出的结构再导回来：用于"导出→导入"往返测试与教师端的差异预览。"""
    normalized = {"items": [
        {**item, "ref": item["identifier"],
         "correctResponse": None}
        for item in exported.get("items", [])
    ]}
    return import_qti(normalized)


__all__ = [
    "Difference",
    "ExportSlot",
    "FOREIGN_GOVERNANCE_KEYS",
    "ImportedItem",
    "ImportReport",
    "QtiError",
    "QtiExportBlocked",
    "QtiImportError",
    "SUPPORTED_INTERACTIONS",
    "diff_roundtrip",
    "export_qti",
    "import_qti",
]
