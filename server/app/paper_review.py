"""任务 7：整卷提交、双人复核与证据哈希（领域服务 + PostgreSQL 仓储）。

## 这个模块回答什么

`PaperReviewService.submit / approve / withdraw` 是计划书 §0.3 规则 8 点名的三个
治理入口。它们不重新实现整卷校验——提交门禁直接调用任务 5 的
`PaperValidationService.assert_valid`；三类渲染是否 ready 由任务 6 的
`assessment.paper_render` 行判定。这里只负责"把当时的事实冻结下来，并让后来人
无法悄悄换掉其中任何一项"。

## 为什么 review_manifest_hash 必须是聚合哈希

规格 §10.5 / 计划书 §0.1：**不以单个 review_decision_item_id 代表整卷通过**。
若只用最后一条审核记录，攻击面是"manifest 改了、复核人还是原来那位，系统拿旧
decision 说通过"——这是最常见的审核漏洞。这里把下列事实一起规范化后 SHA-256：

    schema / paper_version_id / 卷面 manifest / 校验报告 report_hash /
    需要的复核人数 / 每位复核人的 {user_id, decision, decision_seq,
    evidence_hash, 该决定绑定的 manifest_hash}

任何一项变化（换人、换证据、换报告、换卷面）都会改变哈希；数组按
`reviewer_user_id` 排序，所以签署顺序不影响结果。`note` 与时间戳**不参与**哈希：
它们是过程信息，不是"这份卷子被谁以什么证据批准"的判据。

## 为什么草稿自检的 report_hash 在提交后不变

`validation.report_payload` 刻意不含 `paper_status` 与时间字段（见 validation.py
模块注释）。同一份内容在草稿自检、提交门禁、复核人复算时得到同一个 `report_hash`，
所以提交时冻结它是有意义的；如果它随状态变化，两位复核人就永远对不上同一份报告。

## 状态机

    draft --submit(校验通过 + 三渲染 ready)--> in_review
    in_review --approve(第 1 位)--> in_review（round 仍 open，哈希不写）
    in_review --approve(第 2 位不同的人)--> approved（写 review_manifest_hash）
    approved --withdraw(理由)--> withdrawn（round: approved -> invalidated）

`approved` 之后不可改（0027 的 `guard_fixed_projection` 触发器）；撤回不改
manifest / edit_seq / 历史 decisions，只把当前生效标记关掉。这样"撤回了但审核记录
还显示已通过"不会发生：`effective_review_state` 来自 paper.status，`approval_active`
要求 round 仍是 approved。

## 存储

没有引入 ORM：内存仓储表达唯一键与状态机语义供测试；PostgreSQL 实现全部在
`db.transaction(assessment_service=True)` 里（paper_* 是 FORCE RLS），先用
`FOR UPDATE` 锁住 paper_version 再读/写 round 与 decision。decision 表只允许
INSERT，UPDATE/DELETE 由 0032 的触发器拒绝。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator, Literal, Mapping, Protocol
from uuid import UUID, uuid4

from pydantic import Field

from . import db
from .blueprints import IdempotencyRecord
from .contracts import (
    ContractModel,
    PaperStatus,
    Purpose,
    ReviewDecision as ReviewDecisionKind,
    canonical_hash,
)
from .papers import (
    MAX_IDEMPOTENCY_KEY,
    PAPER_READ_ACTION,
    PAPER_REVIEW_ACTION,
    PAPER_SUBMIT_ACTION,
    PAPER_WITHDRAW_ACTION,
    PaperIdempotencyConflictError,
    PaperNotDraftError,
)
from .releases import utcnow

REVIEW_MANIFEST_SCHEMA = "paper-review-manifest/v1"

SUBMIT_SCOPE = "paper_review.submit"
APPROVE_SCOPE = "paper_review.approve"
WITHDRAW_SCOPE = "paper_review.withdraw"

# 提交审核前必须存在的渲染种类。少一类就不允许冻结——打印出来缺答案卷/量规卷的
# "正式卷"在治理上没有意义。
REQUIRED_RENDER_KINDS = ("student", "answer", "rubric")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_REASON = 500
MAX_NOTE = 500


# ---------------------------------------------------------------------------
# 错误：每类都对应一个可区分的前端分支 / HTTP 错误码
# ---------------------------------------------------------------------------

class PaperReviewError(Exception):
    """复核域错误基类。"""


class PaperReviewNotFoundError(PaperReviewError, LookupError):
    """paper_version 不存在（404）。"""


class IdempotencyKeyRequiredError(PaperReviewError):
    """Idempotency-Key 缺失或非法（400）。"""


class ReviewRoundConflictError(PaperReviewError):
    """同一 manifest 已有一轮审核（409）——不能复用旧复核证据。"""


class RendersNotReadyError(PaperReviewError):
    """三类当前渲染没有全部 ready（409）。"""


class ReviewNotOpenError(PaperReviewError):
    """当前不是 in_review / round 不是 open（409），例如已 approved。"""


class ReviewManifestChangedError(PaperReviewError):
    """复核证据绑定的 manifest 与当前卷面不一致（409，证据失效）。"""


class DuplicateReviewerError(PaperReviewError):
    """同一轮里同一人不能签两次（409）。"""


class ContributorReviewError(PaperReviewError):
    """本次贡献者不能复核自己的卷子（403）。"""


class HumanReviewerRequiredError(PaperReviewError):
    """非人类账号（服务账号/机器人）不能复核（403）。"""


class PaperNotApprovedError(PaperReviewError):
    """只有 approved 卷才能撤回（409）。"""


class PaperWithdrawnError(PaperReviewError):
    """已撤回的卷子不能再撤（409）。"""


class WithdrawalReasonRequiredError(PaperReviewError, ValueError):
    """撤回理由为空或过长（422）。"""


class ReviewInputError(PaperReviewError, ValueError):
    """复核入参不合法（422），例如证据哈希不是 64 位小写十六进制。"""


# ---------------------------------------------------------------------------
# 行模型
# ---------------------------------------------------------------------------

class ReviewPaper(ContractModel):
    """`assessment.paper_version` 里复核需要的列（不含卷面内容）。"""

    id: UUID
    paper_id: UUID
    status: PaperStatus
    purpose: Purpose
    course_id: UUID
    offering_id: UUID | None = None
    edit_seq: int = Field(ge=1)
    manifest_hash: str
    submitted_manifest_hash: str | None = None
    review_manifest_hash: str | None = None
    created_by: UUID
    approved_at: datetime | None = None
    withdrawn_by: UUID | None = None
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None


class ReviewRound(ContractModel):
    """`assessment.paper_review_round` + 0032 追加的两列。"""

    id: UUID
    paper_version_id: UUID
    manifest_hash: str
    required_approvals: int = Field(ge=1)
    status: Literal["open", "approved", "returned", "invalidated"]
    submitted_by: UUID
    submitted_at: datetime
    closed_at: datetime | None = None
    validation_report_hash: str | None = None
    submitted_edit_seq: int | None = None


class ReviewDecision(ContractModel):
    """一名复核者对**某一轮**的签署事实；只插入、不修改。"""

    id: UUID
    paper_version_id: UUID
    review_round_id: UUID
    reviewer_user_id: UUID
    manifest_hash: str
    decision: ReviewDecisionKind
    evidence_hash: str
    decision_seq: int = Field(ge=1)
    human: bool
    authorized: bool
    contributor: bool
    note: str | None = None
    created_at: datetime


class PaperReviewState(ContractModel):
    """提交/复核/撤回/读取的统一响应。"""

    paper_version_id: UUID
    paper_id: UUID
    status: PaperStatus
    # 与 status 同值；单独给字段是为了让前端明确"生效中的审核结论"，
    # withdrawn 时它必须是 withdrawn，而不是历史的 approved。
    effective_review_state: PaperStatus
    edit_seq: int
    manifest_hash: str
    submitted_manifest_hash: str | None = None
    review_manifest_hash: str | None = None
    required_approvals: int = Field(ge=0)
    approval_count: int = Field(ge=0)
    approval_active: bool = False
    report_hash: str | None = None
    submitted_edit_seq: int | None = None
    round_id: UUID | None = None
    round_status: Literal["open", "approved", "returned", "invalidated"] | None = None
    submitted_at: datetime | None = None
    closed_at: datetime | None = None
    approved_at: datetime | None = None
    withdrawn_by: UUID | None = None
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None
    decisions: tuple[ReviewDecision, ...] = ()
    # 只标记"同一 Idempotency-Key 的重复请求"，不落库
    replayed: bool = False


# ---------------------------------------------------------------------------
# 规范化：review_manifest_hash 的唯一输入形状
# ---------------------------------------------------------------------------

def review_manifest_payload(*, paper: ReviewPaper, round_: ReviewRound,
                            decisions: tuple[ReviewDecision, ...]) -> dict[str, object]:
    """把"两位复核人 + 各自证据 + 校验报告 + 卷面 manifest"规范化。

    只放判据，不放过程信息：`note`、`created_at`、`submitted_at` 都不在 payload
    里，否则同一组证据在不同时间重算会得到不同哈希。
    """
    ordered = sorted(
        decisions,
        key=lambda item: (str(item.reviewer_user_id), item.decision_seq),
    )
    return {
        "schema": REVIEW_MANIFEST_SCHEMA,
        "paper_version_id": str(paper.id),
        "paper_manifest_hash": paper.manifest_hash,
        "validation_report_hash": round_.validation_report_hash,
        "required_approvals": round_.required_approvals,
        "decisions": [
            {
                "reviewer_user_id": str(item.reviewer_user_id),
                "decision": item.decision,
                "decision_seq": item.decision_seq,
                "evidence_hash": item.evidence_hash,
                "manifest_hash": item.manifest_hash,
            }
            for item in ordered
        ],
    }


def review_manifest_hash(*, paper: ReviewPaper, round_: ReviewRound,
                         decisions: tuple[ReviewDecision, ...]) -> str:
    return canonical_hash(
        review_manifest_payload(paper=paper, round_=round_, decisions=decisions)
    )


def _valid_approval(decision: ReviewDecision, paper: ReviewPaper,
                    round_: ReviewRound) -> bool:
    """一条决定能不能算"有效同意"。

    四个布尔/相等判断缺一不可：人、授权、非贡献者、绑定的 manifest 与当前卷面
    和本轮 round 完全一致。manifest 变了，旧 decision 仍在库里（历史不可改），
    但它不再"有效"。
    """
    return (
        decision.decision == "approved"
        and decision.human
        and decision.authorized
        and not decision.contributor
        and decision.manifest_hash == round_.manifest_hash
        and decision.manifest_hash == paper.manifest_hash
    )


def _approval_count(paper: ReviewPaper, round_: ReviewRound,
                    decisions: tuple[ReviewDecision, ...]) -> int:
    return len({
        decision.reviewer_user_id
        for decision in decisions
        if _valid_approval(decision, paper, round_)
    })


# ---------------------------------------------------------------------------
# 仓储端口
# ---------------------------------------------------------------------------

class PaperReviewUnitOfWork(Protocol):
    def lock_paper(self, paper_version_id: UUID) -> None: ...

    def paper(self, paper_version_id: UUID) -> ReviewPaper | None: ...

    def round_for(self, paper_version_id: UUID,
                  manifest_hash: str | None = None) -> ReviewRound | None: ...

    def decisions(self, paper_version_id: UUID, round_id: UUID,
                  manifest_hash: str) -> tuple[ReviewDecision, ...]: ...

    def course_ref(self, course_id: UUID) -> str | None: ...

    def offering_ref(self, offering_id: UUID | None) -> str | None: ...

    def is_contributor(self, paper_version_id: UUID, actor_id: UUID) -> bool: ...

    def ready_render_kinds(self, paper_version_id: UUID,
                           manifest_hash: str) -> tuple[str, ...]: ...

    def open_round(self, round_: ReviewRound) -> None: ...

    def mark_submitted(self, paper: ReviewPaper, round_: ReviewRound) -> None: ...

    def append_decision(self, decision: ReviewDecision) -> None: ...

    def next_decision_seq(self, paper_version_id: UUID) -> int: ...

    def mark_approved(self, paper: ReviewPaper, round_: ReviewRound,
                      review_manifest_hash: str, approved_at: datetime) -> None: ...

    def close_round(self, round_id: UUID, status: str,
                    closed_at: datetime) -> None: ...

    def mark_withdrawn(self, paper: ReviewPaper, actor_id: UUID, reason: str,
                       withdrawn_at: datetime) -> None: ...

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None: ...

    def save_idempotent(self, record: IdempotencyRecord) -> None: ...


class PaperReviewRepository(Protocol):
    def unit_of_work(self) -> ContextManager[PaperReviewUnitOfWork]: ...


# ---------------------------------------------------------------------------
# 内存仓储：语义与 0027/0032 的唯一键一致，供无库测试
# ---------------------------------------------------------------------------

class InMemoryPaperReviewRepository:
    def __init__(self) -> None:
        self.papers: dict[UUID, ReviewPaper] = {}
        self.rounds: dict[UUID, ReviewRound] = {}
        self.decision_rows: dict[UUID, ReviewDecision] = {}
        self.renders: dict[tuple[UUID, str], set[str]] = {}
        self.contributors: dict[UUID, set[UUID]] = {}
        self.idempotency: dict[tuple[str, str, UUID], IdempotencyRecord] = {}
        self.refs: dict[tuple[str, UUID], str] = {}

    # -- 测试夹具 -----------------------------------------------------------
    def add_paper(self, paper: ReviewPaper) -> ReviewPaper:
        self.papers[paper.id] = paper
        self.contributors.setdefault(paper.id, set()).add(paper.created_by)
        self.refs.setdefault(("course", paper.course_id), "course-ref")
        if paper.offering_id is not None:
            self.refs.setdefault(("offering", paper.offering_id), "offering-ref")
        return paper

    def add_contributor(self, paper_version_id: UUID, actor_id: UUID) -> None:
        self.contributors.setdefault(paper_version_id, set()).add(actor_id)

    def set_renders(self, paper_version_id: UUID, manifest_hash: str,
                    kinds: tuple[str, ...] | set[str]) -> None:
        self.renders[(paper_version_id, manifest_hash)] = set(kinds)

    def decisions_for(self, paper_version_id: UUID,
                      round_id: UUID | None = None) -> list[ReviewDecision]:
        rows = [
            row for row in self.decision_rows.values()
            if row.paper_version_id == paper_version_id
            and (round_id is None or row.review_round_id == round_id)
        ]
        return sorted(rows, key=lambda row: row.decision_seq)

    def round_for(self, paper_version_id: UUID,
                  manifest_hash: str | None = None) -> ReviewRound | None:
        rows = [
            row for row in self.rounds.values()
            if row.paper_version_id == paper_version_id
            and (manifest_hash is None or row.manifest_hash == manifest_hash)
        ]
        return max(rows, key=lambda row: (row.submitted_at, str(row.id))) if rows else None

    # -- 工作单元 -----------------------------------------------------------
    @contextmanager
    def unit_of_work(self) -> Iterator["InMemoryPaperReviewRepository"]:
        yield self

    def lock_paper(self, paper_version_id: UUID) -> None:
        return None

    def paper(self, paper_version_id: UUID) -> ReviewPaper | None:
        return self.papers.get(paper_version_id)

    def decisions(self, paper_version_id: UUID, round_id: UUID,
                  manifest_hash: str) -> tuple[ReviewDecision, ...]:
        return tuple(
            row for row in self.decisions_for(paper_version_id, round_id)
            if row.manifest_hash == manifest_hash
        )

    def course_ref(self, course_id: UUID) -> str | None:
        return self.refs.get(("course", course_id))

    def offering_ref(self, offering_id: UUID | None) -> str | None:
        if offering_id is None:
            return None
        return self.refs.get(("offering", offering_id))

    def is_contributor(self, paper_version_id: UUID, actor_id: UUID) -> bool:
        paper = self.papers.get(paper_version_id)
        if paper is None:
            return False
        return actor_id in self.contributors.get(paper_version_id, set()) or (
            actor_id == paper.created_by
        )

    def ready_render_kinds(self, paper_version_id: UUID,
                           manifest_hash: str) -> tuple[str, ...]:
        return tuple(sorted(self.renders.get((paper_version_id, manifest_hash), set())))

    def open_round(self, round_: ReviewRound) -> None:
        for row in self.rounds.values():
            if (row.paper_version_id == round_.paper_version_id
                    and row.manifest_hash == round_.manifest_hash):
                raise ReviewRoundConflictError(
                    "该 manifest 已有一轮审核；内容变化后请以新 manifest 重新提交"
                )
        self.rounds[round_.id] = round_

    def mark_submitted(self, paper: ReviewPaper, round_: ReviewRound) -> None:
        current = self.papers.get(paper.id)
        if current is None:
            raise PaperReviewNotFoundError(str(paper.id))
        if current.status != "draft":
            raise PaperNotDraftError(f"paper {paper.id} 不是 draft")
        if (current.edit_seq != round_.submitted_edit_seq
                or current.manifest_hash != round_.manifest_hash):
            raise ReviewManifestChangedError(
                "提交时卷面已变化：manifest_hash / edit_seq 与本轮不一致"
            )
        self.papers[paper.id] = current.model_copy(update={
            "status": "in_review",
            "submitted_manifest_hash": round_.manifest_hash,
        })

    def append_decision(self, decision: ReviewDecision) -> None:
        for row in self.decisions_for(decision.paper_version_id,
                                      decision.review_round_id):
            if row.reviewer_user_id == decision.reviewer_user_id:
                raise DuplicateReviewerError(
                    "同一复核人不能对同一轮重复签署；需要一位不同的复核人"
                    "（different reviewer）"
                )
        if any(row.paper_version_id == decision.paper_version_id
               and row.decision_seq == decision.decision_seq
               for row in self.decision_rows.values()):
            raise ReviewInputError("decision_seq 冲突：并发签署请重试")
        self.decision_rows[decision.id] = decision

    def next_decision_seq(self, paper_version_id: UUID) -> int:
        return max(
            (row.decision_seq for row in self.decision_rows.values()
             if row.paper_version_id == paper_version_id),
            default=0,
        ) + 1

    def mark_approved(self, paper: ReviewPaper, round_: ReviewRound,
                      review_manifest_hash: str, approved_at: datetime) -> None:
        current = self.papers.get(paper.id)
        if current is None:
            raise PaperReviewNotFoundError(str(paper.id))
        if (current.status != "in_review"
                or current.submitted_manifest_hash != round_.manifest_hash
                or current.manifest_hash != round_.manifest_hash):
            raise ReviewManifestChangedError(
                "批准前卷面不再匹配提交时的 manifest；复核证据失效"
            )
        self.papers[paper.id] = current.model_copy(update={
            "status": "approved",
            "review_manifest_hash": review_manifest_hash,
            "approved_at": approved_at,
        })
        self.rounds[round_.id] = round_.model_copy(update={
            "status": "approved",
            "closed_at": approved_at,
        })

    def close_round(self, round_id: UUID, status: str,
                    closed_at: datetime) -> None:
        row = self.rounds.get(round_id)
        if row is None:
            return
        self.rounds[round_id] = row.model_copy(update={
            "status": status,
            "closed_at": closed_at,
        })

    def mark_withdrawn(self, paper: ReviewPaper, actor_id: UUID, reason: str,
                       withdrawn_at: datetime) -> None:
        current = self.papers.get(paper.id)
        if current is None:
            raise PaperReviewNotFoundError(str(paper.id))
        if current.status != "approved":
            raise PaperNotApprovedError(
                f"只有 approved 卷可以撤回，当前 {current.status}"
            )
        self.papers[paper.id] = current.model_copy(update={
            "status": "withdrawn",
            "withdrawn_by": actor_id,
            "withdrawn_at": withdrawn_at,
            "withdrawal_reason": reason,
        })

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None:
        return self.idempotency.get((scope, idempotency_key, actor_id))

    def save_idempotent(self, record: IdempotencyRecord) -> None:
        key = (record.scope, record.idempotency_key, record.actor_id)
        if key in self.idempotency:
            raise PaperIdempotencyConflictError(
                f"幂等键已被使用：{record.idempotency_key}"
            )
        self.idempotency[key] = record


# ---------------------------------------------------------------------------
# PostgreSQL 仓储
# ---------------------------------------------------------------------------

_PAPER_COLUMNS = """id, paper_id, status, paper_kind AS purpose, course_id, offering_id,
                    edit_seq, manifest_hash, submitted_manifest_hash, review_manifest_hash,
                    created_by, approved_at, withdrawn_by, withdrawn_at, withdrawal_reason"""

_ROUND_COLUMNS = """id, paper_version_id, manifest_hash, required_approvals, status,
                    submitted_by, submitted_at, closed_at, validation_report_hash,
                    submitted_edit_seq"""

_DECISION_COLUMNS = """id, paper_version_id, review_round_id, reviewer_user_id,
                       manifest_hash, decision, evidence_hash, decision_seq,
                       human, authorized, contributor, note, created_at"""


def _round_from_row(row: Mapping[str, Any]) -> ReviewRound:
    return ReviewRound(**{column: row[column] for column in ReviewRound.model_fields})


def _decision_from_row(row: Mapping[str, Any]) -> ReviewDecision:
    return ReviewDecision(
        **{column: row[column] for column in ReviewDecision.model_fields}
    )


class _PostgresPaperReviewUnitOfWork:
    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def lock_paper(self, paper_version_id: UUID) -> None:
        self.cur.execute(
            "SELECT id FROM assessment.paper_version WHERE id = %s FOR UPDATE",
            (paper_version_id,),
        )
        self.cur.fetchone()

    def paper(self, paper_version_id: UUID) -> ReviewPaper | None:
        self.cur.execute(
            f"SELECT {_PAPER_COLUMNS} FROM assessment.paper_version WHERE id = %s",
            (paper_version_id,),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        return ReviewPaper(**{column: row[column] for column in ReviewPaper.model_fields})

    def round_for(self, paper_version_id: UUID,
                  manifest_hash: str | None = None) -> ReviewRound | None:
        sql = (
            f"SELECT {_ROUND_COLUMNS} FROM assessment.paper_review_round "
            "WHERE paper_version_id = %s"
        )
        params: tuple[Any, ...] = (paper_version_id,)
        if manifest_hash is not None:
            sql += " AND manifest_hash = %s"
            params += (manifest_hash,)
        sql += " ORDER BY submitted_at DESC, id DESC LIMIT 1"
        self.cur.execute(sql, params)
        row = self.cur.fetchone()
        return _round_from_row(row) if row else None

    def decisions(self, paper_version_id: UUID, round_id: UUID,
                  manifest_hash: str) -> tuple[ReviewDecision, ...]:
        self.cur.execute(
            f"""SELECT {_DECISION_COLUMNS}
                  FROM assessment.paper_review_decision
                 WHERE paper_version_id = %s AND review_round_id = %s
                   AND manifest_hash = %s
                 ORDER BY decision_seq, id""",
            (paper_version_id, round_id, manifest_hash),
        )
        return tuple(_decision_from_row(row) for row in self.cur.fetchall())

    def course_ref(self, course_id: UUID) -> str | None:
        self.cur.execute(
            "SELECT ref FROM assessment.id_ref WHERE kind = 'course' AND subject = %s",
            (course_id,),
        )
        row = self.cur.fetchone()
        return row["ref"] if row else None

    def offering_ref(self, offering_id: UUID | None) -> str | None:
        if offering_id is None:
            return None
        self.cur.execute(
            "SELECT ref FROM assessment.id_ref WHERE kind = 'offering' AND subject = %s",
            (offering_id,),
        )
        row = self.cur.fetchone()
        return row["ref"] if row else None

    def is_contributor(self, paper_version_id: UUID, actor_id: UUID) -> bool:
        self.cur.execute(
            """SELECT 1 AS hit
                 FROM assessment.paper_version p
                 JOIN assessment.paper_blueprint_version b
                   ON b.id = p.blueprint_version_id
                 JOIN assessment.candidate_snapshot s
                   ON s.id = p.candidate_snapshot_id
                WHERE p.id = %s
                  AND %s IN (p.created_by, b.created_by, s.created_by)
                UNION
               SELECT 1 AS hit
                 FROM assessment.paper_edit_event
                WHERE paper_version_id = %s AND actor_id = %s
                LIMIT 1""",
            (paper_version_id, actor_id, paper_version_id, actor_id),
        )
        return self.cur.fetchone() is not None

    def ready_render_kinds(self, paper_version_id: UUID,
                           manifest_hash: str) -> tuple[str, ...]:
        self.cur.execute(
            """SELECT DISTINCT render_kind
                 FROM assessment.paper_render
                WHERE paper_version_id = %s AND paper_manifest_hash = %s
                  AND status = 'ready'
                ORDER BY render_kind""",
            (paper_version_id, manifest_hash),
        )
        return tuple(row["render_kind"] for row in self.cur.fetchall())

    def open_round(self, round_: ReviewRound) -> None:
        self.cur.execute(
            """INSERT INTO assessment.paper_review_round
                 (id, paper_version_id, manifest_hash, required_approvals, status,
                  submitted_by, submitted_at, closed_at, validation_report_hash,
                  submitted_edit_seq)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (paper_version_id, manifest_hash) DO NOTHING""",
            (
                round_.id, round_.paper_version_id, round_.manifest_hash,
                round_.required_approvals, round_.status, round_.submitted_by,
                round_.submitted_at, round_.closed_at,
                round_.validation_report_hash, round_.submitted_edit_seq,
            ),
        )
        if self.cur.rowcount == 0:
            raise ReviewRoundConflictError(
                "该 manifest 已有一轮审核；内容变化后请以新 manifest 重新提交"
            )

    def mark_submitted(self, paper: ReviewPaper, round_: ReviewRound) -> None:
        self.cur.execute(
            """UPDATE assessment.paper_version
                  SET status = 'in_review', submitted_manifest_hash = %s
                WHERE id = %s AND status = 'draft'
                  AND edit_seq = %s AND manifest_hash = %s""",
            (
                round_.manifest_hash, paper.id, round_.submitted_edit_seq,
                round_.manifest_hash,
            ),
        )
        if self.cur.rowcount != 1:
            raise PaperNotDraftError(
                f"paper {paper.id} 不是 draft，或提交时卷面已变化"
            )

    def append_decision(self, decision: ReviewDecision) -> None:
        self.cur.execute(
            """INSERT INTO assessment.paper_review_decision
                 (id, paper_version_id, review_round_id, reviewer_user_id,
                  manifest_hash, decision, evidence_hash, decision_seq,
                  human, authorized, contributor, note, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                decision.id, decision.paper_version_id, decision.review_round_id,
                decision.reviewer_user_id, decision.manifest_hash,
                decision.decision, decision.evidence_hash, decision.decision_seq,
                decision.human, decision.authorized, decision.contributor,
                decision.note, decision.created_at,
            ),
        )

    def next_decision_seq(self, paper_version_id: UUID) -> int:
        self.cur.execute(
            """SELECT coalesce(max(decision_seq), 0) + 1 AS next_seq
                 FROM assessment.paper_review_decision
                WHERE paper_version_id = %s""",
            (paper_version_id,),
        )
        row = self.cur.fetchone()
        return int(row["next_seq"]) if row else 1

    def mark_approved(self, paper: ReviewPaper, round_: ReviewRound,
                      review_manifest_hash: str, approved_at: datetime) -> None:
        self.cur.execute(
            """UPDATE assessment.paper_version
                  SET status = 'approved', review_manifest_hash = %s,
                      approved_at = %s
                WHERE id = %s AND status = 'in_review'
                  AND submitted_manifest_hash = %s AND manifest_hash = %s""",
            (
                review_manifest_hash, approved_at, paper.id,
                round_.manifest_hash, paper.manifest_hash,
            ),
        )
        if self.cur.rowcount != 1:
            raise ReviewManifestChangedError(
                "批准前卷面不再匹配提交时的 manifest；复核证据失效"
            )
        self.cur.execute(
            """UPDATE assessment.paper_review_round
                  SET status = 'approved', closed_at = %s
                WHERE id = %s AND status = 'open'""",
            (approved_at, round_.id),
        )
        if self.cur.rowcount != 1:
            raise ReviewNotOpenError("复核轮次已关闭，不能再批准")

    def close_round(self, round_id: UUID, status: str,
                    closed_at: datetime) -> None:
        self.cur.execute(
            """UPDATE assessment.paper_review_round
                  SET status = %s, closed_at = %s
                WHERE id = %s AND status IN ('open', 'approved')""",
            (status, closed_at, round_id),
        )

    def mark_withdrawn(self, paper: ReviewPaper, actor_id: UUID, reason: str,
                       withdrawn_at: datetime) -> None:
        self.cur.execute(
            """UPDATE assessment.paper_version
                  SET status = 'withdrawn', withdrawn_by = %s,
                      withdrawn_at = %s, withdrawal_reason = %s
                WHERE id = %s AND status = 'approved'""",
            (actor_id, withdrawn_at, reason, paper.id),
        )
        if self.cur.rowcount != 1:
            raise PaperNotApprovedError(
                f"只有 approved 卷可以撤回，当前 paper {paper.id}"
            )

    def find_idempotent(self, scope: str, idempotency_key: str,
                        actor_id: UUID) -> IdempotencyRecord | None:
        self.cur.execute(
            """SELECT id, scope, idempotency_key, actor_id, request_hash,
                      response, created_at
                 FROM assessment.idempotency_record
                WHERE scope = %s AND idempotency_key = %s AND actor_id = %s""",
            (scope, idempotency_key, actor_id),
        )
        row = self.cur.fetchone()
        if not row:
            return None
        return IdempotencyRecord(
            id=row["id"], scope=row["scope"],
            idempotency_key=row["idempotency_key"], actor_id=row["actor_id"],
            request_hash=row["request_hash"], response=_as_dict(row["response"]),
            created_at=row["created_at"],
        )

    def save_idempotent(self, record: IdempotencyRecord) -> None:
        self.cur.execute(
            """INSERT INTO assessment.idempotency_record
                 (id, scope, idempotency_key, actor_id, request_hash, response)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (scope, idempotency_key, actor_id) DO NOTHING""",
            (
                record.id, record.scope, record.idempotency_key,
                record.actor_id, record.request_hash, _json(record.response),
            ),
        )
        if self.cur.rowcount == 0:
            raise PaperIdempotencyConflictError(
                f"幂等键已被并发请求使用：{record.idempotency_key}"
            )


