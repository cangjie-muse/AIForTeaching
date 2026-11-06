"""三类打印卷渲染（server/app/rendering.py）的服务层测试。

为什么这层必须钉死：

  1. **学生卷的输入形状**：渲染输入不是"整行题目对象减去几个键"，而是显式白名单。
     多一个键（`answer`、`answer_test`、`hidden_note`）就当场失败，而不是等模板
     少渲染一次、答案随 HTML 注释或 data 属性漏出去。
  2. **三份产物彼此独立**：学生/答案/量规各自注册一个 `agent.artifact_version`，
     内容哈希不同、`contains_solution` 不同、权限动作不同；任何"先生成再用 CSS
     隐藏"的做法都会让三份哈希相同，这里直接断言哈希两两不同。
  3. **重试不产生第二份文件**：内容寻址 + `(output_hash, media_kind, created_by)`
     唯一键是 worker 重试的唯一安全网；同输入渲染两次必须复用同一 artifact 与
     `paper_render` 行。
  4. **撤回不可渲染**：`withdrawn` 卷在任何 render_kind 下都必须是 410 语义，
     而不是"还能导出最后一次"。

本仓库 Python 测试不引入 pytest（见 server/tests/test_papers.py 的说明），顶层仍是
pytest 风格的 `test_` 函数，`run_tests()` 手动收集、逐项打印。

运行：`python server/tests/test_rendering.py`
"""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.artifacts import MemoryObjectStore, StaticAuthorizer  # noqa: E402
from app.contracts import StudentProjectionLeakError, canonical_hash  # noqa: E402
from app.rendering import (  # noqa: E402
    MEDIA_KIND,
    MIME_TYPE,
    PDF_NOTICE,
    PRINT_NOTE,
    RENDERER_VERSION,
    TEMPLATE_VERSION,
    InMemoryPaperRenderRepository,
    PaperRenderIncompleteError,
    PaperRenderNotFoundError,
    PaperRenderService,
    PaperRenderWithdrawnError,
    RenderSection,
    RenderSlot,
    RenderSubject,
    build_student_projection,
)

CONTEXT = UUID("11111111-1111-5111-8111-111111111111")
MANIFEST = "a" * 64


# ---------------------------------------------------------------------------
# 夹具：一份合成数学卷
# ---------------------------------------------------------------------------

def sample_subject(*, status: str = "draft") -> RenderSubject:
    """两份分节、三个槽位的合成卷。

    题干/答案里的 SECRET_* 不是装饰：test_render_security.py 用它们做**内容级**
    泄露断言（不是字段名断言），所以这里让答案、量规、解析各带一个唯一标记。
    `display_config` 里额外塞 `solution` / `answer_test`，验证学生投影在数据层
    就把它们丢掉，而不是靠模板不读。
    """
    proof = RenderSlot(
        slot_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
        code="A1",
        slot_no=1,
        question_no="1.1",
        statement_md="设 $f:[0,1]\\to\\mathbb{R}$ 连续。证明 $f$ 在 $[0,1]$ 上有界。",
        answer_md="SECRET_ANSWER 取 $M=\\max_{x\\in[0,1]}|f(x)|$，由连续函数在闭区间上有界可知。",
        score=Decimal("10.00"),
        source_no="习题2.1-3",
        difficulty=3,
        display_config={
            "response_kind": "proof",
            "answer_space_lines": 14,
            "rubric": {
                "steps": [
                    {"criterion": "SECRET_RUBRIC 写出有界性定义", "score": "4.00"},
                    {"criterion": "SECRET_RUBRIC 引用闭区间连续函数性质", "score": "6.00"},
                ]
            },
            "solution": "SECRET_SOLUTION 这是只允许出现在答案卷的解析。",
            "answer_test": "SECRET_ANSWER_TEST",
            "hidden_note": "SECRET_HIDDEN",
        },
    )
    calculation = RenderSlot(
        slot_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
        code="B1",
        slot_no=1,
        question_no="2.1",
        statement_md="计算 $\\int_0^1 x^2\\,\\mathrm{d}x$，写出每一步依据。",
        answer_md="SECRET_ANSWER-2 $\\int_0^1 x^2\\,\\mathrm{d}x=1/3$。",
        score=Decimal("15.00"),
        source_no=None,
        difficulty=1,
        display_config={"response_kind": "calculation", "answer_space_lines": 10},
    )
    section_a = RenderSection(
        code="A", title="一、证明题", instruction="写出完整证明。",
        sort_order=0, slots=(proof,),
    )
    section_b = RenderSection(
        code="B", title="二、计算题", instruction="写出计算过程。",
        sort_order=1, slots=(calculation,),
    )
    return RenderSubject(
        paper_version_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"),
        paper_id=UUID("cccccccc-cccc-4ccc-8ccc-ccccccccccc1"),
        course_id=UUID("dddddddd-dddd-4ddd-8ddd-ddddddddddd1"),
        course_ref="analysis",
        offering_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"),
        offering_ref="class-2026-fall",
        release_context_id=CONTEXT,
        course_title="数学分析 I",
        version_short_code=MANIFEST[:8],
        status=status,
        manifest_hash=MANIFEST,
        paper_kind="coursework",
        total_score=Decimal("25.00"),
        duration_seconds=5400,
        instructions="闭卷。请按题号在答题区作答，保持卷面整洁。",
        sections=(section_a, section_b),
    )


