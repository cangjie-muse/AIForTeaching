"""任务 7：提交 / 双复核 / 撤回 API 的接口级测试。

服务层只回答"证据齐不齐、哈希怎么算"；真正会被前端和脚本踩到的是状态码、
幂等头与权限。本文件固定 AUTH_MODE=dev，用请求头模拟教师与复核人，通过
`dependency_overrides` 把 PostgreSQL 服务换成内存仓储——SQL / 触发器由
test_paper_review.py 的静态断言与 scripts/verify-migrations.cjs 的真引擎覆盖。

错误码（detail 为 `{code, message}`）：
    400 idempotency_key_required
    401 未登录（auth.require）
    403 角色不足 / course_scope_denied / contributor_cannot_review /
        human_reviewer_required
    404 paper_not_found
    409 paper_not_draft / renders_not_ready / review_not_open /
        review_manifest_changed / duplicate_reviewer / paper_not_approved /
        paper_withdrawn / review_round_conflict / idempotency_conflict
    422 paper_invalid（带 report 与 report_hash）/ review_input_invalid /
        withdrawal_reason_required

运行：`python server/tests/test_paper_review_api.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import paper_reviews as api  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402
from app.contracts import (  # noqa: E402
    ValidationIssue,
    ValidationResult,
    canonical_hash,
)
from app.paper_review import (  # noqa: E402
    REQUIRED_RENDER_KINDS,
    InMemoryPaperReviewRepository,
    PaperReviewService,
    ReviewPaper,
)
from app.validation import PaperValidationBlockedError, report_payload  # noqa: E402

# 身份解析在模块加载时读 AUTH_MODE；本文件只跑接口，固定成 dev（请求头身份）。
auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
REVIEWER_1 = {"X-Actor": "acc-review-1", "X-Roles": "teacher"}
REVIEWER_2 = {"X-Actor": "acc-review-2", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "stu-1", "X-Roles": "student"}

COURSE_ID = UUID("11111111-1111-5111-8111-111111111111")
OFFERING_ID = UUID("22222222-2222-5222-8222-222222222222")
BLUEPRINT_ID = UUID("33333333-3333-5333-8333-333333333333")
SNAPSHOT_ID = UUID("44444444-4444-5444-8444-444444444444")
TEACHER = ref_uuid("account", "acc-teacher")
REVIEWER_1_ID = ref_uuid("account", "acc-review-1")
REVIEWER_2_ID = ref_uuid("account", "acc-review-2")
MANIFEST = canonical_hash({"paper": "api"})
EVIDENCE_1 = canonical_hash({"evidence": "reviewer-1"})
EVIDENCE_2 = canonical_hash({"evidence": "reviewer-2"})


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class StubAuthorizer:
    def __init__(self) -> None:
        self.denied: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if actor_id in self.denied:
            raise PermissionError("course scope denied")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return True


class StubValidator:
    def __init__(self, result: ValidationResult) -> None:
        self.result = result

    def assert_valid(self, *, actor_id: UUID, paper_version_id: UUID) -> ValidationResult:
        if not self.result.valid:
            raise PaperValidationBlockedError(self.result)
        return self.result


def validation_result(*, valid: bool = True) -> ValidationResult:
    issues = () if valid else (
        ValidationIssue(
            code="slot_unfilled", message="槽位 A1 还没有选题",
            severity="error", section_code="A", slot_no=1),
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
        update={"report_hash": canonical_hash(report_payload(result))})


def stack(*, valid: bool = True) -> SimpleNamespace:
    repository = InMemoryPaperReviewRepository()
    paper = repository.add_paper(ReviewPaper(
        id=uuid4(), paper_id=uuid4(), status="draft", purpose="formal",
        course_id=COURSE_ID, offering_id=OFFERING_ID, edit_seq=5,
        manifest_hash=MANIFEST, submitted_manifest_hash=None,
        review_manifest_hash=None, created_by=TEACHER,
    ))
    repository.set_renders(paper.id, MANIFEST, REQUIRED_RENDER_KINDS)
    validator = StubValidator(validation_result(valid=valid))
    service = PaperReviewService(
        repository=repository,
        authorizer=StubAuthorizer(),
        validator=validator,
    )
    return SimpleNamespace(
        repository=repository, paper=paper, service=service, validator=validator)


def client(service: PaperReviewService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if service is not None:
        app.dependency_overrides[api.get_review_service] = lambda: service
    return TestClient(app)


def expect_status(response, status: int, code: str | None = None) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == status, (
        f"预期 {status}，实际 {response.status_code}：{response.text}"
    )
    if code is not None:
        detail = response.json().get("detail")
        assert isinstance(detail, dict) and detail.get("code") == code, (
            f"预期错误码 {code}，实际 {detail!r}"
        )


def submit(http: TestClient, paper_id: UUID, *, key: str | None = "submit-1",
           headers: dict | None = None):
    merged = dict(TEACHER_HEADERS)
    merged.update(headers or {})
    if key is not None:
        merged["Idempotency-Key"] = key
    return http.post(f"/api/v1/paper-versions/{paper_id}/submit-review",
                     headers=merged)


def approve(http: TestClient, paper_id: UUID, headers: dict, *,
            evidence: str = EVIDENCE_1, manifest: str = MANIFEST,
            key: str | None = "approve-1"):
    merged = dict(headers)
    if key is not None:
        merged["Idempotency-Key"] = key
    return http.post(
        f"/api/v1/paper-versions/{paper_id}/approve",
        json={"manifest_hash": manifest, "evidence_hash": evidence},
        headers=merged,
    )


def withdraw(http: TestClient, paper_id: UUID, *, key: str | None = "withdraw-1",
             reason: str = "题目提前泄露"):
    headers = dict(TEACHER_HEADERS)
    if key is not None:
        headers["Idempotency-Key"] = key
    return http.post(f"/api/v1/paper-versions/{paper_id}/withdraw",
                     json={"reason": reason}, headers=headers)


def approve_twice(http: TestClient, paper_id: UUID) -> None:
    expect_status(submit(http, paper_id, key="submit-pair"), 200)
    expect_status(approve(http, paper_id, REVIEWER_1, evidence=EVIDENCE_1,
                          key="pair-1"), 200)
    expect_status(approve(http, paper_id, REVIEWER_2, evidence=EVIDENCE_2,
                          key="pair-2"), 200)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_requires_idempotency_key_and_replays() -> None:
    f = stack()
    with client(f.service) as http:
        missing = submit(http, f.paper.id, key=None)
        first = submit(http, f.paper.id, key="submit-ok")
        replay = submit(http, f.paper.id, key="submit-ok")
    expect_status(missing, 400, "idempotency_key_required")
    expect_status(first, 200)
    body = first.json()
    assert body["status"] == "in_review"
    assert body["manifest_hash"] == MANIFEST
    assert body["submitted_manifest_hash"] == MANIFEST
    assert body["report_hash"] == f.validator.result.report_hash
    assert body["submitted_edit_seq"] == 5
    assert body["round_status"] == "open"
    assert body["required_approvals"] == 2
    expect_status(replay, 200)
    assert replay.headers.get("Idempotent-Replay") == "true"


def test_submit_validation_failure_returns_report_hash() -> None:
    f = stack(valid=False)
    with client(f.service) as http:
        response = submit(http, f.paper.id, key="invalid-1")
    expect_status(response, 422, "paper_invalid")
    detail = response.json()["detail"]
    assert detail["report_hash"] == f.validator.result.report_hash
    assert detail["report"]["valid"] is False
    assert detail["report"]["issues"][0]["code"] == "slot_unfilled"
    assert f.repository.papers[f.paper.id].status == "draft"


def test_submit_without_renders_returns_409() -> None:
    f = stack()
    f.repository.set_renders(f.paper.id, MANIFEST, ("student",))
    with client(f.service) as http:
        response = submit(http, f.paper.id, key="no-renders")
    expect_status(response, 409, "renders_not_ready")


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def test_approve_requires_in_review_then_two_distinct_reviewers() -> None:
    f = stack()
    with client(f.service) as http:
        too_early = approve(http, f.paper.id, REVIEWER_1, key="early")
        expect_status(submit(http, f.paper.id, key="submit-a"), 200)
        first = approve(http, f.paper.id, REVIEWER_1, evidence=EVIDENCE_1,
                        key="review-1")
        duplicate = approve(http, f.paper.id, REVIEWER_1, evidence=EVIDENCE_1,
                            key="review-1-again")
        second = approve(http, f.paper.id, REVIEWER_2, evidence=EVIDENCE_2,
                         key="review-2")
    expect_status(too_early, 409, "review_not_open")
    expect_status(first, 200)
    assert first.json()["status"] == "in_review"
    assert first.json()["review_manifest_hash"] is None
    expect_status(duplicate, 409, "duplicate_reviewer")
    expect_status(second, 200)
    body = second.json()
    assert body["status"] == "approved"
    assert body["review_manifest_hash"]
    assert body["approval_count"] == 2
    assert body["round_status"] == "approved"


def test_contributor_cannot_approve() -> None:
    f = stack()
    f.repository.add_contributor(f.paper.id, REVIEWER_1_ID)
    with client(f.service) as http:
        expect_status(submit(http, f.paper.id, key="submit-c"), 200)
        response = approve(http, f.paper.id, REVIEWER_1, key="contrib")
    expect_status(response, 403, "contributor_cannot_review")


def test_manifest_change_returns_evidence_expired() -> None:
    f = stack()
    with client(f.service) as http:
        expect_status(submit(http, f.paper.id, key="submit-d"), 200)
        f.repository.papers[f.paper.id] = f.repository.papers[f.paper.id].model_copy(
            update={"manifest_hash": "f" * 64})
        response = approve(http, f.paper.id, REVIEWER_1, key="expired")
    expect_status(response, 409, "review_manifest_changed")


def test_bad_evidence_hash_is_422_with_domain_code() -> None:
    f = stack()
    with client(f.service) as http:
        expect_status(submit(http, f.paper.id, key="submit-e"), 200)
        response = approve(http, f.paper.id, REVIEWER_1, evidence="not-a-hash",
                           key="bad-hash")
    expect_status(response, 422, "review_input_invalid")


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------

def test_withdraw_explains_history_and_rejects_repeat() -> None:
    f = stack()
    with client(f.service) as http:
        approve_twice(http, f.paper.id)
        state = withdraw(http, f.paper.id, key="withdraw-ok")
        again = withdraw(http, f.paper.id, key="withdraw-again")
        detail = http.get(f"/api/v1/paper-versions/{f.paper.id}/review",
                          headers=TEACHER_HEADERS)
    expect_status(state, 200)
    body = state.json()
    assert body["status"] == "withdrawn"
    assert body["effective_review_state"] == "withdrawn"
    assert body["approval_active"] is False
    assert body["round_status"] == "invalidated"
    assert body["review_manifest_hash"]
    assert len(body["decisions"]) == 2
    expect_status(again, 409, "paper_withdrawn")
    expect_status(detail, 200)
    assert detail.json()["effective_review_state"] == "withdrawn"


def test_withdraw_draft_is_409_not_approved_and_reason_is_required() -> None:
    f = stack()
    with client(f.service) as http:
        early = withdraw(http, f.paper.id, key="early-withdraw")
        expect_status(submit(http, f.paper.id, key="submit-w"), 200)
        in_review = withdraw(http, f.paper.id, key="review-withdraw")
        blank = withdraw(http, f.paper.id, key="blank", reason="   ")
    expect_status(early, 409, "paper_not_approved")
    expect_status(in_review, 409, "paper_not_approved")
    expect_status(blank, 422, "withdrawal_reason_required")


# ---------------------------------------------------------------------------
# 权限与读取
# ---------------------------------------------------------------------------

def test_student_forbidden_and_anonymous_unauthorized() -> None:
    f = stack()
    with client(f.service) as http:
        student = submit(http, f.paper.id, key="student", headers=STUDENT)
        anonymous = http.post(
            f"/api/v1/paper-versions/{f.paper.id}/submit-review",
            headers={"Idempotency-Key": "anon"})
        read_student = http.get(
            f"/api/v1/paper-versions/{f.paper.id}/review", headers=STUDENT)
    assert student.status_code == 403, student.text
    assert anonymous.status_code == 401, anonymous.text
    assert read_student.status_code == 403, read_student.text


def test_unknown_paper_is_404() -> None:
    f = stack()
    with client(f.service) as http:
        response = submit(http, uuid4(), key="missing")
    expect_status(response, 404, "paper_not_found")


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
    print(f"✓ 整卷提交/双复核/撤回 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