class PostgresPaperReviewRepository:
    @contextmanager
    def unit_of_work(self) -> Iterator[_PostgresPaperReviewUnitOfWork]:
        with db.transaction(assessment_service=True) as cur:
            yield _PostgresPaperReviewUnitOfWork(cur)


# ---------------------------------------------------------------------------
# 应用服务
# ---------------------------------------------------------------------------

class PaperReviewService:
    """`submit` / `approve` / `withdraw` 是纸卷治理的唯一写入口。"""

    def __init__(
        self,
        *,
        repository: PaperReviewRepository | None = None,
        authorizer: Any | None = None,
        validator: Any | None = None,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utcnow,
        require_renders: bool = True,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer
        self.validator = validator
        self._new_id = id_factory
        self._now = clock
        self.require_renders = require_renders

    # -- 提交 ---------------------------------------------------------------
    def submit(self, *, actor_id: UUID, paper_version_id: UUID,
               idempotency_key: str | None = None) -> PaperReviewState:
        key = _clean_key(idempotency_key)
        self._require_ports()
        with self._unit_of_work() as uow:
            uow.lock_paper(paper_version_id)
            paper = self._load(uow, paper_version_id)
            self._authorize(uow, actor_id, paper, PAPER_SUBMIT_ACTION)

            fingerprint = canonical_hash({
                "action": "submit",
                "paper_version_id": paper.id,
                "manifest_hash": paper.manifest_hash,
                "edit_seq": paper.edit_seq,
            })
            replayed = self._replay(uow, SUBMIT_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return replayed

            if paper.status != "draft":
                raise PaperNotDraftError(
                    f"只有 draft 可以提交审核，当前 {paper.status}"
                )
            validation = self.validator.assert_valid(
                actor_id=actor_id, paper_version_id=paper.id)
            report_hash = _report_hash(validation)
            if self.require_renders:
                ready = set(uow.ready_render_kinds(paper.id, paper.manifest_hash))
                missing = [
                    kind for kind in REQUIRED_RENDER_KINDS if kind not in ready
                ]
                if missing:
                    raise RendersNotReadyError(
                        "提交审核前必须有三类当前渲染 ready；缺少：" + "、".join(missing)
                    )
            if uow.round_for(paper.id, paper.manifest_hash) is not None:
                raise ReviewRoundConflictError(
                    "该 manifest 已有一轮审核；内容变化后请以新 manifest 重新提交"
                )

            now = self._now()
            round_ = ReviewRound(
                id=self._new_id(),
                paper_version_id=paper.id,
                manifest_hash=paper.manifest_hash,
                required_approvals=_required_approvals(paper),
                status="open",
                submitted_by=actor_id,
                submitted_at=now,
                closed_at=None,
                validation_report_hash=report_hash,
                submitted_edit_seq=paper.edit_seq,
            )
            uow.mark_submitted(paper, round_)
            uow.open_round(round_)
            submitted = paper.model_copy(update={
                "status": "in_review",
                "submitted_manifest_hash": paper.manifest_hash,
            })
            state = self._state(submitted, round_, ())
            self._save_replay(uow, SUBMIT_SCOPE, key, actor_id, fingerprint, state)
            return state

    # -- 复核 ---------------------------------------------------------------
    def approve(self, *, actor_id: UUID, paper_version_id: UUID,
                manifest_hash: str, evidence_hash: str,
                note: str | None = None,
                idempotency_key: str | None = None) -> PaperReviewState:
        key = _clean_key(idempotency_key)
        manifest = _clean_manifest_hash(manifest_hash)
        evidence = _clean_evidence_hash(evidence_hash)
        cleaned_note = _clean_note(note)
        self._require_ports()
        with self._unit_of_work() as uow:
            uow.lock_paper(paper_version_id)
            paper = self._load(uow, paper_version_id)
            self._authorize(uow, actor_id, paper, PAPER_REVIEW_ACTION)

            fingerprint = canonical_hash({
                "action": "approve",
                "paper_version_id": paper.id,
                "manifest_hash": manifest,
                "evidence_hash": evidence,
                "note": cleaned_note,
            })
            replayed = self._replay(uow, APPROVE_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return replayed

            if paper.status != "in_review":
                raise ReviewNotOpenError(
                    f"approve 只能从 in_review 出发，当前 {paper.status}"
                )
            round_ = uow.round_for(paper.id)
            if round_ is None or round_.status != "open":
                raise ReviewNotOpenError(
                    "没有 open 的复核轮次（可能已被批准、退回或失效）"
                )
            # 证据失效判据：请求声明的 manifest、提交时冻结的 manifest、
            # round 的 manifest、当前卷面 manifest 必须四者一致。
            if (manifest != paper.manifest_hash
                    or paper.submitted_manifest_hash != paper.manifest_hash
                    or round_.manifest_hash != paper.manifest_hash):
                raise ReviewManifestChangedError(
                    "manifest 已变化：复核证据失效（review evidence expired）；"
                    "请对当前卷面重新提交并重新复核"
                )
            if uow.is_contributor(paper.id, actor_id):
                raise ContributorReviewError(
                    "本次卷面的贡献者不能复核自己的卷子"
                )
            if not self.authorizer.is_human(actor_id):
                raise HumanReviewerRequiredError(
                    "复核必须由人类用户完成，服务账号不能签字"
                )

            existing = uow.decisions(paper.id, round_.id, paper.manifest_hash)
            if any(row.reviewer_user_id == actor_id for row in existing):
                raise DuplicateReviewerError(
                    "同一复核人不能对同一轮重复签署；需要一位不同的复核人"
                    "（different reviewer）"
                )
            decision_seq = uow.next_decision_seq(paper.id)
            decision = ReviewDecision(
                id=self._new_id(),
                paper_version_id=paper.id,
                review_round_id=round_.id,
                reviewer_user_id=actor_id,
                manifest_hash=paper.manifest_hash,
                decision="approved",
                evidence_hash=evidence,
                decision_seq=decision_seq,
                human=True,
                authorized=True,
                contributor=False,
                note=cleaned_note,
                created_at=self._now(),
            )
            uow.append_decision(decision)
            decisions = existing + (decision,)
            count = _approval_count(paper, round_, decisions)
            if count < round_.required_approvals:
                state = self._state(paper, round_, decisions)
                self._save_replay(
                    uow, APPROVE_SCOPE, key, actor_id, fingerprint, state)
                return state

            approved_at = self._now()
            valid = tuple(
                row for row in decisions if _valid_approval(row, paper, round_)
            )
            digest = review_manifest_hash(
                paper=paper, round_=round_, decisions=valid)
            uow.mark_approved(paper, round_, digest, approved_at)
            approved_paper = paper.model_copy(update={
                "status": "approved",
                "review_manifest_hash": digest,
                "approved_at": approved_at,
            })
            closed_round = round_.model_copy(update={
                "status": "approved", "closed_at": approved_at,
            })
            state = self._state(approved_paper, closed_round, decisions)
            self._save_replay(uow, APPROVE_SCOPE, key, actor_id, fingerprint, state)
            return state

    # -- 撤回 ---------------------------------------------------------------
    def withdraw(self, *, actor_id: UUID, paper_version_id: UUID, reason: str,
                 idempotency_key: str | None = None) -> PaperReviewState:
        key = _clean_key(idempotency_key)
        cleaned = _clean_reason(reason)
        self._require_ports()
        with self._unit_of_work() as uow:
            uow.lock_paper(paper_version_id)
            paper = self._load(uow, paper_version_id)
            self._authorize(uow, actor_id, paper, PAPER_WITHDRAW_ACTION)

            fingerprint = canonical_hash({
                "action": "withdraw",
                "paper_version_id": paper.id,
                "reason": cleaned,
            })
            replayed = self._replay(uow, WITHDRAW_SCOPE, key, actor_id, fingerprint)
            if replayed is not None:
                return replayed

            if paper.status == "withdrawn":
                raise PaperWithdrawnError("该卷已撤回，不能重复撤回")
            if paper.status != "approved":
                raise PaperNotApprovedError(
                    f"只有 approved 卷可以撤回，当前 {paper.status}"
                )

            now = self._now()
            uow.mark_withdrawn(paper, actor_id, cleaned, now)
            round_ = uow.round_for(paper.id)
            closed_round = round_
            if round_ is not None and round_.status in ("open", "approved"):
                # 关键：round 不能留在 approved。历史哈希保留在 paper/decision 上，
                # 但生效状态必须是 invalidated + approval_active=false。
                uow.close_round(round_.id, "invalidated", now)
                closed_round = round_.model_copy(update={
                    "status": "invalidated", "closed_at": now,
                })
            withdrawn = paper.model_copy(update={
                "status": "withdrawn",
                "withdrawn_by": actor_id,
                "withdrawn_at": now,
                "withdrawal_reason": cleaned,
            })
            decisions = (
                uow.decisions(paper.id, round_.id, round_.manifest_hash)
                if round_ is not None else ()
            )
            state = self._state(withdrawn, closed_round, decisions)
            self._save_replay(
                uow, WITHDRAW_SCOPE, key, actor_id, fingerprint, state)
            return state

    # -- 读取 ---------------------------------------------------------------
    def get(self, *, actor_id: UUID,
            paper_version_id: UUID) -> PaperReviewState:
        self._require_ports()
        with self._unit_of_work() as uow:
            paper = self._load(uow, paper_version_id)
            self._authorize(uow, actor_id, paper, PAPER_REVIEW_ACTION)
            round_ = uow.round_for(paper.id)
            decisions = (
                uow.decisions(paper.id, round_.id, round_.manifest_hash)
                if round_ is not None else ()
            )
            return self._state(paper, round_, decisions)

    # -- 内部 ---------------------------------------------------------------
    def _require_ports(self) -> None:
        if self.repository is None:
            raise PaperReviewError("缺少复核仓储")
        if self.authorizer is None:
            raise PermissionError("缺少授权端口：拒绝无授权的写操作")
        if self.validator is None:
            raise PaperReviewError("缺少整卷校验端口：提交门禁不能跳过校验")

    def _unit_of_work(self) -> ContextManager[PaperReviewUnitOfWork]:
        if self.repository is None:  # pragma: no cover
            raise PaperReviewError("缺少复核仓储")
        return self.repository.unit_of_work()

    def _load(self, uow: PaperReviewUnitOfWork,
              paper_version_id: UUID) -> ReviewPaper:
        paper = uow.paper(paper_version_id)
        if paper is None:
            raise PaperReviewNotFoundError(f"paper_version 不存在：{paper_version_id}")
        return paper

    def _authorize(self, uow: PaperReviewUnitOfWork, actor_id: UUID,
                   paper: ReviewPaper, action: str) -> None:
        course_ref = uow.course_ref(paper.course_id)
        if not course_ref:
            # fail closed：没有文本课程号就判断不了"谁教这门课"
            raise PermissionError("纸卷缺少 course_ref，无法做课程范围授权")
        self.authorizer.require(
            actor_id, action,
            course_id=paper.course_id,
            course_ref=course_ref,
            offering_id=paper.offering_id,
            offering_ref=uow.offering_ref(paper.offering_id),
        )
        if not self.authorizer.is_human(actor_id):
            raise HumanReviewerRequiredError(
                "纸卷治理动作只能由人类用户完成，服务账号不能提交/复核/撤回"
            )

    def _state(self, paper: ReviewPaper, round_: ReviewRound | None,
               decisions: tuple[ReviewDecision, ...]) -> PaperReviewState:
        count = (
            _approval_count(paper, round_, decisions)
            if round_ is not None else 0
        )
        active = (
            paper.status == "approved"
            and round_ is not None
            and round_.status == "approved"
            and paper.review_manifest_hash is not None
        )
        return PaperReviewState(
            paper_version_id=paper.id,
            paper_id=paper.paper_id,
            status=paper.status,
            effective_review_state=paper.status,
            edit_seq=paper.edit_seq,
            manifest_hash=paper.manifest_hash,
            submitted_manifest_hash=paper.submitted_manifest_hash,
            review_manifest_hash=paper.review_manifest_hash,
            required_approvals=round_.required_approvals if round_ else 0,
            approval_count=count,
            approval_active=active,
            report_hash=round_.validation_report_hash if round_ else None,
            submitted_edit_seq=round_.submitted_edit_seq if round_ else None,
            round_id=round_.id if round_ else None,
            round_status=round_.status if round_ else None,
            submitted_at=round_.submitted_at if round_ else None,
            closed_at=round_.closed_at if round_ else None,
            approved_at=paper.approved_at,
            withdrawn_by=paper.withdrawn_by,
            withdrawn_at=paper.withdrawn_at,
            withdrawal_reason=paper.withdrawal_reason,
            decisions=decisions,
            replayed=False,
        )

    def _replay(self, uow: PaperReviewUnitOfWork, scope: str, key: str | None,
                actor_id: UUID, fingerprint: str) -> PaperReviewState | None:
        if key is None:
            return None
        record = uow.find_idempotent(scope, key, actor_id)
        if record is None:
            return None
        if record.request_hash != fingerprint:
            raise PaperIdempotencyConflictError(
                "同一 Idempotency-Key 被用于不同的复核载荷"
            )
        stored = PaperReviewState.model_validate(record.response)
        return stored.model_copy(update={"replayed": True})

    def _save_replay(self, uow: PaperReviewUnitOfWork, scope: str,
                     key: str | None, actor_id: UUID, fingerprint: str,
                     state: PaperReviewState) -> None:
        if key is None:
            return
        uow.save_idempotent(IdempotencyRecord(
            id=self._new_id(),
            scope=scope,
            idempotency_key=key,
            actor_id=actor_id,
            request_hash=fingerprint,
            response=state.model_dump(mode="json"),
            created_at=self._now(),
        ))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _required_approvals(paper: ReviewPaper) -> int:
    """正式卷两名；课程作业/练习一名（与计划书任务 7 的 required 口径一致）。"""
    return 2 if paper.purpose == "formal" else 1


def _report_hash(validation: Any) -> str:
    digest = getattr(validation, "report_hash", None)
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        raise PaperReviewError(
            "整卷校验器必须返回 64 位小写十六进制 report_hash，否则无法冻结证据"
        )
    if getattr(validation, "valid", True) is not True:
        # 校验器本应 raise；万一被包装成返回值，这里 fail closed。
        raise PaperReviewError("整卷校验未通过：不能提交审核")
    return digest


def _clean_key(key: str | None) -> str | None:
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned or len(cleaned) > MAX_IDEMPOTENCY_KEY:
        raise IdempotencyKeyRequiredError(
            f"Idempotency-Key 不能为空且不超过 {MAX_IDEMPOTENCY_KEY} 字符"
        )
    return cleaned


def _clean_manifest_hash(value: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ReviewInputError(
            "manifest_hash 必须是 64 位小写十六进制 SHA-256（不接受空白或大写）"
        )
    return value


def _clean_evidence_hash(value: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ReviewInputError(
            "evidence_hash 必须是 64 位小写十六进制 SHA-256（不接受空白或大写）"
        )
    return value


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()
    if len(cleaned) > MAX_NOTE:
        raise ReviewInputError(f"复核备注不能超过 {MAX_NOTE} 字符")
    return cleaned or None


def _clean_reason(reason: str) -> str:
    cleaned = (reason or "").strip()
    if not cleaned:
        raise WithdrawalReasonRequiredError(
            "撤回必须给出理由：审计要能解释为什么这份已批准卷不能继续使用"
        )
    if len(cleaned) > MAX_REASON:
        raise WithdrawalReasonRequiredError(
            f"撤回理由不能超过 {MAX_REASON} 字符"
        )
    return cleaned


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return dict(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "APPROVE_SCOPE",
    "ContributorReviewError",
    "DuplicateReviewerError",
    "HumanReviewerRequiredError",
    "IdempotencyKeyRequiredError",
    "InMemoryPaperReviewRepository",
    "MAX_NOTE",
    "MAX_REASON",
    "PaperNotApprovedError",
    "PaperReviewError",
    "PaperReviewNotFoundError",
    "PaperReviewRepository",
    "PaperReviewService",
    "PaperReviewState",
    "PaperReviewUnitOfWork",
    "PaperWithdrawnError",
    "PostgresPaperReviewRepository",
    "REQUIRED_RENDER_KINDS",
    "REVIEW_MANIFEST_SCHEMA",
    "RendersNotReadyError",
    "ReviewDecision",
    "ReviewInputError",
    "ReviewManifestChangedError",
    "ReviewNotOpenError",
    "ReviewPaper",
    "ReviewRound",
    "ReviewRoundConflictError",
    "SUBMIT_SCOPE",
    "WITHDRAW_SCOPE",
    "WithdrawalReasonRequiredError",
    "review_manifest_hash",
    "review_manifest_payload",
]
