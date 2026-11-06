"""版本化手工蓝图（server/app/blueprints.py）的服务语义与事务/迁移静态检查。

为什么单独测这一层：蓝图版本是整卷内容的唯一输入。它一旦出问题，后面每一步都会
"忠实地"把错误传下去——

  * 分值用 float 求和，100 分卷会出现 99.99999999999999；
  * JSON 键顺序参与哈希，教师改一个字典的插入顺序就让已批准的卷"内容变了"；
  * 版本号靠 count(*) 而不是 base_version 乐观锁，两个人同时保存就得到两个 v2；
  * 幂等键只在内存里记，进程重启后同一个重试请求又插一版。

这些错误都不会当场抛异常，所以在本层钉死。运行方式沿用仓库习惯：
`python server/tests/test_blueprints.py`（没有引入 pytest；文件顶层仍是 pytest 风格的
test_ 函数，装了就同样能收集）。

数据库相关的部分（RLS 的 SET LOCAL、advisory lock、唯一键）在仓库里没有可连的
PostgreSQL，因此用"假连接池记录 SQL"和"读迁移文本做结构断言"两种方式覆盖，
真正的连库集成测试留到有 DATABASE_URL 的环境。
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from pydantic import ValidationError  # noqa: E402  —— 供 test_ 函数按需使用

from app import db  # noqa: E402
from app.artifacts import StaticAuthorizer  # noqa: E402
from app.blueprints import (  # noqa: E402
    BlueprintService,
    BlueprintValidationError,
    InMemoryBlueprintRepository,
    IdempotencyConflictError,
    StaleBaseVersionError,
    ref_uuid,
)
from app.contracts import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
    canonical_hash,
)

SCHEMA_PATH = ROOT / "packages" / "contracts" / "assessment" / "blueprint.schema.json"
MIGRATION_DIR = ROOT / "db" / "migrations"
SCHEMA_SQL = ROOT / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def slot(code: str, score: str, **overrides: object) -> BlueprintSlot:
    payload: dict[str, object] = {
        "code": code,
        "score": Decimal(score),
        "exercise_kinds": (),
        "required_concept_ids": (),
        "learning_outcome_ids": (),
        "difficulty_label": None,
        "estimated_seconds": None,
        "mandatory_exercise_version_ids": (),
        "excluded_exercise_version_ids": (),
    }
    payload.update(overrides)
    return BlueprintSlot(**payload)  # type: ignore[arg-type]


def content(**overrides: object) -> BlueprintContent:
    payload: dict[str, object] = {
        "schema_version": "paper-blueprint/v1",
        "purpose": "formal",
        "course_id": uuid4(),
        "offering_id": uuid4(),
        "release_context_id": uuid4(),
        "total_score": Decimal("100.00"),
        "duration_seconds": 7200,
        "sections": (
            BlueprintSection(
                code="A",
                title="基础",
                instruction="写出证明",
                slots=(slot("A1", "40.00"), slot("A2", "60.00")),
            ),
        ),
        "hard_constraints": {"coverage_required": True},
        "soft_objectives": {},
        "review_policy": "formal_dual_review",
    }
    payload.update(overrides)
    return BlueprintContent(**payload)  # type: ignore[arg-type]


def service_for(actor: UUID, actions: set[str] | None = None):
    repository = InMemoryBlueprintRepository()
    authorizer = StaticAuthorizer({actor: actions if actions is not None else {"paper_blueprint.write", "paper_blueprint.read"}})
    return BlueprintService(repository=repository, authorizer=authorizer), repository


def expect(exc_type: type[BaseException], fn) -> BaseException:  # type: ignore[no-untyped-def]
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"预期抛出 {exc_type.__name__}，但调用成功返回")


# ---------------------------------------------------------------------------
# 计划书任务 2 步骤 1 的两条用例（逐字语义）
# ---------------------------------------------------------------------------

def test_blueprint_version_is_hashed_and_decimal_scores_are_exact() -> None:
    actor = uuid4()
    service = BlueprintService(
        repository=None,
        authorizer=StaticAuthorizer({actor: {"paper_blueprint.write"}}),
    )
    value = content()
    first = service.create_version(
        actor_id=actor, content=value, blueprint_id=None, base_version_id=None
    )
    second = service.create_version(
        actor_id=actor, content=value, blueprint_id=None, base_version_id=None
    )
    assert first.version_no == second.version_no == 1
    assert first.total_score == Decimal("100.00")
    assert sum((s.score for s in first.content.sections[0].slots), Decimal("0")) == Decimal("100.00")
    assert first.content_hash == second.content_hash
    assert first.content_hash == canonical_hash(value)


def test_blueprint_rejects_fractional_cent_and_score_mismatch() -> None:
    # 注意：BlueprintSlot 在构造时就会拒绝三位小数（common.Score 的 AfterValidator），
    # 所以这里绕过模型直接测服务层的兜底校验——服务层不能假设调用方一定先过 Pydantic。
    bad_score = Decimal("0.001")
    readback = expect(
        ValueError,
        lambda: BlueprintService.validate_content(bad_score, Decimal("1.00")),
    )
    assert "two decimal places" in str(readback)
    error = expect(
        ValueError,
        lambda: BlueprintService.validate_content(Decimal("90.00"), Decimal("100.00")),
    )
    assert "total_score" in str(error)
    # 更早一层：契约模型在构造时就拒绝三位小数。服务层的 validate_content 是兜底，
    # 不是唯一防线——两者都要成立，否则绕过 Pydantic 的内部调用会把脏分值写进库。
    try:
        slot("A1", "0.001")
    except ValidationError:
        pass
    else:
        raise AssertionError("BlueprintSlot 接受了三位小数")


# ---------------------------------------------------------------------------
# 内容哈希与校验
# ---------------------------------------------------------------------------

def test_content_hash_ignores_key_order_and_decimal_scale() -> None:
    course, offering, context = uuid4(), uuid4(), uuid4()
    first = content(
        course_id=course, offering_id=offering, release_context_id=context,
        hard_constraints={"b": 2, "a": 1}, total_score=Decimal("100.00"),
    )
    second = content(
        course_id=course, offering_id=offering, release_context_id=context,
        hard_constraints={"a": 1, "b": 2}, total_score=Decimal("100"),
    )
    assert canonical_hash(first) == canonical_hash(second), "键顺序或 Decimal 标度改变了内容哈希"
    assert canonical_hash(first) != canonical_hash(
        content(course_id=course, offering_id=offering, release_context_id=context,
                duration_seconds=8100)
    ), "时长变化没有改变内容哈希"


def test_sections_and_slot_codes_must_be_unique() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    duplicated_section = content(
        sections=(
            BlueprintSection(code="A", title="一", slots=(slot("A1", "40.00"), slot("A2", "60.00"))),
            BlueprintSection(code="A", title="二", slots=(slot("B1", "10.00"),)),
        ),
        total_score=Decimal("110.00"),
    )
    expect(BlueprintValidationError, lambda: service.create_version(
        actor_id=actor, content=duplicated_section, blueprint_id=None, base_version_id=None))
    duplicated_slot = content(
        sections=(
            BlueprintSection(code="A", title="一", slots=(slot("A1", "40.00"), slot("A1", "60.00"))),
        )
    )
    expect(BlueprintValidationError, lambda: service.create_version(
        actor_id=actor, content=duplicated_slot, blueprint_id=None, base_version_id=None))


def test_formal_blueprint_requires_offering_and_dual_review() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    expect(BlueprintValidationError, lambda: service.create_version(
        actor_id=actor, content=content(offering_id=None),
        blueprint_id=None, base_version_id=None))
    expect(BlueprintValidationError, lambda: service.create_version(
        actor_id=actor, content=content(review_policy="single_review"),
        blueprint_id=None, base_version_id=None))


def test_constraint_assisted_assembly_is_accepted_but_closed_set() -> None:
    """阶段 B 落地后 constraint_assisted 必须能建；第三个取值仍然要拒。

    这条原本断言"阶段 A 拒绝 constraint_assisted"（当时建了也没有 assembly_run
    可用）。阶段 B 的 0034 与 AssemblyService 已经能落卷，所以闸门从"只许 manual"
    变成"只许这两个取值"——不放宽成任意字符串。
    """
    actor = uuid4()
    service, _ = service_for(actor)
    created = service.create_version(
        actor_id=actor, content=content(assembly_mode="constraint_assisted"),
        blueprint_id=None, base_version_id=None)
    assert created.assembly_mode == "constraint_assisted", created.assembly_mode

    # 闭集由契约类型（Literal）先挡住：第三个取值根本构造不出来，服务层那句判断
    # 是第二道门（防有人将来把契约放宽了却没补服务层）。
    expect(ValueError, lambda: content(assembly_mode="auto"))


def test_total_score_must_equal_slot_sum() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    error = expect(BlueprintValidationError, lambda: service.create_version(
        actor_id=actor, content=content(total_score=Decimal("120.00")),
        blueprint_id=None, base_version_id=None))
    assert "total_score" in str(error)


# ---------------------------------------------------------------------------
# 版本号与乐观锁
# ---------------------------------------------------------------------------

def test_first_version_must_not_carry_base_version() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    expect(StaleBaseVersionError, lambda: service.create_version(
        actor_id=actor, content=content(), blueprint_id=uuid4(),
        base_version_id=uuid4()))


def test_appending_requires_the_current_base_version() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    blueprint_id = uuid4()
    first = service.create_version(
        actor_id=actor, content=content(), blueprint_id=blueprint_id, base_version_id=None)
    assert first.version_no == 1

    # 不带 base_version → 拒绝：否则两个人同时保存会各得到一版"最新"
    expect(StaleBaseVersionError, lambda: service.create_version(
        actor_id=actor, content=content(duration_seconds=8100),
        blueprint_id=blueprint_id, base_version_id=None))
    # 带过期 base_version → 冲突
    expect(StaleBaseVersionError, lambda: service.create_version(
        actor_id=actor, content=content(duration_seconds=8100),
        blueprint_id=blueprint_id, base_version_id=uuid4()))

    second = service.create_version(
        actor_id=actor, content=content(duration_seconds=8100),
        blueprint_id=blueprint_id, base_version_id=first.id)
    assert second.version_no == 2
    assert second.base_version_id == first.id
    assert second.content_hash != first.content_hash


def test_duplicate_content_returns_the_existing_version() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    blueprint_id = uuid4()
    value = content()
    first = service.create_version(
        actor_id=actor, content=value, blueprint_id=blueprint_id, base_version_id=None)
    second = service.create_version(
        actor_id=actor, content=value, blueprint_id=blueprint_id, base_version_id=None)
    assert second.id == first.id
    assert second.version_no == 1, "内容相同却生成了新版本"
    assert second.created is False, "内容去重被误报成新建"
    assert second.replayed is False, "内容去重不是幂等重放"


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_idempotent_replay_returns_the_same_version() -> None:
    actor = ref_uuid("account", "acc-teacher")
    service, _ = service_for(actor)
    value = content(
        course_id=ref_uuid("course", "analysis"),
        offering_id=ref_uuid("offering", "class-1"),
    )
    first = service.create_version(
        actor_id=actor, content=value, blueprint_id=uuid4(), base_version_id=None,
        idempotency_key="key-0001", course_ref="analysis", offering_ref="class-1",
        actor_ref="acc-teacher")
    assert first.replayed is False
    replay = service.create_version(
        actor_id=actor, content=value, blueprint_id=first.blueprint_id, base_version_id=None,
        idempotency_key="key-0001", course_ref="analysis", offering_ref="class-1",
        actor_ref="acc-teacher")
    assert replay.id == first.id
    assert replay.version_no == first.version_no == 1
    assert replay.replayed is True
    assert replay.course_ref == "analysis"
    assert replay.created_by_ref == "acc-teacher"


def test_same_idempotency_key_with_different_payload_conflicts() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    blueprint_id = uuid4()
    value = content(course_id=ref_uuid("course", "analysis"))
    service.create_version(
        actor_id=actor, content=value, blueprint_id=blueprint_id, base_version_id=None,
        idempotency_key="key-0002", course_ref="analysis")
    expect(IdempotencyConflictError, lambda: service.create_version(
        actor_id=actor, content=content(course_id=ref_uuid("course", "analysis"),
                                       duration_seconds=5400),
        blueprint_id=blueprint_id, base_version_id=None, idempotency_key="key-0002",
        course_ref="analysis"))


def test_idempotency_is_scoped_to_the_actor() -> None:
    """同一个键由另一个用户使用不算重放，否则会读到别人的蓝图。"""
    one, two = uuid4(), uuid4()
    repository = InMemoryBlueprintRepository()
    authorizer = StaticAuthorizer({
        one: {"paper_blueprint.write"}, two: {"paper_blueprint.write"},
    })
    service = BlueprintService(repository=repository, authorizer=authorizer)
    value = content(course_id=ref_uuid("course", "analysis"))
    first = service.create_version(
        actor_id=one, content=value, blueprint_id=uuid4(), base_version_id=None,
        idempotency_key="shared-key", course_ref="analysis")
    second = service.create_version(
        actor_id=two, content=value, blueprint_id=uuid4(), base_version_id=None,
        idempotency_key="shared-key", course_ref="analysis")
    assert first.id != second.id
    assert first.blueprint_id != second.blueprint_id


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def test_write_requires_paper_blueprint_write_permission() -> None:
    actor = uuid4()
    service, _ = service_for(actor, actions={"paper_blueprint.read"})
    expect(PermissionError, lambda: service.create_version(
        actor_id=actor, content=content(), blueprint_id=None, base_version_id=None))


def test_service_accounts_cannot_create_blueprints() -> None:
    actor = uuid4()
    authorizer = StaticAuthorizer({actor: {"paper_blueprint.write"}})
    authorizer.service_accounts.add(actor)
    service = BlueprintService(repository=InMemoryBlueprintRepository(), authorizer=authorizer)
    expect(PermissionError, lambda: service.create_version(
        actor_id=actor, content=content(), blueprint_id=None, base_version_id=None))


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def test_version_payload_serializes_scores_as_two_place_strings() -> None:
    actor = uuid4()
    service, _ = service_for(actor)
    row = service.create_version(
        actor_id=actor, content=content(), blueprint_id=uuid4(), base_version_id=None)
    dumped = row.model_dump(mode="json")
    assert dumped["total_score"] == "100.00", dumped["total_score"]
    assert dumped["content"]["sections"][0]["slots"][0]["score"] == "40.00"
    assert isinstance(dumped["total_score"], str), "分值不能序列化成 JSON 数字"


# ---------------------------------------------------------------------------
# db.transaction 的 RLS 作用域
# ---------------------------------------------------------------------------

class _RecordingCursor:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log

    def execute(self, sql: str, params: object = ()) -> None:
        self.log.append((sql, params))


class _RecordingConnection:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log

    def cursor(self):  # type: ignore[no-untyped-def]
        log = self.log

        @contextmanager
        def _cursor():
            yield _RecordingCursor(log)

        return _cursor()


class _RecordingPool:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log

    def connection(self):  # type: ignore[no-untyped-def]
        log = self.log

        @contextmanager
        def _connection():
            yield _RecordingConnection(log)

        return _connection()


@contextmanager
def recording_pool():  # type: ignore[no-untyped-def]
    log: list[tuple[str, object]] = []
    original = db.pool
    db.pool = _RecordingPool(log)  # type: ignore[assignment]
    try:
        yield log
    finally:
        db.pool = original  # type: ignore[assignment]


def test_transaction_sets_local_assessment_service_before_any_statement() -> None:
    with recording_pool() as log:
        with db.transaction(assessment_service=True) as cur:
            cur.execute("INSERT INTO assessment.paper_blueprint_version DEFAULT VALUES")
    assert log[0][0] == "SET LOCAL app.assessment_service = 'on'", log[0][0]
    assert log[1][0].startswith("INSERT INTO assessment.paper_blueprint_version")


def test_transaction_sets_course_scope_through_parameterized_set_config() -> None:
    with recording_pool() as log:
        with db.transaction(course_ids=["analysis", "analysis-ii"]):
            pass
    sql, params = log[0]
    assert "set_config('app.course_ids'" in sql
    # 课程范围必须走参数，不能拼进 SQL 字符串
    assert params == ("analysis,analysis-ii",), params
    assert "'analysis'" not in sql


def test_transaction_without_flags_keeps_old_behaviour() -> None:
    with recording_pool() as log:
        with db.transaction() as cur:
            cur.execute("SELECT 1")
    assert log == [("SELECT 1", ())], log


# ---------------------------------------------------------------------------
# 持久化实现与迁移的静态断言（本机无 PostgreSQL）
# ---------------------------------------------------------------------------

def test_postgres_repository_runs_every_write_inside_assessment_transaction() -> None:
    from inspect import getsource

    from app.blueprints import PostgresBlueprintRepository, _PostgresUnitOfWork

    unit_source = getsource(_PostgresUnitOfWork)
    assert "pg_advisory_xact_lock" in unit_source, (
        "版本号分配没有 advisory lock，两个并发请求会各自拿到同一个 version_no"
    )
    assert "ON CONFLICT (content_hash, blueprint_id) DO NOTHING" in unit_source, (
        "重复内容没有按唯一键去重，重试会撞 UNIQUE(content_hash, blueprint_id)"
    )
    assert "assessment.idempotency_record" in unit_source, (
        "幂等记录没有和版本插入写在同一个事务里"
    )
    repository_source = getsource(PostgresBlueprintRepository)
    assert "db.transaction(assessment_service=True)" in repository_source, (
        "PostgreSQL 仓储没有在事务内设置 SET LOCAL app.assessment_service='on'，"
        "FORCE RLS 的 paper_* 表会拒绝读写"
    )


def test_idempotency_migration_exists_and_is_additive() -> None:
    files = sorted(p.name for p in MIGRATION_DIR.glob("*.sql"))
    assert "0027_assessment_baseline.sql" in files
    index = files.index("0027_assessment_baseline.sql")
    assert index + 1 < len(files), "0027 之后没有新增迁移"
    name = files[index + 1]
    assert name.startswith("0028_"), f"任务 2 的迁移应编号 0028，实际 {name}"
    text = (MIGRATION_DIR / name).read_text(encoding="utf-8")
    flat = " ".join(text.lower().split())
    assert "create table if not exists assessment.idempotency_record" in flat
    assert "unique (scope, idempotency_key, actor_id)" in flat, (
        "幂等记录缺少 (scope, key, actor) 唯一键：同一键会被重复消费"
    )
    assert "create table if not exists assessment.id_ref" in flat, (
        "缺少 uuid ↔ 本仓库文本 id 的桥接表：assessment 表全部是 uuid，而 app_account/course 是 text"
    )
    assert "unique (kind, ref)" in flat
    for forbidden in ("drop table", "drop column", "delete from", "truncate"):
        assert forbidden not in flat, f"迁移里出现禁止操作：{forbidden}"


def test_generated_schema_contains_the_new_assessment_tables() -> None:
    text = SCHEMA_SQL.read_text(encoding="utf-8").lower()
    for table in ("assessment.idempotency_record", "assessment.id_ref"):
        assert f"create table if not exists {table}" in text, (
            f"db/schema.sql 没有重新生成：缺少 {table}（改完迁移要跑 python db/migrate.py --concat）"
        )


def test_blueprint_schema_matches_the_shared_contract() -> None:
    assert SCHEMA_PATH.is_file(), f"缺少 {SCHEMA_PATH.relative_to(ROOT)}"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False

    properties = schema["properties"]
    assert set(properties) == set(BlueprintContent.model_fields), (
        "JSON Schema 与 BlueprintContent 字段不一致："
        f"多了 {sorted(set(properties) - set(BlueprintContent.model_fields))}，"
        f"少了 {sorted(set(BlueprintContent.model_fields) - set(properties))}"
    )
    required = set(schema["required"])
    expected_required = {
        name for name, field in BlueprintContent.model_fields.items()
        if field.is_required()
    }
    assert required == expected_required, f"required 不一致：{sorted(required)}"

    assert properties["total_score"]["pattern"] == r"^\d+\.\d{2}$"
    assert properties["purpose"]["enum"] == ["formal", "coursework", "practice"]
    assert properties["review_policy"]["enum"] == ["formal_dual_review", "single_review"]
    assert properties["assembly_mode"]["enum"] == ["manual", "constraint_assisted"]
    assert properties["schema_version"]["const"] == "paper-blueprint/v1"
    assert properties["course_id"]["format"] == "uuid"
    assert properties["duration_seconds"]["minimum"] == 1

    slot_schema = schema["$defs"]["slot"]
    assert slot_schema["additionalProperties"] is False
    assert slot_schema["properties"]["score"]["pattern"] == r"^\d+\.\d{2}$"
    assert schema["$defs"]["section"]["additionalProperties"] is False


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
        except Exception as exc:  # noqa: BLE001 —— 脚本模式下把异常也记成失败
            failures.append(name)
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 蓝图服务测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
