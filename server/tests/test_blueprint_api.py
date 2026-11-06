"""蓝图版本 HTTP 契约（server/app/paper_blueprints.py）的接口级测试。

为什么单独测路由层：服务层只回答"内容合不合法、版本号是多少"，真正会被前端和
脚本踩到的是**状态码与幂等语义**——
  * 学生 / 无课程范围的教师必须是 403，未登录是 401（前端据此决定弹登录还是报权限）；
  * 缺 Idempotency-Key 要 400，而不是默默创建两版；
  * 同键同载荷重放返回同一版本（200），同键不同载荷是 409；
  * 过期 base_version 是 409，不是覆盖别人的修改。

本机没有可连的 PostgreSQL，因此用 FastAPI 的 dependency_overrides 把服务换成内存
仓储；实际 SQL 的 RLS / 唯一键由 test_blueprints.py 的静态断言覆盖。

运行：`python server/tests/test_blueprint_api.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import paper_blueprints as api  # noqa: E402
from app.blueprints import (  # noqa: E402
    BlueprintService,
    InMemoryBlueprintRepository,
    UnknownCourseError,
    ref_uuid,
)

# 身份解析在模块加载时读 AUTH_MODE；本文件只跑接口，固定成 dev（请求头身份）。
auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}

# 幂等重放要求载荷逐字节一致：release_context_id 每次随机生成的话，
# 同键第二次请求会被正确判成"不同载荷"。所以夹具里固定它。
RELEASE_CONTEXT = str(uuid4())


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class ScopedAuthorizer:
    """按课程范围授权的测试替身：与生产 CourseScopeAuthorizer 的端口一致。"""

    def __init__(self, courses: set[str], teachers: set[str]) -> None:
        self.courses = courses
        self.teachers = teachers
        self.service_accounts: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if action not in {"paper_blueprint.write", "paper_blueprint.read"}:
            raise PermissionError(action)
        course_ref = scope.get("course_ref")
        if course_ref == "missing":
            raise UnknownCourseError(str(course_ref))
        if course_ref not in self.courses:
            raise PermissionError(f"course not in scope: {course_ref}")
        if scope.get("actor_ref") not in self.teachers:
            raise PermissionError(f"teacher has no class in course: {scope.get('actor_ref')}")
        offering_ref = scope.get("offering_ref")
        if offering_ref is not None and not str(offering_ref).startswith("class-"):
            raise PermissionError(f"offering not in scope: {offering_ref}")

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


def payload(**overrides: object) -> dict:
    body: dict[str, object] = {
        "course_ref": "analysis",
        "offering_ref": "class-2026-fall",
        "release_context_id": RELEASE_CONTEXT,
        "purpose": "formal",
        "total_score": "100.00",
        "duration_seconds": 7200,
        "sections": [
            {
                "code": "A",
                "title": "基础",
                "instruction": "写出完整证明",
                "slots": [
                    {"code": "A1", "score": "40.00", "exercise_kinds": ["proof"]},
                    {"code": "A2", "score": "60.00"},
                ],
            }
        ],
        "hard_constraints": {"coverage_required": True},
        "soft_objectives": {},
        "review_policy": "formal_dual_review",
        "base_version_id": None,
    }
    body.update(overrides)
    return body


def make_service(authorizer: ScopedAuthorizer | None = None) -> BlueprintService:
    return BlueprintService(
        repository=InMemoryBlueprintRepository(),
        authorizer=authorizer or ScopedAuthorizer({"analysis"}, {"acc-teacher"}),
    )


def client(service: BlueprintService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if service is not None:
        app.dependency_overrides[api.get_blueprint_service] = lambda: service
    return TestClient(app)


def create(client_: TestClient, blueprint_id: UUID | None = None, **overrides: object):
    headers = dict(TEACHER_HEADERS)
    key = overrides.pop("idempotency_key", "idem-0001")
    if key is not None:
        headers["Idempotency-Key"] = str(key)
    return client_.post(
        f"/api/v1/paper-blueprints/{blueprint_id or uuid4()}/versions",
        json=payload(**overrides),
        headers=headers,
    )


def expect_status(response, status: int, code: str | None = None) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == status, (
        f"预期 {status}，实际 {response.status_code}：{response.text}"
    )
    if code is not None:
        detail = response.json().get("detail")
        assert isinstance(detail, dict) and detail.get("code") == code, (
            f"预期错误码 {code}，实际 {detail!r}"
        )


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------

def test_teacher_creates_a_blueprint_version() -> None:
    service = make_service()
    with client(service) as http:
        response = create(http, **{"idempotency_key": "create-1"})
    expect_status(response, 201)
    body = response.json()
    assert body["version_no"] == 1
    assert body["total_score"] == "100.00", body["total_score"]
    assert body["course_ref"] == "analysis"
    assert body["offering_ref"] == "class-2026-fall"
    assert body["created_by_ref"] == "acc-teacher"
    assert body["course_id"] == str(ref_uuid("course", "analysis"))
    assert body["content"]["sections"][0]["slots"][0]["score"] == "40.00"
    assert body["replayed"] is False
    assert body["created"] is True
    assert response.headers.get("Idempotent-Replay", "false").lower() == "false"


def test_replay_returns_the_same_version_with_200() -> None:
    service = make_service()
    blueprint_id = uuid4()
    with client(service) as http:
        first = create(http, blueprint_id=blueprint_id, idempotency_key="create-2")
        second = create(http, blueprint_id=blueprint_id, idempotency_key="create-2")
    expect_status(first, 201)
    expect_status(second, 200)
    assert second.json()["id"] == first.json()["id"]
    assert second.headers.get("Idempotent-Replay", "").lower() == "true"


def test_duplicate_content_with_new_key_returns_200() -> None:
    """内容没变、换了一把幂等键：不是重放，但也不该长出新版本。"""
    service = make_service()
    blueprint_id = uuid4()
    with client(service) as http:
        first = create(http, blueprint_id=blueprint_id, idempotency_key="dup-1")
        second = create(http, blueprint_id=blueprint_id, idempotency_key="dup-2")
    expect_status(first, 201)
    expect_status(second, 200)
    body = second.json()
    assert body["id"] == first.json()["id"]
    assert body["version_no"] == 1
    assert body["created"] is False
    assert body["replayed"] is False
    assert second.headers.get("Idempotent-Replay") is None


def test_same_key_with_different_payload_is_409() -> None:
    service = make_service()
    blueprint_id = uuid4()
    with client(service) as http:
        create(http, blueprint_id=blueprint_id, idempotency_key="create-3")
        conflict = create(
            http, blueprint_id=blueprint_id, idempotency_key="create-3", duration_seconds=5400
        )
    expect_status(conflict, 409, "idempotency_conflict")


def test_missing_idempotency_key_is_400() -> None:
    with client(make_service()) as http:
        response = create(http, idempotency_key=None)
    expect_status(response, 400, "idempotency_key_required")


def test_stale_base_version_is_409() -> None:
    service = make_service()
    blueprint_id = uuid4()
    with client(service) as http:
        first = create(http, blueprint_id=blueprint_id, idempotency_key="v1")
        assert first.status_code == 201
        missing = create(
            http, blueprint_id=blueprint_id, idempotency_key="v2-missing",
            duration_seconds=8100,
        )
        stale = create(
            http, blueprint_id=blueprint_id, idempotency_key="v2-stale",
            duration_seconds=8100, base_version_id=str(uuid4()),
        )
        advanced = create(
            http, blueprint_id=blueprint_id, idempotency_key="v2-ok",
            duration_seconds=8100, base_version_id=first.json()["id"],
        )
    expect_status(missing, 409, "base_version_required")
    expect_status(stale, 409, "stale_base_version")
    expect_status(advanced, 201)
    assert advanced.json()["version_no"] == 2


def test_invalid_total_score_is_422() -> None:
    with client(make_service()) as http:
        response = create(http, idempotency_key="bad-total", total_score="120.00")
    expect_status(response, 422, "blueprint_invalid")


# ---------------------------------------------------------------------------
# 鉴权与课程范围
# ---------------------------------------------------------------------------

def test_student_is_forbidden() -> None:
    """不覆盖依赖：require('teacher') 在构造仓储之前就应当拒绝。"""
    with client(None) as http:
        response = http.post(
            f"/api/v1/paper-blueprints/{uuid4()}/versions",
            json=payload(),
            headers={"X-Actor": "stu-1", "X-Roles": "student", "Idempotency-Key": "k"},
        )
    assert response.status_code == 403, response.text


def test_anonymous_is_unauthorized() -> None:
    with client(None) as http:
        response = http.post(
            f"/api/v1/paper-blueprints/{uuid4()}/versions",
            json=payload(),
            headers={"Idempotency-Key": "k"},
        )
    assert response.status_code == 401, response.text


def test_teacher_without_course_scope_is_forbidden() -> None:
    service = make_service(ScopedAuthorizer({"analysis"}, {"acc-someone-else"}))
    with client(service) as http:
        response = create(http, idempotency_key="no-scope")
    expect_status(response, 403, "course_scope_denied")


def test_unknown_course_is_404() -> None:
    service = make_service()
    with client(service) as http:
        response = create(http, idempotency_key="no-course", course_ref="missing")
    expect_status(response, 404, "course_not_found")


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def test_get_returns_the_version() -> None:
    service = make_service()
    with client(service) as http:
        created = create(http, idempotency_key="get-1")
        response = http.get(
            f"/api/v1/paper-blueprint-versions/{created.json()['id']}",
            headers=TEACHER_HEADERS,
        )
    expect_status(response, 200)
    assert response.json()["id"] == created.json()["id"]
    assert response.json()["version_no"] == 1
    assert response.json()["total_score"] == "100.00"


def test_get_unknown_version_is_404() -> None:
    with client(make_service()) as http:
        response = http.get(
            f"/api/v1/paper-blueprint-versions/{uuid4()}", headers=TEACHER_HEADERS
        )
    expect_status(response, 404, "blueprint_version_not_found")


def test_get_requires_course_scope() -> None:
    service = make_service(ScopedAuthorizer({"analysis"}, {"acc-teacher"}))
    with client(service) as http:
        created = create(http, idempotency_key="get-2")
    other = make_service(ScopedAuthorizer({"other-course"}, {"acc-teacher"}))
    with client(other) as http:
        response = http.get(
            f"/api/v1/paper-blueprint-versions/{created.json()['id']}",
            headers=TEACHER_HEADERS,
        )
    # 版本不存在于另一个仓储 → 404（不是泄露"这个 id 存在但你没权限"）
    expect_status(response, 404, "blueprint_version_not_found")


# ---------------------------------------------------------------------------
# 脚本入口
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
    print(f"✓ 蓝图 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
