"""把「手工卷 + 约束组卷」整条链路要用的演示数据一次灌好，让浏览器里点得通。

为什么需要这个脚本：新建一个环境后，工作台/建卷/约束组卷三页全是空的，因为缺四样
彼此依赖的数据：

    题目审核通过 + 有许可   →  才能进候选清单（candidate eligibility 的硬门槛）
    冻结的 Release Context  →  候选清单只能来自固定 Release（§6.2）
    教学班（teacher + course）→  教师建蓝图要过课程范围授权（真源是班级名单）
    蓝图版本（constraint_assisted）→ 组卷页只对这种蓝图开求解

手工点一遍要跨 4 个页面、填十几个字段，而且**顺序错了会被门禁拒绝且看不出原因**。
这个脚本按依赖顺序一次建好，并打印接下来该点哪里。

幂等：每一段都存在性检查；重复执行不会产生第二份数据（Release 是内容寻址的，
蓝图同内容复用同一版本）。

用法：
    python scripts/seed_demo_paper_flow.py                 # 默认：数学分析 I / teacher
    python scripts/seed_demo_paper_flow.py --course analysis --teacher-username teacher
    python scripts/seed_demo_paper_flow.py --dry-run       # 只打印将要做什么
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import db  # noqa: E402
from app.blueprints import (  # noqa: E402
    BlueprintService,
    PostgresBlueprintRepository,
    ref_uuid,
)
from app.contracts.paper import (  # noqa: E402
    BlueprintContent,
    BlueprintSection,
    BlueprintSlot,
)
from app.releases import PostgresReleaseRepository, ReleaseService  # noqa: E402

# 演示用的卷面结构：三节五槽、100 分。与阶段 A 验收里的那份一致，
# 这样"文档里说的"和"你在界面上看到的"是同一份东西。
SECTION_SPEC = (
    ("A", "计算题", (("A1", "16.00"), ("A2", "24.00"))),
    ("B", "证明题", (("B1", "20.00"), ("B2", "20.00"))),
    ("C", "综合题", (("C1", "20.00"),)),
)
DEFAULT_LICENSE = "self-authored"
# 候选资格只认这两个终态（见 releases.APPROVED_REVIEW_STATUSES）。
APPROVED = "teacher-reviewed"


class Authorizer:
    """脚本用的放行授权端口。

    为什么可以这样：这个脚本**只在 dev/演示库**跑（入口处有 APP_ENV 检查），
    它扮演的是"管理员已经确认过课程范围"的那一步。真实服务里这一步由
    CourseScopeAuthorizer 查 teaching_class 完成——脚本恰好就是去建那个班级的，
    所以不能反过来依赖它（否则第一次跑永远授权失败）。
    """

    def require(self, actor_id, action, **scope) -> None:  # noqa: ANN001
        if not scope.get("course_ref"):
            raise PermissionError("缺少 course_ref")

    def is_human(self, actor_id) -> bool:  # noqa: ANN001
        return True


def say(message: str) -> None:
    print(f"  {message}")


def ensure_account(username: str, role: str, password: str) -> tuple[str, str]:
    """确保账号存在；返回 (账号 uuid 文本, 实际用户名)。"""
    row = db.query_one(
        "SELECT id, username FROM app_account WHERE username = %s", (username,))
    if row:
        say(f"账号已存在：{row['username']}")
        return row["id"], row["username"]
    from app import auth

    account_id = f"acc-demo-{role}"
    db.execute(
        """INSERT INTO app_account
             (id, username, display_name, role, password_hash, password_updated_at,
              must_change_password)
           VALUES (%s, %s, %s, %s, %s, now(), false)""",
        (account_id, username, {"teacher": "演示教师", "admin": "平台管理员"}.get(
            role, username), role, auth.hash_password(password)),
    )
    say(f"新建账号：{username}（{role}）")
    return account_id, username


def ensure_class(course_ref: str, teacher_id: str, name: str) -> str:
    """建教学班；这是"教师能建这门课的蓝图"的唯一真源。"""
    row = db.query_one(
        """SELECT id FROM teaching_class
            WHERE course_id = %s AND teacher_id = %s AND name = %s""",
        (course_ref, teacher_id, name),
    )
    if row:
        say(f"教学班已存在：{row['id']}")
        return row["id"]
    class_id = f"class-{course_ref}-demo"
    db.execute(
        """INSERT INTO teaching_class
             (id, course_id, name, academic_year, term_kind, teacher_id, created_by, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO NOTHING""",
        (class_id, course_ref, name, 2026, "秋", teacher_id, teacher_id,
         "seed_demo_paper_flow.py 建的演示班"),
    )
    say(f"新建教学班：{class_id}（课程 {course_ref}）")
    return class_id


def approve_exercises(course_ref: str, limit: int) -> list[str]:
    """把前 `limit` 道题标成"审核通过 + 有许可 + 有答案"，返回题号。

    这三项是候选资格硬门槛：状态不是 teacher-reviewed/published、许可为空、
    或答案为空，都会在候选清单里被拒，教师看到的是"这道题不能进卷"却不知道
    差哪一项。演示数据先补齐。
    """
    rows = db.query(
        """SELECT id, answer_md, license, review_status
             FROM exercise
            WHERE course_id = %s AND COALESCE(TRIM(answer_md), '') <> ''
            ORDER BY id
            LIMIT %s""",
        (course_ref, limit),
    )
    if not rows:
        raise SystemExit(
            f"课程 {course_ref} 里没有带答案的题目：先灌 db/seed.sql"
            "（psql \"$DATABASE_URL\" -f db/seed.sql）")
    changed = 0
    for row in rows:
        if row["review_status"] == APPROVED and (row["license"] or "").strip():
            continue
        db.execute(
            """UPDATE exercise
                  SET review_status = %s, license = COALESCE(NULLIF(TRIM(license), ''), %s)
                WHERE course_id = %s AND id = %s""",
            (APPROVED, DEFAULT_LICENSE, course_ref, row["id"]),
        )
        changed += 1
    refs = [row["id"] for row in rows]
    say(f"题目：{len(refs)} 道可用于组卷（本次补齐 {changed} 道的审核与许可）")
    return refs


def ensure_release(course_ref: str, exercise_refs: list[str], actor_id, label: str) -> object:
    """冻结一个 Release Context；内容寻址，所以重复跑得到同一个 id。"""
    # ReleaseService 没有授权端口：课程范围判定在 API 路由层做（见 release_contexts.py）。
    service = ReleaseService(repository=PostgresReleaseRepository())
    context, created = service.freeze_idempotent(
        actor_id=actor_id,
        course_ref=course_ref,
        purpose="formal",
        exercise_refs=exercise_refs,
        label=label,
        status="frozen",
    )
    say(f"Release Context {context.id}（{'新建' if created else '已存在，复用'}）"
        f" · {len(exercise_refs)} 道题 · frozen")
    return context


def ensure_blueprint(course_ref: str, context_id, actor_id, actor_ref: str,
                     class_ref: str, total_seconds: int) -> object:
    """建一份 constraint_assisted 蓝图；同内容会复用同一版本。"""
    service = BlueprintService(repository=PostgresBlueprintRepository(),
                               authorizer=Authorizer())
    sections = tuple(
        BlueprintSection(
            code=code,
            title=title,
            instruction="写出完整过程。",
            slots=tuple(
                BlueprintSlot(
                    code=slot_code,
                    score=Decimal(score),
                    # 演示：不给题型限制，让每道题都能进任一槽位——
                    # 首次跑通时最容易卡住的地方就是"槽位条件太严导致无候选"。
                    estimated_seconds=max(60, total_seconds // 5),
                )
                for slot_code, score in slots
            ),
        )
        for code, title, slots in SECTION_SPEC
    )
    content = BlueprintContent(
        schema_version="paper-blueprint/v1",
        purpose="formal",
        course_id=ref_uuid("course", course_ref),
        offering_id=ref_uuid("offering", class_ref),
        release_context_id=context_id,
        total_score=sum((slot.score for section in sections for slot in section.slots),
                        Decimal("0")),
        duration_seconds=total_seconds,
        sections=sections,
        # 锚题留空：单卷不需要；将来做 A/B 时在这里列出允许两卷共用的题号。
        hard_constraints={"anchor_version_ids": []},
        soft_objectives={},
        review_policy="formal_dual_review",
        assembly_mode="constraint_assisted",
    )
    row = service.create_version(
        actor_id=actor_id,
        content=content,
        blueprint_id=None,
        base_version_id=None,
        idempotency_key=f"seed-demo-{course_ref}-{class_ref}-v1",
        course_ref=course_ref,
        offering_ref=class_ref,
        actor_ref=actor_ref,
    )
    say(f"蓝图版本 {row.id}（第 {row.version_no} 版 · constraint_assisted · "
        f"{content.total_score} 分）")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="灌入组卷链路的演示数据")
    parser.add_argument("--course", default="analysis", help="课程号（文本 id）")
    parser.add_argument("--teacher-username", default="teacher")
    parser.add_argument("--teacher-password", default="whu-teacher-2026")
    parser.add_argument("--class-name", default="数学分析 I 演示班")
    parser.add_argument("--exercises", type=int, default=24, help="进入 Release 的题数")
    parser.add_argument("--duration-seconds", type=int, default=7200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if os.environ.get("APP_ENV", "dev") == "prod":
        raise SystemExit("APP_ENV=prod：演示数据脚本不在生产库运行")

    print(f"==> 准备演示数据：课程 {args.course}\n")
    db.pool.open()
    try:
        if db.query_one("SELECT 1 AS ok FROM course WHERE id = %s", (args.course,)) is None:
            raise SystemExit(
                f"没有课程 {args.course}：先灌 db/seed.sql，或换 --course 指定别的课程")

        teacher_id, teacher_username = ensure_account(
            args.teacher_username, "teacher", args.teacher_password)
        actor_uuid = ref_uuid("account", teacher_username)
        class_id = ensure_class(args.course, teacher_id, args.class_name)

        if args.dry_run:
            say("--dry-run：只做存在性检查，不改题目/Release/蓝图")
            return 0

        refs = approve_exercises(args.course, args.exercises)
        context = ensure_release(args.course, refs, actor_uuid, "演示冻结")
        blueprint = ensure_blueprint(
            args.course, context.id, actor_uuid, teacher_username, class_id,
            args.duration_seconds)

        print("\n==> 完成。浏览器里按顺序点：\n")
        base = "http://127.0.0.1:8000/#/course"
        print(f"  1. 登录：{args.teacher_username} / {args.teacher_password}")
        print(f"  2. 建卷（生成候选、挑题）：{base}/{args.course}/paper-build")
        print(f"     · 选第 {blueprint.version_no} 版蓝图 → 生成候选清单 → 挑题 → 建草稿卷")
        print(f"  3. 约束组卷（求解器挑题）：{base}/{args.course}/paper-assembly")
        print(f"     · 选同一版蓝图 → 开始求解 → 看诊断与方案 → 用方案建卷")
        print(f"     · 想要 A/B 两卷：勾上「同时生成 A/B 两卷」再求解")
        print(f"  4. 整卷审核与发布：{base}/{args.course}/paper-ops?version=<卷版本 id>")
        print("\n  说明：约束组卷要在 worker 在跑时才会出结果"
              "（systemctl status mathagent-worker）。")
        return 0
    finally:
        db.pool.close()


if __name__ == "__main__":
    sys.exit(main())
