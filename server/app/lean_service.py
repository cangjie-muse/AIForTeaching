"""Lean 形式化验证的接入点——**尚未实现**，这里只放契约与唯一写入口。

为什么现在就把这个文件摆出来：分层（`none / lean / human / strongest`）和可见范围
里的「Lean 验证 + 人工审核」档都已经在库里、在页面上、在测试里。缺的只是"谁来跑
Lean"。把契约先定死，接验证器时只改 `run_lean()`，不动审核页、目录闸、可见范围。

## 判"一条 Lean 证明成立"要几步

1. **形式化**：把原题写成 Lean 4 命题（`statement_lean`）。这一步不能自动化，
   必须有人写；写错了命题，证明再漂亮也没意义。
2. **编译**：在 Lean 4 + mathlib 环境里编译证明，成功才 `compiled = true`。
3. **忠实性**：人确认"形式化命题与原题的条件、结论一致"（`faithful_status`）。
   编译通过但命题写弱了（比如偷偷加了 `h : 0 < x`）是最危险的假通过。

`v_exercise_verification` 只认 `compiled = true AND faithful_status = 'teacher-reviewed'`，
两道缺一不可。所以"跑了 Lean"不等于"lean 层"。

## 接验证器时要做什么

    def run_lean(statement_lean: str, proof: str, toolchain: str) -> LeanBuild:
        ...  # 真的编译，返回退出码与日志
        return LeanBuild(ok=..., log=..., toolchain=..., proofs={"Main.lean": proof})

    LEAN_AVAILABLE = True   # 打开开关后 UI 的"(未接入)"提示自动消失

其余（写库、审计、时间戳）复用 `record_proof()`，不要另写一份 SQL。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import db

# 页面与接口用它判断"能不能选 Lean 这一档"。没接验证器时永远是 False，
# 界面据此标注"Lean 验证待接入"，避免让人以为选了就有人兜底。
LEAN_AVAILABLE = False

# 接进来时填：leanprover/lean4:v4.12.0 + mathlib@<commit>
TOOLCHAIN = ""


class LeanUnavailable(RuntimeError):
    """验证器还没接。调用方应转成 501，而不是假装成功。"""


@dataclass
class LeanBuild:
    """一次真实编译的结果。"""

    ok: bool
    log: str = ""
    toolchain: str = TOOLCHAIN
    proofs: dict[str, str] = field(default_factory=dict)

    @property
    def log_sha256(self) -> str:
        return hashlib.sha256(self.log.encode("utf-8")).hexdigest()


def available() -> bool:
    return LEAN_AVAILABLE


def run_lean(statement_lean: str, proof: str | None = None) -> LeanBuild:  # pragma: no cover
    """真正跑 Lean 的地方。当前未接入，直接拒绝。"""
    raise LeanUnavailable(
        "Lean 形式化验证尚未接入：需要 Lean 4 + mathlib 构建环境。"
        "接入前请用人工审核这一档；数据库与界面已就绪，接上 run_lean() 即生效。"
    )


def verify_exercise(course_id: str, exercise_id: str, proof: str | None = None) -> LeanBuild:
    """对外唯一的"验证一道题"入口。编译结果由调用方决定是否 `record_proof`。"""
    row = db.query_one(
        "SELECT statement_lean FROM lean_proof WHERE course_id = %s AND exercise_id = %s "
        "ORDER BY ord LIMIT 1",
        (course_id, exercise_id),
    )
    if not row or not row["statement_lean"]:
        raise ValueError(f"{course_id}/{exercise_id} 还没有形式化命题，无法验证")
    return run_lean(row["statement_lean"], proof)


def record_proof(
    course_id: str,
    exercise_id: str,
    build: LeanBuild,
    *,
    statement_lean: str,
    proof_file: str | None = None,
    faithful_status: str = "draft",
    faithful_note: str | None = None,
    actor: str | None = None,
    ord_: int = 1,
) -> dict:
    """把一次编译结果写进 lean_proof。**唯一写入口**，避免各处的哈希/时间戳算错。

    注意：调用这里不等于题目就升到 lean 层了——还要 `faithful_status='teacher-reviewed'`
    且 `compiled=True`，视图才会给出 lean / strongest。
    """
    if not statement_lean.strip():
        raise ValueError("statement_lean 不能为空：没有命题就没有验证对象")
    proof_sha = None
    if build.ok and proof_file:
        proof_sha = hashlib.sha256(build.proofs.get(proof_file, "").encode("utf-8")).hexdigest()

    row = db.query_one(
        """
        INSERT INTO lean_proof (course_id, exercise_id, ord, statement_lean, proof_file,
                                proof_sha256, toolchain, build_log_sha256, compiled,
                                compiled_at, faithful_status, faithful_note, faithful_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, %s, %s)
        ON CONFLICT (course_id, exercise_id, ord) DO UPDATE
        SET statement_lean = EXCLUDED.statement_lean,
            proof_file = EXCLUDED.proof_file,
            proof_sha256 = EXCLUDED.proof_sha256,
            toolchain = EXCLUDED.toolchain,
            build_log_sha256 = EXCLUDED.build_log_sha256,
            compiled = EXCLUDED.compiled,
            compiled_at = EXCLUDED.compiled_at,
            faithful_status = EXCLUDED.faithful_status,
            faithful_note = EXCLUDED.faithful_note,
            faithful_by = EXCLUDED.faithful_by
        RETURNING course_id, exercise_id, ord, compiled, faithful_status, toolchain
        """,
        (course_id, exercise_id, ord_, statement_lean, proof_file, proof_sha,
         build.toolchain, build.log_sha256, build.ok, build.ok, faithful_status,
         faithful_note, actor),
    )
    # lean_verified_at 只是 v_exercise_verification 的一个辅助字段；tier 由视图判定，
    # 所以这里同步写 exercise，避免两处口径不一致。
    if build.ok and faithful_status == "teacher-reviewed":
        db.execute(
            "UPDATE exercise SET lean_verified_at = now() "
            "WHERE course_id = %s AND id = %s AND lean_verified_at IS NULL",
            (course_id, exercise_id),
        )
    return row or {}
