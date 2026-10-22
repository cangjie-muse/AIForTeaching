"""上线自检。

在真服务器上跑一条命令，回答"这台机器现在能不能上线"，而不是让人翻清单逐项对。

原则：
- **不知道就说不知道**。查不了的（证书、nginx、备份是否真的能恢复）标 SKIP 并说明原因，
  不用"看起来没问题"糊过去。
- **危险的组合直接 FAIL**，不降级成警告：`APP_ENV=prod` + `AUTH_MODE=dev` 意味着
  任何人带着 `X-Actor` 头就能当管理员。
- 退出码：0 = 可以上线（允许有警告）；1 = 有阻断项。

用法：
    python scripts/preflight.py                       # 读环境变量
    python scripts/preflight.py --url postgresql://…  # 显式给库
    python scripts/preflight.py --offline             # 不连库，只查配置与依赖
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
SECRETS_HINTS = ("secrets", ".secrets")

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
MARKS = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}
ASCII_MARKS = {PASS: "[OK]", WARN: "[!]", FAIL: "[X]", SKIP: "[-]"}


def use_safe_marks() -> None:
    """Windows 控制台默认 GBK，直接 print ✓ 会抛 UnicodeEncodeError 而不是显示方块。

    自检脚本因为"打印不出勾"而崩掉，是最没必要的一种失败。做法是：
    - 输出流本来就支持 ✓：什么都不改。
    - 不支持（GBK）：整体退回 ASCII 记号（[OK] / [!] / [X]），中文照常输出——
      GBK 能显示中文，只是没有勾。**不要**为了勾把整个流改成 UTF-8：
      那样中文会在 GBK 控制台上变成乱码，为了一个符号牺牲整屏可读性不划算。
    - 实在写不出去的字符替换成 ?，不让脚本崩。
    """
    global MARKS
    encoding = sys.stdout.encoding or "ascii"
    try:
        "✓".encode(encoding)
        return
    except (LookupError, UnicodeEncodeError):
        MARKS = ASCII_MARKS
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def check(name: str, status: str, detail: str = "") -> Check:
    return Check(name, status, detail)


def width(text: str) -> int:
    """中文按两格算。用 len() 对不齐，运维在终端里看着就别扭。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def check_python() -> list[Check]:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 10)
    return [check(
        "Python 版本",
        PASS if ok else FAIL,
        f"{major}.{minor}（代码用到 `X | None` 语法，需要 ≥ 3.10）" if not ok else f"{major}.{minor}",
    )]


