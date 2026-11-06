"""迁移 0027 的静态契约检查（不连数据库）。

为什么用静态检查：仓库的 Python 测试套件刻意不连真库（`npm run check` 要能在
没有 PostgreSQL 的机器上跑完），而迁移本身仍需要防止三类回归——
  1. 少了表/列，阶段 A 后续任务一写就报 relation does not exist；
  2. 分值列不是 numeric(8,2)，换成 float/double 会让 100 分卷出现 99.9999；
  3. 迁移不是幂等的，或者顺手删了既有对象（本项目只做加法）。

因此这里读 SQL 文本做结构断言。真库的执行验证属于集成测试，本次未运行，
已在交付说明里标注。

运行：`python server/tests/test_assessment_migration.py`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "0027_assessment_baseline.sql"

REQUIRED_TABLES = {
    "assessment.paper_blueprint_version",
    "assessment.candidate_snapshot",
    "assessment.paper_version",
    "assessment.paper_section",
    "assessment.paper_slot",
    "assessment.paper_review_round",
    "assessment.paper_render",
    "assessment.attempt_policy_version",
    "assessment.item_exposure_event",
    "assessment.assignment",
    "assessment.assignment_item",
    "agent.artifact",
    "agent.artifact_version",
    "agent.artifact_access_event",
}

ARTIFACT_COLUMNS = {
    "release_context_id",
    "visibility",
    "contains_solution",
    "contains_answer_test",
    "output_hash",
    "storage_uri",
}


def sql() -> str:
    assert MIGRATION.is_file(), f"迁移文件不存在：{MIGRATION.relative_to(ROOT)}"
    return MIGRATION.read_text(encoding="utf-8")


def flat() -> str:
    """压平空白并转小写，避免换行/缩进让断言失效。"""
    return re.sub(r"\s+", " ", sql().lower())


def nospace(value: str) -> str:
    """去掉全部空白再比较：SQL 里 `status IN (...)` 的空格写法不该影响断言。"""
    return re.sub(r"\s", "", value.lower())


def test_migration_number_follows_0026() -> None:
    names = sorted(p.name for p in (ROOT / "db" / "migrations").glob("*.sql"))
    assert "0026_job_heartbeat.sql" in names
    assert names.index("0027_assessment_baseline.sql") == names.index("0026_job_heartbeat.sql") + 1, (
        "0027 没有紧跟在 0026 之后：" + "、".join(names[-3:])
    )


def test_required_tables_are_created_idempotently() -> None:
    text = flat()
    missing = [
        table for table in REQUIRED_TABLES
        if f"create table if not exists {table}" not in text
    ]
    assert not missing, "缺少幂等建表（CREATE TABLE IF NOT EXISTS）：" + "、".join(sorted(missing))


def test_schemas_are_created_idempotently() -> None:
    text = flat()
    for schema in ("assessment", "agent"):
        assert f"create schema if not exists {schema}" in text, f"缺少 CREATE SCHEMA IF NOT EXISTS {schema}"


def test_score_columns_use_numeric_8_2() -> None:
    text = flat()
    for table, columns in {
        "assessment.paper_blueprint_version": ("total_score",),
        "assessment.paper_version": ("total_score",),
        "assessment.paper_section": ("section_score",),
        "assessment.paper_slot": ("score",),
        "assessment.assignment_item": ("score",),
    }.items():
        body = table_body(text, table)
        for column in columns:
            assert re.search(rf"\b{column} numeric\(8,2\)", body), (
                f"{table}.{column} 不是 numeric(8,2)"
            )


def test_practice_penalty_uses_numeric_5_2() -> None:
    body = table_body(flat(), "assessment.attempt_policy_version")
    assert re.search(r"\bpractice_penalty numeric\(5,2\)", body), "练习罚分不是 numeric(5,2)"


def test_candidate_manifest_is_unique_per_blueprint() -> None:
    body = table_body(flat(), "assessment.candidate_snapshot")
    normalized = re.sub(r"[`\"\s]", "", body)
    assert "unique(blueprint_version_id,candidate_manifest_hash)" in normalized, (
        "candidate_snapshot 缺少 (blueprint_version_id, candidate_manifest_hash) 复合唯一键"
    )


def test_artifact_version_has_security_columns() -> None:
    body = table_body(flat(), "agent.artifact_version")
    missing = sorted(column for column in ARTIFACT_COLUMNS if column not in body)
    assert not missing, "agent.artifact_version 缺少安全元数据列：" + "、".join(missing)
    assert re.search(r"output_hash char\(64\)", body), "output_hash 不是 char(64)"
    assert "artifact_access_event" in flat()


def test_paper_status_admits_only_four_states() -> None:
    body = table_body(flat(), "assessment.paper_version")
    assert nospace("check(status in ('draft','in_review','approved','withdrawn'))") in nospace(body), (
        "paper_version.status 没有限定为 draft/in_review/approved/withdrawn 四态"
    )


def test_assignment_time_ordering_is_enforced() -> None:
    body = table_body(flat(), "assessment.assignment")
    assert nospace("check(visible_at<=due_at and due_at<=hard_close_at)") in nospace(body), (
        "assignment 缺少 visible_at <= due_at <= hard_close_at 约束"
    )


def test_render_kinds_are_limited_to_three() -> None:
    body = table_body(flat(), "assessment.paper_render")
    assert nospace("check(render_kind in ('student','answer','rubric'))") in nospace(body), (
        "paper_render.render_kind 没有限定为 student/answer/rubric"
    )


def test_migration_is_additive_only() -> None:
    text = flat()
    for pattern, why in (
        (r"\bdrop table\b", "删表"),
        (r"\bdrop column\b", "删列"),
        (r"\bdrop schema\b", "删 schema"),
        (r"\btruncate\b", "清空表"),
        (r"\bdelete from\b", "删除数据"),
        (r"\balter column\b[^;]*\bdrop\b", "删列"),
    ):
        assert not re.search(pattern, text), f"迁移里出现禁止操作：{why}"


def test_public_assignment_table_is_not_altered() -> None:
    text = flat()
    assert not re.search(r"alter table (?:public\.)?assignment\b", text), (
        "不应改动既有 public.assignment 的语义；阶段 A 的打印作业放在 assessment 命名空间"
    )


def test_student_render_security_is_service_only() -> None:
    text = flat()
    assert "create policy render_service_only on assessment.paper_render" in text, (
        "paper_render 缺少仅服务账号可读的 RLS 策略"
    )
    for table in ("paper_version", "paper_section", "paper_slot", "paper_render"):
        assert f"alter table assessment.{table} force row level security" in text, (
            f"{table} 没有 FORCE ROW LEVEL SECURITY"
        )


def table_body(text: str, qualified: str) -> str:
    """截取 `create table if not exists <name> ( ... );` 的括号内容。"""
    marker = f"create table if not exists {qualified} ("
    start = text.find(marker)
    assert start >= 0, f"找不到 {qualified} 的建表语句"
    depth = 0
    for index in range(start + len(marker) - 1, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(f"{qualified} 的建表语句括号不平衡")


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
    print(f"✓ 迁移静态检查全部通过（{len(tests)} 项，未连真库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
