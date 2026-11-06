"""组卷 API：状态码与"无解也是成功运行"的语义。

被替换的是服务（`get_assembly_service` 依赖）：这些用例要钉的是**HTTP 契约**，
不是求解。组卷本身的语义由 test_assembly_pipeline.py 用真实服务覆盖。

要钉住的几件事：

  * 启动接口把 INFEASIBLE/FAILED 当 201 返回，而不是 4xx——教师需要读到原因；
  * 没有方案的运行选方案是 409，不是 500，也不能是 201；
  * 复验不通过时返回 409 + `issues` + `paper_version_id`，绝不返回草稿；
  * 写接口缺 Idempotency-Key 是 400（不是"默认帮你生成一个"）；
  * 组卷路由不向普通学生开放（角色闸门在 auth.require，这里验证它真的生效）。

运行：`python server/tests/test_assembly_api.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import assembly_api as api  # noqa: E402
from app.assembly.materialize import AssemblyValidationBlocked  # noqa: E402
from app.assembly.repository import AssemblyRunRow  # noqa: E402
from app.assembly.service import (  # noqa: E402
    IdempotencyConflict,
    RunNotFound,
    RunNotSolved,
    SolutionIndexInvalid,
    SolutionMismatch,
)
from app.assembly.contracts import (  # noqa: E402
    BlueprintSnapshot,
    PaperDraft,
    RunStatus,
    SlotSpec,
    slot_identity,
    AssemblySelection,
    make_solution_manifest,
    solution_to_json,
)
from decimal import Decimal  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
STUDENT = {"X-Actor": "acc-student", "X-Roles": "student"}
VERSION = UUID("4a0e0000-0000-5000-8000-000000000002")
RUN = UUID("7a1b0000-0000-4000-8000-000000000001")
PEER_RUN = UUID("7a1b0000-0000-4000-8000-000000000011")
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)


def run_row(status: RunStatus = RunStatus.OPTIMAL, *, solutions=None,
            diagnostics: dict | None = None, optimal: bool = True) -> AssemblyRunRow:
    payload = solutions if solutions is not None else ([
        {"variant_label": "single", "manifest_hash": "a" * 64,
         "optimal_proven": optimal, "solver_status": str(status), "objective_terms": []}
    ] if status in {RunStatus.OPTIMAL, RunStatus.FEASIBLE} else [])
    return AssemblyRunRow(
        id=RUN, blueprint_version_id=VERSION,
        actor_id=UUID("0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a"),
        idempotency_key="run-key",
        candidate_manifest={"candidates": []},
        candidate_manifest_hash="b" * 64,
        candidate_snapshot_id=UUID("c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0"),
        solver_name="cp-sat", solver_version="9.11.4210",
        solver_config={"random_seed": 7},
        status=status,
        solution_manifest=tuple(payload),
        diagnostics=diagnostics or {},
        best_bound=0, optimality_gap_milli=0,
        created_at=NOW, started_at=NOW, completed_at=NOW,
    )


class StubService:
    """只实现路由用到的方法；错误由构造参数注入。"""

    def __init__(self, *, row=None, start_error=None, select_error=None,
                 draft=None) -> None:
        self.row = row or run_row()
        self.start_error = start_error
        self.select_error = select_error
        self.draft = draft or PaperDraft(
            paper_version_id=UUID("d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0"),
            manifest_hash="c" * 64, status="draft")
        self.starts: list[dict] = []
        self.blueprint = None
        self.selects: list[dict] = []
        self.solver_config = None

    def start(self, *, actor_id, blueprint_version_id, idempotency_key):
        if self.start_error:
            raise self.start_error
        self.starts.append({"actor": actor_id, "version": blueprint_version_id,
                            "key": idempotency_key, "config": self.solver_config})
        from app.assembly.contracts import AssemblyStartResult

        return AssemblyStartResult(assembly_run_id=self.row.id,
                                   status=self.row.status, reused=False)

    def run(self, *, actor_id, run_id):
        if run_id != self.row.id:
            raise RunNotFound("运行不存在")
        return self.row

    def latest(self, *, actor_id, blueprint_version_id, limit=20):
        return [self.row]

    def _load_blueprint(self, actor_id, blueprint_version_id):
        if self.blueprint is None:
            raise AssertionError("测试要先给 StubService.blueprint 赋值")
        return self.blueprint

    def select(self, *, actor_id, run_id, index, idempotency_key):
        if self.select_error:
            raise self.select_error
        self.selects.append({"index": index, "key": idempotency_key})
        return self.draft


def client(service) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_assembly_service] = lambda: service
    return TestClient(app)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def test_start_returns_201_with_run_payload() -> None:
    service = StubService()
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-1"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["assembly_run_id"] == str(RUN)
    assert body["status"] == "optimal"
    assert body["solved"] is True
    assert body["optimal_proven"] is True
    assert body["reused"] is False
    # 账本字段必须给出去：回放靠它判断"这次运行读的是哪份候选"
    assert body["candidate_manifest_hash"] == "b" * 64
    assert body["candidate_snapshot_id"] is not None


def test_infeasible_run_is_201_not_an_error() -> None:
    """无解是一次成功的运行：教师需要读原因，而不是看到一个 4xx。"""
    service = StubService(row=run_row(RunStatus.INFEASIBLE,
                                     diagnostics={"reason": "precheck_proved",
                                                  "issues": [{"code": "slot_has_no_candidate",
                                                              "message": "A2 没有候选",
                                                              "slot_code": "A2"}]}))
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-2"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["solved"] is False
    assert body["solutions"] == []
    assert body["diagnostics"]["issues"][0]["slot_code"] == "A2"


def test_failed_run_reports_solver_unavailable_not_a_hidden_success() -> None:
    service = StubService(row=run_row(RunStatus.FAILED, diagnostics={
        "reason": "solver_error", "detail": "SolverUnavailableError: 缺少 ortools"}))
    body = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-3"}).json()
    assert body["status"] == "failed"
    assert body["solved"] is False
    assert "ortools" in body["diagnostics"]["detail"]


def test_missing_idempotency_key_is_400() -> None:
    service = StubService()
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers=TEACHER)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "idempotency_key_required"
    assert service.starts == []


def test_blank_idempotency_key_is_400() -> None:
    response = client(StubService()).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "   "})
    assert response.status_code == 400


def test_solver_config_is_passed_to_the_service() -> None:
    service = StubService()
    client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={"random_seed": 11, "time_limit_seconds": 5, "max_solutions": 2},
        headers={**TEACHER, "Idempotency-Key": "run-4"})
    config = service.solver_config
    assert config.random_seed == 11 and config.time_limit_seconds == 5
    assert config.max_solutions == 2


def test_max_solutions_above_three_is_422() -> None:
    response = client(StubService()).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={"max_solutions": 4}, headers={**TEACHER, "Idempotency-Key": "run-5"})
    assert response.status_code == 422, response.text


def test_unknown_field_in_config_is_422() -> None:
    response = client(StubService()).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={"num_workers": 4}, headers={**TEACHER, "Idempotency-Key": "run-6"})
    assert response.status_code == 422


def test_idempotency_conflict_is_409() -> None:
    service = StubService(start_error=IdempotencyConflict("同一把键换了蓝图"))
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-7"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "idempotency_conflict"


def test_permission_error_is_403() -> None:
    service = StubService(start_error=PermissionError("不在课程范围"))
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-8"})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "course_scope_denied"


def test_blueprint_translation_error_is_422() -> None:
    from app.assembly.blueprint import BlueprintTranslationError

    service = StubService(start_error=BlueprintTranslationError("A/B 尚未实现"))
    response = client(service).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**TEACHER, "Idempotency-Key": "run-9"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "blueprint_not_solvable"


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def test_get_run_returns_payload() -> None:
    response = client(StubService()).get(f"/api/v1/assembly-runs/{RUN}", headers=TEACHER)
    assert response.status_code == 200, response.text
    assert response.json()["assembly_run_id"] == str(RUN)


def test_get_unknown_run_is_404() -> None:
    response = client(StubService()).get(f"/api/v1/assembly-runs/{UUID(int=5)}",
                                         headers=TEACHER)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "assembly_run_not_found"


def test_list_runs_returns_array() -> None:
    response = client(StubService()).get(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs", headers=TEACHER)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_limit_is_validated() -> None:
    response = client(StubService()).get(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs?limit=0", headers=TEACHER)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "limit_out_of_range"


# ---------------------------------------------------------------------------
# 选方案落卷
# ---------------------------------------------------------------------------

def test_select_creates_draft_and_says_review_is_still_required() -> None:
    service = StubService()
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/0/paper",
        headers={**TEACHER, "Idempotency-Key": "pick-1"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert "复核" in body["next_step"], "响应必须说明草稿还要过审核"
    assert service.selects == [{"index": 0, "key": "pick-1"}]


def test_select_without_solutions_is_409() -> None:
    service = StubService(select_error=RunNotSolved("本次运行状态是 infeasible，没有可选方案"))
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/0/paper",
        headers={**TEACHER, "Idempotency-Key": "pick-2"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "assembly_run_not_solved"


def test_select_out_of_range_index_is_422() -> None:
    service = StubService(select_error=SolutionIndexInvalid("序号越界"))
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/9/paper",
        headers={**TEACHER, "Idempotency-Key": "pick-3"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "solution_index_invalid"


def test_select_tampered_solution_is_409() -> None:
    service = StubService(select_error=SolutionMismatch("方案被改过"))
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/0/paper",
        headers={**TEACHER, "Idempotency-Key": "pick-4"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "solution_manifest_mismatch"


def test_validation_blocked_reports_issues_and_paper() -> None:
    service = StubService(select_error=AssemblyValidationBlocked(
        "未通过独立复验",
        paper_version_id=UUID("e0e0e0e0-e0e0-4e0e-8e0e-e0e0e0e0e0e0"),
        issues=[{"code": "estimated_seconds_missing", "message": "缺时长",
                 "severity": "error", "slot_no": 2}],
    ))
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/0/paper",
        headers={**TEACHER, "Idempotency-Key": "pick-5"})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "assembly_validation_blocked"
    assert detail["issues"][0]["code"] == "estimated_seconds_missing"
    # 卷子确实建出来了（在草稿里），前端要能跳过去看它
    assert detail["paper_version_id"] == "e0e0e0e0-e0e0-4e0e-8e0e-e0e0e0e0e0e0"


def _two_solution_service():
    """两个方案的运行 + 一份只有 A1 槽位的蓝图（结构比较需要蓝图槽位表）。"""
    slot_id = slot_identity(VERSION, "A", "A1")
    blueprint = BlueprintSnapshot(
        blueprint_version_id=VERSION, course_id=UUID(int=10), offering_id=UUID(int=11),
        release_context_id=UUID(int=12), purpose="formal",
        total_score=Decimal("50.00"), duration_seconds=3600,
        slots=(SlotSpec(slot_id=slot_id, code="A1", section_code="A",
                        score=Decimal("50.00"), estimated_seconds=600),),
    )

    def manifest(number: int, label: str, *, optimal: bool) -> dict:
        selection = AssemblySelection(
            slot_id=slot_id, slot_code="A1",
            exercise_version_id=UUID(int=number),
            exercise_instance_id=None,
            stable_exercise_id=UUID(int=10_000 + number),
            exercise_family_id=UUID(int=100 + number),
            source_release_id=UUID(int=30_000 + number),
            decision_seq=1, score_hundredths=5000,
            estimated_seconds=600, difficulty=3,
            dependency_hash=f"dep-{number}", content_hash=f"content-{number}",
        )
        built = make_solution_manifest(
            variant_label=label, selections=(selection,), objective_terms=(),
            objective_value=0, best_bound=0, optimality_gap_milli=0,
            solver_status=RunStatus.OPTIMAL if optimal else RunStatus.FEASIBLE,
            optimal_proven=optimal, structure_report={},
        )
        return solution_to_json(built)

    row = run_row(solutions=[manifest(1, "single", optimal=True),
                             manifest(2, "single-2", optimal=False)])
    service = StubService(row=row)
    service.blueprint = blueprint
    return service

def test_comparison_returns_structural_verdict_and_no_equivalence_claim() -> None:
    """比较接口必须同时给出结构结论和"不代表统计等值"这句话。"""
    response = client(_two_solution_service()).get(
        f"/api/v1/assembly-runs/{RUN}/comparison?left=0&right=1", headers=TEACHER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["statistical_equivalence"] is False, (
        "本系统没有等值设计，接口不能声称统计等值")
    assert "不代表统计等值" in body["note"]
    assert isinstance(body["slots"], list)
    assert body["summary"]["slots"] >= 1


def test_comparison_requires_two_solutions() -> None:
    service = StubService(row=run_row(RunStatus.INFEASIBLE,
                                     diagnostics={"reason": "precheck_proved"}))
    response = client(service).get(
        f"/api/v1/assembly-runs/{RUN}/comparison", headers=TEACHER)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "assembly_run_not_solved"


def test_comparison_index_out_of_range_is_422() -> None:
    response = client(StubService()).get(
        f"/api/v1/assembly-runs/{RUN}/comparison?left=0&right=9", headers=TEACHER)
    assert response.status_code == 422, response.text


def test_student_cannot_compare_solutions() -> None:
    response = client(StubService()).get(
        f"/api/v1/assembly-runs/{RUN}/comparison", headers=STUDENT)
    assert response.status_code == 403, response.text


def _pair_service():
    """两次运行、同一份蓝图、互不重叠：用来测配对成功的路径。"""
    base = _two_solution_service()

    class PairService(StubService):
        def __init__(self) -> None:
            super().__init__(row=base.row)
            self.blueprint = base.blueprint
            self.peer = base.row.model_copy(update={"id": PEER_RUN})

        def run(self, *, actor_id, run_id):
            if run_id == RUN:
                return self.row
            if run_id == PEER_RUN:
                return self.peer
            raise RunNotFound("运行不存在")

    return PairService()


def test_ab_pair_reports_rejection_with_issues() -> None:
    """两个方案共用同一道题且没有锚题 → 409 + 可展示的问题清单。"""
    response = client(_pair_service()).get(
        f"/api/v1/assembly-runs/{RUN}/ab-pair?peer_run_id={PEER_RUN}", headers=TEACHER)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "ab_pair_rejected"
    assert detail["issues"], "拒绝时必须给出可展示的问题"
    assert "不代表统计等值" in detail["note"]


def test_ab_pair_accepts_declared_anchor() -> None:
    anchor = str(UUID(int=1))
    response = client(_pair_service()).get(
        f"/api/v1/assembly-runs/{RUN}/ab-pair?peer_run_id={PEER_RUN}&index=0&peer_index=0"
        f"&anchors={anchor}", headers=TEACHER)
    # 方案 0 的两卷都用 A1 槽位但题目不同（UUID(1) vs UUID(2)），所以本来就无重叠；
    # 这里断言的是"声明锚题不会让接口出错，且结论仍然是结构匹配、无等值主张"。
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["statistical_equivalence"] is False
    assert "分别提交审核" in body["next_step"]


def test_ab_pair_unknown_run_is_404() -> None:
    response = client(_pair_service()).get(
        f"/api/v1/assembly-runs/{RUN}/ab-pair?peer_run_id={UUID(int=777)}",
        headers=TEACHER)
    assert response.status_code == 404, response.text


def test_ab_pair_index_out_of_range_is_422() -> None:
    response = client(_pair_service()).get(
        f"/api/v1/assembly-runs/{RUN}/ab-pair?peer_run_id={PEER_RUN}&peer_index=9",
        headers=TEACHER)
    assert response.status_code == 422, response.text


def test_student_cannot_pair() -> None:
    response = client(_pair_service()).get(
        f"/api/v1/assembly-runs/{RUN}/ab-pair?peer_run_id={PEER_RUN}", headers=STUDENT)
    assert response.status_code == 403, response.text


def test_select_requires_idempotency_key() -> None:
    service = StubService()
    response = client(service).post(
        f"/api/v1/assembly-runs/{RUN}/solutions/0/paper", headers=TEACHER)
    assert response.status_code == 400
    assert service.selects == []


# ---------------------------------------------------------------------------
# 角色闸门与能力声明
# ---------------------------------------------------------------------------

def test_student_cannot_start_a_run() -> None:
    response = client(StubService()).post(
        f"/api/v1/paper-blueprint-versions/{VERSION}/assembly-runs",
        json={}, headers={**STUDENT, "Idempotency-Key": "run-student"})
    assert response.status_code == 403, response.text


def test_student_cannot_read_a_run() -> None:
    response = client(StubService()).get(f"/api/v1/assembly-runs/{RUN}", headers=STUDENT)
    assert response.status_code == 403, response.text


def test_solver_capability_is_honest_about_missing_ortools() -> None:
    capability = api.solver_capability()
    assert capability["requirements_file"].endswith("requirements-assembly.txt")
    if capability["ortools_version"] is None:
        assert capability["degraded"] is True
        assert capability["solver"] == "unavailable", (
            "没装 ortools 时不能声称 solver 是 cp-sat")
    else:
        assert capability["degraded"] is False


def test_routes_are_registered() -> None:
    paths = api.registered_routes()
    assert any("assembly-runs" in item for item in paths), paths
    assert any(item.startswith("POST") and "solutions" in item for item in paths), paths


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
    print(f"✓ 组卷 API 测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
