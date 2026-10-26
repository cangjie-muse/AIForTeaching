"""`.env` 加载的单元测试。

这一层存在的理由是线上踩过的坑：systemd 用 EnvironmentFile 注入变量，
手动跑脚本没有，于是同一个 DATABASE_URL 在服务里正确、在终端里是开发默认值，
报出来却是 `password authentication failed for user "whu"`——看着像数据库坏了。

钉住的规则：
1. 只认 `KEY=VALUE`，注释、空行、`export` 前缀都要能吃；
2. 只补空缺，已有的环境变量优先（systemd / 命令行给的不能被文件覆盖）；
3. 值两端的引号去掉，值里的 `=` 保留；
4. 文件不存在不是错误。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import envfile  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}")
    else:
        failures.append(name)
        print(f"  ✗ {name}{('：' + detail) if detail else ''}")


def main() -> int:
    print("解析")
    text = "\n".join([
        "\ufeff# 注释行",
        "",
        "DATABASE_URL=postgresql://whu:secret@127.0.0.1:5432/whu_teaching",
        "export APP_ENV=prod",
        'QUOTED="有空格 的值"',
        "SINGLE='单引号'",
        "WITH_EQUALS=a=b=c",
        "  空格键 = 带空格  ",
        "没有等号的行",
        "=无名",
    ])
    values = envfile.parse(text)
    check("读到连接串", values.get("DATABASE_URL", "").endswith("/whu_teaching"))
    check("吃掉 export 前缀", values.get("APP_ENV") == "prod")
    check("去掉双引号", values.get("QUOTED") == "有空格 的值")
    check("去掉单引号", values.get("SINGLE") == "单引号")
    check("值里的等号保留", values.get("WITH_EQUALS") == "a=b=c")
    check("跳过无等号的行", "没有等号的行" not in values)
    check("跳过没有键名的行", "" not in values)

    print("只补空缺")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text("EXISTING=from-file\nNEW_KEY=from-file\n", encoding="utf-8")
        os.environ["EXISTING"] = "from-env"
        os.environ.pop("NEW_KEY", None)
        try:
            loaded = envfile.load(path)
            check("文件里的键都返回了", set(loaded) == {"EXISTING", "NEW_KEY"})
            check("已有的环境变量不被覆盖", os.environ["EXISTING"] == "from-env")
            check("空缺的键被补上", os.environ.get("NEW_KEY") == "from-file")
        finally:
            os.environ.pop("NEW_KEY", None)
            os.environ.pop("EXISTING", None)

    print("文件不存在")
    missing = Path(tempfile.gettempdir()) / "definitely-not-here.env"
    check("返回空字典而不抛异常", envfile.load(missing) == {})

    print("默认位置")
    check("默认读仓库根的 .env", envfile.ENV_FILE == (ROOT / ".env"))

    print()
    if failures:
        print(f"✗ {len(failures)} 项未通过：" + "、".join(failures))
        return 1
    print("✓ .env 加载测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
