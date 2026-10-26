#!/usr/bin/env bash
# 更新线上代码（git pull → 依赖 → 迁移 → 重启 → 自检）。
#
#   bash deploy/update.sh                  # 拉 main、装依赖、跑迁移、重启、自检
#   bash deploy/update.sh --dry-run        # 只看会发生什么，不改任何东西
#   bash deploy/update.sh --with-corpus    # 顺带重建问答语料（知识点正文变了才需要）
#
# 为什么要写成脚本：手工更新最容易漏的是第 3 步的迁移。跳过它，代码与库结构就错位，
# 而症状是"某个接口 500"，不是"迁移没跑"，排查要很久。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="${SERVICE:-whu-teaching}"
DRY=0; CORPUS=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --with-corpus) CORPUS=1 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

cd "$ROOT"
[ -f .env ] || die "没有 .env。第一次部署请先看 docs/服务器部署教程.md"
# shellcheck disable=SC1091
set -a; . ./.env; set +a
[ -n "${DATABASE_URL:-}" ] || die ".env 里没有 DATABASE_URL"

# 从哪个远端拉：GitHub 在国内经常连不上，默认优先 gitee，其次 origin/github。
# 想固定某一家就显式指定：GIT_REMOTE=gitee bash deploy/update.sh
BRANCH="${GIT_BRANCH:-main}"
REMOTE="${GIT_REMOTE:-}"
if [ -z "$REMOTE" ]; then
  for candidate in gitee origin github; do
    if git remote get-url "$candidate" >/dev/null 2>&1; then REMOTE="$candidate"; break; fi
  done
fi
[ -n "$REMOTE" ] || die "这个仓库没有配任何 git 远端，先 git remote add gitee <地址>"

# 一个远端连不上就换下一个：服务器在国内，GitHub 超时是常态，不该因此卡住整次更新
pull_from() {
  local remote="$1"
  git remote get-url "$remote" >/dev/null 2>&1 || return 1
  git fetch --quiet "$remote" "$BRANCH" 2>/dev/null || return 1
  git pull --ff-only "$remote" "$BRANCH" >/dev/null 2>&1 || return 1
  return 0
}

BEFORE="$(git rev-parse --short HEAD)"
say "1. 拉取代码（当前 $BEFORE）"
if [ "$DRY" -eq 1 ]; then
  for candidate in "$REMOTE" gitee origin github; do
    git remote get-url "$candidate" >/dev/null 2>&1 || continue
    if git fetch --quiet "$candidate" "$BRANCH" 2>/dev/null; then
      echo "    远端：$candidate"
      git log --oneline "HEAD..$candidate/$BRANCH" | sed 's/^/    /'
      echo "    （dry-run：到此为止）"
      exit 0
    fi
    echo "    ！$candidate 连不上，换下一个"
  done
  die "所有远端都连不上；先修网络或改用 gitee"
fi

PULLED=0
for candidate in "$REMOTE" gitee origin github; do
  if pull_from "$candidate"; then
    ok "已从 $candidate 更新"
    PULLED=1
    break
  fi
  git remote get-url "$candidate" >/dev/null 2>&1 && echo "  ！从 $candidate 拉取失败，换下一个"
done
[ "$PULLED" = 1 ] || die "所有远端都拉不下来。检查 GIT_REMOTE、网络，或手动 git status 看是否有本地改动挡住 ff-only"
AFTER="$(git rev-parse --short HEAD)"
if [ "$BEFORE" = "$AFTER" ]; then
  ok "已是最新（$AFTER），继续走完依赖与迁移检查"
else
  ok "$BEFORE → $AFTER"
  git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

say "2. 依赖"
.venv/bin/pip install -q -r server/requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null \
  || .venv/bin/pip install -q -r server/requirements.txt
ok "依赖已同步"

say "3. 数据库迁移"
.venv/bin/python db/migrate.py --status | tail -3
.venv/bin/python db/migrate.py --apply
ok "迁移已应用到最新"

if [ "$CORPUS" -eq 1 ]; then
  say "4. 重建问答语料（知识点正文会整体替换）"
  .venv/bin/python scripts/rebuild_qa_corpus.py
fi

say "5. 重启服务"
sudo systemctl restart "$SERVICE"
# worker 是可选的：没装它的机器上 systemctl 会报 not found，不该因此中断更新
if systemctl list-unit-files | grep -q '^whu-worker.service'; then
  sudo systemctl restart whu-worker
  sleep 1
  systemctl is-active --quiet whu-worker && ok "whu-worker 运行中" \
    || echo "  ！whu-worker 没起来，教材生成任务会排队不动：sudo journalctl -u whu-worker -n 40"
fi
sleep 2
systemctl is-active --quiet "$SERVICE" && ok "$SERVICE 运行中" || {
  sudo journalctl -u "$SERVICE" -n 40 --no-pager
  die "$SERVICE 没起来，日志在上面"
}

say "6. 自检"
.venv/bin/python scripts/preflight.py || {
  echo
  echo "preflight 报阻断项。服务已经起来了，但先别对外宣布更新完成。"
  exit 1
}
curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null && ok "健康检查通过"
echo
echo "更新完成：$AFTER"
echo "回滚：git checkout $BEFORE && .venv/bin/python db/migrate.py --status"
echo "      （迁移只前进不后退：要退回旧代码，需先确认旧代码能在新表结构上跑）"
