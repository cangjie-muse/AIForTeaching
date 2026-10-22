#!/usr/bin/env python3
"""重置某个账号的口令 / 解锁账号。

DBeaver 里能看到 `app_account.password_hash`，但那是 PBKDF2 哈希——**不可逆**，
谁也没法从库里"读出"原口令。忘记口令只有重置这一条路。

用法：
    python scripts/reset_password.py --username zhangsan          # 交互输两遍新口令
    python scripts/reset_password.py --username zhangsan --unlock # 只解锁，不改口令
    echo '新口令' | python scripts/reset_password.py --username zhangsan --password-stdin

重置后会做三件事，缺一件都会留下麻烦：
  1. 写新哈希；
  2. `must_change_password = true`——临时口令不该长期有效；
  3. 清零 `failed_attempts`、清空 `locked_until`——否则账号还锁着，新口令也登不进。
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app import auth, db  # noqa: E402

# 口令规则和"建第一个管理员"共用一份，避免两处口径漂移
_spec = importlib.util.spec_from_file_location("create_admin", ROOT / "scripts" / "create_admin.py")
create_admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_admin)


def reset(username: str, password: str | None) -> dict:
    """返回做了什么，便于打印与测试断言。"""
    username = username.strip().lower()
    row = db.query_one(
        "SELECT id, username, role, disabled_at FROM app_account WHERE username = %s",
        (username,),
    )
    if not row:
        raise SystemExit(f"没有这个账号：{username}")
    if row["disabled_at"]:
        raise SystemExit(
            f"账号 {username} 已停用（disabled_at 非空）。先恢复启用，再重置口令。"
        )

    if password is None:
        db.execute(
            """UPDATE app_account
                  SET failed_attempts = 0, locked_until = NULL, updated_at = now()
                WHERE id = %s""",
            (row["id"],),
        )
        return {"username": row["username"], "unlocked": True, "password_reset": False}

    problem = create_admin.validate_password(password, username)
    if problem:
        raise SystemExit(f"口令不合格：{problem}")

    db.execute(
        """UPDATE app_account
              SET password_hash = %s,
                  password_updated_at = now(),
                  must_change_password = true,
                  failed_attempts = 0,
                  locked_until = NULL,
                  updated_at = now()
            WHERE id = %s""",
        (auth.hash_password(password), row["id"]),
    )
    return {"username": row["username"], "unlocked": True, "password_reset": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="重置账号口令 / 解锁")
    parser.add_argument("--username", required=True, help="登录名")
    parser.add_argument("--unlock", action="store_true", help="只解锁，不改口令")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读口令")
    args = parser.parse_args()

    password: str | None = None
    if not args.unlock:
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
            if not password:
                raise SystemExit("标准输入里没有口令")
        else:
            first = getpass.getpass("新口令：")
            second = getpass.getpass("再输一遍：")
            if first != second:
                raise SystemExit("两次输入不一致")
            password = first

    db.pool.open()
    try:
        result = reset(args.username, password)
    finally:
        db.pool.close()

    if result["password_reset"]:
        print(f"已重置 {result['username']} 的口令，并标记「下次登录必须改」。")
    else:
        print(f"已解锁 {result['username']}（口令未改）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
