#!/usr/bin/env bash
# 把代码托管从 GitHub 换到 Gitee（也适用于换回 GitHub，或换到任何别的远程）。
#
# 做的事：加/改 gitee 远程 → 推全部分支和标签 → 把 origin 指向 Gitee，
# 原来的 GitHub 远程改名为 github 留着当备份。不改任何文件内容。
#
# 用法：
#   bash deploy/migrate-to-gitee.sh --url git@gitee.com:你的用户名/AIForTeaching.git
#   bash deploy/migrate-to-gitee.sh --url <地址> --dry-run     # 只检查，不推
#   bash deploy/migrate-to-gitee.sh --url <地址> --no-backup   # 不保留 GitHub 远程
#
# 服务器上不用这个脚本，一条命令就够（见 docs/迁移到Gitee.md）。

set -euo pipefail

URL=""
DRY_RUN=0
KEEP_BACKUP=1
BACKUP_NAME="github"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-backup) KEEP_BACKUP=0; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "不认识的参数：$1" ;;
  esac
done

[ -n "$URL" ] || die "缺少 --url。例：bash deploy/migrate-to-gitee.sh --url git@gitee.com:user/AIForTeaching.git"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "这里不是 git 仓库"
cd "$ROOT"

say "0. 当前状态"
git remote -v | sed 's/^/    /' || true
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
ok "当前分支：$CURRENT_BRANCH"
if [ -n "$(git status --porcelain)" ]; then
  warn "工作区有未提交改动（迁移不影响它们，但先提交更稳妥）"
fi

say "1. 体积检查"
# Gitee 对单文件和仓库总量都有限制；超了会在 push 到一半时失败，很难看。
# git ls-tree -l 直接给出每个文件的大小（第 4 列），不用逐个 stat
BIGGEST="$(git ls-tree -r -l HEAD | sort -k4 -n | tail -1)"
TOTAL="$(git count-objects -vH | awk '/size-pack/ {print $2" "$3}')"
ok "仓库对象总量：$TOTAL"
if [ -n "$BIGGEST" ]; then
  SIZE="$(printf '%s' "$BIGGEST" | awk '{print $4}')"
  NAME="$(printf '%s' "$BIGGEST" | cut -f2)"
  MB=$(( ${SIZE:-0} / 1024 / 1024 ))
  if [ "$MB" -ge 50 ]; then
    warn "最大单文件 ${MB}MB（$NAME），Gitee 可能拒收；考虑 Git LFS 或移出仓库"
  else
    ok "最大单文件 ${MB}MB（$NAME），在常见限制之内"
  fi
fi

say "2. 能不能连上目标仓库"
if git ls-remote --heads "$URL" >/dev/null 2>&1; then
  ok "可以访问：$URL"
else
  warn "访问失败。私有仓库需要先配好 SSH key，或用 https + 私人令牌"
  warn "先在本地手动跑一次看报错：git ls-remote $URL"
  [ "$DRY_RUN" = 1 ] || die "连接不上就不继续了（--dry-run 可以只做检查）"
fi

if [ "$DRY_RUN" = 1 ]; then
  say "dry-run：检查到此为止，没有改远程、没有推送"
  exit 0
fi

say "3. 配置远程"
if git remote get-url gitee >/dev/null 2>&1; then
  git remote set-url gitee "$URL"
  ok "已更新远程 gitee"
else
  git remote add gitee "$URL"
  ok "已添加远程 gitee"
fi

say "4. 推送全部分支与标签"
# --all 推所有本地分支；标签单独推，否则 tag 不会跟过去
git push gitee --all
[ "$(git tag | wc -l)" -eq 0 ] || git push gitee --tags
ok "推送完成"

say "5. 把 origin 指向 Gitee"
ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
if [ "$ORIGIN_URL" = "$URL" ]; then
  ok "origin 已经指向 Gitee"
else
  if [ "$KEEP_BACKUP" = 1 ] && [ -n "$ORIGIN_URL" ]; then
    if git remote get-url "$BACKUP_NAME" >/dev/null 2>&1; then
      warn "远程 $BACKUP_NAME 已存在，跳过改名（GitHub 备份保持原样）"
      git remote remove gitee
    else
      git remote rename origin "$BACKUP_NAME"
      git remote rename gitee origin
      ok "origin → Gitee；原 GitHub 改名为 $BACKUP_NAME（备份用）"
    fi
  else
    git remote remove origin 2>/dev/null || true
    git remote rename gitee origin
    ok "origin → Gitee（未保留旧远程）"
  fi
fi

say "6. 验证"
git remote -v | sed 's/^/    /'
git ls-remote --heads origin | wc -l | xargs -I{} echo "    远程分支数：{}"
ok "本地已切换完成"

cat <<'EOF'

接下来（服务器上那条也要改，否则它还在拉 GitHub）：

  cd ~/AIForTeaching
  git remote set-url origin <同一个地址>
  git fetch origin && git status

验证两边一致：

  git rev-parse HEAD            # 服务器
  git rev-parse HEAD            # 本地，应当相同

EOF