def check_packages() -> list[Check]:
    results: list[Check] = []
    missing: list[str] = []
    for module in ("fastapi", "uvicorn", "psycopg", "psycopg_pool"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    results.append(check(
        "后端依赖",
        FAIL if missing else PASS,
        f"缺少 {', '.join(missing)}；先 pip install -r server/requirements.txt" if missing else "fastapi / uvicorn / psycopg / psycopg_pool 均可导入",
    ))

    try:
        import httpx  # noqa: F401
        results.append(check("端到端测试依赖", PASS, "httpx 可用（可选）"))
    except ImportError:
        results.append(check("端到端测试依赖", SKIP, "未安装 httpx，test:api 无法运行（不影响上线）"))
    return results


def check_env() -> list[Check]:
    app_env = os.environ.get("APP_ENV", "dev").lower()
    auth_mode = os.environ.get("AUTH_MODE", "dev").lower()
    egress = os.environ.get("MODEL_EGRESS", "allowlist").lower()
    fake_model = os.environ.get("DEV_FAKE_MODEL") == "1"
    results: list[Check] = []

    if app_env == "prod" and auth_mode == "dev":
        results.append(check(
            "身份认证组合",
            FAIL,
            "APP_ENV=prod 且 AUTH_MODE=dev：任何人带 X-Actor 头即可冒充管理员。"
            "上线前必须改为 cas（或先接入统一认证）。",
        ))
    elif app_env == "prod":
        results.append(check("身份认证组合", PASS, f"APP_ENV=prod / AUTH_MODE={auth_mode}"))
    else:
        results.append(check("身份认证组合", WARN, f"APP_ENV={app_env} / AUTH_MODE={auth_mode}：仅限开发"))

    if app_env == "prod" and fake_model:
        results.append(check("演示模式", FAIL, "DEV_FAKE_MODEL=1 会让学伴返回固定演示文本，生产必须关闭"))
    elif fake_model:
        results.append(check("演示模式", WARN, "DEV_FAKE_MODEL=1：问答返回演示文本（仅开发）"))
    else:
        results.append(check("演示模式", PASS, "未开启"))

    if egress == "any":
        results.append(check(
            "出校策略",
            WARN,
            "MODEL_EGRESS=any：任何外部地址都允许接收教材原文。"
            "默认应为 allowlist，且把供应商域名逐个登记。",
        ))
    elif egress in ("allowlist", "internal-only"):
        results.append(check("出校策略", PASS, f"MODEL_EGRESS={egress}（失败关闭）"))
    else:
        results.append(check("出校策略", FAIL, f"MODEL_EGRESS={egress} 不是合法值（allowlist / any / internal-only）"))

    # CORS 在生产上留 * 等于让任何站点拿用户的 cookie 调接口。dev 里开着方便，
    # 上线了还留着就不是"待办"，是漏洞——所以 prod 下按阻断处理。
    origins = [item.strip() for item in os.environ.get("CORS_ORIGINS", "").split(",") if item.strip()]
    if "*" in origins or any(origin == "*" for origin in origins):
        results.append(check(
            "CORS 白名单",
            FAIL if app_env == "prod" else WARN,
            "CORS_ORIGINS 含 *：任何站点都能带凭证调接口。上线前改成具体域名。",
        ))
    elif not origins:
        results.append(check(
            "CORS 白名单",
            WARN,
            "CORS_ORIGINS 未设置：同源部署没问题；如果前端与 API 不同域名，需要显式列出。",
        ))
    else:
        results.append(check("CORS 白名单", PASS, f"{len(origins)} 个来源：{'、'.join(origins[:3])}"))

    # 会话 cookie 的 Secure 标记。prod 默认带 Secure，http 下浏览器不回传，
    # 症状是"登录成功、一刷新就掉线"。域名与证书到位前允许显式关掉，
    # 但必须留一条记录，否则以后没人知道它为什么是关的。
    secure_raw = os.environ.get("COOKIE_SECURE", "").strip().lower()
    if secure_raw in ("0", "false", "no", "off"):
        # 只提醒、不阻断：这是"域名与证书还没批下来"期间的合法过渡状态，
        # 卡在这里会逼人绕过自检，反而更糟。醒目地记一条就够。
        results.append(check(
            "会话 cookie",
            WARN,
            "COOKIE_SECURE=0：会话 cookie 不带 Secure，只能在 http 下用。"
            "域名与证书到位后立刻删掉这一行并重启。",
        ))
    elif app_env == "prod":
        results.append(check("会话 cookie", PASS, "带 Secure（需要 https 访问）"))
    else:
        results.append(check("会话 cookie", PASS, "开发环境：不带 Secure，允许 http"))
    return results


def check_secrets() -> list[Check]:
    candidates = [os.environ.get("SECRETS_DIR")] + [str(ROOT / name) for name in SECRETS_HINTS]
    found = next((Path(p) for p in candidates if p and Path(p).exists()), None)
    if not found:
        return [check(
            "密钥目录",
            WARN,
            "未找到 secrets 目录：模型密钥若走环境变量则无妨；否则问答与向量化会失败",
        )]

    keys = sorted(found.glob("*.key"))
    if not keys:
        return [check("密钥目录", WARN, f"{found} 存在但没有 *.key 文件")]

    wide: list[str] = []
    if os.name != "nt":
        wide = [str(path.name) for path in keys if (path.stat().st_mode & 0o077) != 0]
    status = WARN if wide else PASS
    detail = f"{found} 下 {len(keys)} 个密钥文件"
    if wide:
        detail += f"；{'、'.join(wide)} 对其他用户可读，改 chmod 600"
    elif os.name == "nt":
        detail += "（Windows 上不检查 POSIX 权限位）"
    return [check("密钥目录", status, detail)]


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def check_database(url: str | None) -> list[Check]:
    if not url:
        return [check("数据库", FAIL, "没有 DATABASE_URL：后端启动即失败")]

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return [check("数据库", SKIP, "psycopg 未安装，无法检查")]

    try:
        conn = psycopg.connect(url, connect_timeout=8)
    except Exception as exc:  # 连接失败的形态很多，统一暴露原文
        return [check("数据库", FAIL, f"连不上：{str(exc)[:200]}")]

    results: list[Check] = []
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT version() AS v")
            version = cur.fetchone()["v"].split(",")[0]
            results.append(check("数据库", PASS, version[:80]))

            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if row:
                results.append(check("pgvector", PASS, f"已安装 {row['extversion']}"))
            else:
                results.append(check(
                    "pgvector",
                    FAIL,
                    "未安装 vector 扩展：教材切片检索不可用。生产库必须安装 pgvector。",
                ))

            cur.execute("SELECT to_regclass('schema_migration') AS t")
            if not cur.fetchone()["t"]:
                results.append(check("数据库迁移", FAIL, "schema_migration 表不存在：还没跑过 db/migrate.py"))
            else:
                cur.execute("SELECT version, checksum FROM schema_migration")
                applied = {row["version"]: row["checksum"] for row in cur.fetchall()}
                results.extend(_check_migrations(applied))

            results.extend(_check_model_endpoints(cur))
    finally:
        conn.close()
    return results


def _check_migrations(applied: dict[str, str]) -> list[Check]:
    sys.path.insert(0, str(ROOT / "db"))
    from migrate import checksum, files  # noqa: PLC0415 —— 只在这里需要

    pending = [path.name for path in files() if path.name not in applied]
    drifted = [
        path.name for path in files()
        if path.name in applied and applied[path.name] != checksum(path)
    ]
    results: list[Check] = []
    if pending:
        results.append(check("数据库迁移", FAIL, f"未应用 {len(pending)} 个：{', '.join(pending[:5])}"))
    else:
        results.append(check("数据库迁移", PASS, f"{len(applied)} 个迁移全部应用"))
    if drifted:
        results.append(check(
            "迁移校验和",
            FAIL,
            f"{', '.join(drifted[:5])} 的内容与库里记录的校验和不一致——迁移被改过，"
            "已应用的迁移不能改，请新增一个迁移文件。",
        ))
    else:
        results.append(check("迁移校验和", PASS, "与库里记录一致"))
    return results


def _check_model_endpoints(cur) -> list[Check]:
    cur.execute("SELECT to_regclass('model_endpoint') AS t")
    if not cur.fetchone()["t"]:
        return [check("模型端点", SKIP, "model_endpoint 表不存在（迁移未应用）")]
    cur.execute(
        """SELECT kind, name, base_url, model, data_scope, api_key_path
           FROM model_endpoint WHERE enabled ORDER BY kind"""
    )
    rows = cur.fetchall()
    if not rows:
        return [check("模型端点", WARN, "没有启用的模型端点：问答与向量化都会失败")]

    results: list[Check] = []
    for row in rows:
        missing_key = bool(row["api_key_path"]) and not (ROOT / row["api_key_path"]).exists()
        if missing_key:
            results.append(check(
                f"模型端点 {row['kind']}",
                FAIL,
                f"{row['name']} 的密钥文件不存在：{row['api_key_path']}",
            ))
        else:
            results.append(check(
                f"模型端点 {row['kind']}",
                PASS,
                f"{row['name']} / {row['model']} / {row['data_scope']}",
            ))
    return results


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(offline: bool, url: str | None) -> list[Check]:
    checks: list[Check] = []
    checks += check_python()
    checks += check_packages()
    checks += check_env()
    checks += check_secrets()
    if offline:
        checks.append(check("数据库", SKIP, "--offline：按你的要求跳过了数据库检查"))
    else:
        checks += check_database(url or os.environ.get("DATABASE_URL"))
    checks.append(check(
        "证书与反向代理",
        SKIP,
        "需要登录服务器确认：TLS 证书有效期、nginx 是否只反代 /api 与静态目录、"
        "是否禁止外部直接访问 8000 端口",
    ))
    checks.append(check(
        "备份可恢复性",
        SKIP,
        "只能人工演练：按《回滚与恢复》做一次真实恢复，并记录耗时。"
        "没演练过的备份不算备份。",
    ))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="上线自检")
    parser.add_argument("--url", help="数据库连接串；不给则取 DATABASE_URL")
    parser.add_argument("--offline", action="store_true", help="跳过数据库检查")
    args = parser.parse_args()

    use_safe_marks()
    checks = run(args.offline, args.url)
    pad = max(width(item.name) for item in checks)
    for item in checks:
        gap = " " * (pad - width(item.name))
        print(f"  {MARKS[item.status]} {item.name}{gap}  {item.detail}")

    fails = [item for item in checks if item.status == FAIL]
    warns = [item for item in checks if item.status == WARN]
    skips = [item for item in checks if item.status == SKIP]
    print()
    print(f"通过 {sum(1 for item in checks if item.status == PASS)} · "
          f"警告 {len(warns)} · 阻断 {len(fails)} · 无法检查 {len(skips)}")
    if fails:
        print("\n阻断项（必须处理才能上线）：")
        for item in fails:
            print(f"  ✗ {item.name}：{item.detail}")
        return 1
    print("\n没有阻断项。" + ("警告项建议在上线前处理。" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
