"""在线作答 API 的错误语义与"学生只能看自己"（阶段 C 任务 7）。

API 层要钉的是四件事：

  * 缺 Idempotency-Key → 400（写接口不能"帮你生成一个"）；
  * 越权与不存在都给 404（不告诉试探者存在性）；
  * 冲突、已结束、已撤回分别是不同的 409，前端才能给不同的提示；
  * 响应体里**没有**答案字段（`answer/solution/rubric` 扫描）。

服务用依赖注入替换成内存实现：这些用例测的是 HTTP 契约，不是 SQL。

运行：`python server/tests/test_online_api.py`
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import online_api as api  # noqa: E402
from app.online.contracts import (  # noqa: E402
    AttemptFinalized,
    PolicyDenied,
    ResourceNotFound,
    ResponseConflict,
    SaveResponseResult,
    Withdrawn,
)
from app.online.repository import (  # noqa: E402
    AttemptAssignment,
    AttemptItemSeed,
    InMemoryAttemptRepository,
)
from app.online.responses import (  # noqa: E402
    IdempotencyConflict,
    InMemoryResponseRepository,
)
from app.online.service import AttemptService, StartResult  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
STUDENT = {"X-Actor": "acc-student", "X-Roles": "student"}
NOW = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
ASSIGNMENT = UUID("dd000000-0000-4000-8000-000000000001")
ATTEMPT = UUID("aa000000-0000-4000-8000-000000000001")
SLOT = UUID("cc000000-0000-4000-8000-0000000000a1")


class StubAttemptService:
    def __init__(self, *, start_error=None, found=None, owner_error=None, row=None):
        self.start_error = start_error
        self.found = found
        self.owner_error = owner_error
        self.row = row
        self._now = lambda: NOW

    def find_active(self, *, student_id, assignment_id):
        return self.found

    def start(self, *, student_id, assignment_id, idempotency_key=None):
        if self.start_error:
            raise self.start_error
        return StartResult(attempt_id=ATTEMPT, status="in_progress", attempt_no=1,
                           server_deadline_at=NOW + timedelta(hours=1),
                           worker_due_at=NOW + timedelta(hours=1, minutes=10),
                           reused=False, item_count=2)

    def owner_check(self, *, attempt_id, student_id):
        if self.owner_error:
            raise self.owner_error
        return self.row


class StubResponseService:
    def __init__(self, *, error=None):
        self.error = error
        self.saved = []

    def save(self, **kwargs):
        if self.error:
            raise self.error
        self.saved.append(kwargs)
        return SaveResponseResult(
            attempt_id=kwargs["attempt_id"], slot_id=kwargs["slot_id"],
            client_revision_id=kwargs["client_revision_id"], revision_no=1,
            server_received_at=NOW, raw_response=kwargs["raw_response"],
            parse_status="parsed")


def client(attempt_service, response_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_attempt_service] = lambda: attempt_service
    app.dependency_overrides[api.get_response_service] = (
        lambda: response_service or StubResponseService())
    return TestClient(app)


# ---------------------------------------------------------------------------
# 幂等键与状态码
# ---------------------------------------------------------------------------

def test_start_requires_idempotency_key() -> None:
    response = client(StubAttemptService()).post(
        f"/api/v1/assignments/{ASSIGNMENT}/attempts", headers=STUDENT)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_start_returns_attempt_payload() -> None:
    response = client(StubAttemptService()).post(
        f"/api/v1/assignments/{ASSIGNMENT}/attempts",
        headers={**STUDENT, "Idempotency-Key": "start-1"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attempt_id"] == str(ATTEMPT)
    assert body["item_count"] == 2
    assert body["status"] == "in_progress"


def test_start_maps_domain_errors() -> None:
    cases = [
        (PolicyDenied("assignment_not_open: 未发布"), 422, "assignment_not_open"),
        (Withdrawn("作业已撤回"), 409, "assignment_withdrawn"),
        (ResourceNotFound("不存在"), 404, "attempt_not_found"),
    ]
    for error, status, code in cases:
        response = client(StubAttemptService(start_error=error)).post(
            f"/api/v1/assignments/{ASSIGNMENT}/attempts",
            headers={**STUDENT, "Idempotency-Key": "start-x"})
        assert response.status_code == status, (error, response.text)
        assert response.json()["detail"]["code"] == code


def test_no_active_attempt_is_204() -> None:
    response = client(StubAttemptService()).get(
        f"/api/v1/assignments/{ASSIGNMENT}/attempt", headers=STUDENT)
    assert response.status_code == 204, response.text


def test_active_attempt_is_reported() -> None:
    found = StartResult(attempt_id=ATTEMPT, status="in_progress", attempt_no=1,
                        server_deadline_at=NOW, worker_due_at=NOW, reused=True,
                        item_count=2)
    response = client(StubAttemptService(found=found)).get(
        f"/api/v1/assignments/{ASSIGNMENT}/attempt", headers=STUDENT)
    assert response.status_code == 200
    assert response.json()["attempt_id"] == str(ATTEMPT)


def test_other_students_attempt_is_404() -> None:
    response = client(StubAttemptService(
        owner_error=ResourceNotFound("不属于本人"))).get(
        f"/api/v1/attempts/{ATTEMPT}", headers=STUDENT)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "attempt_not_found"


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------

def test_save_requires_client_revision_and_base() -> None:
    response = client(StubAttemptService(), StubResponseService()).patch(
        f"/api/v1/attempts/{ATTEMPT}/slots/{SLOT}",
        json={"raw_response": {"text": "x"}}, headers=STUDENT)
    assert response.status_code == 422, response.text


def test_save_returns_revision_and_parse_status() -> None:
    response = client(StubAttemptService(), StubResponseService()).patch(
        f"/api/v1/attempts/{ATTEMPT}/slots/{SLOT}",
        json={"client_revision_id": "c-1", "base_revision_no": 0,
              "raw_response": {"text": "x+1"}}, headers=STUDENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision_no"] == 1
    assert body["parse_status"] == "parsed"


def test_conflict_carries_server_result() -> None:
    conflict = ResponseConflict("response_conflict: 服务端已是第 1 版")
    conflict.server_result = SaveResponseResult(
        attempt_id=ATTEMPT, slot_id=SLOT, client_revision_id="c-1", revision_no=1,
        server_received_at=NOW, raw_response={"text": "最新"}, parse_status="parsed")
    response = client(StubAttemptService(), StubResponseService(
        error=conflict)).patch(
        f"/api/v1/attempts/{ATTEMPT}/slots/{SLOT}",
        json={"client_revision_id": "c-2", "base_revision_no": 0,
              "raw_response": {"text": "旧"}}, headers=STUDENT)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "response_conflict"
    assert detail["server_result"]["revision_no"] == 1, "必须带回服务端最新本人响应"


def test_finalized_and_idempotency_conflict_are_distinct_409s() -> None:
    for error, code in ((AttemptFinalized("已交卷"), "attempt_finalized"),
                        (IdempotencyConflict("同一 client id 换了内容"),
                         "idempotency_conflict")):
        response = client(StubAttemptService(), StubResponseService(
            error=error)).patch(
            f"/api/v1/attempts/{ATTEMPT}/slots/{SLOT}",
            json={"client_revision_id": "c-1", "base_revision_no": 1,
                  "raw_response": {"text": "x"}}, headers=STUDENT)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == code


def test_student_role_is_required_for_saving() -> None:
    response = client(StubAttemptService(), StubResponseService()).patch(
        f"/api/v1/attempts/{ATTEMPT}/slots/{SLOT}",
        json={"client_revision_id": "c-1", "base_revision_no": 0,
              "raw_response": {}}, headers={"X-Actor": "acc-teacher", "X-Roles": "teacher"})
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# 交卷
# ---------------------------------------------------------------------------

def test_finalize_requires_idempotency_key() -> None:
    response = client(StubAttemptService()).post(
        f"/api/v1/attempts/{ATTEMPT}/finalize", headers=STUDENT)
    assert response.status_code == 400


def test_attempt_payload_never_contains_answers() -> None:
    """学生端 DTO 必须是白名单：这条把"答案不会出现在响应里"钉在 HTTP 层。"""
    from app.online.contracts import SlotRow

    class Row:
        id = ATTEMPT
        assignment_id = ASSIGNMENT
        paper_version_id = UUID(int=7)
        student_id = UUID(int=8)
        attempt_no = 1
        status = "in_progress"
        started_at = NOW
        server_deadline_at = NOW + timedelta(hours=1)
        worker_due_at = NOW + timedelta(hours=1)

    service = StubAttemptService(row=Row())

    class Repo(api.PostgresOnlineRepository):  # type: ignore[misc]
        def assignment(self, assignment_id):  # noqa: D102
            return AttemptAssignment(
                id=ASSIGNMENT, course_id=UUID(int=1), course_ref="analysis",
                offering_id=UUID(int=2), paper_version_id=UUID(int=7),
                status="published", distribution_kind="online",
                visible_at=NOW - timedelta(hours=1), due_at=NOW + timedelta(hours=1),
                hard_close_at=NOW + timedelta(hours=2),
                policy=api.compute_deadline.__globals__["PolicyTiming"](),
            )

        def student_slots(self, attempt_id):  # noqa: D102
            return (SlotRow(slot_id=SLOT, slot_no=1, stem="求极限", input_kind="text"),)

    original = api.PostgresOnlineRepository
    api.PostgresOnlineRepository = Repo
    try:
        response = client(service).get(f"/api/v1/attempts/{ATTEMPT}", headers=STUDENT)
    finally:
        api.PostgresOnlineRepository = original
    assert response.status_code == 200, response.text
    encoded = json.dumps(response.json(), ensure_ascii=False)
    for forbidden in ('"answer"', '"solution"', '"rubric"', '"answer_test"'):
        assert forbidden not in encoded, f"学生响应里出现了 {forbidden}"
    assert response.json()["slots"][0]["stem"] == "求极限"


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
    print(f"✓ 在线作答 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
