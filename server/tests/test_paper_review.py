"""任务 7：整卷提交与双复核证据哈希（领域层测试）。

这里刻意**不连数据库**，与仓库其余 Python 套件一致：内存仓储负责表达 0027/0032
的唯一键与状态机语义，SQL / RLS / 触发器由静态断言覆盖，真引擎执行交给
`node scripts/verify-migrations.cjs`（PGlite）。

覆盖的核心语义：
  1. submit 只从 draft 出发，必须整卷校验通过 + 三类当前渲染 ready；
     提交时把 manifest_hash / report_hash / edit_seq 冻结进 review round。
  2. 正式卷必须两位**不同、非贡献者**的人类复核人；一审后仍是 in_review，
     第二次不同的复核通过后才写 review_manifest_hash 并转 approved。
  3. review_manifest_hash 规范化「两位复核人 + 各自证据 + 校验报告哈希 +
     卷面 manifest」；换人、换证据、换报告、换 manifest 都会变，顺序不影响。
  4. manifest 变了旧证据失效：approve 必须同时匹配 body 里的 manifest、
     paper.submitted_manifest_hash、round.manifest_hash 与 paper.manifest_hash。
  5. 撤回后 round 从 approved 变 invalidated，历史复核哈希保留但
     `approval_active=false` / `effective_review_state=withdrawn`，不会出现
     "撤回了但审核记录还显示已通过"。

运行：`python server/tests/test_paper_review.py`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.contracts import (  # noqa: E402
    ValidationIssue,
    ValidationResult,
    canonical_hash,
)
from app.paper_review import (  # noqa: E402
    REQUIRED_RENDER_KINDS,
    REVIEW_MANIFEST_SCHEMA,
    ContributorReviewError,
    DuplicateReviewerError,
    HumanReviewerRequiredError,
    InMemoryPaperReviewRepository,
    PaperNotApprovedError,
    PaperReviewNotFoundError,
    PaperReviewService,
    PaperWithdrawnError,
    ReviewDecision,
    ReviewInputError,
    ReviewManifestChangedError,
    ReviewNotOpenError,
    ReviewPaper,
    ReviewRound,
    RendersNotReadyError,
    WithdrawalReasonRequiredError,
    review_manifest_payload,
)
from app.papers import PaperIdempotencyConflictError, PaperNotDraftError  # noqa: E402
from app.validation import PaperValidationBlockedError, report_payload  # noqa: E402

COURSE_ID = UUID("11111111-1111-5111-8111-111111111111")
OFFERING_ID = UUID("22222222-2222-5222-8222-222222222222")
BLUEPRINT_ID = UUID("33333333-3333-5333-8333-333333333333")
SNAPSHOT_ID = UUID("44444444-4444-5444-8444-444444444444")
CREATOR = UUID("55555555-5555-5555-8555-555555555555")
REVIEWER_A = UUID("aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa")
REVIEWER_B = UUID("bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb")
REVIEWER_C = UUID("cccccccc-cccc-5ccc-8ccc-cccccccccccc")
WORKER = UUID("dddddddd-dddd-5ddd-8ddd-dddddddddddd")
MANIFEST = canonical_hash({"paper": "content"})
EVIDENCE_A = canonical_hash({"evidence": "reviewer-a"})
EVIDENCE_B = canonical_hash({"evidence": "reviewer-b"})


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class StubAuthorizer:
    """只记录动作与范围；身份是否人类由 `service_accounts` 控制。"""

    def __init__(self, *, denied: set[UUID] | None = None) -> None:
        self.denied = denied or set()
        self.service_accounts: set[UUID] = set()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        self.calls.append((action, scope))
        if actor_id in self.denied:
            raise PermissionError("course scope denied")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


class StubValidator:
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        self.calls: list[tuple[UUID, UUID]] = []

    def assert_valid(self, *, actor_id: UUID, paper_version_id: UUID) -> ValidationResult:
        self.calls.append((actor_id, paper_version_id))
        if not self.result.valid:
            raise PaperValidationBlockedError(self.result)
        return self.result


def validation_result(*, valid: bool = True, seed: str = "report") -> ValidationResult:
    issues = () if valid else (
        ValidationIssue(
            code="slot_unfilled",
            message="槽位 A1 还没有选题",
            severity="error",
            section_code="A",
            slot_no=1,
        ),
    )
    result = ValidationResult(
        schema_version="paper-validation-report/v1",
        paper_version_id=uuid4(),
        paper_status="draft",
        blueprint_version_id=BLUEPRINT_ID,
        candidate_snapshot_id=SNAPSHOT_ID,
        release_context_id=COURSE_ID,
        manifest_hash=MANIFEST,
        recomputed_manifest_hash=MANIFEST,
        valid=valid,
        error_count=0 if valid else 1,
        warning_count=0,
        issues=issues,
        report_hash="0" * 64,
    )
    return result.model_copy(
        update={"report_hash": canonical_hash(report_payload(result) | {"seed": seed})}
    )


def make_paper(*, status: str = "draft", purpose: str = "formal",
               **overrides: object) -> ReviewPaper:
    values: dict[str, object] = {
        "id": uuid4(),
        "paper_id": uuid4(),
        "status": status,
        "purpose": purpose,
        "course_id": COURSE_ID,
        "offering_id": OFFERING_ID,
        "edit_seq": 3,
        "manifest_hash": MANIFEST,
        "submitted_manifest_hash": None,
        "review_manifest_hash": None,
        "created_by": CREATOR,
        "approved_at": None,
        "withdrawn_by": None,
        "withdrawn_at": None,
        "withdrawal_reason": None,
    }
    values.update(overrides)
    return ReviewPaper(**values)  # type: ignore[arg-type]


def stack(*, status: str = "draft", purpose: str = "formal",
          renders: bool = True, valid: bool = True,
          contributors: tuple[UUID, ...] = ()) -> SimpleNamespace:
    repository = InMemoryPaperReviewRepository()
    paper = repository.add_paper(make_paper(status=status, purpose=purpose))
    if renders:
        repository.set_renders(paper.id, paper.manifest_hash, REQUIRED_RENDER_KINDS)
    for contributor in contributors:
        repository.add_contributor(paper.id, contributor)
    authorizer = StubAuthorizer()
    validator = StubValidator(validation_result(valid=valid))
    service = PaperReviewService(
        repository=repository,
        authorizer=authorizer,
        validator=validator,
    )
    return SimpleNamespace(
        repository=repository, paper=paper, authorizer=authorizer,
        validator=validator, service=service,
    )


def submit(f: SimpleNamespace, *, key: str | None = None):
    return f.service.submit(
        actor_id=CREATOR,
        paper_version_id=f.paper.id,
        idempotency_key=key,
    )


def approve(f: SimpleNamespace, reviewer: UUID, evidence: str,
            *, manifest: str | None = None, key: str | None = None):
    return f.service.approve(
        actor_id=reviewer,
        paper_version_id=f.paper.id,
        manifest_hash=manifest if manifest is not None else MANIFEST,
        evidence_hash=evidence,
        idempotency_key=key,
    )


def approve_twice(f: SimpleNamespace):
    submit(f)
    first = approve(f, REVIEWER_A, EVIDENCE_A, key="approve-a")
    second = approve(f, REVIEWER_B, EVIDENCE_B, key="approve-b")
    return first, second


def raises(exc_type, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        fn(*args, **kwargs)
    except exc_type as exc:  # noqa: BLE001
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"预期 {exc_type.__name__}，实际 {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"预期 {exc_type.__name__}，但调用成功了")


# ---------------------------------------------------------------------------
# submit：冻结 manifest / report_hash / edit_seq
# ---------------------------------------------------------------------------

def test_submit_freezes_manifest_report_hash_and_edit_seq() -> None:
    f = stack()
    before = f.repository.papers[f.paper.id]
    state = submit(f)
    assert state.status == "in_review"
    assert state.submitted_manifest_hash == before.manifest_hash
    assert state.manifest_hash == before.manifest_hash
    assert state.report_hash == f.validator.result.report_hash
    assert state.submitted_edit_seq == before.edit_seq == state.edit_seq
    assert state.round_status == "open"
    assert state.required_approvals == 2
    assert state.approval_count == 0
    assert state.review_manifest_hash is None
    stored = f.repository.papers[f.paper.id]
    assert stored.status == "in_review"
    assert stored.submitted_manifest_hash == before.manifest_hash
    round_ = f.repository.round_for(f.paper.id)
    assert round_ is not None
    assert round_.validation_report_hash == f.validator.result.report_hash
    assert round_.submitted_edit_seq == before.edit_seq
    assert f.validator.calls == [(CREATOR, f.paper.id)]


def test_submit_rejects_non_draft_and_invalid_report() -> None:
    invalid = stack(valid=False)
    raises(PaperValidationBlockedError, submit, invalid)
    assert invalid.repository.papers[invalid.paper.id].status == "draft"
    assert invalid.repository.round_for(invalid.paper.id) is None

    f = stack()
    submit(f)
    raises(PaperNotDraftError, submit, f)
    assert len(f.repository.decisions_for(f.paper.id)) == 0


def test_submit_requires_all_three_current_renders() -> None:
    f = stack(renders=False)
    f.repository.set_renders(f.paper.id, f.paper.manifest_hash, ("student", "answer"))
    error = raises(RendersNotReadyError, submit, f)
    assert "rubric" in str(error)
    assert f.repository.papers[f.paper.id].status == "draft"

    # 旧 manifest 的 ready 渲染不能顶数：只有与当前 manifest 相同的才算
    f.repository.set_renders(f.paper.id, f.paper.manifest_hash, REQUIRED_RENDER_KINDS)
    state = submit(f)
    assert state.status == "in_review"


def test_submit_rejects_a_duplicate_review_round_for_same_manifest() -> None:
    """同一 manifest 已经审过就不能再开一轮：0027 的 UNIQUE 在内存里同样成立。"""
    f = stack()
    submit(f)
    round_ = f.repository.round_for(f.paper.id)
    assert round_ is not None
    # 模拟数据库里有人把状态改回 draft（0027 触发器允许 in_review→draft），
    # 但同一 manifest 的 round 已存在。服务必须显式拒绝，而不是复用旧复核证据。
    f.repository.papers[f.paper.id] = f.repository.papers[f.paper.id].model_copy(
        update={"status": "draft", "submitted_manifest_hash": None}
    )
    raises(Exception, submit, f)


# ---------------------------------------------------------------------------
# approve：两人、不同、非贡献者、人类
# ---------------------------------------------------------------------------

def test_formal_paper_needs_two_distinct_humans_and_only_then_sets_hash() -> None:
    f = stack()
    submit(f)
    first = approve(f, REVIEWER_A, EVIDENCE_A, key="a-1")
    assert first.status == "in_review"
    assert first.review_manifest_hash is None
    assert first.approval_count == 1
    assert f.repository.papers[f.paper.id].review_manifest_hash is None

    second = approve(f, REVIEWER_B, EVIDENCE_B, key="b-1")
    assert second.status == "approved"
    assert second.review_manifest_hash is not None
    assert second.approval_count == 2
    assert second.round_status == "approved"
    assert second.approval_active is True
    stored = f.repository.papers[f.paper.id]
    assert stored.status == "approved"
    assert stored.review_manifest_hash == second.review_manifest_hash
    round_ = f.repository.round_for(f.paper.id)
    assert round_ is not None and round_.status == "approved" and round_.closed_at is not None
    decisions = f.repository.decisions_for(f.paper.id)
    assert len(decisions) == 2
    payload = review_manifest_payload(
        paper=stored, round_=round_, decisions=tuple(decisions))
    assert canonical_hash(payload) == second.review_manifest_hash
    assert payload["paper_manifest_hash"] == stored.manifest_hash
    assert payload["validation_report_hash"] == f.validator.result.report_hash
    assert [item["reviewer_user_id"] for item in payload["decisions"]] == sorted(
        [str(REVIEWER_A), str(REVIEWER_B)]
    )


def test_coursework_paper_needs_one_approval_only() -> None:
    f = stack(purpose="coursework")
    submit(f)
    state = approve(f, REVIEWER_A, EVIDENCE_A)
    assert state.status == "approved"
    assert state.required_approvals == 1
    assert state.approval_count == 1
    assert state.review_manifest_hash is not None


def test_same_reviewer_twice_is_rejected_not_silently_ignored() -> None:
    f = stack()
    submit(f)
    approve(f, REVIEWER_A, EVIDENCE_A, key="same-1")
    error = raises(
        DuplicateReviewerError, approve, f, REVIEWER_A, EVIDENCE_A, key="same-2")
    assert "different reviewer" in str(error)
    assert f.repository.papers[f.paper.id].status == "in_review"
    assert len(f.repository.decisions_for(f.paper.id)) == 1

    # 同一个 Idempotency-Key 的重复请求是**重试**，不是"同一个人审两次"：
    replay = approve(f, REVIEWER_A, EVIDENCE_A, key="same-1")
    assert replay.replayed is True
    assert replay.approval_count == 1
    assert f.repository.papers[f.paper.id].status == "in_review"


def test_contributor_and_service_account_cannot_approve() -> None:
    contributor = REVIEWER_C
    f = stack(contributors=(contributor,))
    submit(f)
    raises(ContributorReviewError, approve, f, contributor, EVIDENCE_A)
    assert f.repository.papers[f.paper.id].status == "in_review"

    f.authorizer.service_accounts.add(WORKER)
    raises(HumanReviewerRequiredError, approve, f, WORKER, EVIDENCE_A)
    assert f.repository.papers[f.paper.id].status == "in_review"
    assert len(f.repository.decisions_for(f.paper.id)) == 0


def test_approve_rejects_draft_and_already_approved() -> None:
    f = stack()
    raises(ReviewNotOpenError, approve, f, REVIEWER_A, EVIDENCE_A)

    f2 = stack()
    approve_twice(f2)
    raises(ReviewNotOpenError, approve, f2, REVIEWER_C, canonical_hash({"e": "c"}))


def test_evidence_hash_must_be_lowercase_sha256() -> None:
    f = stack()
    submit(f)
    for bad in ("abc", "Z" * 64, "A" * 64, " " + "a" * 64):
        error = raises(
            ReviewInputError, approve, f, REVIEWER_A, bad, key=f"bad-{bad[:4]}")
        assert "evidence_hash" in str(error)
    assert f.repository.papers[f.paper.id].status == "in_review"


# ---------------------------------------------------------------------------
# manifest 变化 = 旧证据失效
# ---------------------------------------------------------------------------

def test_manifest_change_invalidates_prior_approval() -> None:
    f = stack()
    submit(f)
    approve(f, REVIEWER_A, EVIDENCE_A)
    old = MANIFEST
    new = "f" * 64
    # 模拟有人绕过服务直接改库；服务不能拿旧 decision 凑够两人。
    f.repository.papers[f.paper.id] = f.repository.papers[f.paper.id].model_copy(
        update={"manifest_hash": new}
    )
    error = raises(
        ReviewManifestChangedError, approve, f, REVIEWER_B, EVIDENCE_B, manifest=old)
    assert "manifest" in str(error).lower()
    assert f.repository.papers[f.paper.id].status == "in_review"
    assert f.repository.papers[f.paper.id].review_manifest_hash is None


def test_approve_binds_explicit_manifest_from_request() -> None:
    f = stack()
    submit(f)
    error = raises(
        ReviewManifestChangedError, approve, f, REVIEWER_A, EVIDENCE_A,
        manifest="f" * 64,
    )
    assert "manifest" in str(error).lower()
    assert len(f.repository.decisions_for(f.paper.id)) == 0


def test_round_manifest_must_match_submitted_and_current_manifest() -> None:
    f = stack()
    submit(f)
    round_ = f.repository.round_for(f.paper.id)
    assert round_ is not None
    f.repository.rounds[round_.id] = round_.model_copy(
        update={"manifest_hash": "e" * 64}
    )
    raises(
        ReviewManifestChangedError, approve, f, REVIEWER_A, EVIDENCE_A)


# ---------------------------------------------------------------------------
# review_manifest_hash 规范化
# ---------------------------------------------------------------------------

def _hash_inputs() -> tuple[ReviewPaper, ReviewRound, tuple[ReviewDecision, ...]]:
    paper = make_paper(
        id=UUID("10000000-0000-4000-8000-000000000001"),
        status="in_review",
        manifest_hash=MANIFEST,
        submitted_manifest_hash=MANIFEST,
        edit_seq=7,
    )
    round_ = ReviewRound(
        id=UUID("20000000-0000-4000-8000-000000000002"),
        paper_version_id=paper.id,
        manifest_hash=MANIFEST,
        required_approvals=2,
        status="open",
        submitted_by=CREATOR,
        submitted_at="2026-11-06T00:00:00Z",  # type: ignore[arg-type]
        closed_at=None,
        validation_report_hash="1" * 64,
        submitted_edit_seq=7,
    )
    decisions = (
        ReviewDecision(
            id=uuid4(), paper_version_id=paper.id, review_round_id=round_.id,
            reviewer_user_id=REVIEWER_A, manifest_hash=MANIFEST,
            decision="approved", evidence_hash=EVIDENCE_A, decision_seq=1,
            human=True, authorized=True, contributor=False, note=None,
            created_at="2026-11-06T00:01:00Z",  # type: ignore[arg-type]
        ),
        ReviewDecision(
            id=uuid4(), paper_version_id=paper.id, review_round_id=round_.id,
            reviewer_user_id=REVIEWER_B, manifest_hash=MANIFEST,
            decision="approved", evidence_hash=EVIDENCE_B, decision_seq=2,
            human=True, authorized=True, contributor=False, note=None,
            created_at="2026-11-06T00:02:00Z",  # type: ignore[arg-type]
        ),
    )
    return paper, round_, decisions


def test_review_manifest_normalisation_covers_every_evidence_field() -> None:
    paper, round_, decisions = _hash_inputs()
    base = review_manifest_payload(paper=paper, round_=round_, decisions=decisions)
    base_hash = canonical_hash(base)
    assert base["schema"] == REVIEW_MANIFEST_SCHEMA
    assert base["paper_manifest_hash"] == MANIFEST
    assert base["validation_report_hash"] == "1" * 64
    assert base["required_approvals"] == 2
    # 顺序不影响：规范化按 reviewer_user_id 排序
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_, decisions=tuple(reversed(decisions))
    )) == base_hash
    # 换一个复核人 → 换哈希
    replaced = decisions[1].model_copy(update={
        "reviewer_user_id": REVIEWER_C,
        "evidence_hash": canonical_hash({"evidence": "c"}),
    })
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_, decisions=(decisions[0], replaced)
    )) != base_hash
    # 换证据哈希 → 换哈希
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_,
        decisions=(decisions[0], decisions[1].model_copy(
            update={"evidence_hash": "2" * 64})),
    )) != base_hash
    # 换校验报告哈希 → 换哈希
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_.model_copy(
            update={"validation_report_hash": "3" * 64}),
        decisions=decisions,
    )) != base_hash
    # 换卷面 manifest → 换哈希
    assert canonical_hash(review_manifest_payload(
        paper=paper.model_copy(update={"manifest_hash": "4" * 64}),
        round_=round_, decisions=decisions,
    )) != base_hash
    # 换双审人数 → 换哈希
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_.model_copy(update={"required_approvals": 1}),
        decisions=decisions,
    )) != base_hash


def test_review_manifest_does_not_hash_free_text_or_timestamps() -> None:
    paper, round_, decisions = _hash_inputs()
    base = review_manifest_payload(paper=paper, round_=round_, decisions=decisions)
    noisy = (
        decisions[0].model_copy(update={"note": "同意；但第 3 题表述建议再改"}),
        decisions[1],
    )
    assert canonical_hash(review_manifest_payload(
        paper=paper, round_=round_, decisions=noisy)) == canonical_hash(base)
    assert "note" not in base["decisions"][0]
    assert "created_at" not in base["decisions"][0]


def test_draft_self_check_report_hash_survives_submit() -> None:
    """report_hash 不含 paper_status / 时间，所以草稿自检与提交时是同一个值。"""
    result = validation_result()
    draft_hash = canonical_hash(report_payload(result))
    in_review = result.model_copy(update={"paper_status": "in_review"})
    approved = result.model_copy(
        update={"paper_status": "approved", "review_manifest_hash": "f" * 64})
    assert canonical_hash(report_payload(in_review)) == draft_hash
    assert canonical_hash(report_payload(approved)) == draft_hash
    assert "paper_status" not in report_payload(result)
    assert "created_at" not in report_payload(result)


# ---------------------------------------------------------------------------
# 撤回：状态与审核记录必须能解释
# ---------------------------------------------------------------------------

def test_withdraw_invalidates_round_but_keeps_decision_history() -> None:
    f = stack()
    _, approved = approve_twice(f)
    assert approved.status == "approved"
    historical_hash = approved.review_manifest_hash

    state = f.service.withdraw(
        actor_id=CREATOR, paper_version_id=f.paper.id, reason="题目在班级群里提前泄露",
        idempotency_key="withdraw-1",
    )
    assert state.status == "withdrawn"
    assert state.effective_review_state == "withdrawn"
    assert state.approval_active is False
    assert state.round_status == "invalidated"
    assert state.review_manifest_hash == historical_hash
    assert state.withdrawal_reason == "题目在班级群里提前泄露"
    assert state.withdrawn_by == CREATOR
    assert state.withdrawn_at is not None
    assert len(state.decisions) == 2
    assert f.repository.papers[f.paper.id].status == "withdrawn"
    round_ = f.repository.round_for(f.paper.id)
    assert round_ is not None and round_.status == "invalidated"
    assert round_.closed_at is not None
    # 历史复核事实没被删除：仍然解释得清"谁在哪个 manifest 上签过"
    assert len(f.repository.decisions_for(f.paper.id)) == 2

    raises(PaperWithdrawnError, f.service.withdraw, actor_id=CREATOR,
           paper_version_id=f.paper.id, reason="再撤一次")


def test_withdraw_requires_approved_state_and_nonempty_reason() -> None:
    draft = stack()
    raises(PaperNotApprovedError, draft.service.withdraw, actor_id=CREATOR,
           paper_version_id=draft.paper.id, reason="还没批准")

    f = stack()
    submit(f)
    raises(PaperNotApprovedError, f.service.withdraw, actor_id=CREATOR,
           paper_version_id=f.paper.id, reason="审核中不能算已批准")

    g = stack()
    approve_twice(g)
    raises(WithdrawalReasonRequiredError, g.service.withdraw, actor_id=CREATOR,
           paper_version_id=g.paper.id, reason="   ")


# ---------------------------------------------------------------------------
# 读取、幂等与错误
# ---------------------------------------------------------------------------

def test_get_returns_review_state_and_404() -> None:
    f = stack()
    submit(f)
    state = f.service.get(actor_id=CREATOR, paper_version_id=f.paper.id)
    assert state.status == "in_review"
    assert state.report_hash == f.validator.result.report_hash
    assert state.decisions == ()
    raises(PaperReviewNotFoundError, f.service.get, actor_id=CREATOR,
           paper_version_id=uuid4())


def test_idempotency_replay_and_conflict() -> None:
    f = stack()
    first = submit(f, key="submit-1")
    replay = submit(f, key="submit-1")
    assert replay.replayed is True
    assert replay.status == first.status
    assert len(f.repository.rounds) == 1

    approve(f, REVIEWER_A, EVIDENCE_A, key="approve-shared")
    raises(
        PaperIdempotencyConflictError, approve, f, REVIEWER_A, EVIDENCE_B,
        key="approve-shared",
    )


# ---------------------------------------------------------------------------
# 静态断言：迁移 0032 与 PostgreSQL 仓储
# ---------------------------------------------------------------------------

MIGRATION = ROOT / "db" / "migrations" / "0032_paper_review_decisions.sql"
SERVICE = ROOT / "server" / "app" / "paper_review.py"


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8").lower())


def test_migration_0032_adds_decision_evidence_without_touching_0027() -> None:
    assert MIGRATION.is_file(), "缺少迁移 db/migrations/0032_paper_review_decisions.sql"
    text = _flat(MIGRATION)
    assert "create table if not exists assessment.paper_review_decision" in text
    assert "review_round_id uuid not null references assessment.paper_review_round" in text
    assert "unique (review_round_id, reviewer_user_id)" in text
    assert "unique (paper_version_id, decision_seq)" in text
    assert "evidence_hash char(64) not null" in text
    assert "manifest_hash char(64) not null" in text
    assert "decision text not null check (decision in ('approved','rejected','revoked'))" in text
    assert (
        "check (decision <> 'approved' or (human and authorized and not contributor))"
        in text
    )
    # 提交时冻结的报告哈希与编辑序号挂在既有 round 上，不另造表
    assert "add column if not exists validation_report_hash char(64)" in text
    assert "add column if not exists submitted_edit_seq integer" in text
    # 审计事实不可变 + RLS
    assert "before update or delete on assessment.paper_review_decision" in text
    assert "alter table assessment.paper_review_decision force row level security" in text
    assert "create policy review_decision_course_scope" in text
    # 只做加法
    for pattern in (r"\bdrop table\b", r"\bdrop column\b", r"\bdrop schema\b",
                    r"\btruncate\b", r"\bdelete from\b"):
        assert not re.search(pattern, text), f"迁移里出现禁止操作：{pattern}"
    # verify-migrations 的 numeric 白名单只允许 practice_penalty(5,2)
    assert "numeric(" not in text


def test_postgres_store_is_transactional_and_guarded() -> None:
    text = SERVICE.read_text(encoding="utf-8").lower()
    assert "db.transaction(assessment_service=true)" in text
    assert "for update" in text
    assert "insert into assessment.paper_review_decision" in text
    assert "update assessment.paper_review_round" in text
    assert "and status = 'draft'" in text
    assert "and status = 'in_review'" in text
    assert "and status = 'approved'" in text
    assert "submitted_manifest_hash" in text
    for forbidden in ("sqlalchemy", "alembic", "celery"):
        assert forbidden not in text, f"不应引入 {forbidden}"


def test_review_action_names_are_registered_with_paper_authorizer() -> None:
    from app.papers import (
        PAPER_REVIEW_ACTION,
        PAPER_SUBMIT_ACTION,
        PAPER_WITHDRAW_ACTION,
        PaperScopeAuthorizer,
    )

    assert PAPER_SUBMIT_ACTION == "paper.submit"
    assert PAPER_REVIEW_ACTION == "paper.review"
    assert PAPER_WITHDRAW_ACTION == "paper.withdraw"
    for action in (PAPER_SUBMIT_ACTION, PAPER_REVIEW_ACTION, PAPER_WITHDRAW_ACTION):
        assert action in PaperScopeAuthorizer._ACTIONS


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

def run_tests() -> int:
    tests = [
        (name, value) for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  ✗ {name}：{exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 整卷提交与双复核测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
