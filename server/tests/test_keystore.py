"""模型密钥路径的单元测试。

钉住三条规则，都是线上真出过问题的：

1. 密钥目录必须是**绝对路径**。Web 服务的 WorkingDirectory 是 `server/`，
   worker 是仓库根，用相对路径时两个进程解析到不同目录——页面显示"已配置"，
   worker 里的模型调用却报 MissingKey。
2. 数据库里存的是仓库相对路径 `secrets/<name>.key`，不是裸文件名。
   存裸文件名时 preflight 拼成 `ROOT / "sophnet.key"`，配了密钥也判缺失。
3. 升级前已经落在 `server/secrets/` 的密钥必须还能读到，
   否则升级那一刻全部 AI 功能一起失效。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app import keystore  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}")
    else:
        failures.append(name)
        print(f"  ✗ {name}{('：' + detail) if detail else ''}")


def main() -> int:
    print("密钥目录与路径解析")

    check(
        "默认目录是绝对路径",
        keystore.SECRETS_DIR.is_absolute(),
        f"实际 {keystore.SECRETS_DIR}",
    )
    check(
        "默认目录在仓库根下",
        keystore.SECRETS_DIR == (ROOT / "secrets").resolve(),
        f"实际 {keystore.SECRETS_DIR}",
    )

    # 目录不能跟着进程 cwd 走：换一个 cwd 再取一次路径，结果必须不变。
    before = keystore.key_path("sophnet")
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.chdir(tmp)
            after = keystore.key_path("sophnet")
        finally:
            # 必须先离开临时目录：Windows 上 cwd 在里面时删不掉它
            os.chdir(original)
    check("换 cwd 后路径不变", before == after, f"{before} ≠ {after}")

    print("数据库里存的路径")
    check(
        "存的是仓库相对路径",
        keystore.display_path("sophnet") == "secrets/sophnet.key",
        keystore.display_path("sophnet"),
    )
    check(
        "不存裸文件名",
        "/" in keystore.display_path("sophnet"),
    )
    # preflight 用 ROOT 拼这个值，拼出来必须就是 key_path() 的结果
    check(
        "preflight 拼得出同一路径",
        (ROOT / keystore.display_path("sophnet")).resolve() == keystore.key_path("sophnet"),
    )

    print("历史位置兜底")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical = root / "canonical"
        legacy = root / "server" / "secrets"
        legacy.mkdir(parents=True)
        (legacy / "old.key").write_text("legacy-value", encoding="utf-8")

        saved_dir, saved_legacy = keystore.SECRETS_DIR, keystore.LEGACY_DIRS
        keystore.SECRETS_DIR = canonical.resolve()
        keystore.LEGACY_DIRS = (legacy,)
        try:
            check("规范位置没有时读历史位置", keystore.read_key("old") == "legacy-value")
            check("历史位置也没有时返回 None", keystore.read_key("missing") is None)

            keystore.write_key("new", "fresh-value")
            check("写入落在规范位置", (canonical / "new.key").exists())
            check("写入后读得到", keystore.read_key("new") == "fresh-value")
            check(
                "权限是 0600（Windows 上允许失败）",
                os.name == "nt" or (canonical / "new.key").stat().st_mode & 0o777 == 0o600,
            )
            # 规范位置优先：同名时不该被历史文件盖掉
            (legacy / "new.key").write_text("stale-value", encoding="utf-8")
            check("同名时规范位置优先", keystore.read_key("new") == "fresh-value")
        finally:
            keystore.SECRETS_DIR, keystore.LEGACY_DIRS = saved_dir, saved_legacy

    print("非法名称")
    for bad in ("../etc/passwd", "SOPHNET", "a/b", ""):
        try:
            keystore.key_path(bad)
            check(f"拒绝非法名 {bad!r}", False, "没有抛 ValueError")
        except ValueError:
            check(f"拒绝非法名 {bad!r}", True)

    print()
    if failures:
        print(f"✗ {len(failures)} 项未通过：" + "、".join(failures))
        return 1
    print("✓ 密钥路径测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
