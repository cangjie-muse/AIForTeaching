"""曝光门禁：练习公开答案 / 正式保密卷 / 跨班保密窗口的冲突判定。

规格 §14.2 要求"候选预过滤与 assignment 发布终检都执行曝光冲突检查"。两处各写一套
规则是最容易出事的分工：预检放行了，终检才发现冲突，教师已经在选好的卷子上花了
半小时；反过来终检更松，就等于把泄题放进了课堂。所以判定只有这一份实现，
预检（`CandidateService.snapshot`）和发布终检（任务 5）都调它。

判定依据是 `assessment.item_exposure_event`（0027 已建，真实存在）：

    practice_answer_released   练习卷公开了答案/解析
    formal_answer_released     正式卷公开了答案
    secure_assigned            某班把这道题（同族）用作保密卷
    student_opened             学生已经在某班打开过这道保密题
    withdrawn                  该题（同族）被撤回

三条规则：

1. **撤回无条件拦截**：任何用途都不能再用一个已撤回的族——撤回意味着"这题不能再用"，
   不是"换个用途还能用"。
2. **正式卷（formal）拒绝任何已公开答案的族**：练习/正式答案一旦释放，题目就不再保密。
   题目只有一道，公开过的答案收不回来。
3. **正式卷拒绝与别的班级保密窗口重叠**：不同保密窗口的班级不得因为"同一门课"
   自动共享题目（§14.2 最后一条）。scope_offering_id 为 NULL 表示"跨班/全局"，
   同样算重叠。

`visible_from` 语义：只有 `visible_from <= as_of` 的事件才算"已经发生"。这让预检
可以回答"到开考那一刻会不会冲突"，而不是只看"现在有没有冲突"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from .contracts import CandidateEvidence, ExposureEvent

# 答案已经公开的事件：正式保密卷必须避开。
ANSWER_RELEASED_KINDS = frozenset({"practice_answer_released", "formal_answer_released"})
# 保密占用事件：与别的班级重叠即冲突。
SECURE_OCCUPIED_KINDS = frozenset({"secure_assigned", "student_opened"})
KNOWN_EVENT_KINDS = ANSWER_RELEASED_KINDS | SECURE_OCCUPIED_KINDS | {"withdrawn"}


class ExposureConflict(ValueError):
    """曝光冲突：携带原因码（event_kind 或 `overlapping_secure_offering`）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _as_utc(moment: datetime) -> datetime:
    """统一到 UTC；naive 时间按 UTC 解释（库列是 timestamptz，正常都带时区）。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class ExposureService:
    @staticmethod
    def assert_no_conflict(
        purpose: str,
        offering_id: UUID | None,
        candidates: Sequence[CandidateEvidence],
        events: Sequence[ExposureEvent],
        as_of: datetime,
    ) -> None:
        """冲突就抛 `ExposureConflict`；没有冲突就安静返回。

        `offering_id` 为本卷的班级；为 None 表示"课程级/未绑定班级"，此时任何
        带班级范围的保密占用都算重叠（fail closed）。
        """
        cutoff = _as_utc(as_of)
        visible = [event for event in events if _as_utc(event.visible_from) <= cutoff]
        unknown = {
            event.event_kind for event in visible if event.event_kind not in KNOWN_EVENT_KINDS
        }
        if unknown:
            # 出现了实现不认识的事件类型：宁可阻断，也不要在"看不懂"的情况下放行。
            raise ExposureConflict(
                "unknown_exposure_event",
                f"unknown exposure event kind: {sorted(unknown)}",
            )
        withdrawn_families = {
            event.exercise_family_id
            for event in visible
            if event.event_kind == "withdrawn"
        }
        for item in candidates:
            family = item.exercise_family_id
            if family in withdrawn_families:
                raise ExposureConflict(
                    "withdrawn", f"withdrawn: {item.exercise_version_id}"
                )
            if purpose != "formal":
                # 练习/作业卷不要求保密：公开答案不构成冲突。
                continue
            for event in visible:
                if event.exercise_family_id != family:
                    continue
                if event.event_kind in ANSWER_RELEASED_KINDS:
                    raise ExposureConflict(
                        event.event_kind,
                        f"{event.event_kind}: {item.exercise_version_id}",
                    )
            for event in visible:
                if event.exercise_family_id != family:
                    continue
                if (
                    event.event_kind in SECURE_OCCUPIED_KINDS
                    and event.scope_offering_id not in {None, offering_id}
                ):
                    raise ExposureConflict(
                        "overlapping_secure_offering",
                        f"overlapping secure offering: {item.exercise_version_id}",
                    )


__all__ = [
    "ANSWER_RELEASED_KINDS",
    "ExposureConflict",
    "ExposureService",
    "KNOWN_EVENT_KINDS",
    "SECURE_OCCUPIED_KINDS",
]
