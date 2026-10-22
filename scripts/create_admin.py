"""在服务器上创建第一个管理员账号。

为什么不用 `scripts/dev_seed_accounts.py`：那个脚本塞的是**写进文档的默认口令**
（`admin / whu-admin-2026`），在服务器上等于留了一道后门。生产环境该用它建号，
但这个脚本要能建出"只有你知道口令、并且第一次登录必须改"的管理员。

用法：

    python scripts/create_admin.py --username admin
    python scripts/create_admin.py --username admin --display-name 教务管理员
    echo '长口令' | python scripts/create_admin.py --username admin --password-stdin

口令不写进命令行参数（会留在 shell 历史和 ps 输出里），默认交互式输入两次。
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import auth, db  # noqa: E402

MIN_LEN = 12
# 这些是"写在仓库里/文档里/示例里"的口令，出现在生产库就是事故。
KNOWN_WEAK = {
    "whu-admin-2026", "whu-teacher-2026", "whu-student-2026",
    "admin123456", "password1234", "123456789012", "qwertyuiop12",
}


def validate_password(password: str, username: str = "") -> str | None:
    """返回不合格的原因；None 表示通过。

    规则不追求"复杂度打分"，只排除三件在服务器上真会出事的情况：
    太短（被离线爆破）、是仓库里出现过的默认口令、包含登录名。
    """
    if len(password) < MIN_LEN:
        return f"口令至少 {MIN_LEN} 个字符（现在 {len(password)} 个）"
    if password.lower() in KNOWN_WEAK:
        return "这个口令出现在仓库文档或示例里，等于公开的"
    if username and username.lower() in password.lower():
        return "口令里不要包含登录名"
    if not any(char.isdigit() for char in password):
        return "至少含一个数字"
    if len(set(password)) < 6:
        return "重复字符太多，换一个"
    return None


def create_admin(username: str, display_name: str, password: str) -> str:
    """建账号，返回 id。已存在则中止——不覆盖现有口令。"""
    username = username.strip().lower()
    existing = db.query_one("SELECT id FROM app_account WHERE username = %s", (username,))
    if existing:
        raise SystemExit(
            f"账号 {username} 已存在（{existing['id']}）。\n"
            "本脚本不覆盖现有口令：要改密请登录后在「账号」页改，或由管理员重置。"
        )
    account_id = f"acc-{uuid.uuid4().hex[:12]}"
    db.execute(
        """INSERT INTO app_account
             (id, username, display_name, role, password_hash, password_updated_at,
              must_change_password)
           VALUES (%s, %s, %s, 'admin', %s, now(), true)""",
        (account_id, username, display_name or username, auth.hash_password(password)),
    )
    return account_id


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("标准输入里没有口令")
        return password
    first = getpass.getpass("设置管理员口令：")
    second = getpass.getpass("再输一遍：")
    if first != second:
        raise SystemExit("两次输入不一致")
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="创建平台管理员账号")
    parser.add_argument("--username", required=True, help="登录名，会转成小写")
    parser.add_argument("--display-name", default="", help="显示名，默认与登录名相同")
    parser.add_argument("--password-stdin", action="store_true",
                        help="从标准输入读口令（用于自动化），不读会交互式问两次")
    args = parser.parse_args()

    password = read_password(args.password_stdin)
    problem = validate_password(password, args.username)
    if problem:
        raise SystemExit(f"口令不合格：{problem}")

    db.pool.open()
    try:
        account_id = create_admin(args.username, args.display_name, password)
    finally:
        db.pool.close()

    print(f"已创建管理员：{args.username}（{account_id}）")
    print("首次登录会要求修改口令——这是标记在账号上的，不依赖前端自觉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
