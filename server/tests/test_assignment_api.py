"""任务 8：打印作业 API（建草稿 / 发布 / 撤回 / 读取）的接口级测试。

服务层只回答"规则过不过"；前端与脚本真正撞到的是状态码、幂等头与权限。本文件
固定 AUTH_MODE=dev，用请求头模拟教师/学生，通过 `dependency_overrides` 把
PostgreSQL 服务换成内存仓储——SQL、触发器、advisory lock 由
`server/tests/test_assignments.py` 的静态断言与 `scripts/verify-migrations.cjs`
的真引擎断言覆盖。

运行：`python server/tests/test_assignment_api.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import assignment_api as api  # noqa: E402
from app import auth  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402
from app.contracts import (  # noqa: E402
    ExposureEvent,
    PaperSectionView,
    PaperSlotView,
    canonical_hash,
)
from app.assignments import (  # noqa: E402
    AssignmentService,
    InMemoryAssignmentRepository,
)
from app.papers import PaperVersionRow  # noqa: E402

# 身份解析在模块加载时读 AUTH_MODE；本文件只跑接口，固定成 dev（请求头身份）。
auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "stu-1", "X-Roles": "student"}

COURSE_REF = "analysis"
OFFERING_REF = "class-2026-01"
COURSE_ID = ref_uuid("course", COURSE_REF)
OFFERING_ID = ref_uuid("offering", OFFERING_REF)
RELEASE_ID = UUID("33333333-3333-5333-8333-333333333333")
SOURCE_RELEASE = UUID("44444444-4444-5444-8444-444444444444")
PAPER_ID = UUID("55555555-5555-5555-8555-555555555555")
PAPER_VERSION_ID = UUID("66666666-6666-5666-8666-666666666666")
SNAPSHOT_ID = UUID("77777777-7777-5777-8777-777777777777")
ACTOR = ref_uuid("account", "acc-teacher")
MANIFEST = canonical_hash({"paper": "api-fixture"})
REVIEW_MANIFEST = canonical_hash({"review": "api-fixture"})

VISIBLE = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 10, 3, 30, tzinfo=timezone.utc)


class StubAuthorizer:
    def __init__(self) -> None:
        self.denied = False

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if self.denied:
            raise PermissionError("course scope denied")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")

    def is_human(self, actor_id: UUID) -> bool:
        return True


def make_paper(*, status: str = "approved", paper_kind: str = "formal",
               review_manifest: str | None = REVIEW_MANIFEST) -> PaperVersionRow:
    slots = tuple(
        PaperSlotView(
            id=UUID(int=100 + no),
            section_code="A",
            code=f"A{no}",
            slot_no=no,
            exercise_id=UUID(int=700 + no),
            exercise_version_id=UUID(int=800 + no),
            exercise_instance_id=None,
            exercise_family_id=UUID(int=900 + no),
            source_release_id=SOURCE_RELEASE,
            release_context_id=RELEASE_ID,
            score=Decimal(score),
            estimated_seconds=600,
            display_config={},
            dependency_manifest_hash=canonical_hash({"dep": no}),
            content_hash=canonical_hash({"slot": no}),
        )
        for no, score in ((1, "40.00"), (2, "60.00"))
    )
    section = PaperSectionView(
        code="A", title="一、计算题", instruction="写出步骤",
        sort_order=1, section_score=Decimal("100.00"), slots=slots,
        content_hash=canonical_hash({"section": "A"}),
    )
    return PaperVersionRow(
        id=PAPER_VERSION_ID, paper_id=PAPER_ID, version_no=1,
        candidate_snapshot_id=SNAPSHOT_ID,
        blueprint_version_id=UUID(int=555),
        release_context_id=RELEASE_ID, course_id=COURSE_ID,
        offering_id=OFFERING_ID, purpose=paper_kind, paper_kind=paper_kind,
        status=status, total_score=Decimal("100.00"), duration_seconds=7200,
        edit_seq=3, manifest_hash=MANIFEST,
        submitted_manifest_hash=MANIFEST, review_manifest_hash=review_manifest,
        sections=(section,), created_by=ACTOR, created_at=VISIBLE,
    )


def client_for(paper: PaperVersionRow | None = None,
               events: tuple[ExposureEvent, ...] = (),
               denied: bool = False) -> tuple[TestClient, InMemoryAssignmentRepository]:
    paper = paper or make_paper()
    repo = InMemoryAssignmentRepository(papers=[paper], courses=[COURSE_REF],
                                        events=events)
    repo.refs[("course", COURSE_ID)] = COURSE_REF
    repo.refs[("offering", OFFERING_ID)] = OFFERING_REF
    authorizer = StubAuthorizer()
    authorizer.denied = denied
    service = AssignmentService(repository=repo, authorizer=authorizer,
                                clock=lambda: VISIBLE)

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_assignment_service] = lambda: service
    return TestClient(app), repo


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "paper_version_id": str(PAPER_VERSION_ID),
        "offering_ref": OFFERING_REF,
        "visible_at": VISIBLE.isoformat(),
        "due_at": DUE.isoformat(),
        "hard_close_at": CLOSE.isoformat(),
    }
    payload.update(overrides)
    return payload


def make_draft(client: TestClient) -> str:
    response = client.post("/api/v1/assignments", json=create_payload(),
                           headers={**TEACHER, "Idempotency-Key": "api-create"})
    assert response.status_code == 201, response.text
    return response.json()["assignment"]["id"]


# ---------------------------------------------------------------------------
# 一、鉴权与幂等
# ---------------------------------------------------------------------------

def test_missing_idempotency_key_is_400() -> None:
    client, _ = client_for()
    response = client.post("/api/v1/assignments", json=create_payload(),
                           headers=TEACHER)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_students_cannot_create_assignments() -> None:
    client, _ = client_for()
    response = client.post("/api/v1/assignments", json=create_payload(),
                           headers={**STUDENT, "Idempotency-Key": "k"})
    assert response.status_code == 403, response.text


def test_create_then_publish_then_read() -> None:
    client, repo = client_for()
    created = client.post("/api/v1/assignments", json=create_payload(),
                          headers={**TEACHER, "Idempotency-Key": "c-1"})
    assert created.status_code == 201, created.text
    body = created.json()
    assignment_id = body["assignment"]["id"]
    assert body["assignment"]["status"] == "draft"
    assert body["assignment"]["course_ref"] == COURSE_REF
    assert body["assignment"]["offering_ref"] == OFFERING_REF

    published = client.post(f"/api/v1/assignments/{assignment_id}/publish",
                            headers={**TEACHER, "Idempotency-Key": "p-1"})
    assert published.status_code == 200, published.text
    published_body = published.json()
    assert published_body["assignment"]["status"] == "published"
    assert len(published_body["items"]) == 2
    assert published_body["items"][0]["score"] == "40.00"
    assert sum(Decimal(item["score"]) for item in published_body["items"]) == Decimal("100.00")

    fetched = client.get(f"/api/v1/assignments/{assignment_id}", headers=TEACHER)
    assert fetched.status_code == 200
    assert fetched.json()["assignment"]["status"] == "published"


def test_replaying_create_returns_200_with_replay_header() -> None:
    client, _ = client_for()
    first = client.post("/api/v1/assignments", json=create_payload(),
                        headers={**TEACHER, "Idempotency-Key": "same"})
    second = client.post("/api/v1/assignments", json=create_payload(),
                         headers={**TEACHER, "Idempotency-Key": "same"})
    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["assignment"]["id"] == first.json()["assignment"]["id"]


# ---------------------------------------------------------------------------
# 二、发布终检的错误码
# ---------------------------------------------------------------------------

def test_publishing_an_unapproved_paper_is_422() -> None:
    client, _ = client_for(paper=make_paper(status="draft"))
    response = client.post("/api/v1/assignments", json=create_payload(),
                           headers={**TEACHER, "Idempotency-Key": "c-bad"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "assignment_not_approved"


def test_time_order_violation_is_422() -> None:
    client, _ = client_for()
    response = client.post(
        "/api/v1/assignments",
        json=create_payload(due_at=(VISIBLE - timedelta(hours=1)).isoformat()),
        headers={**TEACHER, "Idempotency-Key": "c-time"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "time_order_invalid"


def test_exposure_conflict_is_422_with_reason_code() -> None:
    conflict = ExposureEvent(
        exercise_version_id=UUID(int=801),
        exercise_family_id=UUID(int=901),
        scope_offering_id=None,
        event_kind="practice_answer_released",
        visible_from=VISIBLE - timedelta(days=1),
        occurred_at=VISIBLE - timedelta(days=1),
    )
    client, _ = client_for(events=(conflict,))
    assignment_id = make_draft(client)
    response = client.post(f"/api/v1/assignments/{assignment_id}/publish",
                           headers={**TEACHER, "Idempotency-Key": "p-conflict"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "exposure_conflict"


def test_course_scope_denied_is_403() -> None:
    client, _ = client_for(denied=True)
    response = client.post("/api/v1/assignments", json=create_payload(),
                           headers={**TEACHER, "Idempotency-Key": "c-denied"})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "course_scope_denied"


def test_unknown_assignment_is_404() -> None:
    client, _ = client_for()
    response = client.get(f"/api/v1/assignments/{uuid4()}", headers=TEACHER)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "assignment_not_found"


# ---------------------------------------------------------------------------
# 三、撤回
# ---------------------------------------------------------------------------

def test_withdraw_after_publish_and_double_withdraw_conflict() -> None:
    client, _ = client_for()
    assignment_id = make_draft(client)
    client.post(f"/api/v1/assignments/{assignment_id}/publish",
                headers={**TEACHER, "Idempotency-Key": "p-1"})

    withdrawn = client.post(f"/api/v1/assignments/{assignment_id}/withdraw",
                            json={"reason": "第 2 题答案已在班级群流出"},
                            headers={**TEACHER, "Idempotency-Key": "w-1"})
    assert withdrawn.status_code == 200, withdrawn.text
    body = withdrawn.json()["assignment"]
    assert body["status"] == "withdrawn"
    assert body["withdrawn_at"] is not None
    assert "泄" in body["withdrawal_reason"] or "流出" in body["withdrawal_reason"]

    again = client.post(f"/api/v1/assignments/{assignment_id}/withdraw",
                        json={"reason": "再撤一次"},
                        headers={**TEACHER, "Idempotency-Key": "w-2"})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "assignment_state_conflict"


def test_withdraw_without_reason_is_422() -> None:
    client, _ = client_for()
    assignment_id = make_draft(client)
    client.post(f"/api/v1/assignments/{assignment_id}/publish",
                headers={**TEACHER, "Idempotency-Key": "p-1"})
    response = client.post(f"/api/v1/assignments/{assignment_id}/withdraw",
                           json={"reason": " "},
                           headers={**TEACHER, "Idempotency-Key": "w-blank"})
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# 手动收集
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
        except Exception as error:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(error).__name__}: {error}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 打印作业 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
