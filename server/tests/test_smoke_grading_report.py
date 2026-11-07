"""`scripts/smoke_grading.py` 的离线部分自检（不需要 PostgreSQL）。

联调脚本本身也在服务器上跑过两次才修好（tuple/dict、打印格式）；它至少应当有
一条本机可跑的自检，避免"改了 Report 逻辑却要等下一次上服务器才知道"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_grading import Report  # noqa: E402


def test_report_counts_checked_and_failed() -> None:
    report = Report()
    assert report.check("甲", True) is True
    assert report.check("乙", False, "原因") is False
    assert report.checked == 2
    assert report.failed == 1


def test_report_skip_does_not_count_as_checked() -> None:
    report = Report()
    report.skip("丙", "没有数据")
    assert report.checked == 0, "跳过不能计入已检查项，否则会虚报覆盖"


def test_script_uses_dict_rows() -> None:
    """源码级断言：psycopg 默认给 tuple，脚本必须显式要 dict_row。"""
    source = (ROOT / "scripts/smoke_grading.py").read_text(encoding="utf-8")
    assert "row_factory=psycopg.rows.dict_row" in source
    assert 'row["name"]' in source


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
    print(f"✓ 联调脚本自检通过（{len(tests)} 项）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run_tests())
