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

BEFORE="$(git rev-parse --short HEAD)"
say "1. 拉取代码（当前 $BEFORE）"
if [ "$DRY" -eq 1 ]; then
  git fetch --quiet origin main
  git log --oneline "HEAD..origin/main" | sed 's/^/    /'
  echo "    （dry-run：到此为止）"
  exit 0
fi
git fetch --quiet origin main
git pull --ff-only origin main
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
