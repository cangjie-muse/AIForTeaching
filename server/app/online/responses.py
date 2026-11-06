"""逐槽响应保存（阶段 C 任务 4）：幂等、base_revision 冲突、非法输入留证。

三条规则（设计 §11.2）：

1. **保存身份是 `attempt + slot + client_revision_id`。** 同一把客户端 id 重试只会有
   一条修订——网络抖动不该把同一个答案存两遍。
2. **按槽比较 `base_revision_no`，不按整卷。** 两个标签页改不同的题不该互相报错；
   改同一道题才 409，并把**服务端最新本人响应**带回去，让学生知道覆盖了什么。
3. **过期与非法输入不是一回事。**
   * 截止之后（或已交卷/已撤回）再保存 → `attempt_finalized`；
   * 输入本身解析不了 → **仍然存下来**，`parse_status='invalid'` + 原因，
     交给评阅/人工处理。拒收会让"学生明明写了字，却显示没写"变成事故。

为什么非法输入也要留证（而不是直接 400 丢掉）：学生写了字、系统说"没收到"，
是考场上最不该发生的事。留着原文 + 标记无效，比拒收更接近事实。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

from ..contracts.hashing import canonical_hash
from ..releases import utcnow
from .contracts import (
    AttemptFinalized,
    AttemptStatus,
    ParseStatus,
    ResourceNotFound,
    ResponseConflict,
    SaveResponseResult,
    Withdrawn,
)

# 非法输入的固定原因码（前端按码翻译；不要临时拼字符串）
INVALID_EMPTY = "empty_response"
INVALID_OVERSIZE = "response_too_large"
INVALID_SHAPE = "unexpected_shape"

MAX_RESPONSE_BYTES = 64 * 1024


class IdempotencyConflict(Exception):
    """同一 client_revision_id 被用于不同的内容：这是客户端 bug，必须报，不能吞。"""

    code = "idempotency_conflict"


def looks_invalid(raw: dict[str, Any] | None) -> tuple[bool, str | None]:
    """形状层面的粗检（不解释数学，只看"能不能当文本/公式存"）。

    刻意不做业务校验：阶段 C 不跑 CAS、不判对错（§0.3 规则 7）。这里只判断
    "空 / 过大 / 结构不是对象"，其余交给阶段 D。
    """
    if raw is None or not raw:
        return True, INVALID_EMPTY
    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        return True, INVALID_SHAPE
    if isinstance(text, str) and len(text) > MAX_RESPONSE_BYTES:
        return True, INVALID_OVERSIZE
    if text is None and not raw.get("latex") and not raw.get("attachments"):
        return True, INVALID_SHAPE
    return False, None


class ResponseRow:
    """一条已存响应（内存/数据库共用的形状）。"""

    def __init__(self, *, id: UUID, attempt_id: UUID, slot_id: UUID, revision_no: int,
                 client_revision_id: str, raw_response: dict[str, Any],
                 parse_status: ParseStatus, invalid_reasons: Sequence[str],
                 server_received_at: datetime) -> None:
        self.id = id
        self.attempt_id = attempt_id
        self.slot_id = slot_id
        self.revision_no = revision_no
        self.client_revision_id = client_revision_id
        self.raw_response = raw_response
        self.parse_status = parse_status
        self.invalid_reasons = tuple(invalid_reasons)
        self.server_received_at = server_received_at

    def to_save_result(self) -> SaveResponseResult:
        return SaveResponseResult(
            attempt_id=self.attempt_id, slot_id=self.slot_id,
            client_revision_id=self.client_revision_id,
            revision_no=self.revision_no, server_received_at=self.server_received_at,
            raw_response=self.raw_response, parse_status=self.parse_status,
        )


class InMemoryResponseRepository:
    """与 0036 的 `(attempt, client_revision_id)` / `(attempt, slot, revision_no)` 唯一同义。"""

    def __init__(self) -> None:
        self.rows: list[ResponseRow] = []
        self.attempts: dict[UUID, Any] = {}  # attempt 行由测试塞进来

    def add_attempt(self, row: Any) -> None:
        self.attempts[row.id] = row

    def get_attempt(self, attempt_id: UUID) -> Any | None:
        return self.attempts.get(attempt_id)

    def by_client_id(self, attempt_id: UUID, client_revision_id: str) -> ResponseRow | None:
        for row in self.rows:
            if row.attempt_id == attempt_id and row.client_revision_id == client_revision_id:
                return row
        return None

    def latest(self, attempt_id: UUID, slot_id: UUID) -> ResponseRow | None:
        found = [row for row in self.rows
                 if row.attempt_id == attempt_id and row.slot_id == slot_id]
        return max(found, key=lambda row: row.revision_no) if found else None

    def add(self, row: ResponseRow) -> ResponseRow:
        if self.by_client_id(row.attempt_id, row.client_revision_id) is not None:
            raise ValueError("uq_response_revision_client: client_revision_id 重复")
        if self.latest(row.attempt_id, row.slot_id) is not None and self.latest(
                row.attempt_id, row.slot_id).revision_no == row.revision_no:
            raise ValueError("uq_response_revision_no: revision_no 重复")
        self.rows.append(row)
        return row


class ResponseService:
    def __init__(
        self,
        *,
        repository: Any,
        clock: Callable[[], datetime] = utcnow,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self._now = clock
        self._new_id = id_factory

    def save(self, *, attempt_id: UUID, slot_id: UUID, student_id: UUID,
             client_revision_id: str, base_revision_no: int,
             raw_response: dict[str, Any] | None,
             server_now: datetime | None = None) -> SaveResponseResult:
        now = server_now or self._now()
        attempt = self.repository.get_attempt(attempt_id)
        if attempt is None or attempt.student_id != student_id:
            raise ResourceNotFound(f"attempt 不存在或不属于本人：{attempt_id}")

        # 1) 幂等：同一 client_revision_id 必须返回同一条修订
        previous = self.repository.by_client_id(attempt_id, client_revision_id)
        if previous is not None:
            if previous.slot_id != slot_id or previous.raw_response != raw_response:
                raise IdempotencyConflict(
                    f"{client_revision_id} 被用于不同的内容（槽位或响应不一致）")
            return previous.to_save_result()

        # 2) 状态与时间：交卷/撤回/逾期都不能再写
        if getattr(attempt, "assignment_status", None) == "withdrawn" \
                or attempt.status == "withdrawn":
            raise Withdrawn(f"作业已撤回：不能再保存（{attempt_id}）")
        if attempt.status != "in_progress" or now > attempt.server_deadline_at:
            raise AttemptFinalized(
                f"作答已结束，不能再保存（attempt 状态 {attempt.status}）")

        # 3) 逐槽 base_revision 比较：过期时把服务端最新本人响应带回去
        latest = self.repository.latest(attempt_id, slot_id)
        current_no = latest.revision_no if latest is not None else 0
        if base_revision_no != current_no:
            conflict = ResponseConflict(
                f"response_conflict: 服务端已是第 {current_no} 版，客户端基于 "
                f"第 {base_revision_no} 版")
            conflict.server_result = (
                latest.to_save_result() if latest is not None
                else SaveResponseResult.empty(attempt_id=attempt_id, slot_id=slot_id))
            raise conflict

        # 4) 非法输入照样落库，但标成 invalid 并留原因
        invalid, reason = looks_invalid(raw_response)
        parse_status: ParseStatus = "invalid" if invalid else "parsed"
        row = ResponseRow(
            id=self._new_id(), attempt_id=attempt_id, slot_id=slot_id,
            revision_no=current_no + 1, client_revision_id=str(client_revision_id),
            raw_response=raw_response or {}, parse_status=parse_status,
            invalid_reasons=(reason,) if reason else (),
            server_received_at=now,
        )
        self.repository.add(row)
        return row.to_save_result()

    def latest_for_slot(self, *, attempt_id: UUID, slot_id: UUID,
                        student_id: UUID) -> SaveResponseResult:
        attempt = self.repository.get_attempt(attempt_id)
        if attempt is None or attempt.student_id != student_id:
            raise ResourceNotFound(f"attempt 不存在或不属于本人：{attempt_id}")
        row = self.repository.latest(attempt_id, slot_id)
        if row is None:
            return SaveResponseResult.empty(attempt_id=attempt_id, slot_id=slot_id)
        return row.to_save_result()


def response_fingerprint(attempt_id: UUID, slot_id: UUID, raw: dict[str, Any] | None) -> str:
    """回执/manifest 用的规范哈希；与 canonical_hash 同一套归一化。"""
    return canonical_hash({"attempt": str(attempt_id), "slot": str(slot_id),
                           "raw": raw or {}})


__all__ = [
    "INVALID_EMPTY",
    "INVALID_OVERSIZE",
    "INVALID_SHAPE",
    "IdempotencyConflict",
    "InMemoryResponseRepository",
    "MAX_RESPONSE_BYTES",
    "ResponseRow",
    "ResponseService",
    "AttemptStatus",
    "looks_invalid",
    "response_fingerprint",
]
