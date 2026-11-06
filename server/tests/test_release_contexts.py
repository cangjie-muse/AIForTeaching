"""阶段 A 任务 8：Release Context 冻结入口（内容寻址的幂等）。

任务 3 只做了 `ReleaseService.freeze` 的服务层，没有 HTTP 入口，候选接口在真实
环境永远 422。本文件锁住补上入口后必须成立的三条：

1. **同一请求得到同一个 Release**：id 由内容（课程/班级/用途/base/每条题号+内容哈希）
   唯一决定。重试不是"又建一个 Release"，而是命中同一行。
2. **内容变了就是另一条 Release**：改了题、换了班级、换了用途，id 必然不同——
   这不是幂等失效，而是本来就该分叉。
3. **不存在的题号要报出来**：冻结时题号来自用户输入，写错一个就要 422，而不是
   悄悄少冻结两道题。

运行：`python server/tests/test_release_contexts.py`
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.releases import (  # noqa: E402
    ExerciseSnapshot,
    InMemoryReleaseRepository,
    ReleaseScopeError,
    ReleaseService,
    UnknownExerciseError,
)

ACTOR = UUID("88888888-8888-5888-8888-888888888888")
COURSE = "analysis"

EXERCISES = (
    ExerciseSnapshot(
        course_ref=COURSE, exercise_ref="2.1",
        prompt_md="求 $\\int_0^1 x\\,dx$。", answer_md="$1/2$。",
        review_status="published", license="cc-by", difficulty=2,
    ),
    ExerciseSnapshot(
        course_ref=COURSE, exercise_ref="2.2",
        prompt_md="判断级数 $\\sum 1/n^2$ 的敛散性。",
        answer_md="收敛。", review_status="published", license="cc-by",
    ),
)


def build() -> tuple[ReleaseService, InMemoryReleaseRepository]:
    repo = InMemoryReleaseRepository(exercises=EXERCISES)
    return ReleaseService(repository=repo), repo


def freeze(service: ReleaseService, **overrides: object):
    kwargs = dict(
        actor_id=ACTOR, course_ref=COURSE, purpose="formal",
        exercise_refs=("2.1", "2.2"), offering_ref="class-2026-01",
    )
    kwargs.update(overrides)
    return service.freeze_idempotent(**kwargs)


def test_same_request_freezes_once() -> None:
    service, repo = build()
    first, created_first = freeze(service)
    second, created_second = freeze(service)

    assert created_first is True
    assert created_second is False, "同一请求第二次冻结应当命中已有 Release"
    assert first.id == second.id
    assert first.content_hash == second.content_hash
    assert len(repo.contexts) == 1, "重试建出了第二个 Release"


def test_changing_content_produces_a_new_release() -> None:
    service, _ = build()
    first, _ = freeze(service)
    second, _ = freeze(service, exercise_refs=("2.1",))
    assert first.id != second.id
    assert first.content_hash != second.content_hash


def test_changing_scope_or_purpose_produces_a_new_release() -> None:
    service, _ = build()
    base, _ = freeze(service)
    other_purpose, _ = freeze(service, purpose="practice")
    other_class, _ = freeze(service, offering_ref="class-2026-02")
    assert len({base.id, other_purpose.id, other_class.id}) == 3


def test_unknown_exercise_is_reported_not_silently_dropped() -> None:
    service, _ = build()
    try:
        freeze(service, exercise_refs=("2.1", "9.9"))
    except UnknownExerciseError as error:
        assert "9.9" in str(error)
        return
    raise AssertionError("不存在的题号被静默忽略")


def test_empty_exercise_list_is_rejected() -> None:
    service, _ = build()
    try:
        freeze(service, exercise_refs=())
    except ReleaseScopeError:
        return
    raise AssertionError("空题号列表冻结出了 Release")


def test_items_are_pinned_to_the_frozen_version() -> None:
    service, _ = build()
    context, _ = freeze(service)
    items = service.items(context.id)
    assert len(items) == 2
    for item in items:
        assert item.release_context_id == context.id
        assert item.state == "included"
        assert len(item.content_hash) == 64
        assert item.exercise_version_id is not None
        assert item.exercise_family_id is not None


def test_freeze_is_denied_without_a_repository() -> None:
    service = ReleaseService(repository=None)
    try:
        freeze(service)
    except Exception as error:  # noqa: BLE001 —— 缺仓储就是拒绝，具体类型不重要
        assert "仓储" in str(error)
        return
    raise AssertionError("没有仓储也冻结出了 Release")


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
    print(f"✓ Release 冻结入口测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
