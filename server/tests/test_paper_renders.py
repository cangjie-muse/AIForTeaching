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


def client_with(service) -> TestClient:
    """把渲染服务换成桩：路由的授权依赖由 FastAPI 注入，替换依赖比打补丁稳。"""
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_render_service] = lambda: service
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


# ---------------------------------------------------------------------------
# 三类产物的读取与下载
# ---------------------------------------------------------------------------

STUDENT_RENDER = UUID("77777777-7777-5777-8777-777777777777")
ANSWER_RENDER = UUID("88888888-8888-5888-8888-888888888888")
RUBRIC_RENDER = UUID("99999999-9999-5999-8999-999999999999")


def _record(render_id, kind: str):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        id=render_id,
        render_kind=kind,
        output_hash=(kind[0] * 64),
        paper_manifest_hash="m" * 64,
        created_at=datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc),
    )


def _subject(status: str = "approved"):
    from types import SimpleNamespace

    return SimpleNamespace(
        paper_version_id=PAPER,
        status=status,
        manifest_hash="m" * 64,
        course_title="数学分析 I",
        version_short_code="A-01",
    )


class StubRenderService:
    """只实现路由用到的两个方法；参数校验留在真实服务里由 test_rendering 覆盖。"""

    def __init__(self, *, renders=None, status="approved",
                 list_error: Exception | None = None,
                 download_error: Exception | None = None,
                 denied: bool = False) -> None:
        self.renders = renders if renders is not None else [
            _record(STUDENT_RENDER, "student"), _record(ANSWER_RENDER, "answer")]
        self.subject = _subject(status)
        self.list_error = list_error
        self.download_error = download_error
        self.denied = denied

    def list_current_renders(self, actor_id, paper_version_id):
        if self.denied:
            raise PermissionError("没有任何卷种的导出权限")
        if self.list_error:
            raise self.list_error
        return self.subject, tuple(self.renders)

    def download(self, actor_id, render_id):
        if self.denied:
            raise PermissionError("没有该卷种的导出权限")
        if self.download_error:
            raise self.download_error
        record = next((row for row in self.renders if row.id == render_id), None)
        if record is None:
            from app.rendering import PaperRenderNotFoundError

            raise PaperRenderNotFoundError(f"渲染不存在：{render_id}")
        return b"<html>paper</html>", record, self.subject


def test_list_reports_current_manifest_renders_and_missing_kinds() -> None:
    service = StubRenderService()
    response = client_with(service).get(f"/api/v1/paper-versions/{PAPER}/renders",
                                        headers=TEACHER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["render_kind"] for item in body["renders"]] == ["student", "answer"]
    assert body["missing_kinds"] == ["rubric"]
    # 顺序永远按 student/answer/rubric：列表顺序随数据库返回顺序变，教师会以为少了卷种
    assert body["renders"][0]["download_path"].endswith(f"{STUDENT_RENDER}/download")
    # 没有 PDF 依赖就不要在接口里假装有：前端要能把这句话显示给教师
    assert body["pdf_implemented"] is False
    assert body["media_type"].startswith("text/html")
    assert body["manifest_hash"] == "m" * 64


def test_list_denied_is_403() -> None:
    response = client_with(StubRenderService(denied=True)).get(
        f"/api/v1/paper-versions/{PAPER}/renders", headers=TEACHER)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "render_scope_denied"


def test_list_of_withdrawn_paper_is_410() -> None:
    from app.rendering import PaperRenderWithdrawnError

    service = StubRenderService(list_error=PaperRenderWithdrawnError("卷子已撤回"))
    response = client_with(service).get(f"/api/v1/paper-versions/{PAPER}/renders",
                                        headers=TEACHER)
    assert response.status_code == 410, response.text
    assert response.json()["detail"]["code"] == "paper_withdrawn"


def test_download_forces_attachment_and_no_sniff() -> None:
    response = client_with(StubRenderService()).get(
        f"/api/v1/paper-renders/{STUDENT_RENDER}/download", headers=TEACHER)
    assert response.status_code == 200, response.text
    assert response.content == b"<html>paper</html>"
    assert response.headers["content-type"].startswith("text/html")
    # 同源 HTML 若被内联渲染就成了可执行文档：三件事必须同时在
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "no-store"


def test_download_filename_keeps_chinese_and_ascii_fallback() -> None:
    headers = client_with(StubRenderService()).get(
        f"/api/v1/paper-renders/{ANSWER_RENDER}/download", headers=TEACHER).headers
    disposition = headers["content-disposition"]
    # ASCII 兜底：中文课程名不能原样塞进 filename=（有的客户端会截断成乱码）
    ascii_part = disposition.split("filename=")[1].split(";")[0]
    assert ascii_part.isascii() and ascii_part.endswith(".html"), ascii_part
    # RFC 5987 原名用百分号编码，教师下载后仍能认出是哪门课的哪一份
    assert "filename*=UTF-8''" in disposition
    assert "%E6%95%B0%E5%AD%A6%E5%88%86%E6%9E%90" in disposition  # 数学分析
    assert "answer" in disposition


def test_download_unknown_render_is_404() -> None:
    response = client_with(StubRenderService()).get(
        f"/api/v1/paper-renders/{RUBRIC_RENDER}/download", headers=TEACHER)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "render_not_found"


def test_download_denied_is_403() -> None:
    response = client_with(StubRenderService(denied=True)).get(
        f"/api/v1/paper-renders/{ANSWER_RENDER}/download", headers=TEACHER)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "render_scope_denied"


def test_download_of_withdrawn_paper_is_410() -> None:
    from app.rendering import PaperRenderWithdrawnError

    service = StubRenderService(download_error=PaperRenderWithdrawnError("卷子已撤回"))
    response = client_with(service).get(
        f"/api/v1/paper-renders/{STUDENT_RENDER}/download", headers=TEACHER)
    assert response.status_code == 410, response.text


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
