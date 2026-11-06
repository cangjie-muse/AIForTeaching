"""阶段 B 任务 1：候选 manifest 的固定与哈希。

这里钉住的是"哪些量必须来自真实数据、哪些量不许由调用方自报"：

  * 清单哈希对输入顺序不敏感（同一批题换个顺序不能变成"另一份清单"）；
  * 曝光冲突的题被排除并留下原因码，而不是让整次求解带着一个已经泄了的题；
  * `recent_use_count / exposure_count / answer_released` 从真实曝光事件统计，
    调用方给什么值都不影响结果；
  * 启用时长约束时缺 `estimated_seconds` 直接拒绝——不能让求解器把"未知"当 0。

仓库的 Python 测试不连库，所以这里用桩替换阶段 A 的取数端口；SQL 与真引擎由
`scripts/verify-migrations.cjs` 与集成测试覆盖。

运行：`python server/tests/test_assembly_candidate.py`
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.assembly.candidate import (  # noqa: E402
    AssemblyCandidateService,
    CandidateSnapshotError,
    CandidateSourceResult,
)
from app.assembly.contracts import (  # noqa: E402
    BlueprintSnapshot,
    HardConstraints,
    SlotSpec,
    SoftObjectives,
    slot_identity,
)
from app.blueprints import ref_uuid  # noqa: E402
from app.contracts.candidate import CandidateRecord, RejectedCandidate  # noqa: E402
from app.contracts.hashing import canonical_hash, normalize  # noqa: E402
from app.contracts.paper import CandidateEvidence  # noqa: E402

COURSE_ID = ref_uuid("course", "analysis")
OFFERING_ID = ref_uuid("offering", "analysis-2026a")
CONTEXT = UUID("3f8f0d1a-0000-5000-8000-000000000001")
BP_VERSION = UUID("4a0e0000-0000-5000-8000-000000000002")
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
LOCK_FILE = ROOT / "server" / "requirements-assembly.txt"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def blueprint(*, max_duration: int | None = None, target_duration: int | None = None,
              mode: str = "single", purpose: str = "formal") -> BlueprintSnapshot:
    slot = SlotSpec(
        slot_id=slot_identity(BP_VERSION, "A", "A1"),
        code="A1", section_code="A", score=_decimal("100.00"), estimated_seconds=600,
    )
    return BlueprintSnapshot(
        blueprint_version_id=BP_VERSION,
        course_id=COURSE_ID,
        offering_id=OFFERING_ID,
        release_context_id=CONTEXT,
        purpose=purpose,
        total_score=_decimal("100.00"),
        duration_seconds=5400,
        slots=(slot,),
        hard_constraints=HardConstraints(max_duration_seconds=max_duration),
        soft_objectives=SoftObjectives(target_duration_seconds=target_duration),
        assembly_mode=mode,
    )


def _decimal(text: str):
    from decimal import Decimal

    return Decimal(text)


def evidence(number: int, *, family: int | None = None, seconds: int | None = 600,
             content: str | None = None) -> CandidateEvidence:
    digest = canonical_hash({"ref": f"ex-{number}"}) if content is None else content
    return CandidateEvidence(
        release_context_id=CONTEXT,
        source_release_id=UUID(int=500 + number),
        exercise_id=UUID(int=1000 + number),
        exercise_version_id=UUID(int=2000 + number),
        exercise_instance_id=None,
        exercise_family_id=UUID(int=(family if family is not None else 9000 + number)),
        exercise_kind="proof",
        concept_ids=(UUID(int=77),),
        learning_outcome_ids=(),
        difficulty_label=f"difficulty-{1 + number % 5}",
        estimated_seconds=seconds,
        decision_seqs={"exercise": 3},
        latest_decisions={"exercise": "approved"},
        dependency_manifest_hash=canonical_hash({"dep": number}),
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


def record(number: int, **kwargs: object) -> CandidateRecord:
    return CandidateRecord(
        evidence=evidence(number, **kwargs),  # type: ignore[arg-type]
        release_state="included",
        review_status="teacher-reviewed",
        license="licensed",
        answer_present=True,
    )


def event(*, family: int, kind: str, visible: datetime = NOW - timedelta(days=1),
          offering: UUID | None = None, number: int = 1):
    from types import SimpleNamespace

    return SimpleNamespace(
        exercise_version_id=UUID(int=3000 + number),
        exercise_instance_id=None,
        exercise_family_id=UUID(int=family),
        scope_offering_id=offering,
        event_kind=kind,
        visible_from=visible,
        occurred_at=visible,
    )


class StubSource:
    def __init__(self, accepted, rejected=(), events=(), as_of=NOW):
        self.accepted = tuple(accepted)
        self.rejected = tuple(rejected)
        self.events = tuple(events)
        self.as_of = as_of
        self.calls = 0

    def load(self, *, actor_id, blueprint):
        self.calls += 1
        return CandidateSourceResult(
            release_context_id=blueprint.release_context_id,
            accepted=self.accepted,
            rejected=self.rejected,
            exposure_events=self.events,
            as_of=self.as_of,
            candidate_snapshot_id=UUID(int=4242),
        )


def service(source) -> AssemblyCandidateService:
    return AssemblyCandidateService(source=source)


# ---------------------------------------------------------------------------
# 哈希与确定性
# ---------------------------------------------------------------------------

def test_lock_file_pins_exact_solver_version() -> None:
    """锁定文件只允许一行精确钉版本（求解结论要能回放，跨版本只许产生新运行）。

    这里只断言**文件内容**；ortools 是否装得上由 `test_assembly_solver.py` 验证——
    本文件刻意不 import ortools，这样"候选 manifest 与求解器无关"这件事本身
    也被测到了：没有求解器也照样能固定候选清单。
    """
    text = LOCK_FILE.read_text(encoding="utf-8")
    pins = [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert pins == ["ortools==9.11.4210"], pins


def test_candidate_manifest_does_not_import_the_solver() -> None:
    """候选固定不该依赖 CP-SAT：装不上求解器的机器也必须能固定候选清单。"""
    import ast

    source = (ROOT / "server" / "app" / "assembly" / "candidate.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
    }
    assert not [name for name in imported if "ortools" in name], imported


def test_manifest_hash_is_stable_and_ignores_input_order() -> None:
    first = record(1)
    second = record(2)
    one = service(StubSource([second, first])).snapshot(actor_id=UUID(int=1), blueprint=blueprint())
    two = service(StubSource([first, second])).snapshot(actor_id=UUID(int=1), blueprint=blueprint())
    assert one.manifest_hash == two.manifest_hash
    assert [str(item.exercise_version_id) for item in one.candidates] == sorted(
        str(item.exercise_version_id) for item in one.candidates
    ), "候选必须按 id 排序，否则顺序变成哈希的一部分"


def test_manifest_hash_covers_candidates_and_exclusions() -> None:
    one = service(StubSource([record(1)])).snapshot(actor_id=UUID(int=1), blueprint=blueprint())
    two = service(StubSource([record(1), record(2)])).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    assert one.manifest_hash != two.manifest_hash


def test_manifest_hash_is_recomputable_from_the_payload() -> None:
    manifest = service(StubSource([record(1)])).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    payload = {
        "blueprint_version_id": str(BP_VERSION),
        "release_context_id": str(CONTEXT),
        "candidates": [normalize(item) for item in manifest.candidates],
        "exclusions": [normalize(item) for item in manifest.exclusions],
    }
    assert canonical_hash(payload) == manifest.manifest_hash, "哈希必须能由公开载荷独立复算"


def test_manifest_is_json_safe() -> None:
    manifest = service(StubSource([record(1)])).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    import json

    # jsonb 写入前必须能序列化：UUID/Decimal 漏一个就会在运行到一半时炸
    text = json.dumps(asdict(manifest), ensure_ascii=False, default=str)
    assert str(BP_VERSION) in text


# ---------------------------------------------------------------------------
# 曝光
# ---------------------------------------------------------------------------

def test_exposure_conflict_is_excluded_with_reason() -> None:
    conflicted = record(1, family=500)
    kept = record(2, family=501)
    events = (event(family=500, kind="practice_answer_released"),)
    manifest = service(StubSource([conflicted, kept], events=events)).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    assert [item.exercise_version_id for item in manifest.candidates] == [
        kept.evidence.exercise_version_id]
    assert [(item.exercise_version_id, item.reason) for item in manifest.exclusions] == [
        (conflicted.evidence.exercise_version_id, "exposure_conflict")]


def test_future_event_does_not_exclude_yet() -> None:
    """`visible_from` 在未来 = 还没发生；预检要能回答"到那时会怎样"，不是"现在怎样"。"""
    future = record(1, family=500)
    events = (event(family=500, kind="formal_answer_released",
                    visible=NOW + timedelta(days=3)),)
    manifest = service(StubSource([future], events=events)).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    assert len(manifest.candidates) == 1
    assert manifest.exclusions == ()


def test_stage_a_exposure_rejections_are_recorded() -> None:
    """阶段 A 已经排除的题也要进清单：教师需要知道"它为什么不在候选里"。"""
    rejected = RejectedCandidate(
        exercise_version_id=UUID(int=7777),
        exercise_id=UUID(int=7778),
        exercise_family_id=UUID(int=7779),
        review_status="teacher-reviewed",
        reasons=("exposure_conflict",),
    )
    other = RejectedCandidate(
        exercise_version_id=UUID(int=8887),
        exercise_id=UUID(int=8888),
        exercise_family_id=UUID(int=8889),
        review_status="draft",
        reasons=("review_not_approved",),
    )
    manifest = service(StubSource([record(1)], rejected=(rejected, other))).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    # 只有曝光原因进 manifest 的 exclusions：审核未通过是阶段 A 的候选资格结论，
    # 不是本快照"排除"的结果，混进来会让两份清单看起来矛盾。
    assert [item.reason for item in manifest.exclusions] == ["exposure_conflict"]
    assert manifest.exclusions[0].exercise_version_id == rejected.exercise_version_id


# ---------------------------------------------------------------------------
# 真实统计量
# ---------------------------------------------------------------------------

def test_counts_come_from_events_not_from_the_caller() -> None:
    target = record(1, family=500)
    events = (
        event(family=500, kind="secure_assigned", number=1),
        event(family=500, kind="student_opened", number=2),
        event(family=500, kind="practice_answer_released", number=3),
        event(family=999, kind="secure_assigned", number=4),  # 别的族，不该算进来
    )
    # 用练习卷（coursework）来测统计量：正式卷遇到"已公开答案"会直接排除，
    # 那样就看不到计数了——两件事必须分开测。
    manifest = service(StubSource([target], events=events)).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint(purpose="coursework"))
    candidate = manifest.candidates[0]
    assert candidate.recent_use_count == 2, "保密使用次数必须按族统计真实事件"
    assert candidate.exposure_count == 1
    assert candidate.answer_released is True


def test_formal_paper_excludes_family_with_released_answer() -> None:
    """同一批事件换成正式卷就变成排除——统计与判定用的是同一份事实。"""
    target = record(1, family=500)
    events = (event(family=500, kind="practice_answer_released"),)
    manifest = service(StubSource([target], events=events)).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    assert manifest.candidates == ()
    assert manifest.exclusions[0].reason == "exposure_conflict"


def test_clean_candidate_has_zero_counts() -> None:
    manifest = service(StubSource([record(1)])).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    candidate = manifest.candidates[0]
    assert candidate.recent_use_count == 0
    assert candidate.exposure_count == 0
    assert candidate.answer_released is False
    assert candidate.preferred is False, "本仓库没有'教师偏好'真源，不能凭空置真"


def test_candidate_identity_fields_come_from_stage_a_evidence() -> None:
    source_record = record(3)
    manifest = service(StubSource([source_record])).snapshot(
        actor_id=UUID(int=1), blueprint=blueprint())
    candidate = manifest.candidates[0]
    assert candidate.exercise_version_id == source_record.evidence.exercise_version_id
    assert candidate.stable_exercise_id == source_record.evidence.exercise_id
    assert candidate.source_release_id == source_record.evidence.source_release_id
    assert candidate.exercise_family_id == source_record.evidence.exercise_family_id
    assert candidate.decision_seq == 3
    assert candidate.difficulty == 4, "difficulty-N 标签要翻成整数：3 题的标签是 difficulty-4"
    # 没有权重真源时按 primary/1 记录，覆盖约束等价于"至少出现一次"
    assert [(item.role, item.weight) for item in candidate.concept_weights] == [("primary", 1)]


# ---------------------------------------------------------------------------
# 失败即拒绝
# ---------------------------------------------------------------------------

def test_hard_duration_requires_estimated_seconds() -> None:
    try:
        service(StubSource([record(1, seconds=None)])).snapshot(
            actor_id=UUID(int=1), blueprint=blueprint(max_duration=3600))
    except CandidateSnapshotError as error:
        assert "estimated_seconds" in str(error), str(error)
    else:
        raise AssertionError("启用时长约束时缺预计时长必须拒绝，不能当 0 参与求解")


def test_soft_duration_target_also_requires_seconds() -> None:
    try:
        service(StubSource([record(1, seconds=None)])).snapshot(
            actor_id=UUID(int=1), blueprint=blueprint(target_duration=3600))
    except CandidateSnapshotError as error:
        assert "estimated_seconds" in str(error), str(error)
    else:
        raise AssertionError("有时长软目标就必须有时长，否则目标函数无从计算")


def test_missing_content_hash_is_rejected() -> None:
    broken = record(1, content="")
    try:
        service(StubSource([broken])).snapshot(
            actor_id=UUID(int=1), blueprint=blueprint())
    except CandidateSnapshotError as error:
        assert "内容哈希" in str(error) or "hash" in str(error).lower(), str(error)
    else:
        raise AssertionError("没有内容哈希的候选不能进 manifest：回放时无法证明题目没变")


def test_release_context_mismatch_is_rejected() -> None:
    class WrongContextSource(StubSource):
        def load(self, *, actor_id, blueprint):
            result = super().load(actor_id=actor_id, blueprint=blueprint)
            return CandidateSourceResult(
                release_context_id=UUID(int=999),
                accepted=result.accepted,
                rejected=result.rejected,
                exposure_events=result.exposure_events,
                as_of=result.as_of,
            )

    try:
        service(WrongContextSource([record(1)])).snapshot(
            actor_id=UUID(int=1), blueprint=blueprint())
    except CandidateSnapshotError as error:
        assert "Release Context" in str(error), str(error)
    else:
        raise AssertionError("来源与蓝图的 Release Context 必须一致，否则是跨 Release 取题")


def test_source_is_asked_exactly_once() -> None:
    source = StubSource([record(1)])
    assembly_service = service(source)
    assembly_service.snapshot(actor_id=UUID(int=1), blueprint=blueprint())
    assembly_service.snapshot(actor_id=UUID(int=1), blueprint=blueprint())
    # 两次调用两次取数：候选资格会随时间变化，缓存它等于把旧结论当新结论
    assert source.calls == 2


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
    print(f"✓ 候选 manifest 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
