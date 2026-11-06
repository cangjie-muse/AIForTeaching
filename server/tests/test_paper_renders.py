"""任务 10 前置：打印卷渲染入口（POST /paper-versions/{id}/renders）。

补的是一个真实缺口：submit-review 要求三类卷都 ready，而在此之前**没有任何 HTTP
方式**把渲染任务入队——教师会永远撞 409 renders_not_ready。

要钉住的：
  1. 已 ready 的类型不再入队（避免重复文件与无谓的 worker 负载）；
  2. 只入队 paper-render 这一种 kind（worker 才认得）；
  3. 撤回的卷子不能导出；
  4. 未知渲染类型 422，而不是悄悄忽略。

本文件用桩替换仓储 / db / 授权：仓库 Python 测试不连库，SQL 由
scripts/verify-migrations.cjs 的真引擎断言与静态检查覆盖。

运行：`python server/tests/test_paper_renders.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app import blueprints, papers  # noqa: E402
from app import paper_renders as api  # noqa: E402
from app.blueprints import ref_uuid  # noqa: E402

auth.AUTH_MODE = "dev"
auth.DEV_ROLES = frozenset()

# 路由模块导入时用的是哪个授权类——必须在 install() 打桩之前抓下来，
# 否则后面断言到的只是桩自己。
ROUTE_AUTHORIZER = api.PaperScopeAuthorizer

TEACHER = {"X-Actor": "acc-teacher", "X-Roles": "teacher"}
PAPER = UUID("66666666-6666-5666-8666-666666666666")
COURSE_ID = ref_uuid("course", "analysis")


class StubPaper:
    def __init__(self, status: str = "approved") -> None:
        self.status = status
        self.course_id = COURSE_ID


def install(monkeypatch_status: str = "approved", ready: set[str] | None = None,
            denied: bool = False, enqueued: list | None = None):
    """把路由依赖的三样东西换成桩：卷面仓储、db、课程范围判定。"""
    ready = ready or set()
    enqueued = enqueued if enqueued is not None else []

    class StubUnitOfWork:
        def get(self, version_id):
            return StubPaper(monkeypatch_status) if version_id == PAPER else None

    class StubRepo:
        def unit_of_work(self):
            uow = StubUnitOfWork()
            return _Ctx(uow)

    class StubAuthorizer:
        def __init__(self, actor):
            self.actor = actor

        def require(self, actor_id, action, **scope):
            if denied:
                raise PermissionError("course scope denied")

    def query_one(sql, params=()):
        enqueued.append(params[0])
        return {"id": len(enqueued)}

    def query(sql, params=()):
        return [{"render_kind": kind} for kind in ready]

    api.PostgresPaperRepository = StubRepo
    api.PaperScopeAuthorizer = StubAuthorizer
    api.ref_uuid = ref_uuid
    api.db = SimpleNamespace(query_one=query_one, query=query)
    api._course_ref = lambda course_id: "analysis"
    return enqueued


class _Ctx:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_enqueues_only_missing_kinds() -> None:
    enqueued = install(ready={"student"})
    response = client().post(f"/api/v1/paper-versions/{PAPER}/renders", json={},
                             headers=TEACHER)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["already_ready"] == ["student"]
    assert [job["render_kind"] for job in body["jobs"]] == ["answer", "rubric"]
    assert len(enqueued) == 2, "已 ready 的类型不该再入队"


def test_all_ready_enqueues_nothing() -> None:
    enqueued = install(ready={"student", "answer", "rubric"})
    response = client().post(f"/api/v1/paper-versions/{PAPER}/renders", json={},
                             headers=TEACHER)
    assert response.status_code == 202
    assert response.json()["jobs"] == []
    assert enqueued == []


def test_unknown_render_kind_is_422() -> None:
    install()
    response = client().post(f"/api/v1/paper-versions/{PAPER}/renders",
                             json={"render_kinds": ["teacher"]}, headers=TEACHER)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "unknown_render_kind"


def test_withdrawn_paper_cannot_be_rendered() -> None:
    install(monkeypatch_status="withdrawn")
    response = client().post(f"/api/v1/paper-versions/{PAPER}/renders", json={},
                             headers=TEACHER)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "paper_withdrawn"


def test_unknown_paper_is_404() -> None:
    install()
    response = client().post(f"/api/v1/paper-versions/{UUID(int=1)}/renders", json={},
                             headers=TEACHER)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "paper_not_found"


def test_course_scope_denied_is_403() -> None:
    install(denied=True)
    response = client().post(f"/api/v1/paper-versions/{PAPER}/renders", json={},
                             headers=TEACHER)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "course_scope_denied"


def test_job_payload_matches_worker_expectations() -> None:
    """worker 读的是 payload.paper_version_id 与 payload.render_kind，键名错一个就跑不起来。"""
    enqueued = install()
    client().post(f"/api/v1/paper-versions/{PAPER}/renders",
                  json={"render_kinds": ["answer"]}, headers=TEACHER)
    import json
    payload = json.loads(enqueued[0])
    assert payload["paper_version_id"] == str(PAPER)
    assert payload["render_kind"] == "answer"


def test_paper_write_action_survives_the_real_authorizer_chain() -> None:
    """`paper.write` 只有经过 PaperScopeAuthorizer 才是合法动作。

    这一项是为一个真实的接错线而加的：把 `paper.write` 直接交给
    `CourseScopeAuthorizer`，它不在白名单里，会抛 PermissionError——
    结果**所有教师都 403**，而上面那些用桩替换授权的用例一个也发现不了。
    """
    assert ROUTE_AUTHORIZER is papers.PaperScopeAuthorizer, (
        "路由必须用 PaperScopeAuthorizer：PaperScopeAuthorizer 才能把 "
        "paper.write 映射成课程范围判定认识的动作"
    )
    actor = auth.Actor(id="acc-teacher", roles=frozenset({"teacher"}))
    seen: list[str] = []
    original = blueprints.CourseScopeAuthorizer.require

    def spy(self, actor_id, action, **scope):  # noqa: ANN001 - 只为记录动作名
        seen.append(action)

    blueprints.CourseScopeAuthorizer.require = spy
    try:
        papers.PaperScopeAuthorizer(actor).require(
            ref_uuid("account", actor.id), "paper.write", course_ref="analysis")
    finally:
        blueprints.CourseScopeAuthorizer.require = original
    assert seen == ["paper_blueprint.write"], seen

    # 反向确认：同一个动作名绕过映射就进不去，这不是多余的一步——
    # 它就是"为什么必须有 PaperScopeAuthorizer"的证据。
    try:
        blueprints.CourseScopeAuthorizer(actor).require(
            ref_uuid("account", actor.id), "paper.write", course_ref="analysis")
    except PermissionError:
        pass
    else:  # pragma: no cover - 若将来白名单放开，这条会提醒回来改注释
        raise AssertionError("CourseScopeAuthorizer 意外接受了未映射的动作 paper.write")



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
    print(f"✓ 卷渲染入口测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
