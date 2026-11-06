"""任务 9 前端数据源：组卷工作台的聚合读接口。

前端要在一个页面里回答"这门课每份卷到哪一步了"。逐个接口拼要四次往返、中间状态
还会错位；这里聚合成一次只读查询，并把状态压成 `next_action`（下一步该干什么），
因为"渲染没做"这种事不该让教师先撞 409 再猜。

运行：`python server/tests/test_workbench.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import workbench_api as api  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402
from app.workbench import (  # noqa: E402
    WorkbenchPaper,
    WorkbenchRepository,
    build_view,
    compute_next_action,
)

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
COURSE_REF = "analysis"
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)


def paper(**overrides: object) -> WorkbenchPaper:
    values: dict[str, object] = dict(
        paper_version_id=uuid4(),
        paper_id=uuid4(),
        version_no=1,
        paper_kind="formal",
        status="draft",
        total_score="100.00",
        edit_seq=2,
        manifest_hash="a" * 64,
        updated_at=NOW,
        renders_ready=0,
        assignments_total=0,
        assignments_published=0,
    )
    values.update(overrides)
    return WorkbenchPaper(**values)  # type: ignore[arg-type]


class StubRepository(WorkbenchRepository):
    def __init__(self, papers: list[WorkbenchPaper]) -> None:
        super().__init__([])
        self._papers = papers

    def papers(self, course_ref: str) -> list[WorkbenchPaper]:  # type: ignore[override]
        return list(self._papers) if course_ref == COURSE_REF else []


class StubAuthorizer:
    def __init__(self, denied: bool = False) -> None:
        self.denied = denied

    def require(self, actor_id: UUID, action: str, **scope: object) -> None:
        if self.denied:
            raise PermissionError("course scope denied")
        if not scope.get("course_ref"):
            raise PermissionError("missing course_ref")


def client_for(papers: list[WorkbenchPaper], denied: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    repo = StubRepository(papers)
    app.dependency_overrides[api.get_workbench_repository] = lambda: repo
    # 授权也替换掉：真实实现要查 course / teaching_class 两张表，接口测试不连库。
    app.dependency_overrides[api.get_workbench_authorizer] = lambda: StubAuthorizer(denied)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 一、next_action 的判定顺序
# ---------------------------------------------------------------------------

def test_draft_with_score_asks_for_validation() -> None:
    assert compute_next_action(paper(status="draft", total_score="100.00")) == "validate"


def test_draft_without_score_asks_to_fill_slots() -> None:
    assert compute_next_action(paper(status="draft", total_score="")) == "fill_slots"


def test_in_review_waits_for_the_second_reviewer() -> None:
    assert compute_next_action(paper(status="in_review")) == "await_review"


def test_approved_paper_needs_rendering_before_publishing() -> None:
    assert compute_next_action(paper(status="approved", renders_ready=2)) == "render"
    assert compute_next_action(paper(status="approved", renders_ready=3)) == "publish_assignment"
    assert compute_next_action(paper(
        status="approved", renders_ready=3, assignments_total=1,
        assignments_published=1)) == "none"


def test_withdrawn_paper_is_terminal() -> None:
    assert compute_next_action(paper(status="withdrawn")) == "withdrawn"


# ---------------------------------------------------------------------------
# 二、聚合视图
# ---------------------------------------------------------------------------

def test_view_counts_by_status_and_fills_next_action() -> None:
    papers = [
        paper(status="draft"),
        paper(status="draft"),
        paper(status="in_review"),
        paper(status="approved", renders_ready=3),
    ]
    view = build_view(COURSE_REF, papers)
    assert view.counts == {"draft": 2, "in_review": 1, "approved": 1}
    assert [item.next_action for item in view.papers] == [
        "validate", "validate", "await_review", "publish_assignment"]


def test_workbench_never_carries_answer_material() -> None:
    """工作台响应不得出现题干/答案/量规：它没有理由携带答案侧数据。"""
    client = client_for([paper(status="approved", renders_ready=3)])
    response = client.get("/api/v1/paper-workbench", params={"course_ref": COURSE_REF},
                          headers=TEACHER)
    assert response.status_code == 200, response.text
    payload = response.text
    for forbidden in ("statement", "answer_md", "solution", "rubric", "prompt"):
        assert forbidden not in payload, f"工作台响应里出现了 {forbidden}"


def test_workbench_requires_teacher_role() -> None:
    client = client_for([paper()])
    response = client.get("/api/v1/paper-workbench", params={"course_ref": COURSE_REF},
                          headers={"X-Actor": "stu-1", "X-Roles": "student"})
    assert response.status_code == 403, response.text


def test_course_scope_denied_is_403_with_code() -> None:
    client = client_for([paper()], denied=True)
    response = client.get("/api/v1/paper-workbench", params={"course_ref": COURSE_REF},
                          headers=TEACHER)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "course_scope_denied"


def test_workbench_rejects_empty_course_ref() -> None:
    client = client_for([paper()])
    response = client.get("/api/v1/paper-workbench", params={"course_ref": ""},
                          headers=TEACHER)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# 手动收集
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
        except Exception as error:  # noqa: BLE001
            failures.append(name)
            print(f"  ✗ {name}：{type(error).__name__}: {error}")
    print()
    if failures:
        print(f"✗ {len(failures)}/{len(tests)} 项未通过：" + "、".join(failures))
        return 1
    print(f"✓ 组卷工作台测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
