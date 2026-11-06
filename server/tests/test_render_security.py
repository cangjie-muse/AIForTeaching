"""学生卷隔离与对象存储落盘的安全测试。

这份文件只回答两个问题，且都用**内容**而不是字段名证明：

  1. 学生卷产物里到底有没有答案/解析/量规/answer_test 的文本？
     `test_student_artifact_contains_no_answer_solution_or_rubric_text` 把同一份
     合成卷渲染三次，在 student 的字节里逐个搜 `SECRET_*`，同时确认 answer/rubric
     里确实搜得到——否则"没搜到"可能只是夹具没写。
  2. 本地对象存储是不是内容寻址 + 原子写？路径穿越、半截临时文件、覆盖写是否
     都被挡住？渲染产物最终要落到这里，写坏了就是"卷子打不开"。

另外用静态断言钉住生产仓储：`assessment.paper_render` 与
`agent.artifact_version` 的写入必须在 `db.transaction(assessment_service=True)`
里——paper_render 是 FORCE RLS，忘了设作用域的表现是"渲染成功但一行都读不到"。

运行：`python server/tests/test_render_security.py`
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(HERE))

from app import artifacts as artifacts_module  # noqa: E402
from app.artifacts import (  # noqa: E402
    LocalObjectStore,
    MemoryObjectStore,
    StaticAuthorizer,
    default_artifact_root,
)
from app.contracts import StudentProjectionLeakError  # noqa: E402
from app.rendering import (  # noqa: E402
    MEDIA_KIND,
    PaperRenderService,
    PaperRenderWithdrawnError,
    assert_student_output_safe,
)
from test_rendering import ALL_GRANTS, sample_subject, service_with  # noqa: E402

SECRETS = (
    "SECRET_ANSWER",
    "SECRET_ANSWER-2",
    "SECRET_SOLUTION",
    "SECRET_RUBRIC",
    "SECRET_HIDDEN",
    "SECRET_ANSWER_TEST",
)


# ---------------------------------------------------------------------------
# 1. 内容级泄露断言（本任务的硬性要求）
# ---------------------------------------------------------------------------

def test_student_artifact_contains_no_answer_solution_or_rubric_text() -> None:
    subject = sample_subject()
    store = MemoryObjectStore()
    service, actor, _ = service_with(subject, store=store)

    student = service.render(actor, subject.paper_version_id, "student")
    answer = service.render(actor, subject.paper_version_id, "answer")
    rubric = service.render(actor, subject.paper_version_id, "rubric")

    student_bytes = student.body
    student_text = student_bytes.decode("utf-8")
    for secret in SECRETS:
        assert secret not in student_text, (
            f"学生卷产物里出现了答案侧文本 {secret}：\n{student_text[:1200]}"
        )
        # 从对象存储按内容地址再读一遍：防止"返回值安全、落盘不安全"
        key = service.object_key(student.artifact.output_hash)
        assert secret not in store.get(key).decode("utf-8")

    # 反向断言：如果 answer/rubric 里也搜不到，上面的断言就是空转
    answer_text = answer.body.decode("utf-8")
    rubric_text = rubric.body.decode("utf-8")
    assert "SECRET_ANSWER" in answer_text
    assert "SECRET_SOLUTION" in answer_text, "答案卷没有带解析"
    assert "SECRET_ANSWER" in rubric_text
    assert "SECRET_RUBRIC" in rubric_text
    for text in (answer_text, rubric_text):
        assert "SECRET_ANSWER_TEST" not in text, "answer_test 不应进入任何打印卷"
        assert "SECRET_HIDDEN" not in text, "隐藏批注不应进入任何打印卷"

    # 学生卷仍然有题干
    assert "证明" in student_text


def test_student_output_guard_blocks_a_leaky_template_before_write() -> None:
    subject = sample_subject()
    store = MemoryObjectStore()

    def leaky_renderer(*, template: str, kind: str, context: object) -> bytes:
        # 模拟"模板错误地把答案整段写进学生卷"。服务必须在落盘前拦住。
        answer = subject.sections[0].slots[0].answer_md or ""
        return f"<article class=\"slot\"><p>{answer}</p></article>".encode("utf-8")

    service, actor, repository = service_with(
        subject, store=store, renderer=leaky_renderer
    )
    try:
        service.render(actor, subject.paper_version_id, "student")
    except StudentProjectionLeakError:
        pass
    else:
        raise AssertionError("学生卷模板泄题时服务仍然把文件落了盘")

    assert repository.renders == [], "失败的渲染写入了 paper_render 行"
    assert store.objects == {}, f"失败的渲染仍然写入了对象存储：{list(store.objects)}"


def test_student_output_guard_ignores_text_shared_with_the_statement() -> None:
    # 题干与答案相同（如"写出定义"）时不能误报，否则正常卷子打不开。
    from app.rendering import RenderSection, RenderSlot

    slot = RenderSlot(
        slot_id=uuid4(),
        code="A1",
        slot_no=1,
        question_no="1.1",
        statement_md="请写出函数在闭区间上有界的定义。",
        answer_md="请写出函数在闭区间上有界的定义。",
        score=1,
        display_config={},
    )
    subject = replace(
        sample_subject(),
        sections=(RenderSection("A", "一", "", 0, (slot,)),),
    )
    assert_student_output_safe(
        f"<p>{slot.statement_md}</p>".encode("utf-8"), subject
    )


# ---------------------------------------------------------------------------
# 2. 本地对象存储：内容寻址 + 原子写 + 路径安全
# ---------------------------------------------------------------------------

def test_local_object_store_is_content_addressed_and_writes_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalObjectStore(tmp)
        key = "paper_render/" + "d" * 64
        store.put(key, b"<html>paper</html>")
        assert store.get(key) == b"<html>paper</html>"
        assert store.path_for(key).resolve() == (
            Path(tmp) / "paper_render" / ("d" * 64)
        ).resolve()
        assert store.path_for(key).read_bytes() == b"<html>paper</html>"


def test_local_object_store_uses_atomic_replace_and_leaves_no_temp_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalObjectStore(tmp)
        key = "paper_render/" + "e" * 64
        store.put(key, b"first")
        store.put(key, b"second")
        assert store.get(key) == b"second"
        leftovers = [p for p in Path(tmp).rglob("*") if p.is_file() and p.name != "e" * 64]
        assert leftovers == [], f"原子写留下了临时文件：{leftovers}"


def test_local_object_store_failed_write_keeps_previous_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalObjectStore(tmp)
        key = "paper_render/" + "f" * 64
        store.put(key, b"good")
        original = artifacts_module.os.replace
        artifacts_module.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            store.put(key, b"bad")
        except OSError:
            pass
        else:
            raise AssertionError("rename 失败没有向上抛出")
        finally:
            artifacts_module.os.replace = original
        assert store.get(key) == b"good", "rename 失败后旧内容被破坏"
        leftovers = [p for p in Path(tmp).rglob("*.part")] + [
            p for p in Path(tmp).rglob(".tmp-*")
        ]
        assert leftovers == [], f"失败写入留下了临时文件：{leftovers}"


def test_local_object_store_rejects_path_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalObjectStore(tmp)
        for key in ("../escape", "/absolute", "paper_render/../../escape",
                    "paper_render\\..\\escape", "C:/windows/system32/x"):
            try:
                store.put(key, b"x")
            except ValueError:
                continue
            raise AssertionError(f"对象存储接受了越界 key：{key!r}")


def test_local_object_store_default_root_is_under_existing_uploads_dir() -> None:
    upload_dir = Path(os.environ.get("UPLOAD_DIR", ROOT / "uploads"))
    root = default_artifact_root()
    assert root == upload_dir / "artifacts", (
        f"本地产物根目录不在既有 uploads 约定下：{root} != {upload_dir / 'artifacts'}"
    )
    assert root.name == "artifacts"


def test_artifact_storage_uri_is_content_addressed_and_hides_paper_identity() -> None:
    subject = sample_subject()
    service, actor, _ = service_with(subject)
    outcome = service.render(actor, subject.paper_version_id, "answer")
    uri = outcome.artifact.storage_uri
    assert uri.startswith("s3://private-artifacts/paper_render/"), uri
    assert outcome.artifact.output_hash in uri
    assert str(subject.paper_version_id) not in uri
    assert "SECRET" not in uri
    assert "signed" not in uri.lower() and "http" not in uri.lower()


# ---------------------------------------------------------------------------
# 3. 下载：撤回、权限、访问审计
# ---------------------------------------------------------------------------

def test_download_rechecks_permission_and_withdrawal() -> None:
    subject = sample_subject()
    store = MemoryObjectStore()
    service, actor, repository = service_with(subject, store=store)
    outcome = service.render(actor, subject.paper_version_id, "answer")

    body, record, loaded = service.download(actor, outcome.render.id)
    assert body == outcome.body
    assert record.id == outcome.render.id
    assert loaded.paper_version_id == subject.paper_version_id
    assert len(repository.artifact_repository().access_events) == 1

    # 换一个只有学生卷权限的账号：不能下载答案卷
    other = uuid4()
    restricted = PaperRenderService(
        repository=repository,
        authorizer=StaticAuthorizer({
            other: {"artifact.read", "paper.render.student"}
        }),
        object_store=store,
    )
    try:
        restricted.download(other, outcome.render.id)
    except PermissionError:
        pass
    else:
        raise AssertionError("只有学生卷权限的账号下载了答案卷")

    # 撤回后，即便是原导出人也不能再下载
    repository.subject = replace(subject, status="withdrawn")
    try:
        service.download(actor, outcome.render.id)
    except PaperRenderWithdrawnError:
        pass
    else:
        raise AssertionError("撤回后的卷子仍然可以下载")


def test_list_renders_hides_kinds_the_actor_may_not_export() -> None:
    subject = sample_subject()
    store = MemoryObjectStore()
    service, actor, repository = service_with(subject, store=store)
    service.render(actor, subject.paper_version_id, "student")
    service.render(actor, subject.paper_version_id, "answer")
    service.render(actor, subject.paper_version_id, "rubric")

    other = uuid4()
    restricted = PaperRenderService(
        repository=repository,
        authorizer=StaticAuthorizer({
            other: {"paper.render.student"}
        }),
        object_store=store,
    )
    loaded, records = restricted.list_current_renders(other, subject.paper_version_id)
    assert loaded.paper_version_id == subject.paper_version_id
    assert {r.render_kind for r in records} == {"student"}, (
        f"列表泄露了无权导出的卷种：{[r.render_kind for r in records]}"
    )


def test_list_renders_only_returns_current_manifest() -> None:
    subject = sample_subject()
    service, actor, repository = service_with(subject)
    service.render(actor, subject.paper_version_id, "student")
    repository.subject = replace(subject, manifest_hash="b" * 64)
    _, records = service.list_current_renders(actor, subject.paper_version_id)
    assert records == (), "草稿编辑后仍把旧 manifest 的渲染当成可用预览"


# ---------------------------------------------------------------------------
# 4. 生产仓储的静态断言（本机没有可连的 PostgreSQL）
# ---------------------------------------------------------------------------

def test_postgres_render_repository_writes_artifacts_and_paper_render() -> None:
    source = (ROOT / "server" / "app" / "rendering.py").read_text(encoding="utf-8")
    assert "db.transaction(assessment_service=True)" in source, (
        "paper_render 是 FORCE RLS：写入必须在 assessment_service 事务里"
    )
    assert "INSERT INTO assessment.paper_render" in source
    assert "INSERT INTO agent.artifact_version" in source
    assert "INSERT INTO agent.artifact " in source
    assert "INSERT INTO agent.artifact_access_event" in source


def test_render_repository_uses_unique_key_for_retry_idempotency() -> None:
    source = (ROOT / "server" / "app" / "rendering.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (paper_version_id, render_kind, output_hash)" in source, (
        "paper_render 没有按 UNIQUE(paper_version_id, render_kind, output_hash) 去重"
    )
    assert "ON CONFLICT (output_hash, media_kind, created_by)" in source


def test_student_context_never_queries_answer_columns() -> None:
    source = (ROOT / "server" / "app" / "rendering.py").read_text(encoding="utf-8")
    # student 路径只能从投影构造，不能拿 answer/rubric 再"过滤"
    assert "build_student_projection" in source
    assert "STUDENT_KEYS" in source


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
    print(f"✓ 渲染安全与对象存储测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