ALL_GRANTS = {
    "artifact.write",
    "artifact.read",
    "paper.render.student",
    "paper.render.answer",
    "paper.render.rubric",
}


def service_with(
    subject: RenderSubject | None = None,
    *,
    grants: set[str] | None = None,
    store: MemoryObjectStore | None = None,
    renderer=None,
) -> tuple[PaperRenderService, UUID, InMemoryPaperRenderRepository]:
    actor = uuid4()
    repository = InMemoryPaperRenderRepository(subject or sample_subject())
    service = PaperRenderService(
        repository=repository,
        authorizer=StaticAuthorizer({actor: set(grants or ALL_GRANTS)}),
        object_store=store or MemoryObjectStore(),
        renderer=renderer,
    )
    return service, actor, repository


# ---------------------------------------------------------------------------
# 1. 学生投影白名单
# ---------------------------------------------------------------------------

def test_student_projection_drops_all_sensitive_keys_before_template() -> None:
    row = {
        "slot_id": uuid4(),
        "question_no": "1",
        "statement_ast": {"text": "证明连续"},
        "public_assets": [],
        "response_kind": "proof",
        "answer_space_lines": 8,
        # 以下每个键都必须被丢掉；不是"模板不读"
        "answer": "SECRET",
        "rubric": {"steps": [5]},
        "answer_test": "x=1",
        "solution": "SECRET",
        "hidden_note": "SECRET",
    }
    projection = build_student_projection(
        uuid4(), "ABC123", "数学分析 I", "闭卷", [row]
    )
    dumped = repr(projection)
    assert "SECRET" not in dumped, dumped
    assert "rubric" not in dumped, dumped
    assert "answer_test" not in dumped, dumped
    assert projection.slots[0].statement_ast == {"text": "证明连续"}


def test_student_projection_rejects_nested_answer_keys() -> None:
    row = {
        "slot_id": uuid4(),
        "question_no": "1",
        # 题干 AST 是自由字典，字段锁管不到内部：必须靠递归扫描
        "statement_ast": {"text": "证明连续", "answer_key": "SECRET"},
        "public_assets": [],
        "response_kind": "proof",
        "answer_space_lines": 8,
    }
    try:
        build_student_projection(uuid4(), "ABC123", "数学分析 I", "闭卷", [row])
    except StudentProjectionLeakError:
        return
    raise AssertionError("嵌套的 answer_key 没有被学生投影拒绝")


