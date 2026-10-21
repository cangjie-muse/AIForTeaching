"""Lean 验证接入点的契约测试（不连数据库、不跑 Lean）。

现在还没接验证器，所以测的不是"证明对不对"，而是"口留得对不对"：
1. 没接时 `available()` 必须是 False——界面据此标"待接入"，不能假装有；
2. 调 `run_lean()` 必须明确报"未接入"，而不是返回一个看似成功的空结果；
3. `record_proof()` 拒绝空命题——没有命题就没有验证对象；
4. 编译日志哈希稳定，方便以后核对"这条证明是不是当初那条"。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import lean_service  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  ✗ {name}  — {detail}")


print("未接入状态必须是诚实的：")
check("available() 为 False", lean_service.available() is False)
check("工具链字符串为空（接了才填）", lean_service.TOOLCHAIN == "", repr(lean_service.TOOLCHAIN))

try:
    lean_service.run_lean("theorem t : 1 = 1 := rfl")
    check("run_lean 明确报未接入", False, "居然返回了结果")
except lean_service.LeanUnavailable as exc:
    message = str(exc)
    check("run_lean 明确报未接入", True, message[:40])
    check("报错里说清楚缺什么", "Lean 4" in message and "mathlib" in message, message[:80])
    check("报错里给出可用的替代方案", "人工审核" in message, message[:80])

print("\n写库入口的防护：")
try:
    lean_service.record_proof(
        "c", "e", lean_service.LeanBuild(ok=True), statement_lean="   ")
    check("空命题被拒绝", False, "空命题写进去了")
except ValueError as exc:
    check("空命题被拒绝", "命题" in str(exc), str(exc)[:50])

print("\n编译结果对象：")
build = lean_service.LeanBuild(ok=False, log="error: unknown identifier 'foo'")
check("失败结果保留日志", build.ok is False and "unknown identifier" in build.log)
check("日志哈希是 64 位十六进制", len(build.log_sha256) == 64 and all(
    char in "0123456789abcdef" for char in build.log_sha256), build.log_sha256[:16])
check("同一日志哈希稳定",
      lean_service.LeanBuild(ok=False, log="x").log_sha256
      == lean_service.LeanBuild(ok=False, log="x").log_sha256)
check("不同日志哈希不同",
      lean_service.LeanBuild(ok=False, log="x").log_sha256
      != lean_service.LeanBuild(ok=False, log="y").log_sha256)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print("Lean 接入点契约测试全部通过。")
