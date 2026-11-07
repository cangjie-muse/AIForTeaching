"""待复核列表：教师只看到"需要人看"的提交，且不含答案正文。

运行：`python server/tests/test_grading_list_api.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import grading_list_api as api  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "acc-student", "X-Roles": "student"}
SUBMISSION = UUID("dd000000-0000-4000-8000-000000000001")
STUDENT_ID = UUID("5e5e0000-0000-4000-8000-0000000000d1")


def rows(limit: int = 50) -> list[dict]:
    data = [{"submission_id": str(SUBMISSION), "student_id": str(STUDENT_ID),
             "submitted_at": "2026-11-07T01:00:00Z", "item_count": 5,
             "requires_human_count": 2, "attempt_no": 1}]
    return data[:limit]


def client(wired: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if wired:
        api.wire_pending(lambda status, limit: rows(limit))
    else:
        api.pending = None
    return TestClient(app)


def test_list_returns_pending_submissions() -> None:
    response = client().get("/api/v1/grading/submissions", headers=TEACHER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["requires_human_count"] == 2


def test_list_never_contains_answer_text() -> None:
    """列表只给定位信息：学生答案正文不该在这里出现（点进去才拉）。"""
    import json

    body = client().get("/api/v1/grading/submissions", headers=TEACHER).json()
    encoded = json.dumps(body, ensure_ascii=False).lower()
    for forbidden in ('"raw_response"', '"answer"', '"response_text"'):
        assert forbidden not in encoded, f"列表里出现了 {forbidden}"


def test_limit_is_respected_and_bounded() -> None:
    response = client().get("/api/v1/grading/submissions?limit=1", headers=TEACHER)
    assert response.status_code == 200
    too_many = client().get("/api/v1/grading/submissions?limit=999", headers=TEACHER)
    assert too_many.status_code == 422, "上限必须被拒绝，而不是默默截断"


def test_bad_status_value_is_rejected() -> None:
    response = client().get("/api/v1/grading/submissions?status=whatever", headers=TEACHER)
    assert response.status_code == 422


def test_student_cannot_list_submissions() -> None:
    response = client().get("/api/v1/grading/submissions", headers=STUDENT)
    assert response.status_code == 403


def test_unwired_source_returns_503() -> None:
    response = client(wired=False).get("/api/v1/grading/submissions", headers=TEACHER)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "grading_not_configured"


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
    print(f"✓ 待复核列表接口测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