def test_student_projection_rejects_unknown_public_asset_fields() -> None:
    row = {
        "slot_id": uuid4(),
        "question_no": "1",
        "statement_ast": {"text": "看图作答"},
        "public_assets": [
            {"asset_id": "fig-1", "url": "https://example.test/fig-1.png",
             "mime_type": "image/png", "alt_text": "函数图像",
             "answer_key": "SECRET"}  # 资产自由字典里的洞
        ],
        "response_kind": "written",
        "answer_space_lines": 6,
    }
    try:
        build_student_projection(uuid4(), "ABC123", "数学分析 I", "闭卷", [row])
    except Exception as error:  # noqa: BLE001 — pydantic ValidationError 或投影泄露都算拒绝
        assert "answer_key" in str(error) or "extra" in str(error).lower(), str(error)
        return
    raise AssertionError("公开资产里多出的 answer_key 没有被拒绝")


# ---------------------------------------------------------------------------
# 2. 三类渲染的独立产物
# ---------------------------------------------------------------------------

def test_render_kinds_have_distinct_artifact_security_flags() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    student = service.render(actor, subject.paper_version_id, "student")
    answer = service.render(actor, subject.paper_version_id, "answer")
    rubric = service.render(actor, subject.paper_version_id, "rubric")

    assert student.artifact.contains_solution is False
    assert student.artifact.contains_answer_test is False
    assert answer.artifact.contains_solution is True
    assert answer.artifact.contains_answer_test is False
    assert rubric.artifact.contains_solution is True
    assert len({
        student.artifact.output_hash,
        answer.artifact.output_hash,
        rubric.artifact.output_hash,
    }) == 3, "三份产物的内容哈希相同：说明答案/量规混进了学生卷，或模板没有区分"


def test_render_records_manifest_template_and_hashes() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    outcome = service.render(actor, subject.paper_version_id, "student")
    record = outcome.render

    assert record.paper_version_id == subject.paper_version_id
    assert record.render_kind == "student"
    assert record.paper_manifest_hash == subject.manifest_hash
    assert record.template_version == TEMPLATE_VERSION
    assert record.renderer_version == RENDERER_VERSION
    assert record.status == "ready"
    assert record.language_code == "zh-CN"
    assert len(record.security_manifest_hash) == 64
    assert len(record.font_manifest_hash) == 64
    assert record.output_hash == outcome.artifact.output_hash
    assert record.artifact_version_id == outcome.artifact.id
    assert record.created_by == actor


def test_rerender_is_idempotent_and_does_not_duplicate_artifacts() -> None:
    subject = sample_subject()
    store = MemoryObjectStore()
    service, actor, repository = service_with(subject, store=store)
    first = service.render(actor, subject.paper_version_id, "student")
    second = service.render(actor, subject.paper_version_id, "student")

    assert first.artifact.id == second.artifact.id, "同一输入渲染两次产生了两个 artifact 版本"
    assert first.artifact.output_hash == second.artifact.output_hash
    assert first.render.id == second.render.id, "同一输入渲染两次产生了两个 paper_render 行"
    assert len(repository.renders) == 1
    assert len(store.objects) == 1, f"对象存储里出现了重复对象：{list(store.objects)}"


def test_answer_volume_contains_answer_but_not_rubric() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    answer = service.render(actor, subject.paper_version_id, "answer").body.decode("utf-8")
    rubric = service.render(actor, subject.paper_version_id, "rubric").body.decode("utf-8")

    assert "SECRET_ANSWER" in answer
    assert "SECRET_SOLUTION" in answer, "答案卷应包含解析"
    assert "SECRET_RUBRIC" not in answer, "答案卷不应包含量规细则"
    assert "SECRET_ANSWER" in rubric, "量规卷应同时给出答案要点"
    assert "SECRET_RUBRIC" in rubric


def test_renderer_is_printable_html_and_declares_pdf_not_implemented() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    for kind in ("student", "answer", "rubric"):
        body = service.render(actor, subject.paper_version_id, kind).body.decode("utf-8")
        assert body.startswith("<!DOCTYPE html>"), body[:80]
        # 机器可读的事实留在属性里，接口响应另有 pdf_implemented 字段。
        assert 'data-pdf-implemented="false"' in body
        assert 'name="pdf-status" content="not-implemented"' in body
        # 卷面是给学生/教师打印的：打印指引可以出现，开发说明不能出现。
        assert PRINT_NOTE in body
        assert PDF_NOTICE not in body, "卷面印出了开发说明（PDF 未实现的原因）"
        assert "WeasyPrint" not in body and "reportlab" not in body
    assert MIME_TYPE == "text/html; charset=utf-8"
    assert MEDIA_KIND == "paper_render"


