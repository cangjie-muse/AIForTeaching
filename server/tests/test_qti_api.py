"""阶段 E 任务 3：QTI HTTP 契约（导入只出草稿、拒绝带原因、导出被拒 409）。

运行：`python server/tests/test_qti_api.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import qti_api as api  # noqa: E402
from app.qti.contracts import ExportSlot  # noqa: E402
from app.qti.service import InMemoryQtiRepository, QtiService  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "acc-student", "X-Roles": "student"}
PAPER = UUID("ee000000-0000-4000-8000-000000000001")


def services(*, approved: bool = True, slots=None) -> api.Services:
    repo = InMemoryQtiRepository()
    qti = QtiService(repository=repo)
    return api.Services(
        qti=qti,
        export_slots=lambda paper: list(slots or []),
        paper_status=lambda paper: "approved" if approved else "draft",
        paper_title=lambda paper: "数学分析 I")


def client(svc) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_services] = lambda: svc
    return TestClient(app)


def document(*items) -> dict:
    return {"items": list(items)}


def item(ref="q-1", *, prompt="求极限", score="10.00", metadata=None) -> dict:
    body = {"identifier": ref, "itemBody": {"prompt": prompt},
            "interaction": {"type": "qti-text-entry"}, "maxScore": score}
    if metadata is not None:
        body["metadata"] = metadata
    return body


def slot(number: int, code: str) -> ExportSlot:
    return ExportSlot(slot_no=number, code=code, score=Decimal("10.00"), prompt="题干",
                      exercise_version_id=UUID(int=100 + number),
                      source_release_id=UUID(int=200 + number))


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def test_import_returns_draft_ready_never_approved() -> None:
    svc = services()
    response = client(svc).post("/api/v1/qti/imports", headers=TEACHER,
                                json={"course_ref": "analysis",
                                      "document": document(item())})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft_ready"
    assert body["item_count"] == 1


def test_import_with_errors_returns_rejected_and_differences() -> None:
    svc = services()
    response = client(svc).post("/api/v1/qti/imports", headers=TEACHER,
                                json={"course_ref": "analysis",
                                      "document": document(item(prompt=" "))})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["error_count"] == 1
    assert body["differences"][0]["code"] == "missing_prompt", (
        "拒绝必须带原因，不能只说「导入失败」")


def test_import_replay_is_reported() -> None:
    svc = services()
    payload = {"course_ref": "analysis", "document": document(item())}
    first = client(svc).post("/api/v1/qti/imports", headers=TEACHER, json=payload)
    second = client(svc).post("/api/v1/qti/imports", headers=TEACHER, json=payload)
    assert second.json()["replayed"] is True
    assert second.json()["run_id"] == first.json()["run_id"]


def test_import_history_is_queryable() -> None:
    svc = services()
    response = client(svc).post("/api/v1/qti/imports", headers=TEACHER,
                                json={"course_ref": "analysis", "document": document(item())})
    run_id = response.json()["run_id"]
    fetched = client(svc).get(f"/api/v1/qti/imports/{run_id}", headers=TEACHER)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "draft_ready"


def test_unknown_import_is_404() -> None:
    response = client(services()).get(f"/api/v1/qti/imports/{UUID(int=999)}", headers=TEACHER)
    assert response.status_code == 404


def test_student_cannot_import() -> None:
    response = client(services()).post("/api/v1/qti/imports", headers=STUDENT,
                                       json={"course_ref": "analysis",
                                             "document": document(item())})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def test_export_returns_payload_and_hash() -> None:
    svc = services(slots=[slot(1, "A1"), slot(2, "A2")])
    response = client(svc).post("/api/v1/qti/exports", headers=TEACHER,
                                json={"paper_version_id": str(PAPER)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "exported"
    assert body["item_count"] == 2
    assert body["payload"]["totalScore"] == "20.00"
    assert len(body["payload_sha256"]) == 64


def test_export_blocked_returns_409_with_reasons() -> None:
    svc = services(approved=False, slots=[slot(1, "A1")])
    response = client(svc).post("/api/v1/qti/exports", headers=TEACHER,
                                json={"paper_version_id": str(PAPER)})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "qti_export_blocked"
    assert "paper_status_draft" in detail["reasons"], (
        "拒绝原因要能直接告诉教师，而不是一个空错误")


def test_export_empty_slots_is_blocked_not_empty_success() -> None:
    svc = services(slots=[])
    response = client(svc).post("/api/v1/qti/exports", headers=TEACHER,
                                json={"paper_version_id": str(PAPER)})
    assert response.status_code == 409
    assert response.json()["detail"]["reasons"]


def test_unwired_qti_service_returns_503() -> None:
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post("/api/v1/qti/imports", headers=TEACHER,
                                    json={"course_ref": "analysis",
                                          "document": document(item())})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "qti_not_configured"


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
    print(f"✓ QTI API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
