#!/usr/bin/env python
"""代码托管的约束（不连网、不连库）。

换托管站最容易出的问题是"文档说 Gitee、某条命令还在拉 GitHub"。
这种残留不会报错，只会在别人照着文档做的时候卡住——所以用测试钉住。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
checks = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'✓' if ok else '✗'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def read(rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


print("迁移脚本")
script = root / "deploy" / "migrate-to-gitee.sh"
check("脚本存在", script.exists())
body = read("deploy/migrate-to-gitee.sh")
check("有 set -euo pipefail", "set -euo pipefail" in body)
check("支持 --dry-run（先看再推）", "--dry-run" in body)
check("推送所有分支和标签", "--all" in body and "--tags" in body)
check("保留旧远程作备份", "--no-backup" in body and "remote rename" in body)
check("会检查单文件体积（Gitee 有上限）", "ls-tree -r -l" in body)
check("dry-run 不改远程", body.index('dry-run：检查到此为止') < body.index('say "3. 配置远程"'))

print("\n文档不能再教人 clone GitHub 上的本项目")
docs = sorted((root / "docs").glob("*.md")) + [root / "README.md"]
offenders = []
for path in docs:
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), 1):
        if re.search(r"github\.com/cangjie-muse", line, re.I):
            offenders.append(f"{path.name}:{line_no}")
check("没有任何文档还写 github.com/cangjie-muse", not offenders, ", ".join(offenders[:3]))

print("\n迁移文档讲清了关键几步")
guide = read("docs/迁移到Gitee.md")
for keyword, label in (
    ("私有", "强调私有仓库"),
    ("ssh -T git@gitee.com", "SSH 连通性自测"),
    ("remote set-url origin", "服务器换远程"),
    ("rev-parse HEAD", "两边提交对比"),
    ("npm run check", "Actions 不跑时的本地门禁"),
):
    check(f"包含：{label}", keyword in guide)

print("\n部署教程指向 Gitee")
tutorial = read("docs/服务器部署教程.md")
check("clone 用 Gitee", "gitee.com" in tutorial and "git clone git@gitee.com" in tutorial)
check("托管行写的是 Gitee", "**Gitee**" in tutorial)
check("链接到迁移文档", "迁移到Gitee.md" in tutorial)
check("不再说'如果 GitHub 拉不下来'", "如果 GitHub 拉不下来" not in tutorial)

print("\nREADME 说明托管在哪")
readme = read("README.md")
check("README 提到 Gitee", "Gitee" in readme)

print("\nbootstrap 的第三方源码地址可覆盖（校园网可能到不了 GitHub）")
bootstrap = read("deploy/bootstrap.sh")
check("用 PGVECTOR_GIT 变量", "PGVECTOR_GIT" in bootstrap)
check("默认仍是官方地址", 'https://github.com/pgvector/pgvector.git' in bootstrap)
check("失败提示告诉人怎么改", "PGVECTOR_GIT=<可达地址>" in bootstrap)

print("")
if failures:
    print(f"失败 {len(failures)} 项：{', '.join(failures)}")
    sys.exit(1)
print(f"托管迁移检查全部通过（{checks} 项）。")