# ---------------------------------------------------------------------------
# 3. 撤回、权限与不完整卷
# ---------------------------------------------------------------------------

def test_withdrawn_paper_cannot_be_rendered_in_any_kind() -> None:
    subject = sample_subject(status="withdrawn")
    service, actor, repository = service_with(subject)
    for kind in ("student", "answer", "rubric"):
        try:
            service.render(actor, subject.paper_version_id, kind)
        except PaperRenderWithdrawnError:
            continue
        raise AssertionError(f"撤回后的卷子仍然可以渲染 {kind} 卷")
    assert repository.renders == [], "撤回卷的失败渲染不应留下 paper_render 行"


def test_unknown_paper_version_is_not_found() -> None:
    service, actor, _ = service_with(sample_subject())
    try:
        service.render(actor, uuid4(), "student")
    except PaperRenderNotFoundError:
        return
    raise AssertionError("不存在的 paper_version 没有报 404 语义错误")


def test_permission_is_separate_per_render_kind() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(
        subject, grants={"artifact.write", "paper.render.student"}
    )
    service.render(actor, subject.paper_version_id, "student")
    try:
        service.render(actor, subject.paper_version_id, "answer")
    except PermissionError:
        return
    raise AssertionError("只有学生卷权限的账号导出了答案卷")


def test_unknown_render_kind_is_rejected() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    try:
        service.render(actor, subject.paper_version_id, "teacher")
    except ValueError:
        return
    raise AssertionError("未知的 render_kind 没有在服务层被拒绝")


def test_incomplete_slot_blocks_render_instead_of_printing_a_blank() -> None:
    subject = sample_subject()
    empty_slot = replace(
        subject.sections[0].slots[0],
        statement_md="",
        answer_md=None,
        source_no=None,
        empty=True,
    )
    broken = replace(
        subject,
        sections=(replace(subject.sections[0], slots=(empty_slot,)), subject.sections[1]),
    )
    service, actor, _ = service_with(broken)
    try:
        service.render(actor, broken.paper_version_id, "student")
    except PaperRenderIncompleteError:
        return
    raise AssertionError("未选题的槽位被渲染成了空白题，而不是明确阻断")


# ---------------------------------------------------------------------------
# 4. worker 接线（静态断言：本层不连真库）
# ---------------------------------------------------------------------------

def test_paper_render_is_in_the_worker_kind_whitelist() -> None:
    source = (ROOT / "server" / "app" / "generation.py").read_text(encoding="utf-8")
    assert "'paper-render'" in source, "claim_next 的 kind 白名单没有 paper-render"
    match = re.search(r"kind IN \((.*?)\)", source, re.S)
    assert match and "paper-render" in match.group(1), source[:200]


def test_worker_dispatches_paper_render_jobs_to_the_render_service() -> None:
    source = (ROOT / "server" / "app" / "generation.py").read_text(encoding="utf-8")
    assert "run_render_job" in source, "run_job 没有把 paper-render 分派到渲染服务"
    assert "from . import rendering" in source


def test_worker_result_describes_render_jobs() -> None:
    source = (ROOT / "scripts" / "run_worker.py").read_text(encoding="utf-8")
    assert "paper-render" in source, "worker 的 describe() 不认 paper-render，成功日志会显示 0 个知识点"


def test_migration_0031_adds_paper_render_job_kind_additively() -> None:
    migrations = sorted((ROOT / "db" / "migrations").glob("0031_*.sql"))
    assert len(migrations) == 1, f"缺少唯一的 0031 迁移：{migrations}"
    text = migrations[0].read_text(encoding="utf-8").lower()
    assert "'paper-render'" in text
    assert "pipeline_job_kind_check" in text
    for forbidden in ("drop table", "delete from", "truncate", "drop column"):
        assert forbidden not in text, f"0031 迁移里出现禁止操作：{forbidden}"


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
    print(f"✓ 三类渲染服务测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
