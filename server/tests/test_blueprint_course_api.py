"""按课程号建蓝图 + 蓝图列表 + 候选题目标签的接口测试。

为什么要有这组接口：`blueprint_id` 是 uuid5，前端拿不到命名空间。只给 uuid 入口的
话，教师建完第一版就再也找不回自己那份蓝图（没有列表），"接着上一份继续"也就无从
谈起。测试要钉住的正是这两件事：
  1. 同一个 (课程号, 别名) 一定推导出同一个 blueprint_id，追加版本不必先知道 uuid；
  2. 列表只给每份蓝图的最新版，且**先授权再查询**——没课程范围的教师连"这门课有
     几份蓝图"都看不到。

标签接口单独测：它是唯一返回题面文字的蓝图侧接口，必须用与候选快照相同的
`exercise_uuid` 当键，否则前端连不上候选。

运行：`python server/tests/test_blueprint_course_api.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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
    blueprint_id_for_course,
    ref_uuid,
)
from app.releases import exercise_uuid  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}

# 幂等重放要求载荷逐字一致，所以 Release Context 固定在夹具里。
RELEASE_CONTEXT = str(uuid4())


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class ScopedAuthorizer:
    """课程范围授权的测试替身：端口与生产 CourseScopeAuthorizer 一致。"""

    def __init__(self, courses: set[str], teachers: set[str]) -> None:
        self.courses = courses
        self.teachers = teachers
        self.asked: list[tuple[str, str]] = []
        self.service_accounts: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if action not in {"paper_blueprint.write", "paper_blueprint.read"}:
            raise PermissionError(action)
        self.asked.append((action, str(scope.get("course_ref"))))
        course_ref = scope.get("course_ref")
        if course_ref == "missing":
            raise UnknownCourseError(str(course_ref))
        if course_ref not in self.courses:
            raise PermissionError(f"course not in scope: {course_ref}")
        if scope.get("actor_ref") not in self.teachers:
            raise PermissionError(f"teacher has no class in course: {scope.get('actor_ref')}")

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


class Clock:
    """单调递增的时钟：排序断言不能靠"两次 uuid4 恰好不同"。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


def make_service(authorizer: ScopedAuthorizer | None = None,
                 clock: Clock | None = None) -> BlueprintService:
    return BlueprintService(
        repository=InMemoryBlueprintRepository(),
        authorizer=authorizer or ScopedAuthorizer({"analysis"}, {"acc-teacher"}),
        clock=clock or Clock(),
    )


