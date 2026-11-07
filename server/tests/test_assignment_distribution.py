"""`create_draft` 接受 distribution_kind（阶段 C 的入口）。

此前把 online/both 只写进了模型，没有从 API 透传到服务层——也就是说
"在线作业"在接口上根本建不出来，学生卷永远发不出去。这条测试就是防回退的。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))


def test_create_draft_accepts_distribution_kind() -> None:
    import inspect

    from app.assignments import AssignmentService

    signature = inspect.signature(AssignmentService.create_draft)
    assert "distribution_kind" in signature.parameters, (
        "服务层必须接受 distribution_kind，否则 API 传进来也没用")
    assert signature.parameters["distribution_kind"].default == "print", (
        "默认必须保持 print：阶段 A 的行为不能被这次改动悄悄改掉")


def test_api_forwards_distribution_kind() -> None:
    source = (ROOT / "server/app/assignment_api.py").read_text(encoding="utf-8")
    assert "distribution_kind=body.distribution_kind" in source
    assert 'Literal["print", "online", "both"]' in source


def test_model_exposes_online_and_both() -> None:
    from app.assignments import AssignmentRow as Row

    field = Row.model_fields["distribution_kind"]
    allowed = set(getattr(field.annotation, "__args__", ()))
    assert allowed == {"print", "online", "both"}, allowed


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
    print(f"✓ 作业分发方式透传测试全部通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
