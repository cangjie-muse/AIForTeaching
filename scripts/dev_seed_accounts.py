"""给本地库补三个演示账号（学生 / 教师 / 管理员）。

只在 AUTH_MODE=dev 或 password 的本地库里合理：生产环境由管理员在平台里建账号，
不该由脚本塞默认口令。所以这里带一个显式开关，且默认口令写进文档、第一次
登录后应当改掉。

幂等：已存在的账号不覆盖（不会把你改过的密码重置回默认值）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import auth, db  # noqa: E402

# 用户名 / 显示名 / 角色 / 初始口令
DEMO_ACCOUNTS = [
    ("admin", "平台管理员", "admin", "whu-admin-2026"),
    ("teacher", "张老师", "teacher", "whu-teacher-2026"),
    ("student", "李同学", "student", "whu-student-2026"),
]


def seed_accounts() -> None:
    created, skipped = [], []
    for username, display_name, role, password in DEMO_ACCOUNTS:
        existing = db.query_one(
            "SELECT id FROM app_account WHERE username = %s", (username,)
        )
        if existing:
            skipped.append(username)
            continue
        db.execute(
            """INSERT INTO app_account
                 (id, username, display_name, role, password_hash, password_updated_at,
                  must_change_password)
               VALUES (%s, %s, %s, %s, %s, now(), false)""",
            (
                f"acc-demo-{role}",
                username,
                display_name,
                role,
                auth.hash_password(password),
            ),
        )
        created.append(f"{username}/{role}")
    if created:
        print(f"演示账号：{', '.join(created)}")
        print("初始口令见 docs/账号与权限.md（本地演示用，上线前必须改）")
    if skipped:
        print(f"演示账号已存在，未覆盖：{', '.join(skipped)}")


if __name__ == "__main__":
    # 连接池要自己开：这是独立脚本，不是 FastAPI 的 lifespan 里跑的。
    db.pool.open()
    try:
        seed_accounts()
    finally:
        db.pool.close()
