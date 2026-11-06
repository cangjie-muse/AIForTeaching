"""候选资格在"真实数据形状"下的集成测试（不连库，用假 cursor 灌真实行形状）。

为什么还要这一层：单元测试喂的是已经算好的 `CandidateRecord`，而生产路径是
**一段 SQL**——`release_context` 的 base 链、`release_item` 的固定版本、
`public.exercise` 的当前内容、`exercise_node` 的依赖、`item_exposure_event` 的曝光。
SQL 里少写一个 `WHERE`，前面的单元测试全绿，老师照样能把已撤回的题放进正式卷。

所以这里做三件事：

  1. 用假 cursor 返回"数据库会给的那些行"，逐类验证不合格对象不会变成 accepted；
  2. 断言写路径确实在 `db.transaction(assessment_service=True)` 里（paper_* 是 FORCE RLS）；
  3. 断言 SQL 只碰真实存在的表（任务书红线：不许假装 Release 体系已存在）。

另外验证两个接口语义：POST 内容键幂等（同 manifest 第二次是 200 + created=False），
GET 返回可解释的拒绝原因但**不返回答案正文**。

运行：`python server/tests/test_candidate_eligibility.py`
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app import paper_blueprints as api  # noqa: E402
from app.blueprints import (  # noqa: E402
    BlueprintService,
    InMemoryBlueprintRepository,
    ref_uuid,
)
from app.candidates import (  # noqa: E402
    CandidateRecord,
    CandidateScope,
    CandidateService,
    InMemoryCandidateRepository,
    PostgresCandidateRepository,
    ReleaseContextNotFoundError,
    ReleaseScopeInfo,
    _PostgresCandidateUnitOfWork,
)
from app.contracts import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    CandidateEvidence,
)
from app.releases import (  # noqa: E402
    PostgresReleaseRepository,
    ReleaseService,
    content_hash,
    exercise_uuid,
    family_id_for,
    version_id_for,
)

COURSE = "analysis"
COURSE_ID = ref_uuid("course", COURSE)
OFFERING = "class-2026-fall"
OFFERING_ID = ref_uuid("offering", OFFERING)
CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()
TEACHER_HEADERS = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}


# ---------------------------------------------------------------------------
# 假 cursor：按 SQL 关键字返回真实行形状
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, responses: list[tuple[str, list[dict]]]) -> None:
        self.responses = responses
        self.log: list[tuple[str, object]] = []
        self.rows: list[dict] = []
        self.rowcount = 0
        self.description: tuple | None = None

    def execute(self, sql: str, params: object = ()) -> None:
        self.log.append((sql, params))
        self.rows = []
        # 候选 SQL 的 CTE 里同时出现 release_context 与 release_item，按"最先声明
        # 的 marker"匹配会把 Release 行当成候选行。取 SQL 里**最后出现**的那个表，
        # 就是这条语句真正要读的主表。
        matches = [
            (marker, rows) for marker, rows in self.responses if marker in sql
        ]
        if matches:
            _, rows = max(matches, key=lambda pair: sql.rfind(pair[0]))
            self.rows = list(rows)
        self.description = ("x",) if self.rows else None
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict]:
        return list(self.rows)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):  # type: ignore[no-untyped-def]
        @contextmanager
        def _cursor():
            yield self._cursor

        return _cursor()


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def connection(self):  # type: ignore[no-untyped-def]
        @contextmanager
        def _connection():
            yield FakeConnection(self._cursor)

        return _connection()


@contextmanager
def fake_db(responses: list[tuple[str, list[dict]]]):  # type: ignore[no-untyped-def]
    cursor = FakeCursor(responses)
    original = db.pool
    db.pool = FakePool(cursor)  # type: ignore[assignment]
    try:
        yield cursor
    finally:
        db.pool = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 行夹具：与 _PostgresCandidateUnitOfWork 的 SELECT 列一一对应
# ---------------------------------------------------------------------------

def release_row(
    *,
    status: str = "frozen",
    course_ref: str = COURSE,
    offering_ref: str | None = OFFERING,
    context_id: UUID = CONTEXT,
) -> dict:
    return {
        "id": context_id,
        "course_id": ref_uuid("course", course_ref),
        "course_ref": course_ref,
        "offering_id": None if offering_ref is None else ref_uuid("offering", offering_ref),
        "offering_ref": offering_ref,
        "purpose": "formal",
        "status": status,
        "item_count": 1,
    }


def record_row(
    ref: str = "ex-1",
    *,
    prompt: str = "证明数列收敛",
    answer: str | None = "由定义可得",
    review_status: str = "teacher-reviewed",
    license: str | None = "licensed",
    language: str = "zh",
    state: str = "included",
    released_content_hash: str | None = None,
    include_exercise: bool = True,
) -> dict:
    digest = content_hash(prompt, answer or "")
    return {
        "course_ref": COURSE,
        "exercise_ref": ref,
        "source_release_id": CONTEXT,
        "exercise_id": exercise_uuid(COURSE, ref),
        "exercise_version_id": version_id_for(COURSE, ref, digest),
        "released_content_hash": released_content_hash or digest,
        "exercise_family_id": family_id_for(COURSE, ref),
        "state": state,
        "joined_exercise_ref": ref if include_exercise else None,
        "prompt_md": prompt if include_exercise else None,
        "answer_md": answer if include_exercise else None,
        "review_status": review_status if include_exercise else None,
        "license": license if include_exercise else None,
        "language": language if include_exercise else None,
        "difficulty": 3 if include_exercise else None,
        "content_version": 1 if include_exercise else None,
        "node_refs": ["n-1"] if include_exercise else None,
    }


def exposure_row(
    *,
    kind: str,
    family_ref: str = "ex-1",
    offering_ref: str | None = OFFERING,
    visible_from: datetime = NOW,
) -> dict:
    return {
        "exercise_version_id": version_id_for(COURSE, family_ref, content_hash("任意题干", "任意答案")),
        "exercise_instance_id": None,
        "exercise_family_id": family_id_for(COURSE, family_ref),
        "scope_offering_id": None if offering_ref is None else ref_uuid("offering", offering_ref),
        "event_kind": kind,
        "visible_from": visible_from,
        "occurred_at": visible_from,
    }


def scope(*, purpose: str = "formal", context_id: UUID = CONTEXT) -> CandidateScope:
    return CandidateScope(
        blueprint_version_id=uuid4(),
        purpose=purpose,  # type: ignore[arg-type]
        course_id=COURSE_ID,
        course_ref=COURSE,
        offering_id=OFFERING_ID,
        offering_ref=OFFERING,
        release_context_id=context_id,
    )


def run_with_fake_db(rows, events, *, purpose="formal", context_id=CONTEXT):  # type: ignore[no-untyped-def]
    """用 Postgres 仓储 + 假 cursor 跑真正的 SQL 分支。"""
    responses = [
        ("assessment.release_context", [] if rows is None else [release_row()]),
        ("assessment.release_item", rows or []),
        ("assessment.item_exposure_event", events or []),
        # INSERT ... RETURNING 的一行；假库不做唯一约束，所以永远"插入了新行"
        ("assessment.candidate_snapshot", [{"id": uuid4(), "created_at": NOW}]),
    ]
    with fake_db(responses):
        service = CandidateService(
            repository=PostgresCandidateRepository(),
            authorizer=ScopedAuthorizer(),
            clock=lambda: NOW,
        )
        return service.snapshot(
            actor_id=uuid4(), scope=scope(purpose=purpose, context_id=context_id)
        )


# ---------------------------------------------------------------------------
# 1. 六类不合格对象：SQL 不能把它们变成 accepted
# ---------------------------------------------------------------------------

def test_clean_row_becomes_an_accepted_candidate() -> None:
    snapshot = run_with_fake_db([record_row()], [])
    assert len(snapshot.accepted) == 1, snapshot.rejected
    assert snapshot.rejected == ()
    assert snapshot.accepted[0].review_status == "teacher-reviewed"


def test_retired_row_is_rejected_as_revoked() -> None:
    snapshot = run_with_fake_db([record_row(review_status="retired")], [])
    assert snapshot.accepted == ()
    assert "revoked" in snapshot.rejected[0].reasons


def test_invalid_license_row_is_rejected() -> None:
    snapshot = run_with_fake_db([record_row(license="unknown")], [])
    assert snapshot.accepted == ()
    assert "license_invalid" in snapshot.rejected[0].reasons


def test_private_draft_row_is_rejected_without_review() -> None:
    snapshot = run_with_fake_db([record_row(review_status="draft")], [])
    assert snapshot.accepted == ()
    assert "review_not_approved" in snapshot.rejected[0].reasons


def test_row_without_answer_is_rejected() -> None:
    snapshot = run_with_fake_db([record_row(answer=None)], [])
    assert snapshot.accepted == ()
    assert "answer_missing" in snapshot.rejected[0].reasons


def test_excluded_release_item_is_rejected() -> None:
    snapshot = run_with_fake_db([record_row(state="excluded")], [])
    assert snapshot.accepted == ()
    assert "outside_release_context" in snapshot.rejected[0].reasons


def test_edited_exercise_is_rejected_as_content_drift() -> None:
    """Release 固定的是旧内容哈希：说明题目被原地改过，不能再当作"审核过的同一版"。"""
    snapshot = run_with_fake_db([record_row(released_content_hash="b" * 64)], [])
    assert snapshot.accepted == ()
    assert "content_drift" in snapshot.rejected[0].reasons, snapshot.rejected[0].reasons


def test_deleted_exercise_is_rejected_not_crashing() -> None:
    snapshot = run_with_fake_db([record_row(include_exercise=False)], [])
    assert snapshot.accepted == ()
    assert "exercise_missing" in snapshot.rejected[0].reasons


def test_practice_answer_exposure_blocks_formal_snapshot_through_sql() -> None:
    snapshot = run_with_fake_db(
        [record_row()], [exposure_row(kind="practice_answer_released")]
    )
    assert snapshot.accepted == ()
    assert "exposure_conflict" in snapshot.rejected[0].reasons


def test_practice_purpose_still_accepts_the_same_row() -> None:
    snapshot = run_with_fake_db(
        [record_row()], [exposure_row(kind="practice_answer_released")], purpose="practice"
    )
    assert snapshot.accepted, snapshot.rejected


# ---------------------------------------------------------------------------
# 2. SQL 形状：base 链、RLS 事务、内容键去重
# ---------------------------------------------------------------------------

def test_release_chain_is_resolved_recursively_and_limited() -> None:
    source = inspect.getsource(_PostgresCandidateUnitOfWork)
    assert "WITH RECURSIVE chain" in source, (
        "读取候选时必须沿 base_release_id 解析有效 base 链，"
        "否则「基础发布 + 小修订」里的题会全部丢失"
    )
    assert "< 32" in source, "base 链没有深度上限：一次误配的环会让查询永远跑不完"
    assert re.search(r"ROW_NUMBER\(\)\s+OVER\s*\(\s*PARTITION BY", source), (
        "同一题在 base 链上出现多次时没有取最近一层，旧版本会盖住新版本"
    )


def test_every_read_and_write_goes_through_assessment_transaction() -> None:
    source = inspect.getsource(PostgresCandidateRepository)
    assert "db.transaction(assessment_service=True)" in source, (
        "候选仓储没在事务里 SET LOCAL app.assessment_service='on'，FORCE RLS 会挡住读写"
    )


def test_snapshot_insert_is_content_keyed() -> None:
    source = inspect.getsource(_PostgresCandidateUnitOfWork)
    assert "ON CONFLICT (blueprint_version_id, candidate_manifest_hash) DO NOTHING" in source, (
        "候选快照没按 (蓝图版本, manifest 哈希) 去重：重试会插第二行"
    )
    assert "assessment.candidate_snapshot" in source
    assert "assessment.release_item" in source
    assert "exercise_node" in source, (
        "没有连 exercise_node：概念覆盖会是空的，任务 4 的组卷约束无法核验"
    )


def test_missing_release_row_is_reported_not_silently_empty() -> None:
    responses = [
        ("assessment.release_context", []),
        ("assessment.release_item", []),
        ("assessment.item_exposure_event", []),
    ]
    with fake_db(responses):
        service = CandidateService(
            repository=PostgresCandidateRepository(),
            authorizer=ScopedAuthorizer(),
            clock=lambda: NOW,
        )
        try:
            service.snapshot(actor_id=uuid4(), scope=scope())
        except ReleaseContextNotFoundError:
            pass
        else:
            raise AssertionError("Release Context 不存在时没有报错，等于静默返回空候选")


def test_writes_are_wrapped_in_a_single_transaction_scope() -> None:
    """假 cursor 上第一条语句必须是 RLS 作用域，否则真库里一行都写不进去。"""
    responses = [
        ("assessment.release_context", [release_row()]),
        ("assessment.release_item", [record_row()]),
        ("assessment.item_exposure_event", []),
        ("assessment.candidate_snapshot", [{
            "id": uuid4(),
            "candidate_manifest": {},
            "candidate_manifest_hash": "c" * 64,
            "created_at": NOW,
        }]),
    ]
    with fake_db(responses) as cursor:
        service = CandidateService(
            repository=PostgresCandidateRepository(),
            authorizer=ScopedAuthorizer(),
            clock=lambda: NOW,
        )
        service.snapshot(actor_id=uuid4(), scope=scope())
    assert cursor.log[0][0] == "SET LOCAL app.assessment_service = 'on'", cursor.log[0][0]


def release_context_row(**overrides: object) -> dict:
    row = {
        "id": CONTEXT,
        "course_id": COURSE_ID,
        "course_ref": COURSE,
        "offering_id": OFFERING_ID,
        "offering_ref": OFFERING,
        "purpose": "formal",
        "status": "frozen",
        "base_release_id": None,
        "content_hash": "a" * 64,
        "created_by": UUID(int=1),
        "created_at": NOW,
        "item_count": 1,
    }
    row.update(overrides)
    return row


def test_release_repository_freezes_and_reads_back_a_context() -> None:
    """冻结写路径必须与 0029 的列一一对应（少一列真库就炸，静态断言查不出来）。"""
    digest = content_hash("证明数列收敛", "由定义可得")
    responses = [
        ("FROM exercise e", [{
            "exercise_ref": "ex-1",
            "prompt_md": "证明数列收敛",
            "answer_md": "由定义可得",
            "review_status": "teacher-reviewed",
            "license": "licensed",
            "language": "zh",
            "difficulty": 3,
            "content_version": 1,
            "node_refs": ["n-1"],
        }]),
        ("assessment.release_context", [release_context_row()]),
        ("assessment.release_item", [{
            "id": UUID(int=7),
            "release_context_id": CONTEXT,
            "source_release_id": CONTEXT,
            "exercise_id": exercise_uuid(COURSE, "ex-1"),
            "exercise_version_id": version_id_for(COURSE, "ex-1", digest),
            "content_hash": digest,
            "exercise_family_id": family_id_for(COURSE, "ex-1"),
            "course_ref": COURSE,
            "exercise_ref": "ex-1",
            "state": "included",
            "decided_by": UUID(int=1),
            "decided_at": NOW,
        }]),
    ]
    with fake_db(responses):
        service = ReleaseService(
            repository=PostgresReleaseRepository(), clock=lambda: NOW
        )
        context = service.freeze(
            actor_id=UUID(int=1), course_ref=COURSE, purpose="formal",
            exercise_refs=("ex-1",), offering_ref=OFFERING,
            release_context_id=CONTEXT,
        )
        items = service.items(CONTEXT)
    assert context.item_count == 1, "冻结返回的 Release 没有条目数"
    assert items[0].exercise_version_id == version_id_for(COURSE, "ex-1", digest)
    assert items[0].exercise_ref == "ex-1"


# ---------------------------------------------------------------------------
# 3. HTTP 契约（内存仓储，不连库）
# ---------------------------------------------------------------------------

def blueprint_content() -> BlueprintContent:
    slot = BlueprintSlot(code="A1", score=Decimal("100.00"))
    return BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        total_score=Decimal("100.00"),
        duration_seconds=7200,
        sections=(BlueprintSection(code="A", title="一", slots=(slot,)),),
        review_policy="formal_dual_review",
    )


class ScopedAuthorizer:
    """按课程范围授权的测试替身，端口与生产 CourseScopeAuthorizer 一致。"""

    def __init__(self) -> None:
        self.service_accounts: set[UUID] = set()

    def require(self, actor_id: UUID, action: str, **facts: object) -> None:
        if action not in {"paper_blueprint.write", "paper_blueprint.read"}:
            raise PermissionError(action)
        if facts.get("course_ref") != COURSE:
            raise PermissionError(f"course not in scope: {facts.get('course_ref')}")

    def is_human(self, actor_id: UUID) -> bool:
        return actor_id not in self.service_accounts


def blueprint_service() -> tuple[BlueprintService, object]:
    service = BlueprintService(
        repository=InMemoryBlueprintRepository(), authorizer=ScopedAuthorizer()
    )
    row = service.create_version(
        actor_id=ref_uuid("account", "acc-teacher"),
        content=blueprint_content(),
        blueprint_id=uuid4(),
        base_version_id=None,
        course_ref=COURSE,
        offering_ref=OFFERING,
        actor_ref="acc-teacher",
    )
    return service, row


def memory_record(
    ref: str = "ex-1",
    *,
    prompt: str = "证明数列收敛",
    answer: str = "由定义可得",
    review_status: str = "teacher-reviewed",
    license: str | None = "licensed",
    release_state: str = "included",
) -> CandidateRecord:
    digest = content_hash(prompt, answer)
    decision = (
        "approved" if review_status in {"teacher-reviewed", "published"}
        else "revoked" if review_status == "retired"
        else "pending"
    )
    evidence = CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=CONTEXT,
        exercise_id=exercise_uuid(COURSE, ref),
        exercise_version_id=version_id_for(COURSE, ref, digest),
        exercise_instance_id=None,
        exercise_family_id=family_id_for(COURSE, ref),
        exercise_kind="proof",
        concept_ids=(ref_uuid("course", "concept-n1"),),
        learning_outcome_ids=(),
        difficulty_label="medium",
        estimated_seconds=600,
        decision_seqs={"exercise": 1},
        latest_decisions={"exercise": decision},
        dependency_manifest_hash="a" * 64,
        content_hash=digest,
        in_context=release_state == "included",
        license_valid=bool(license) and (license or "").lower() not in {
            "unknown", "unlicensed", "none", "denied",
        },
        revoked=decision == "revoked",
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
        release_state=release_state,  # type: ignore[arg-type]
        review_status=review_status,
        license=license,
        answer_present=bool(answer.strip()),
    )


def candidate_repository(*records: CandidateRecord) -> InMemoryCandidateRepository:
    return InMemoryCandidateRepository(
        release=ReleaseScopeInfo(
            release_context_id=CONTEXT,
            course_id=COURSE_ID,
            course_ref=COURSE,
            offering_id=OFFERING_ID,
            offering_ref=OFFERING,
            status="frozen",
            item_count=max(1, len(records)),
        ),
        records=records,
    )


def api_client(blueprints: BlueprintService,
               candidate_service: CandidateService) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_blueprint_service] = lambda: blueprints
    app.dependency_overrides[api.get_candidate_service] = lambda: candidate_service
    return TestClient(app)


def post_snapshot(client: TestClient, version_id: object, key: str = "cand-1"):
    return client.post(
        f"/api/v1/paper-blueprint-versions/{version_id}/candidate-snapshots",
        headers={**TEACHER_HEADERS, "Idempotency-Key": key},
    )


def test_post_is_idempotent_by_candidate_manifest() -> None:
    blueprints, row = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(memory_record()),
        authorizer=ScopedAuthorizer(),
        clock=lambda: NOW,
    )
    with api_client(blueprints, service) as http:
        first = post_snapshot(http, row.id)
        second = post_snapshot(http, row.id, key="cand-2")
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["candidate_manifest_hash"] == second.json()["candidate_manifest_hash"]
    assert second.json()["created"] is False


def test_post_requires_idempotency_key() -> None:
    blueprints, row = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(), authorizer=ScopedAuthorizer(), clock=lambda: NOW
    )
    with api_client(blueprints, service) as http:
        response = http.post(
            f"/api/v1/paper-blueprint-versions/{row.id}/candidate-snapshots",
            headers=TEACHER_HEADERS,
        )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_api_does_not_leak_answer_text() -> None:
    blueprints, row = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(memory_record(answer="机密答案-9f3a")),
        authorizer=ScopedAuthorizer(),
        clock=lambda: NOW,
    )
    with api_client(blueprints, service) as http:
        response = post_snapshot(http, row.id)
    assert response.status_code == 201, response.text
    assert "机密答案-9f3a" not in response.text, "候选接口返回了答案正文"
    assert "证明数列收敛" not in response.text, "候选接口返回了题干正文"
    payload = json.loads(response.text)
    assert payload["accepted"][0]["evidence"]["content_hash"]
    assert payload["rejected"] == []


def test_get_returns_the_stored_snapshot_with_reason_codes() -> None:
    blueprints, row = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(memory_record("ex-bad", review_status="draft")),
        authorizer=ScopedAuthorizer(),
        clock=lambda: NOW,
    )
    with api_client(blueprints, service) as http:
        created = post_snapshot(http, row.id)
        fetched = http.get(
            f"/api/v1/paper-blueprint-versions/{row.id}/candidates",
            headers=TEACHER_HEADERS,
        )
    assert created.status_code == 201, created.text
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["rejected"][0]["reasons"] == ["review_not_approved"]
    assert fetched.json()["candidate_manifest_hash"] == created.json()["candidate_manifest_hash"]


def test_get_without_snapshot_is_404() -> None:
    blueprints, row = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(), authorizer=ScopedAuthorizer(), clock=lambda: NOW
    )
    with api_client(blueprints, service) as http:
        response = http.get(
            f"/api/v1/paper-blueprint-versions/{row.id}/candidates",
            headers=TEACHER_HEADERS,
        )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "candidate_snapshot_not_found"


def test_unknown_blueprint_version_is_404() -> None:
    blueprints, _ = blueprint_service()
    service = CandidateService(
        repository=candidate_repository(), authorizer=ScopedAuthorizer(), clock=lambda: NOW
    )
    with api_client(blueprints, service) as http:
        response = post_snapshot(http, uuid4())
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "blueprint_version_not_found"


def test_student_is_forbidden() -> None:
    blueprints, row = blueprint_service()
    with api_client(blueprints, CandidateService(repository=candidate_repository())) as http:
        response = http.post(
            f"/api/v1/paper-blueprint-versions/{row.id}/candidate-snapshots",
            headers={"X-Actor": "stu-1", "X-Roles": "student", "Idempotency-Key": "k"},
        )
    assert response.status_code == 403, response.text


def test_candidate_scope_from_blueprint_rejects_missing_course_ref() -> None:
    _, row = blueprint_service()
    without_ref = row.model_copy(update={"course_ref": None})
    try:
        CandidateScope.from_version(without_ref)
    except ValueError:
        pass
    else:
        raise AssertionError("蓝图没有 course_ref 时仍构造出了候选范围（无法做课程授权）")


def test_route_derives_scope_from_the_blueprint_not_the_request() -> None:
    """候选范围一律从蓝图版本派生；请求体里不能自己指定 Release Context。"""
    source = inspect.getsource(api.create_candidate_snapshot)
    helper = inspect.getsource(api._candidate_scope)
    assert "CandidateScope.from_version" in helper, (
        "候选范围不是从蓝图派生的：调用方可以指定任意 Release Context 绕过门禁"
    )
    assert "_candidate_scope(version)" in source, (
        "路由没有用蓝图派生的范围，而是自己拼了一个"
    )
    parameters = inspect.signature(api.create_candidate_snapshot).parameters
    assert "release_context_id" not in parameters
    assert "purpose" not in parameters


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
    print(f"✓ 候选资格集成测试全部通过（{len(tests)} 项，未连真库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
