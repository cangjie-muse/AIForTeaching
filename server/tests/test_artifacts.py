"""最小通用 Artifact 边界（server/app/artifacts.py）的行为。

为什么要测：`agent.artifact_version` 是三类打印卷（学生卷/答案卷/量规卷）唯一的
文件真源，也是"可预测 Artifact URL 泄露答案"这条禁令的落点。任务 1 只交付边界
语义——对象存储与数据库仓储尚未接线，所以这里的用例全部跑在内存实现上，
断言的是权限、哈希、寻址与访问审计，而不是持久化。

运行：`python server/tests/test_artifacts.py`
"""

from __future__ import annotations

import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from pydantic import ValidationError  # noqa: E402

from app.artifacts import (  # noqa: E402
    ArtifactService,
    MemoryObjectStore,
    StaticAuthorizer,
)
from app.contracts import ArtifactMetadata  # noqa: E402


def metadata(**overrides: object) -> ArtifactMetadata:
    payload: dict[str, object] = {
        "release_context_id": uuid4(),
        "visibility": "private",
        "contains_solution": False,
        "contains_answer_test": False,
        "mime_type": "application/pdf",
        "media_kind": "paper_render",
    }
    payload.update(overrides)
    return ArtifactMetadata(**payload)  # type: ignore[arg-type]


def service_with(actions: set[str], actor=None) -> ArtifactService:  # type: ignore[no-untyped-def]
    actor = actor or uuid4()
    return ArtifactService(
        MemoryObjectStore(),
        StaticAuthorizer({actor: actions}),
    )


def test_artifact_round_trip_preserves_security_metadata() -> None:
    actor = uuid4()
    service = ArtifactService(
        MemoryObjectStore(),
        StaticAuthorizer({actor: {"artifact.write", "artifact.read"}}),
    )
    context = uuid4()
    body = b"%PDF-1.7 synthetic"
    registered = service.register(
        actor_id=actor,
        body=body,
        metadata=metadata(release_context_id=context, contains_solution=True),
    )
    assert registered.output_hash == sha256(body).hexdigest()
    assert registered.release_context_id == context
    assert registered.contains_solution is True
    assert registered.contains_answer_test is False
    assert registered.byte_size == len(body)
    assert service.open(actor_id=actor, artifact_version_id=registered.id) == body


def test_artifact_registration_requires_write_permission() -> None:
    actor = uuid4()
    service = ArtifactService(MemoryObjectStore(), StaticAuthorizer({actor: {"artifact.read"}}))
    try:
        service.register(actor_id=actor, body=b"x", metadata=metadata())
    except PermissionError:
        return
    raise AssertionError("只有 artifact.read 的调用者仍然能注册 Artifact")


def test_artifact_open_requires_read_permission() -> None:
    actor = uuid4()
    service = ArtifactService(
        MemoryObjectStore(),
        StaticAuthorizer({actor: {"artifact.write", "artifact.read"}}),
    )
    registered = service.register(actor_id=actor, body=b"safe", metadata=metadata())
    try:
        service.open(actor_id=uuid4(), artifact_version_id=registered.id)
    except PermissionError:
        return
    raise AssertionError("没有任何授权的调用者打开了 Artifact")


def test_artifact_storage_uri_is_deterministic_and_not_public() -> None:
    actor = uuid4()
    service = service_with({"artifact.write"}, actor)
    first = service.register(actor_id=actor, body=b"safe", metadata=metadata())
    second = service.register(actor_id=actor, body=b"safe", metadata=metadata())
    assert first.storage_uri.startswith("s3://private-artifacts/"), first.storage_uri
    assert first.storage_uri == second.storage_uri, "同一内容两次注册的寻址不稳定"
    lowered = first.storage_uri.lower()
    for bad in ("signed", "http://", "https://", "public"):
        assert bad not in lowered, f"存储地址里出现 {bad}：{first.storage_uri}"


def test_duplicate_registration_reuses_the_same_version() -> None:
    actor, other_actor = uuid4(), uuid4()
    service = ArtifactService(
        MemoryObjectStore(),
        StaticAuthorizer({actor: {"artifact.write"}, other_actor: {"artifact.write"}}),
    )
    first = service.register(actor_id=actor, body=b"same", metadata=metadata())
    second = service.register(actor_id=actor, body=b"same", metadata=metadata())
    assert first.id == second.id, "同一 (output_hash, media_kind, created_by) 生成了两条版本"
    assert first.version_no == second.version_no == 1
    other = service.register(actor_id=other_actor, body=b"same", metadata=metadata())
    assert other.id != first.id, "不同创建者的相同内容被错误合并"


def test_unknown_visibility_is_rejected() -> None:
    try:
        metadata(visibility="public")
    except ValidationError:
        return
    raise AssertionError("ArtifactMetadata 接受了未定义的可见性 public")


def test_open_records_an_access_event() -> None:
    actor = uuid4()
    service = service_with({"artifact.write", "artifact.read"}, actor)
    registered = service.register(actor_id=actor, body=b"audit", metadata=metadata())
    service.open(actor_id=actor, artifact_version_id=registered.id)
    events = service.repository.access_events  # type: ignore[attr-defined]
    assert len(events) == 1, f"打开 Artifact 没有留下访问审计：{events!r}"
    assert events[0].artifact_version_id == registered.id
    assert events[0].actor_id == actor
    assert events[0].action == "open"


def test_open_unknown_version_fails_loudly() -> None:
    actor = uuid4()
    service = service_with({"artifact.read"}, actor)
    try:
        service.open(actor_id=actor, artifact_version_id=uuid4())
    except (KeyError, LookupError):
        return
    raise AssertionError("打开不存在的 Artifact 版本没有报错")


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
    print(f"✓ Artifact 边界测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