def client(service: BlueprintService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if service is not None:
        app.dependency_overrides[api.get_blueprint_service] = lambda: service
    return TestClient(app)


def body(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "slug": "manual",
        "offering_ref": "class-2026-fall",
        "release_context_id": RELEASE_CONTEXT,
        "purpose": "formal",
        "total_score": "100.00",
        "duration_seconds": 7200,
        "sections": [
            {
                "code": "A",
                "title": "基础",
                "slots": [
                    {"code": "A1", "score": "40.00"},
                    {"code": "A2", "score": "60.00"},
                ],
            }
        ],
        "review_policy": "formal_dual_review",
        "base_version_id": None,
    }
    payload.update(overrides)
    return payload


def create(http: TestClient, course_ref: str = "analysis", key: str = "course-1",
           **overrides: object):
    headers = dict(TEACHER_HEADERS)
    if key is not None:
        headers["Idempotency-Key"] = key
    return http.post(f"/api/v1/courses/{course_ref}/paper-blueprints",
                     json=body(**overrides), headers=headers)


def listing(http: TestClient, course_ref: str = "analysis"):
    return http.get(f"/api/v1/courses/{course_ref}/paper-blueprints",
                    headers=TEACHER_HEADERS)


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
# 身份推导
# ---------------------------------------------------------------------------

def test_same_course_and_slug_derive_the_same_blueprint_id() -> None:
    first = blueprint_id_for_course("analysis", "manual")
    again = blueprint_id_for_course(" analysis ", " manual ")
    assert first == again, "同一课程号/别名的首尾空白不该改变蓝图身份"
    assert first == ref_uuid("paper-blueprint", "analysis/manual"), (
        "别名必须进 uuid5 的输入，否则两份蓝图会撞成一份"
    )
    assert blueprint_id_for_course("analysis", "final") != first


def test_blank_course_or_slug_is_rejected_at_the_boundary() -> None:
    for args in (("", "manual"), ("analysis", "  ")):
        try:
            blueprint_id_for_course(*args)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝空参数：{args!r}")


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------

def test_course_route_creates_the_first_version() -> None:
    service = make_service()
    with client(service) as http:
        response = create(http, key="course-create-1")
    expect_status(response, 201)
    payload = response.json()
    assert payload["version_no"] == 1
    assert payload["course_ref"] == "analysis"
    assert payload["offering_ref"] == "class-2026-fall"
    assert payload["blueprint_id"] == str(blueprint_id_for_course("analysis", "manual"))
    assert payload["course_id"] == str(ref_uuid("course", "analysis"))
    assert payload["created"] is True


def test_repeat_content_does_not_grow_a_second_version() -> None:
    service = make_service()
    with client(service) as http:
        first = create(http, key="course-dup-1")
        second = create(http, key="course-dup-2")
    expect_status(first, 201)
    expect_status(second, 200)
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["version_no"] == 1


def test_changed_content_needs_the_current_base_version() -> None:
    service = make_service()
    with client(service) as http:
        first = create(http, key="course-v1")
        assert first.status_code == 201
        missing = create(http, key="course-v2-missing", duration_seconds=8100)
        stale = create(http, key="course-v2-stale", duration_seconds=8100,
                       base_version_id=str(uuid4()))
        advanced = create(http, key="course-v2-ok", duration_seconds=8100,
                          base_version_id=first.json()["id"])
    expect_status(missing, 409, "base_version_required")
    expect_status(stale, 409, "stale_base_version")
    expect_status(advanced, 201)
    assert advanced.json()["version_no"] == 2


def test_two_slugs_are_two_blueprints() -> None:
    service = make_service()
    with client(service) as http:
        manual = create(http, key="slug-manual", slug="manual")
        final = create(http, key="slug-final", slug="final")
        rows = listing(http).json()
    expect_status(manual, 201)
    expect_status(final, 201)
    assert manual.json()["blueprint_id"] != final.json()["blueprint_id"]
    assert len(rows) == 2


def test_slug_is_restricted_to_unambiguous_characters() -> None:
    with client(make_service()) as http:
        response = create(http, key="slug-bad", slug="期中/期末")
    expect_status(response, 422)


def test_missing_idempotency_key_is_rejected() -> None:
    with client(make_service()) as http:
        response = create(http, key=None)
    expect_status(response, 400, "idempotency_key_required")


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------

def test_new_course_lists_nothing_without_an_error() -> None:
    with client(make_service()) as http:
        response = listing(http)
    expect_status(response, 200)
    assert response.json() == [], "没有蓝图是正常状态，不该用 404 表达"


def test_list_returns_only_the_latest_version_per_blueprint() -> None:
    service = make_service()
    with client(service) as http:
        first = create(http, key="list-v1")
        second = create(http, key="list-v2", duration_seconds=8100,
                        base_version_id=first.json()["id"])
        rows = listing(http).json()
    assert second.status_code == 201
    assert len(rows) == 1, f"同一个蓝图只该出现一行：{rows}"
    assert rows[0]["id"] == second.json()["id"]
    assert rows[0]["version_no"] == 2


def test_list_is_ordered_newest_first() -> None:
    service = make_service()
    with client(service) as http:
        create(http, key="order-old", slug="midterm")
        create(http, key="order-new", slug="final")
        rows = listing(http).json()
    assert [row["blueprint_id"] for row in rows] == [
        str(blueprint_id_for_course("analysis", "final")),
        str(blueprint_id_for_course("analysis", "midterm")),
    ], rows


def test_list_authorises_before_querying() -> None:
    authorizer = ScopedAuthorizer({"analysis"}, {"acc-thief"})
    service = make_service(authorizer)
    with client(service) as http:
        response = listing(http)
    expect_status(response, 403, "course_scope_denied")
    assert authorizer.asked == [("paper_blueprint.read", "analysis")], (
        "授权必须发生在查询之前，否则没权限的人也能数出这门课有几份蓝图"
    )


def test_unknown_course_is_404_not_403() -> None:
    with client(make_service()) as http:
        response = listing(http, course_ref="missing")
    expect_status(response, 404, "course_not_found")


def test_student_cannot_create_or_list_course_blueprints() -> None:
    with client(None) as http:
        created = http.post(
            "/api/v1/courses/analysis/paper-blueprints",
            json=body(),
            headers={"X-Actor": "stu-1", "X-Roles": "student", "Idempotency-Key": "k"},
        )
        listed = http.get("/api/v1/courses/analysis/paper-blueprints",
                          headers={"X-Actor": "stu-1", "X-Roles": "student"})
    assert created.status_code == 403, created.text
    assert listed.status_code == 403, listed.text


# ---------------------------------------------------------------------------
# 候选题目标签
# ---------------------------------------------------------------------------

class FakeDb:
    """最小的 db 替身：只回答"这门课有哪些题"。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def query(self, sql: str, params: tuple) -> list[dict]:
        self.calls.append((sql, params))
        return self.rows


def with_fake_db(rows: list[dict]) -> FakeDb:
    return FakeDb(rows)


def labels(http: TestClient, version_id: str):
    return http.get(
        f"/api/v1/paper-blueprint-versions/{version_id}/candidate-labels",
        headers=TEACHER_HEADERS,
    )


def test_labels_are_keyed_by_the_same_uuid_as_the_candidate_snapshot() -> None:
    service = make_service()
    fake = with_fake_db([
        {"id": "analysis-01", "prompt_md": "# 连续性\n\n证明 f 连续。",
         "review_status": "approved", "license": "self-authored"},
        {"id": "analysis-02", "prompt_md": "", "review_status": "draft", "license": None},
    ])
    original = api.db
    api.db = fake
    try:
        with client(service) as http:
            created = create(http, key="labels-1")
            response = labels(http, created.json()["id"])
    finally:
        api.db = original
    expect_status(response, 200)
    payload = response.json()
    assert payload["count"] == 2
    keys = {item["exercise_id"]: item for item in payload["items"]}
    assert str(exercise_uuid("analysis", "analysis-01")) in keys, (
        "标签的键必须与候选证据里的 exercise_id 同源，否则前端连不上"
    )
    first = keys[str(exercise_uuid("analysis", "analysis-01"))]
    assert first["title"] == "连续性", "标题取第一行并去掉 Markdown 井号"
    assert first["exercise_ref"] == "analysis-01"
    assert first["review_status"] == "approved"
    second = keys[str(exercise_uuid("analysis", "analysis-02"))]
    assert second["title"] == "（无题干）"
    assert fake.calls and fake.calls[0][1] == ("analysis",), (
        "只扫本课程的题，不能把别的课漏出去"
    )


def test_labels_of_unknown_version_are_404() -> None:
    original = api.db
    api.db = with_fake_db([])
    try:
        with client(make_service()) as http:
            response = labels(http, str(uuid4()))
    finally:
        api.db = original
    expect_status(response, 404, "blueprint_version_not_found")


def test_labels_require_course_scope() -> None:
    service = make_service(ScopedAuthorizer({"other-course"}, {"acc-teacher"}))
    creator = make_service()
    with client(creator) as http:
        created = create(http, key="labels-scope")
    original = api.db
    api.db = with_fake_db([])
    try:
        # 换一套授权：同一个版本在只教别的课的教师那里必须是 404（仓储隔离）。
        with client(service) as http:
            response = labels(http, created.json()["id"])
    finally:
        api.db = original
    expect_status(response, 404, "blueprint_version_not_found")


def test_label_title_keeps_one_line_and_truncates() -> None:
    assert api.label_title("第一行\n第二行") == "第一行"
    assert api.label_title("   ## 带井号的标题  ") == "带井号的标题"
    assert api.label_title(None) == "（无题干）"
    long_line = "题" * 100
    title = api.label_title(long_line, limit=10)
    assert len(title) == 10 and title.endswith("…"), title


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
    print(f"✓ 课程蓝图与候选标签测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
