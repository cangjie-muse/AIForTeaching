"""纸卷草稿 API（server/app/paper_versions.py）的接口级测试。

服务层只回答"内容合不合法、edit_seq 推到几"，真正会被前端和脚本踩到的是**状态码、
幂等语义与权限**：

  * 写接口必须带 Idempotency-Key（400），重放同键同载荷返回原结果（200 +
    `Idempotent-Replay`），同键不同载荷 409；
  * 草稿编辑带 `base_version=edit_seq`，过期一律 409 `stale_version` 并回传当前版本，
    前端才能提示"别人已经改过，请刷新"而不是静默覆盖；
  * approved 卷不能 PATCH（409 `paper_not_draft`），只能以新 paper 身份克隆；
  * 替换只能来自建卷时固定的候选快照（422 `candidate_not_in_snapshot`）；
  * 学生 403、未登录 401、无课程范围 403；
  * PATCH 不暴露 status 字段（多传就是 422）。

本机没有可连的 PostgreSQL，因此用 FastAPI 的 dependency_overrides 把服务换成内存
仓储；SQL / 触发器由 test_papers.py 的静态断言覆盖。

运行：`python server/tests/test_paper_api.py`
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import paper_versions as api  # noqa: E402
from app.blueprints import (  # noqa: E402
    BlueprintService,
    InMemoryBlueprintRepository,
    ref_uuid,
)
from app.candidates import (  # noqa: E402
    CandidateService,
    InMemoryCandidateRepository,
    ReleaseScopeInfo,
)
from app.contracts import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    CandidateEvidence,
    CandidateRecord,
    CandidateScope,
    canonical_hash,
)
from app.papers import (  # noqa: E402
    PAPER_READ_ACTION,
    PAPER_WRITE_ACTION,
    InMemoryPaperRepository,
    PaperService,
)
from app.releases import content_hash, exercise_uuid, family_id_for, version_id_for  # noqa: E402

# 身份解析在模块加载时读 AUTH_MODE；本文件只跑接口，固定成 dev（请求头身份）。
auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "class-2026-fall"
OFFERING_ID = ref_uuid("offering", OFFERING)
ACTOR_REF = "acc-teacher"
ACTOR_ID = ref_uuid("account", ACTOR_REF)
ANSWER = "取 $\\varepsilon>0$，由定义……"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

class ScopedAuthorizer:
    def __init__(self, courses: set[str] | None = None,
                 teachers: set[str] | None = None) -> None:
        self.courses = {"analysis"} if courses is None else courses
        self.teachers = {"acc-teacher"} if teachers is None else teachers

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        allowed = {
            "paper_blueprint.write", "paper_blueprint.read",
            PAPER_WRITE_ACTION, PAPER_READ_ACTION,
        }
        if action not in allowed:
            raise PermissionError(action)
        course_ref = scope.get("course_ref")
        if course_ref is None:
            raise PermissionError("缺少课程范围")
        if course_ref not in self.courses:
            raise PermissionError(f"course not in scope: {course_ref}")
        actor_ref = scope.get("actor_ref")
        # 候选服务的授权端口不传 actor_ref（它只判课程范围）；这里只在带身份时校验。
        if actor_ref is not None and actor_ref not in self.teachers:
            raise PermissionError(f"teacher has no class in course: {course_ref}")

    def is_human(self, actor_id: UUID) -> bool:
        return True


def blueprint_content() -> BlueprintContent:
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        sections=(
            BlueprintSection(
                code="A",
                title="基础",
                instruction="写出完整证明",
                slots=(
                    BlueprintSlot(code="A1", score=Decimal("40.00")),
                    BlueprintSlot(code="A2", score=Decimal("60.00")),
                ),
            ),
        ),
        review_policy="formal_dual_review",
    )


def make_record(ref: str = "ex-1") -> CandidateRecord:
    prompt = f"证明第 {ref} 题"
    digest = content_hash(prompt, ANSWER)
    evidence = CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=CONTEXT,
        exercise_id=exercise_uuid(COURSE, ref),
        exercise_version_id=version_id_for(COURSE, ref, digest),
        exercise_instance_id=None,
        exercise_family_id=family_id_for(COURSE, ref),
        exercise_kind="proof",
        concept_ids=(),
        learning_outcome_ids=(),
        difficulty_label="medium",
        estimated_seconds=600,
        decision_seqs={"exercise": 1},
        latest_decisions={"exercise": "approved"},
        dependency_manifest_hash=canonical_hash({"ref": ref, "digest": digest}),
        content_hash=digest,
        in_context=True,
        license_valid=True,
        revoked=False,
        hidden_evaluation=False,
        private_draft=False,
        parameterized=False,
        instance_approved=True,
        dependencies_complete=True,
        render_ready=True,
        language_pair_valid=True,
        purpose_allowed=True,
        cloud_allowed=True,
    )
    return CandidateRecord(
        evidence=evidence,
        release_state="included",
        review_status="teacher-reviewed",
        license="licensed",
        answer_present=True,
    )


def stack(*, authorizer: ScopedAuthorizer | None = None,
          seed_authorizer: ScopedAuthorizer | None = None) -> SimpleNamespace:
    """建一套「蓝图 + 候选快照 + 纸卷服务」。

    `seed_authorizer` 与 `authorizer` 分开，是为了构造"课程范围不同的教师"：
    夹具本身必须有权限把蓝图和候选快照建出来，被测的写路径才用受限的那个。
    """
    seeder = seed_authorizer or ScopedAuthorizer()
    blueprints = BlueprintService(
        repository=InMemoryBlueprintRepository(), authorizer=seeder)
    version = blueprints.create_version(
        actor_id=ACTOR_ID,
        content=blueprint_content(),
        blueprint_id=uuid4(),
        base_version_id=None,
        idempotency_key="api-blueprint",
        course_ref=COURSE,
        offering_ref=OFFERING,
        actor_ref=ACTOR_REF,
    )
    candidates = CandidateService(
        repository=InMemoryCandidateRepository(
            release=ReleaseScopeInfo(
                release_context_id=CONTEXT,
                course_id=COURSE_ID,
                course_ref=COURSE,
                offering_id=OFFERING_ID,
                offering_ref=OFFERING,
                status="frozen",
                item_count=2,
            ),
            records=(make_record("ex-1"), make_record("ex-2")),
        ),
        authorizer=seeder,
    )
    snapshot = candidates.snapshot(
        actor_id=ACTOR_ID, scope=CandidateScope.from_version(version))
    repo = InMemoryPaperRepository()
    service = PaperService(
        repository=repo,
        authorizer=authorizer or seeder,
        blueprints=blueprints,
        candidates=candidates,
    )
    return SimpleNamespace(
        service=service, repo=repo, version=version, snapshot=snapshot,
        authorizer=authorizer or seeder,
    )


def client(service: PaperService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if service is not None:
        app.dependency_overrides[api.get_paper_service] = lambda: service
    return TestClient(app)


def draft_body(f: SimpleNamespace, **overrides: object) -> dict:
    by_exercise = {record.evidence.exercise_id: record for record in f.snapshot.accepted}
    a1 = by_exercise[exercise_uuid(COURSE, "ex-1")].evidence.exercise_version_id
    a2 = by_exercise[exercise_uuid(COURSE, "ex-2")].evidence.exercise_version_id
    body: dict[str, object] = {
        "blueprint_version_id": str(f.version.id),
        "candidate_snapshot_id": str(f.snapshot.id),
        "selections": [
            {"slot_code": "A1", "exercise_version_id": str(a1)},
            {"slot_code": "A2", "exercise_version_id": str(a2)},
        ],
    }
    body.update(overrides)
    return body


def create(http: TestClient, f: SimpleNamespace, *, key: str = "create-1",
           **overrides: object):
    headers = dict(TEACHER_HEADERS)
    if key is not None:
        headers["Idempotency-Key"] = key
    return http.post("/api/v1/paper-versions", json=draft_body(f, **overrides), headers=headers)


def patch_slots(http: TestClient, paper_version_id: str, body: dict,
                *, key: str = "edit-1", headers: dict | None = None):
    merged = dict(TEACHER_HEADERS)
    merged.update(headers or {})
    if key is not None:
        merged["Idempotency-Key"] = key
    return http.patch(f"/api/v1/paper-versions/{paper_version_id}/slots",
                      json=body, headers=merged)


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
# 建卷
# ---------------------------------------------------------------------------

def test_teacher_creates_a_draft_with_fixed_sources() -> None:
    f = stack()
    with client(f.service) as http:
        response = create(http, f, key="create-ok")
    expect_status(response, 201)
    body = response.json()
    assert body["status"] == "draft"
    assert body["edit_seq"] == 1
    assert body["version_no"] == 1
    assert body["candidate_snapshot_id"] == str(f.snapshot.id)
    assert body["total_score"] == "100.00"
    assert body["paper_kind"] == "formal"
    assert body["sections"][0]["section_score"] == "100.00"
    slot = body["sections"][0]["slots"][0]
    assert slot["score"] == "40.00"
    assert slot["exercise_version_id"]
    assert slot["exercise_family_id"]
    assert slot["source_release_id"] == str(CONTEXT)
    assert slot["release_context_id"] == str(CONTEXT)
    assert len(slot["content_hash"]) == 64
    assert response.headers.get("Idempotent-Replay") is None


def test_missing_idempotency_key_is_400() -> None:
    f = stack()
    with client(f.service) as http:
        response = create(http, f, key=None)
    expect_status(response, 400, "idempotency_key_required")


def test_replay_returns_200_with_replay_header() -> None:
    f = stack()
    with client(f.service) as http:
        first = create(http, f, key="create-replay")
        second = create(http, f, key="create-replay")
    expect_status(first, 201)
    expect_status(second, 200)
    assert second.json()["id"] == first.json()["id"]
    assert second.headers.get("Idempotent-Replay", "").lower() == "true"


def test_unknown_slot_code_is_422() -> None:
    f = stack()
    with client(f.service) as http:
        response = create(http, f, key="create-unknown", selections=[
            {"slot_code": "Z9", "exercise_version_id": str(uuid4())},
        ])
    expect_status(response, 422, "unknown_slot_code")


def test_candidate_outside_the_snapshot_is_422() -> None:
    f = stack()
    with client(f.service) as http:
        response = create(http, f, key="create-stranger", selections=[
            {"slot_code": "A1", "exercise_version_id": str(uuid4())},
        ])
    expect_status(response, 422, "candidate_not_in_snapshot")


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def test_edit_moves_score_and_rejects_stale_base_version() -> None:
    f = stack()
    with client(f.service) as http:
        created = create(http, f, key="edit-create")
        paper = created.json()
        slot_id = paper["sections"][0]["slots"][0]["id"]
        operation = {"kind": "score", "slot_id": slot_id, "score": "45.50"}
        first = patch_slots(http, paper["id"], {"base_version": 1, "operations": [operation]},
                            key="edit-1")
        stale = patch_slots(http, paper["id"], {"base_version": 1, "operations": [operation]},
                            key="edit-2")
    expect_status(first, 200)
    assert first.json()["edit_seq"] == 2
    assert first.json()["sections"][0]["slots"][0]["score"] == "45.50"
    expect_status(stale, 409, "stale_version")
    assert stale.json()["detail"]["current_version"] == 2


def test_approved_paper_cannot_be_edited_in_place() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="approved-create").json()
        approved = f.repo.versions[UUID(paper["id"])].model_copy(update={"status": "approved"})
        f.repo.versions[UUID(paper["id"])] = approved
        response = patch_slots(http, paper["id"], {
            "base_version": 1,
            "operations": [{
                "kind": "score",
                "slot_id": paper["sections"][0]["slots"][0]["id"],
                "score": "1.00",
            }],
        }, key="approved-edit")
    expect_status(response, 409, "paper_not_draft")


def test_patch_rejects_unknown_slot_and_out_of_range_move() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="ops-create").json()
        unknown = patch_slots(http, paper["id"], {
            "base_version": 1,
            "operations": [{"kind": "score", "slot_id": str(uuid4()), "score": "1.00"}],
        }, key="ops-unknown")
        bad_move = patch_slots(http, paper["id"], {
            "base_version": 1,
            "operations": [{
                "kind": "move",
                "slot_id": paper["sections"][0]["slots"][0]["id"],
                "target_slot_no": 5,
            }],
        }, key="ops-move")
    expect_status(unknown, 422, "slot_not_found")
    expect_status(bad_move, 422, "invalid_slot_order")


def test_three_decimal_score_and_status_field_are_rejected() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="precision-create").json()
        slot_id = paper["sections"][0]["slots"][0]["id"]
        bad_score = patch_slots(http, paper["id"], {
            "base_version": 1,
            "operations": [{"kind": "score", "slot_id": slot_id, "score": "45.555"}],
        }, key="precision-edit")
        with_status = patch_slots(http, paper["id"], {
            "base_version": 1,
            "status": "approved",
            "operations": [{"kind": "score", "slot_id": slot_id, "score": "45.50"}],
        }, key="status-edit")
    expect_status(bad_score, 422)
    expect_status(with_status, 422)


def test_edit_events_expose_the_change_set() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="events-create").json()
        slot = paper["sections"][0]["slots"][0]
        a2 = next(
            record for record in f.snapshot.accepted
            if record.evidence.exercise_id == exercise_uuid(COURSE, "ex-2")
        )
        edited = patch_slots(http, paper["id"], {
            "base_version": 1,
            "operations": [{
                "kind": "replace",
                "slot_id": slot["id"],
                "exercise_version_id": str(a2.evidence.exercise_version_id),
                "reason": "换成同难度题",
            }],
        }, key="events-edit")
        events = http.get(f"/api/v1/paper-versions/{paper['id']}/edit-events",
                          headers=TEACHER_HEADERS)
    expect_status(edited, 200)
    expect_status(events, 200)
    body = events.json()
    assert len(body) == 1
    event = body[0]
    assert event["kind"] == "replace"
    assert event["edit_seq"] == 2
    assert event["previous_exercise_version_id"] == slot["exercise_version_id"]
    assert event["new_exercise_version_id"] == str(a2.evidence.exercise_version_id)
    assert event["reason"] == "换成同难度题"


# ---------------------------------------------------------------------------
# 克隆
# ---------------------------------------------------------------------------

def test_clone_of_approved_creates_a_new_paper_identity() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="clone-create").json()
        approved = f.repo.versions[UUID(paper["id"])].model_copy(update={"status": "approved"})
        f.repo.versions[UUID(paper["id"])] = approved
        response = http.post(
            "/api/v1/paper-versions",
            json={"base_paper_version_id": paper["id"]},
            headers={**TEACHER_HEADERS, "Idempotency-Key": "clone-api"},
        )
    expect_status(response, 201)
    body = response.json()
    assert body["status"] == "draft"
    assert body["id"] != paper["id"]
    assert body["paper_id"] != paper["paper_id"]
    assert body["base_paper_version_id"] == paper["id"]
    assert body["manifest_hash"] == paper["manifest_hash"]


# ---------------------------------------------------------------------------
# 读取与权限
# ---------------------------------------------------------------------------

def test_get_returns_the_stored_paper_and_404_for_unknown() -> None:
    f = stack()
    with client(f.service) as http:
        paper = create(http, f, key="get-create").json()
        found = http.get(f"/api/v1/paper-versions/{paper['id']}", headers=TEACHER_HEADERS)
        missing = http.get(f"/api/v1/paper-versions/{uuid4()}", headers=TEACHER_HEADERS)
    expect_status(found, 200)
    assert found.json()["id"] == paper["id"]
    expect_status(missing, 404, "paper_not_found")


def test_student_is_forbidden_and_anonymous_is_unauthorized() -> None:
    with client(None) as http:
        student = http.post(
            "/api/v1/paper-versions",
            json={"base_paper_version_id": str(uuid4())},
            headers={"X-Actor": "stu-1", "X-Roles": "student", "Idempotency-Key": "k"},
        )
        anonymous = http.get(f"/api/v1/paper-versions/{uuid4()}")
    assert student.status_code == 403, student.text
    assert anonymous.status_code == 401, anonymous.text


def test_teacher_without_course_scope_is_forbidden() -> None:
    f = stack(authorizer=ScopedAuthorizer(courses={"other-course"}))
    with client(f.service) as http:
        response = create(http, f, key="no-scope")
    expect_status(response, 403, "course_scope_denied")


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
    print(f"✓ 纸卷草稿 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
