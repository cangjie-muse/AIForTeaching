"""阶段 D 任务 8：评分 HTTP 契约、学生安全 DTO 与统一错误语义。

要钉住的：

  * 未装配服务 → 503（不假装成功）；
  * 写接口缺 Idempotency-Key → 400；
  * 确认缺项 → 409 `review_incomplete` 且带 `missing` 列表；
  * rubric 不一致 → 422 `rubric_score_mismatch`；
  * 学生 `my-grades` **只返回已发布版本**，草稿不出现，且不含 rubric/建议字段；
  * 发布/撤回/申诉的错误码互不混淆。

运行：`python server/tests/test_grading_api.py`
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import grading_api as api  # noqa: E402
from app.grading.publish import InMemoryPublishRepository, PublishService  # noqa: E402
from app.grading.revisions import (  # noqa: E402
    ConfirmCommand,
    GradingService,
    ItemDecisionInput,
)

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "acc-student", "X-Roles": "student"}
NOW = datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc)
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
ITEM_A = UUID("dd000000-0000-4000-8000-0000000000a1")
ITEM_B = UUID("dd000000-0000-4000-8000-0000000000b1")
MAX = {ITEM_A: Decimal("5.00"), ITEM_B: Decimal("10.00")}


def build_services(*, confirmed: bool = True):
    repo = InMemoryPublishRepository()
    grading = GradingService(repository=repo.grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=500))
    if confirmed:
        grading.confirm_grade(ConfirmCommand(
            submission_id=SUBMISSION,
            decisions=(ItemDecisionInput(submission_item_id=ITEM_A, score=Decimal("5.00")),
                       ItemDecisionInput(submission_item_id=ITEM_B, score=Decimal("9.00"))),
        ), actor_id=UUID(int=1), item_max_scores=MAX)
    publish = PublishService(repository=repo, grading=grading, clock=lambda: NOW,
                             id_factory=lambda: UUID(int=600))
    return api.Services(grading=grading, publish=publish), repo


def client(services=None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_services] = lambda: services
    return TestClient(app)


# ---------------------------------------------------------------------------
# 装配与错误语义
# ---------------------------------------------------------------------------

def test_unwired_service_returns_503_not_a_fake_success() -> None:
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/confirm", headers=TEACHER,
        json={"decisions": [{"submission_item_id": str(ITEM_A), "score": "1.00"}],
              "item_max_scores": {str(ITEM_A): "5.00"}})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "grading_not_configured"


def test_publish_requires_idempotency_key() -> None:
    services, _ = build_services()
    response = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish", headers=TEACHER,
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_withdraw_requires_idempotency_key_and_reason() -> None:
    services, _ = build_services()
    no_key = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/withdraw", headers=TEACHER,
        json={"reason": "写错"})
    assert no_key.status_code == 400
    bad = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/withdraw", headers=TEACHER,
        json={"reason": "  "})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------

def test_confirm_missing_items_returns_review_incomplete_with_list() -> None:
    services, _ = build_services(confirmed=False)
    response = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/confirm", headers=TEACHER,
        json={"decisions": [{"submission_item_id": str(ITEM_A), "score": "5.00"}],
              "item_max_scores": {str(ITEM_A): "5.00", str(ITEM_B): "10.00"}})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "review_incomplete"
    assert str(ITEM_B) in detail["missing"]


def test_confirm_rubric_mismatch_is_422() -> None:
    services, _ = build_services(confirmed=False)
    response = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/confirm", headers=TEACHER,
        json={"decisions": [{"submission_item_id": str(ITEM_A), "score": "5.00",
                             "criterion_scores": [{"criterion_id": "s1", "score": "2.00"},
                                                  {"criterion_id": "s2", "score": "2.99"}]}],
              "item_max_scores": {str(ITEM_A): "5.00"}})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "rubric_score_mismatch"


def test_confirm_succeeds_and_reports_no_publish_time() -> None:
    services, _ = build_services(confirmed=False)
    response = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/confirm", headers=TEACHER,
        json={"decisions": [
            {"submission_item_id": str(ITEM_A), "score": "5.00"},
            {"submission_item_id": str(ITEM_B), "score": "10.00"}],
            "item_max_scores": {str(ITEM_A): "5.00", str(ITEM_B): "10.00"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["published_at"] is None, "确认不等于发布"
    assert body["total_score"] == "15.00"


# ---------------------------------------------------------------------------
# 发布 / 撤回
# ---------------------------------------------------------------------------

def test_publish_returns_replayed_flag() -> None:
    services, _ = build_services()
    first = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-flag"},
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "published"
    assert first.json()["replayed"] is False


def test_publish_with_key_replays_idempotently() -> None:
    services, _ = build_services()
    payload = {"expected_items": [str(ITEM_A), str(ITEM_B)]}
    first = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-1"}, json=payload)
    second = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-1"}, json=payload)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["revision_no"] == first.json()["revision_no"]


def test_withdraw_then_publish_is_refused_with_distinct_code() -> None:
    services, _ = build_services()
    client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-1"},
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    withdrawn = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/withdraw",
        headers={**TEACHER, "Idempotency-Key": "wd-1"}, json={"reason": "评分标准错"})
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    again = client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-9"},
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] in {"grade_already_published",
                                              "grade_already_withdrawn"}


# ---------------------------------------------------------------------------
# 学生 DTO
# ---------------------------------------------------------------------------

def test_student_grades_only_show_published_versions() -> None:
    services, repo = build_services()
    # 只有确认、没有发布：学生看不到任何成绩
    response = client(services).get("/api/v1/grading/my-grades", headers=STUDENT)
    assert response.status_code == 200
    assert response.json() == [], "确认中的成绩不能提前给学生看"


def test_student_grades_after_publish_contain_no_rubric_fields() -> None:
    services, repo = build_services()
    client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-1"},
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    class StudentActor:
        id = UUID(int=9)
        submission_id = SUBMISSION
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_services] = lambda: services
    app.dependency_overrides[api.require_student] = lambda: StudentActor()
    response = TestClient(app).get("/api/v1/grading/my-grades", headers=STUDENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body and body[0]["total_score"] == "14.00"
    encoded = json.dumps(body, ensure_ascii=False)
    for forbidden in ('"rubric"', '"rationale"', '"suggested_score"', '"model"'):
        assert forbidden not in encoded, f"学生成绩里出现了 {forbidden}"


def test_student_grades_exclude_draft_regrade_versions() -> None:
    services, repo = build_services()
    client(services).post(
        f"/api/v1/grading/submissions/{SUBMISSION}/publish",
        headers={**TEACHER, "Idempotency-Key": "pub-1"},
        json={"expected_items": [str(ITEM_A), str(ITEM_B)]})
    class StudentActor:
        id = UUID(int=9)
        submission_id = SUBMISSION
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_services] = lambda: services
    app.dependency_overrides[api.require_student] = lambda: StudentActor()
    client_fixed = TestClient(app)
    appeal = client_fixed.post("/api/v1/grading/appeals", headers=STUDENT,
                               json={"submission_id": str(SUBMISSION),
                                     "grade_revision_id": str(
                                         repo.grading.grade_revisions[-1].id),
                                     "reason": "第 2 题给少了"})
    assert appeal.status_code == 201, appeal.text
    appeal_id = appeal.json()["appeal_id"]
    client_fixed.post(f"/api/v1/grading/appeals/{appeal_id}/accept", headers=TEACHER)
    client_fixed.post(f"/api/v1/grading/appeals/{appeal_id}/regrade", headers=TEACHER,
                      json={"new_total": "15.00", "change_reason": "复核补分"})
    grades = client_fixed.get("/api/v1/grading/my-grades", headers=STUDENT)
    assert grades.json()[0]["total_score"] == "14.00", "重评草稿不能提前可见"


def test_teacher_role_cannot_call_student_endpoints() -> None:
    services, _ = build_services()
    response = client(services).get("/api/v1/grading/my-grades", headers=TEACHER)
    assert response.status_code == 403


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
    print(f"✓ 评分 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
